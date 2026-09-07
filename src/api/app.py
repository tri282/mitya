"""FastAPI app: lifespan-managed retrieval/generation clients, retrieval lock, request handlers.

Composes P4 (retrieval) and P5 (generation) behind a single online path
(design §IV, rulings R4/R15/R22). Everything expensive -- embedder, BM25,
reranker, Qdrant client, entities/summaries/structure, disk cache -- is
constructed exactly once in `lifespan()` and hung off `app.state`; endpoints
only ever read `request.app.state`, never construct these themselves (design
§IV.3: "never lazily per query").

Two DIFFERENT bounds sit on the request path and are easy to confuse. The
`asyncio.BoundedSemaphore(1)` described below is mutual exclusion over the
non-re-entrant retrieval components. `src.limits.ConcurrencyMiddleware` (v2 P5
step 6) is admission control: bounded in-flight requests per tenant and in
total, held for the whole call including the SSE body, rejecting with 429
rather than queueing. The first protects a data structure; the second is what
stops the unbounded pile P4 measured (R99: 62 s p99, zero rejections).

`asyncio.BoundedSemaphore(1)` serializes the CPU-bound retrieval half of a
request -- route, cache lookup, readiness/ladder, entities, retrieve, rerank,
build_context -- and nothing else. It is taken in `POST /chat` as an
`async with` scoped to exactly that segment and released before
`build_messages`, so generation, the SSE stream, and the client's consumption
of that stream all run OUTSIDE it (D7, P4 step 9(b)). Generation left the box
with the hosted gateway, so `--parallel 1` is no longer a constraint on this
path; what the lock still covers is the non-re-entrant fastembed/FlashRank
models and the local-mode Qdrant client. Bounded rather than plain because a
stray second release is a silent capacity leak in `asyncio.Semaphore` and a
loud ValueError here.

Errors are mapped to HTTP status codes *before* the SSE stream opens wherever
possible (design §IV.2: never a hung stream) by eagerly pulling the first
token from `generate.stream_answer()` inside the handler -- a connect/read
failure there still has an open response to attach 503/504 to. A failure
*after* the first token (mid-stream) cannot change the already-sent 200
status; it is instead surfaced as an `error` SSE event and logged.
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastembed import SparseTextEmbedding, TextEmbedding
from opentelemetry import trace
from pydantic import BaseModel
from qdrant_client import QdrantClient

from src.auth import (
    COOKIE_NAME,
    LoginFailed,
    RateLimited,
    argon2_available,
    login as auth_login,
    logout as auth_logout,
    resolve_session,
)
from src.config import Config
from src.db import init_schema
from src.limits import ConcurrencyMiddleware, TenantConcurrency
from src.config_artifact import (
    active_config_version,
    active_corpus_manifest,
    active_corpus_version,
    assert_embed_model_matches,
)
from src.gateway.ladder import Rung, next_rung, readiness_from_status
from src.metrics import (
    StageTimer,
    configure_metrics,
    record_answer_cost,
    record_cache_bypass,
    record_cache_lookup,
    record_empty_completion,
    record_request_duration,
    record_stage_duration,
    record_ttft,
    record_unmetered_answer,
    record_unpriced_answer,
)
from src import memory_store
from src.memory import (
    AllocationInputs,
    SessionTurns,
    TIER_PASSAGES,
    active_policy,
    context_budget_tokens,
    render_turns,
)
from src.summarizer import write_episode, write_profile_from_turn
from src.observability import (
    STAGE_ALLOCATE,
    STAGE_CACHE,
    STAGE_CONTEXT,
    STAGE_ENTITIES,
    STAGE_GENERATE_FIRST,
    STAGE_GENERATE_STREAM,
    STAGE_MEMORY_WRITE,
    STAGE_RERANK,
    STAGE_RETRIEVE,
    STAGE_ROUTE,
    set_cost,
    RequestIdMiddleware,
    configure_logging,
    configure_tracing,
    current_request_id,
    current_tenant,
)
from src.pricing import FREE, Cost, cost_usd, is_metered, total_usd
from src.rag.context import build_context, expand_spans
from src.rag.entities import EntityMap
from src.gateway.guard import BudgetExceeded, Degraded, RequestTooLarge
from src.gateway.ladder import ContentRefused
from src.gateway.retry import TransportError
from src.limits import QuotaExhausted
from src.rag.generate import (
    LOCAL_PROVIDER,
    active_persona_head,
    build_messages,
    build_sources_footer,
    credential_status,
    serving_provider_name,
    stream_answer,
)
from src.rag.cache import AnswerCache
from src.rag.retrieve import build_hard_filter, build_ranker, rerank, retrieve, retrieve_structural_excerpts
from src.rag.router import route

# app.py is the ASGI entrypoint (`uvicorn src.api.app:app`), not a script with
# a `__main__` guard, so nothing else calls logging.basicConfig -- without
# this, our own logger.info() calls below are silently dropped. Format is
# src/rag/ingest.py's plus the P1 correlation id: every record carries the
# request it belongs to, so no call site has to pass one by hand.
configure_logging(logging.INFO)
logger = logging.getLogger(__name__)
# Per-turn ceiling on a prepended previous query, in characters (D-19(b),
# resized D-20). Deliberately NOT a versioned field, on D-14's rule that a guard
# which gets swept is an axis: it only bites on pathological input, and a third
# ServingConfig field mid-experiment would churn config_version and invalidate
# the arms it exists to protect.
#
# WAS 400, sized on the corpus-wide MEAN of 177 chars -- the wrong distribution.
# The turns this cap must never clip are the literal spans, which run to 547
# chars (`passage_paste` max, and the `anaphora` family's max); 5 of 20 anaphora
# turn-1 spans exceeded 400, so the cap truncated the strongest retrieval signal
# at exactly the arm that depends on it, and the first run measured a suppressed
# effect. Sized now on the MAX authored turn (547) with headroom, which is still
# an order of magnitude under a pasted essay.
# ponytail: one constant, no knob. Version it the day someone needs two values.
_PREV_QUERY_CHAR_CAP = 600


# BM25 model id is hardcoded here to match scripts/ingest_corpus.py:45 and
# scripts/eval_retrieval.py -- it is P2 debt (not lifted into Config), flagged
# in plans/mitya-rag/05-impl/phase-4.md and docs/RUNBOOK-P4.md; repeated
# literally here rather than silently invented as a new Config field.
_BM25_MODEL = "Qdrant/bm25"


class ChatFilters(BaseModel):
    book: str | None = None
    part: int | None = None
    chapter: int | None = None


class ChatRequest(BaseModel):
    query: str
    filters: ChatFilters | None = None
    # v2 P6 step 2(a): promoted from "log-correlation only" to a REAL KEY. It
    # selects the recent-turns transcript (`src/memory.py`'s `SessionTurns`),
    # scoped by the authenticated tenant -- a client-supplied id alone would let
    # one tenant read another's transcript, so the store keys on both.
    # Still not a durable identifier: recent turns live in process memory and
    # die with the process (cp-6a-input.md C-1 / O-A(i)); nothing here is
    # written to SQLite, and CP-6a stays free to decide otherwise.
    session_id: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load every RAG artifact/client ONCE (design §IV.3). Fails fast --
    missing artifacts (Config.validate_rag) or an empty Qdrant collection both
    raise before the server starts accepting requests."""
    Config.validate_rag()
    # v2 P5 step 1 (C10): the one storage decision for the whole system.
    # CREATE TABLE IF NOT EXISTS at startup -- no ORM, no migration tool, no
    # extra service.
    #
    # P6 Batch A adds NO table. The memory schema is CP-6a's to decide
    # (`plans/mitya-v2/02-decide/cp-6a-input.md` §5) and writing it is the act
    # that forecloses the alternatives; the recent-turns tier this batch ships
    # is process memory by design and needs no storage at all.
    init_schema()
    # Fail loudly at startup, not on a user's first login. `cryptography`'s
    # Argon2id is OpenSSL's, and OpenSSL below 3.2 raises UnsupportedAlgorithm
    # on the first derive -- which would otherwise surface as a 500 on /auth/login
    # with the cause buried in a traceback (see src/auth.py).
    if not argon2_available():
        raise RuntimeError(
            "auth: argon2id is unavailable -- cryptography is linked against "
            "OpenSSL < 3.2. Install argon2-cffi and swap src/auth.py's hasher, "
            "or run against a newer OpenSSL. D7 does not permit a fallback KDF."
        )
    if not Config.SESSION_COOKIE_SECURE:
        logger.warning(
            "auth: SESSION_COOKIE_SECURE=0 -- session cookies are being issued "
            "WITHOUT the Secure flag and will travel over plain HTTP. This is "
            "the local-harness setting (see src/config.py); it must never be "
            "set on a deployed instance."
        )
    # v2 P1 step 8 (D9): the corpus manifest and the serving config's
    # EMBED_MODEL must agree, or refuse to start. Without this, swapping
    # EMBED_MODEL and restarting without re-ingesting compares query vectors
    # against an index built by a different model -- degrades silently, no
    # error, no log line (step 1's reproduced finding).
    manifest = active_corpus_manifest()
    assert_embed_model_matches(Config.EMBED_MODEL, manifest)
    # Second call, deliberate: uvicorn installs its own handlers (propagate=False)
    # after importing this module, so the import-time call at :67 cannot reach
    # them. Lifespan runs late enough. Idempotent -- see configure_logging().
    configure_logging(logging.INFO)
    logger.info("lifespan: loading RAG artifacts and models...")
    # v2 P4 Batch C step 7 (D8): loud once, at startup, in the log a run is
    # actually read from -- an unversioned toggle (Config.FAULT_INJECT) has no
    # artifact proof it was off, so the readiness plane and this line are the
    # two places that say so instead.
    if Config.FAULT_INJECT:
        logger.warning(
            "lifespan: MITYA_FAULT_INJECT=%r -- generation faults are being "
            "injected on every request this process serves. Do not read this "
            "run's answers as real.", Config.FAULT_INJECT,
        )

    app.state.embedder = TextEmbedding(Config.EMBED_MODEL)
    app.state.bm25 = SparseTextEmbedding(model_name=_BM25_MODEL)
    app.state.ranker = build_ranker()
    app.state.qdrant = QdrantClient(path=Config.VECTOR_DB_DIR)
    app.state.entities = EntityMap()
    with open(Config.SUMMARIES_PATH, encoding="utf-8") as f:
        app.state.summaries = json.load(f)
    with open(Config.STRUCTURE_PATH, encoding="utf-8") as f:
        app.state.structure = json.load(f)
    app.state.cache = AnswerCache()
    # v2 P6 step 2(a): the recent-turns tier. Hung off app.state like every
    # other expensive-or-shared object (design IV.3), and for one extra reason:
    # it is PROCESS state with no durable backing, so it must be one object per
    # process rather than constructed per request -- a per-request store would
    # be permanently empty and every session would look like turn 1, which
    # would pass every test that only asserts nothing leaked (R115's shape).
    app.state.turns = SessionTurns()
    app.state.sem = asyncio.BoundedSemaphore(1)

    count = app.state.qdrant.count(Config.QDRANT_COLLECTION).count
    if count == 0:
        raise RuntimeError(
            f"Qdrant collection {Config.QDRANT_COLLECTION!r} is empty -- "
            f"run scripts/ingest_corpus.py"
        )
    logger.info(
        "lifespan: ready (qdrant_points=%d, entities=%d, summaries=%d, structure=%d)",
        count, len(app.state.entities._entities), len(app.state.summaries), len(app.state.structure),
    )
    yield
    logger.info("lifespan: shutting down")


app = FastAPI(lifespan=lifespan)

# The tenant resolver the middleware below is wired with. Defined here, above
# `add_middleware`, because that call runs at import time and needs the name.
def _cookie_from_scope(scope, name: str) -> str | None:
    """Read one cookie out of a raw ASGI scope.

    Plain ASGI, so there is no `Request` yet -- the resolver runs inside
    `RequestIdMiddleware`, before Starlette builds one. `SimpleCookie` rather
    than a split on ';' because cookie values are quoted-string encoded and a
    hand-rolled parse gets that wrong in exactly the cases an attacker picks.
    """
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            jar = SimpleCookie()
            try:
                jar.load(value.decode("latin-1"))
            except Exception:  # a malformed Cookie header is "no cookie", not a 500
                return None
            morsel = jar.get(name)
            return morsel.value if morsel else None
    return None


def _resolve_tenant(scope) -> str | None:
    """THE one tenant lookup per request (C11). Injected into
    `RequestIdMiddleware`; returns None for an unauthenticated caller, which
    the middleware renders as `Config.DEFAULT_TENANT` in logs and spans.

    Returning None rather than raising is deliberate: this runs for EVERY
    route including the operator plane (`/health`, `/ready`, `/metrics`),
    which is unauthenticated on purpose -- the degradation ladder and an
    orchestrator's probe cannot depend on a session. Rejection is a per-route
    concern, and that is `require_tenant` below.
    """
    session = resolve_session(_cookie_from_scope(scope, COOKIE_NAME))
    return session.tenant_id if session else None


# Order matters, and only this way round: FastAPIInstrumentor registers its own
# middleware, and Starlette runs the LAST-added middleware outermost. Ours must
# run first so the request_id ContextVar is set before the tracer mints the root
# span -- that is what lets RequestIdGenerator derive trace_id from request_id
# instead of falling back to random (R32).
tracer = configure_tracing(app)
# v2 P5 step 3 (C11): the tenant is resolved ONCE here, from the session
# cookie, and carried on `request.state.tenant` and the `tenant` ContextVar
# for the rest of the request. The X-Tenant-ID header P1 read is ignored from
# this point on -- see src/observability.py's TENANT_HEADER note.
# v2 P5 step 6 (A12(3), D2): bounded per-tenant concurrency, which is also what
# bounds the queue P4 measured as unbounded (R99). Added BEFORE
# RequestIdMiddleware and therefore running INSIDE it -- Starlette runs the
# last-added middleware outermost, and this one reads the tenant that one
# resolves. Held for the whole call, SSE body included; see
# src/limits.py's ConcurrencyMiddleware.
app.state.admission = TenantConcurrency(
    per_tenant=Config.CHAT_MAX_INFLIGHT_PER_TENANT,
    total=Config.CHAT_MAX_INFLIGHT_TOTAL,
    reserve=Config.CHAT_FAIRNESS_RESERVE,
    window_seconds=Config.CHAT_FAIRNESS_WINDOW_SECONDS,
    tolerance=Config.CHAT_FAIRNESS_TOLERANCE,
)
app.add_middleware(ConcurrencyMiddleware, gate=app.state.admission)
app.add_middleware(RequestIdMiddleware, tenant_resolver=_resolve_tenant)
# Second, independent reader on the same span/attribute data (P1 step 7,
# R32) -- see src/metrics.py's module docstring for why this is not a second
# measurement path.
configure_metrics()


# ---------------------------------------------------------------------------
# Auth (v2 P5 steps 2-3; D7, C11)
# ---------------------------------------------------------------------------

def require_tenant(request: Request) -> str:
    """Dependency for every route that serves a tenant's data.

    Reads the tenant the middleware already resolved -- NO second round trip,
    which is C11's stated discriminator. `request.state.tenant` is set for
    every request; it is `Config.DEFAULT_TENANT` when nobody is logged in, and
    an anonymous caller is what this rejects.
    """
    tenant = getattr(request.state, "tenant", None)
    if not tenant or tenant == Config.DEFAULT_TENANT:
        raise HTTPException(status_code=401, detail="authentication required")
    return tenant


class LoginRequest(BaseModel):
    username: str
    password: str


def _client_ip(request: Request) -> str:
    """The address the per-IP login limit counts against.

    X-Forwarded-For is client-supplied and is honoured ONLY when
    `Config.TRUST_FORWARDED_FOR` says a proxy that overwrites it sits in
    front. Otherwise trusting it hands an attacker an unlimited supply of
    distinct "IPs" and the per-IP limit stops existing. When it IS trusted,
    the LAST hop is taken -- the one the trusted proxy appended -- because
    every earlier entry is whatever the client chose to send.
    """
    if Config.TRUST_FORWARDED_FOR:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _set_session_cookie(response: JSONResponse, token: str) -> None:
    """D7's cookie clauses, all four in one place so no call site can drop one.

    `Secure` is a Config knob only because an HTTP client will not return a
    Secure cookie over plain http, which is every local harness run; it
    defaults True and lifespan() warns when it is not.
    """
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=Config.SESSION_TTL_SECONDS,
        httponly=True,
        secure=Config.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


@app.post("/auth/login")
async def auth_login_route(body: LoginRequest, request: Request) -> JSONResponse:
    """Authenticate, rotate the session, set the cookie.

    Both failure modes answer with the SAME status and the SAME body -- an
    unknown username and a wrong password are indistinguishable from out here
    (D7: generic failure messages, no user enumeration). src/auth.py's
    `login()` also makes them cost comparable wall time.
    """
    presented = request.cookies.get(COOKIE_NAME)
    try:
        token = auth_login(
            body.username, body.password, _client_ip(request),
            presented_token=presented,
        )
    except RateLimited as exc:
        raise HTTPException(
            status_code=429, detail="too many attempts",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except LoginFailed:
        raise HTTPException(status_code=401, detail="invalid credentials") from None

    # `body.username.lower()` IS the tenant: `accounts.tenant_id` is the
    # account id and the lookup in auth.login() matched on exactly this value.
    # Re-reading the session we just issued would be a second query on the
    # login path for a value already in hand.
    response = JSONResponse({"tenant": body.username.lower()})
    _set_session_cookie(response, token)
    return response


@app.post("/auth/logout")
async def auth_logout_route(request: Request) -> JSONResponse:
    """Destroy the session server-side and clear the cookie. Idempotent, and
    it does not say whether there was a session to destroy."""
    auth_logout(request.cookies.get(COOKIE_NAME))
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/auth/session")
async def auth_session_route(tenant: str = Depends(require_tenant)) -> dict:
    """Who am I. The SPA's login wall reads this on load (P5 step 7); 401 is
    the unauthenticated answer, supplied by the dependency."""
    return {"tenant": tenant}


def _sse(event: str, payload: dict) -> str:
    """Format one SSE frame. All payloads are JSON so the client has a single
    parse path regardless of event type."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


_metrics_bind_checked = False


@app.get("/metrics")
async def metrics_endpoint(request: Request) -> Response:
    """Prometheus scrape target (P1 step 7). Same uvicorn process as /chat, so
    it inherits uvicorn's bind address -- docs/RUNBOOK-P6.md:87 documents
    `--host 127.0.0.1`. Unauthenticated operational read: warn loudly, once,
    if that is not what actually happened.

    There is no startup-time way to see this: uvicorn's `--host` is a CLI flag
    to the ASGI server process, not something the app it hosts can read, and
    lifespan() runs before any connection exists. `request.scope["server"]` is
    the first point where the real bound address is observable from inside
    the app -- it is what the ASGI server actually accepted the connection on,
    not a client-supplied header. Checked once (module-level flag) so a
    15s-interval scrape does not spam the log.
    """
    global _metrics_bind_checked
    if not _metrics_bind_checked:
        server = request.scope.get("server")
        host = server[0] if server else None
        if host not in ("127.0.0.1", "::1", "localhost"):
            logger.warning(
                "metrics: uvicorn is bound to %r, not loopback -- /metrics is an "
                "unauthenticated operational read reachable from wherever that address "
                "is reachable. See docs/RUNBOOK-P6.md:87 (--host 127.0.0.1).",
                host,
            )
        _metrics_bind_checked = True
    from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


async def _dependency_status(state) -> tuple[bool, dict]:
    """Serving-generation reachability + Qdrant presence/count + artifact load
    status. `healthy` is True only if all green (design §IV.2).

    Factored out of `/health` (v2 P2 step 10, charter V) so `/ready` -- what
    the degradation ladder reads -- shares this one dependency check instead
    of a second copy of it. `/health` keeps calling it too, unchanged: the
    frontend's `useHealth`/`HealthIndicator` already poll `/health` for this
    exact body (`frontend/src/composables/useHealth.ts`), and this is a
    liveness/readiness NAME split, not a behavior change to an existing,
    live consumer.

    The generation check follows what actually SERVES (v2 P4 Batch A0 item 1,
    R89). Before this, it probed `Config.LLAMA_SERVER_URL` unconditionally --
    a process P2 step 6's cutover took out of the request path (A3) -- so a
    perfectly healthy hosted deployment returned 503 `degraded` and the SPA
    rendered it as an outage. `src/rag/generate.py` resolves which provider
    serves, because it is the module that resolves it for the request path
    too; asking a second time here is how the two drift apart.
    """
    healthy = True
    result: dict = {}

    provider = serving_provider_name()
    if provider == LOCAL_PROVIDER:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
                resp = await client.get(f"{Config.LLAMA_SERVER_URL}/health")
                resp.raise_for_status()
            generation_status = "ok"
        except httpx.HTTPError as exc:
            generation_status = f"unreachable: {exc}"
    else:
        generation_status = credential_status(provider)
    if generation_status != "ok":
        healthy = False
    result["generation"] = {
        "model": Config.SERVING_MODEL,
        "provider": provider,
        "status": generation_status,
    }
    # `llama_server` is kept as a SUBSYSTEM line, not the generation verdict.
    # The SPA's degraded tooltip reads this key by name
    # (frontend/src/api/types.ts:80, HealthIndicator.vue:40) and the SPA is
    # explicitly outside Batch A0's scope, so the field stays -- it just stops
    # deciding anything when llama-server is not what serves.
    result["llama_server"] = (
        generation_status if provider == LOCAL_PROVIDER
        else f"not serving generation (provider={provider})"
    )

    try:
        count = state.qdrant.count(Config.QDRANT_COLLECTION).count
        result["qdrant"] = {"collection": Config.QDRANT_COLLECTION, "count": count}
        if count == 0:
            healthy = False
    except Exception as exc:  # local-mode Qdrant errors are not a fixed type
        result["qdrant"] = f"error: {exc}"
        healthy = False

    result["artifacts"] = {
        "entities_loaded": len(state.entities._entities),
        "summaries_loaded": len(state.summaries),
        "structure_loaded": len(state.structure),
    }
    # v2 P4 Batch C step 7 (D8): "what the harness must observe, the API must
    # expose" -- an unversioned toggle leaves no artifact proof it was off, so
    # its state is reported here instead of being inferable only from a hash.
    # Present ONLY when set, so a normal run's `/ready` body is unchanged.
    if Config.FAULT_INJECT:
        result["fault_injection"] = Config.FAULT_INJECT
    # Same rule, and it matters more here: a run served entirely from fixtures
    # must be identifiable from OUTSIDE, or a latency curve measured against the
    # fake is indistinguishable from one measured against a provider.
    if Config.FAKE_PROVIDER:
        result["fake_provider"] = True
    return healthy, result


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """Serving-generation reachability + Qdrant presence/count + artifact
    load status. 200 only if all green (design §IV.2). Kept as-is for the
    frontend's existing contract -- see `/ready` for the split this endpoint
    now shares its check with."""
    healthy, result = await _dependency_status(request.app.state)
    result["status"] = "ok" if healthy else "degraded"
    return JSONResponse(result, status_code=200 if healthy else 503)


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness (v2 P2 step 10, charter V): the same dependency checks as
    `/health`, under the name the degradation ladder actually reads
    (`src/gateway/ladder.py`'s `Readiness`/`readiness_from_status`). Split
    from `/health` as a name, not a behavior -- an orchestrator's liveness
    probe (should this process be restarted?) and the ladder's readiness
    read (can this process serve the primary rung right now?) are two
    different questions asked of the same underlying state, and giving them
    one shared answer under two names is what lets each evolve without the
    other silently changing meaning."""
    state = request.app.state
    healthy, result = await _dependency_status(state)
    result["status"] = "ready" if healthy else "not_ready"
    # The ladder's own view of this same status (Readiness has no `degraded`
    # here -- that is step 4's spend-rate breaker, a per-request runtime
    # state /ready does not have visibility into). `next_rung()` itself is
    # NOT called here: it also needs `cache_hit`, which only exists for a
    # specific query, so a bare /ready poll reports the readiness snapshot,
    # not a rung -- P4 is what exercises actual rung selection under fault
    # injection.
    result["readiness"] = readiness_from_status(result, cache_available=state.cache.enabled).__dict__
    return JSONResponse(result, status_code=200 if healthy else 503)


@app.get("/books")
async def books(request: Request, tenant: str = Depends(require_tenant)) -> dict:
    """Serve structure.json verbatim -- feeds the UI's book/part/chapter filter.

    Auth-gated at P5: `/books` and `/chat` are the tenant plane (R86 measured
    exactly these two as the harness call sites that need credentials).
    `/health`, `/ready` and `/metrics` are the operator plane and stay open --
    the degradation ladder and a liveness probe cannot depend on a session."""
    return request.app.state.structure


@app.post("/chat")
async def chat(
    body: ChatRequest, request: Request, tenant: str = Depends(require_tenant),
) -> StreamingResponse:
    state = request.app.state
    filters = body.filters.model_dump(exclude_none=True) if body.filters else None
    t_start = time.monotonic()

    # Root span only (P1 step 4, A12) -- not on the seven stage spans below,
    # which are children of this one and inherit its trace. A per-stage copy
    # would be seven writes of one constant per request, and Jaeger already
    # filters a trace by a root attribute.
    root_span = trace.get_current_span()
    root_span.set_attribute("tenant", current_tenant())
    # P1 step 9: config_version/corpus_version on the root span, beside
    # tenant -- every answer traces to the exact config and corpus that
    # produced it (D9's stated end-state).
    config_version = active_config_version()
    corpus_version = active_corpus_version()
    root_span.set_attribute("config_version", config_version)
    root_span.set_attribute("corpus_version", corpus_version)

    # D7 (P4 step 9(b)): the lock is scoped to route -> cache -> ladder ->
    # entities -> retrieve -> rerank -> build_context, and is gone before
    # `build_messages` -- generation and the SSE stream run outside it. Every
    # exit from this block releases exactly once, via the context manager:
    # the cache-hit `return`, the ladder/retrieval raises, and falling off the
    # end into generation.
    # ponytail: what makes retrieval safe today is the single-threaded event
    # loop, not this semaphore -- the retrieval stages never await. The
    # semaphore is what keeps it safe if retrieval is ever moved to a thread
    # (`asyncio.to_thread`, an executor) or a second uvicorn worker, and it is
    # a real exclusion across the one await in here (`_dependency_status`).
    async with state.sem:

        # --- route (pure function, zero latency -- src/rag/router.py:10) then
        # cache check (inside the semaphore, per design §III.4/§III.6) ---
        # Route runs first so the cache key (P1 step 4/5c) can include it; a route
        # slice is meaningless without this order, since at lookup time the route
        # would otherwise still be unknown.
        # Timed by hand rather than with StageTimer: `route` is the one stage whose
        # own label value does not exist until the block has finished running.
        _t_route = time.monotonic()
        with tracer.start_as_current_span(STAGE_ROUTE) as span:
            route_name, scope = route(body.query, filters, state.structure, state.summaries)
            span.set_attribute("route.name", route_name)
            span.set_attribute("route.scope_resolved", scope is not None)
            set_cost(span)
        record_stage_duration(time.monotonic() - _t_route, route_name, STAGE_ROUTE)

        def _abort() -> None:
            """Record the request as an error on a pre-stream exit.

            Records ONLY -- it does not touch the semaphore. The lock is released
            by the `async with` above, on every exit from the critical section,
            this raise included (D7). This helper exists because the
            request-duration histogram is what drives BOTH RED panels -- the rate
            and the error ratio. A path that exits without recording is invisible
            to both: measured live at P1 step 7, a 503 from an unreachable
            llama-server recorded all six stage durations and no request at all,
            so the error ratio read 0.000 during a total outage. One helper, so
            there is one place that can be got wrong instead of four.

            Called from BOTH sides of the release point -- the ladder and
            retrieval exits inside the lock, the generation-priming exits after it
            -- which is exactly why it must not release.
            """
            record_request_duration(time.monotonic() - t_start, route_name, "error", tenant)
        # Root attribute is the slice key for a latency/cost percentile (P1 step
        # 4) -- the stage span above keeps its own route.name/scope_resolved
        # detail, this is the per-request dimension.
        root_span.set_attribute("route", route_name)

        # v2 P6 step 7(a), G-P12(b): THE BYPASS. A memory-bearing request does
        # not read and does not write the answer cache.
        #
        # This is a PREDICATE, not a key component -- `AnswerCache._key` gains
        # no `tenant` and no memory fingerprint, and that is the decision, not
        # an omission. R126 measured a correct memory component taking hit rate
        # to 0.000; the only fingerprint that recovered anything (0.157)
        # recovered it BY COLLIDING differing memory states, which is the leak
        # the key was for. So the cached population is memory-free, and a
        # memory-free population under a shared corpus is tenant-invariant
        # (A12(2), C9 CLOSED). Step 7's "extend the cache key with memory
        # state" is retired as written-in-error, per `decisions-p6.md`.
        #
        # Deliberately keyed on WHETHER TURNS EXIST, not on what the active
        # policy did with them: `passages_only` produces a memory-free plan
        # from a memory-bearing session, and letting that back into the cache
        # would make the cached population differ per policy -- two policies'
        # answers pooled under one arm, which is R51's defect in a new place.
        # One predicate, evaluated once, policy-independent.
        recent_turns = state.turns.recent(tenant, body.session_id)
        # CP-6d(a)'s lazy episodic trigger reads its predicate HERE, not at the
        # write below: by then this turn has already been appended to
        # `SessionTurns` and every turn would look like a continuation. A
        # session-id-less request is never a session boundary.
        first_turn_of_session = bool(body.session_id) and not recent_turns
        # Batch C: the persisted tiers join the predicate, and joining it is the
        # SECURITY half of this batch. A turn-1 request from a tenant who has a
        # profile is memory-bearing even with an empty transcript, and caching
        # its answer under a memory-free key would serve one tenant's
        # profile-shaped answer to another -- the exact condition
        # `test_cache_isolation.py` guards, now guarding something real rather
        # than firing on a module name. Two indexed primary-key lookups, on the
        # connection policy `src/db.py` already states.
        profile_facts = memory_store.profile(tenant)
        episodes = memory_store.episodes(tenant)
        memory_bearing = bool(recent_turns or profile_facts or episodes)
        # Computed ONCE. Three `meta` branches publish this value and all three
        # close over this local; none recomputes it and none infers it from
        # `cache_hit`. If a refactor ever moves an emit site out of this scope,
        # PASS the value -- a second evaluation site is the defect this whole
        # comment block exists to prevent.

        with tracer.start_as_current_span(STAGE_CACHE) as span, StageTimer(route_name, STAGE_CACHE):
            cached = (
                None if memory_bearing
                else state.cache.get(body.query, filters, route_name, config_version, corpus_version)
            )
            span.set_attribute("cache.hit", cached is not None)
            span.set_attribute("cache.bypassed_memory", memory_bearing)
            set_cost(span)
        if memory_bearing:
            # v2 P6 step 7(b): THE OTHER HALF OF THE SPLIT, counted.
            #
            # Batch A already kept bypasses out of the hit-rate denominator
            # (below). What was missing is the bypassed population's own
            # number: with only a per-request span attribute, the served share
            # was unknowable in aggregate, and a hit rate over a shrinking
            # population reads exactly like a healthy one. As memory adoption
            # grows this counter is what says "the cache serves less traffic",
            # which is a different fact from "the cache hits less often" --
            # and the two are indistinguishable from the lookup counter alone.
            record_cache_bypass(route_name)
        else:
            # Denominator discipline (`decisions-p6.md` G-P12(b)): hit rate is
            # reported over the memory-free population ONLY, never pooled. A
            # bypassed request counted as a miss would silently deflate the hit
            # rate of the only population the cache still serves. The bypass is
            # counted on its own instrument above, under its own metric name,
            # so no PromQL denominator over `mitya_cache_lookups_total` can
            # pick it up by accident.
            record_cache_lookup(route_name, "hit" if cached is not None else "miss")

        # --- degradation ladder (charter V; policy written at P2 step 10, wired
        # here by P4 Batch A0 item 2) ---
        # Readiness comes from the SAME bridge `/ready` reports, never a second
        # health notion invented for the request path -- that duplication is what
        # R89 cost. In the hosted shape this costs no network at all: the
        # generation check is a config read (`credential_status()`), and only a
        # deployment where llama-server is what serves pays for a probe here.
        #
        # `next_rung()` returns PRIMARY whenever generation is available, cache hit
        # or not: `rung` is which TIER of service this deployment is operating at,
        # and `cache_hit` is the orthogonal fact about what produced the bytes --
        # the same orthogonality the meta event already documents for `route_name`.
        # A cache hit is therefore still served exactly as before; the ladder only
        # changes what happens when generation is NOT available.
        _, dependency_result = await _dependency_status(state)
        readiness = readiness_from_status(dependency_result, cache_available=state.cache.enabled)
        rung = next_rung(readiness, cache_hit=cached is not None)
        # R93: the exception paths below emit no `meta` event, so a failed request
        # had no served-rung field at all. One header, set on every /chat error
        # exit, is the machine-readable answer for those paths; the streaming paths
        # carry `rung` inside `meta`.
        rung_header = {"X-Mitya-Rung": rung.value}

        if cached is not None:
            logger.info(
                "chat: query=%r filters=%s session_id=%s cache_hit=true "
                "cache_population=memory_free",
                body.query, filters, body.session_id,
            )

            cached_outcome = ["error"]

            async def cached_stream():
                try:
                    yield _sse("token", {"text": cached["answer"]})
                    yield _sse("sources", {"sources": cached["sources"], "footer": cached["footer"]})
                    yield _sse("meta", {
                        "request_id": current_request_id(),
                        "tenant": current_tenant(),
                        "config_version": config_version,
                        "corpus_version": corpus_version,
                        # The real route, not a "CACHE" sentinel (P1 5c) --
                        # route and cache_hit are orthogonal dimensions, and
                        # cache_hit already says the second one. This is what
                        # makes cache hits poolable with misses per route.
                        "route_name": route_name,
                        "chunk_ids": [],
                        "ttft_ms": None,
                        "total_ms": int((time.monotonic() - t_start) * 1000),
                        "filter_relaxed": False,
                        "no_context": not cached["sources"],
                        "cache_hit": True,
                        # Why this request neither read nor wrote the answer
                        # cache -- the bypass predicate itself (:687-722), not
                        # an inference from cache_hit. Always False here by
                        # construction: a hit is unreachable when memory-bearing
                        # (:725-727), which is what makes the field readable as
                        # the explanation for a cache_hit:false on a repeat.
                        "memory_bearing": memory_bearing,
                        # Ladder tier this request was served at (P4 Batch A0).
                        "rung": rung.value,
                        # Producing arm (v2 P2 step 9, R51): under per-request
                        # routing one config_version can serve more than one arm,
                        # so config_version alone cannot say which arm this hit
                        # replays -- the value has to carry it.
                        "model": cached["model"],
                        # A cache hit generates nothing, so it bills nothing. Not
                        # a placeholder: this is what makes A5's saving claim
                        # measurable as cost avoided the moment P2 meters
                        # anything, with no further instrumentation.
                        "cost_usd": FREE.usd,
                        "cost_metered": FREE.metered,
                    })
                    yield _sse("done", {})
                    # A cache hit is only reachable on a memory-FREE request
                    # (the bypass above), i.e. turn 1 of a session. It still
                    # produced an answer, so it still becomes turn 1 of the
                    # transcript -- otherwise turn 2 would open with a hole
                    # exactly where the anaphoric follow-up points.
                    state.turns.append(tenant, body.session_id, body.query, cached["answer"])
                    cached_outcome[0] = "ok"
                finally:
                    # In the `finally`, and with the outcome tracked in a cell, for
                    # the same reason cost is stamped there on the generate path: a
                    # client that disconnects mid-stream never reaches the line
                    # above, but it did consume a request, and a duration histogram
                    # that silently drops those measures only the happy path.
                    record_request_duration(
                        time.monotonic() - t_start, route_name, cached_outcome[0], tenant,
                    )

            return StreamingResponse(cached_stream(), media_type="text/event-stream")

        if rung is Rung.UNAVAILABLE:
            # The ladder's bottom rung: generation is down, no cached answer for
            # this query, and retrieval is down too -- there is nothing left to
            # serve. Raised BEFORE retrieval rather than letting retrieval fail on
            # its own, because a 503 that says "the ladder found no rung" and a 500
            # from a broken Qdrant call are different operator facts.
            logger.warning(
                "chat: rung=%s query=%r -- no rung can serve this request (%s)",
                rung.value, body.query, dependency_result.get("generation"),
            )
            _abort()
            raise HTTPException(
                status_code=503,
                detail="serving unavailable: no degradation rung can serve this request",
                headers=rung_header,
            )

        # --- resolve/expand -> retrieve -> rerank -> build_context (route already
        # resolved above, ahead of the cache check) ---
        try:
            active_book = filters.get("book") if filters else None
            # v2 P7 Batch G (D-18): retrieval used to see only this request, while
            # 68 of 108 turns in the session corpus are contentless follow-ups
            # ("why does that matter later on?") -- so 63% of multi-turn requests
            # retrieved on a string with nothing to retrieve on. The last
            # `n_prev_queries` QUESTIONS are prepended, oldest first.
            #
            # Questions only, never answers, and this string never reaches the
            # prompt: it goes to embedding and BM25 and nowhere else (~15 tokens
            # of retrieval input, 0 prompt tokens). Injecting the transcript
            # instead costs 296 tokens/turn AND lets the model answer from the
            # prior answer while the programmatic Sources list still cites what
            # the contentless query returned -- a citation that lies.
            #
            # NOT keyed into the cache, and it does not need to be: a request
            # with prior turns is `memory_bearing` above, which already bypasses
            # the cache entirely (G-P12(b)). `n_prev_queries` can only fire when
            # `recent_turns` is non-empty, so it can only fire on requests that
            # never read or write a cache entry.
            n_prev = Config.N_PREV_QUERIES
            retrieval_query = body.query
            if n_prev and recent_turns:
                # THE LIVE QUESTION GOES FIRST, and that ordering is a correctness
                # requirement rather than a preference (D-19(b)). Nothing in
                # src/rag/retrieve.py caps query length -- `embedder.embed()` and
                # `bm25.query_embed()` are called on whatever string arrives and the
                # encoder truncates SILENTLY FROM THE TAIL. With history prepended,
                # a long enough prior turn cut the user's actual question out of its
                # own retrieval, with no error anywhere. The `passage_paste` family
                # is named for users pasting text and already runs 3x every other
                # family (mean 177 chars, max 547); an essay pasted with "paraphrase
                # this" is the same failure with a bigger constant.
                #
                # Putting it first costs nothing on either leg: BM25's query side
                # assigns a flat 1.0 per token, so it is a bag of words and order is
                # irrelevant there, and the dense leg is near-indifferent. Truncation
                # now eats history instead of the live question.
                #
                # ponytail: last N questions, each capped, current query first. A real
                # query rewriter only if eval shows concatenation losing two-step chains.
                history = [
                    t.question[:_PREV_QUERY_CHAR_CAP] for t in recent_turns[-n_prev:]
                ]
                retrieval_query = "\n".join([body.query] + history)
            with tracer.start_as_current_span(STAGE_ENTITIES) as span, StageTimer(route_name, STAGE_ENTITIES):
                expanded_query, resolved_entities = state.entities.resolve(
                    retrieval_query, active_book=active_book
                )
                span.set_attribute("entities.n_prev_queries", n_prev)
                span.set_attribute("entities.resolved_count", len(resolved_entities))
                set_cost(span)

            filter_relaxed = False
            with tracer.start_as_current_span(STAGE_RETRIEVE) as span, StageTimer(route_name, STAGE_RETRIEVE):
                span.set_attribute("retrieve.route", route_name)
                if route_name == "STRUCTURAL" and scope is not None:
                    # Hard chapter filter: no BM25 leg and no rerank, which is why
                    # this route reaches first token ~45% faster (R30).
                    points = retrieve_structural_excerpts(
                        state.qdrant, state.embedder, expanded_query,
                        scope["book"], scope["part"], scope["chapter"],
                    )
                else:
                    hard_filter = build_hard_filter(filters)
                    points = retrieve(
                        state.qdrant, state.embedder, state.bm25, expanded_query, hard_filter
                    )
                    if not points and hard_filter is not None:
                        # Fallback (design §IV.3): a hard filter that matches zero
                        # chunks retries once unfiltered rather than dead-ending.
                        filter_relaxed = True
                        logger.info(
                            "chat: filter_relaxed=true query=%r filters=%s", body.query, filters,
                        )
                        points = retrieve(
                            state.qdrant, state.embedder, state.bm25, expanded_query, None
                        )
                span.set_attribute("retrieve.points", len(points))
                span.set_attribute("retrieve.filter_relaxed", filter_relaxed)
                set_cost(span)

            if not (route_name == "STRUCTURAL" and scope is not None):
                with tracer.start_as_current_span(STAGE_RERANK) as span, StageTimer(route_name, STAGE_RERANK):
                    span.set_attribute("rerank.points_in", len(points))
                    # v2 P7 Batch H: rerank the FULL list, then slice here, because
                    # BACKFILL needs the ranks BELOW k to refill a merged slot from.
                    # `top_k` has always been a parameter for exactly this reason --
                    # src/p7_objective.py already calls it this way. At
                    # neighbor_expansion=0 the slice is the only thing that happens
                    # and the result is byte-identical to rerank(...)'s default.
                    ranked = rerank(body.query, points, state.ranker, top_k=len(points))
                    points = ranked[:Config.TOP_K_RERANK]
                    span.set_attribute("rerank.points_out", len(points))
                    set_cost(span)

                radius = Config.NEIGHBOR_EXPANSION
                if radius:
                    # Fetch the chunks either side of each hit by point id and stitch
                    # them into contiguous prose (D-13/D-14). DEFAULT route only --
                    # STRUCTURAL skips rerank and its hard filter already pins the
                    # chapter, so there is nothing here for it to do.
                    #
                    # NOT a cost saving at the pinned top_k_rerank=3: the candidate
                    # doc's 13% came from trading k 3->1, and with k fixed this ADDS
                    # windows. src/rag/gate.py:312 BREAKs on any cost increase, so
                    # this ships default-off and promoting it is a pointer write.
                    with tracer.start_as_current_span(STAGE_CONTEXT) as span:
                        points = expand_spans(
                            state.qdrant, Config.QDRANT_COLLECTION, points, ranked, radius,
                            Config.MAX_SPAN_TOKENS, Config.NEIGHBOR_COLLISION_POLICY,
                        )
                        span.set_attribute("expand.radius", radius)
                        span.set_attribute("expand.policy", Config.NEIGHBOR_COLLISION_POLICY)
                        span.set_attribute("expand.spans", len(points))

            with tracer.start_as_current_span(STAGE_CONTEXT) as span, StageTimer(route_name, STAGE_CONTEXT):
                passages_block, sources = build_context(route_name, points, state.summaries, scope)
                no_context = not sources
                span.set_attribute("context.sources", len(sources))
                span.set_attribute("context.no_context", no_context)
                set_cost(span)
            if no_context:
                logger.info("chat: no_context=true query=%r", body.query)
        except ValueError as exc:
            # Unmapped book title / non-integral part-chapter filter -- reject
            # loudly (matches Config.resolve_book_title()'s own contract) rather
            # than let it surface as a 500.
            _abort()
            raise HTTPException(status_code=400, detail=str(exc), headers=rung_header) from exc
        except Exception:
            _abort()
            raise

    if rung is Rung.RETRIEVAL_ONLY:
        # Generation is down and this query has no cached answer, but retrieval
        # is up: serve the passages and generate nothing (charter V's fourth
        # rung). No `token` event is emitted at all -- with no generation there
        # is no persona, and inventing prose here is precisely the failure mode
        # the rung exists to avoid. How the SPA PRESENTS this state is O9,
        # decided at CP-4c by looking at what it renders; Batch A0 does not
        # touch `frontend/`.
        logger.warning(
            "chat: rung=%s query=%r sources=%d -- generation unavailable, serving "
            "passages only (%s)",
            rung.value, body.query, len(sources), dependency_result.get("generation"),
        )
        footer = "" if no_context else build_sources_footer(sources)
        retrieval_only_outcome = ["error"]

        async def retrieval_only_stream():
            try:
                yield _sse("sources", {"sources": sources, "footer": footer})
                yield _sse("meta", {
                    "request_id": current_request_id(),
                    "tenant": current_tenant(),
                    "config_version": config_version,
                    "corpus_version": corpus_version,
                    "route_name": route_name,
                    "chunk_ids": [p.id for p in points],
                    # No token ever arrived because none was asked for -- None,
                    # not 0, for the same reason as the empty-completion path
                    # (R33): a number here would read as a measurement.
                    "ttft_ms": None,
                    "total_ms": int((time.monotonic() - t_start) * 1000),
                    "filter_relaxed": filter_relaxed,
                    "no_context": no_context,
                    "cache_hit": False,
                    # The bypass predicate, computed once at :722. See the
                    # cached branch for why it rides beside cache_hit.
                    "memory_bearing": memory_bearing,
                    "rung": rung.value,
                    # Nothing generated, so nothing billed and no producing arm
                    # to name. `model` is None rather than
                    # `Config.SERVING_MODEL`: the configured intent did not run.
                    "model": None,
                    "cost_usd": FREE.usd,
                    "cost_metered": FREE.metered,
                })
                yield _sse("done", {})
                retrieval_only_outcome[0] = "ok"
            finally:
                # Same posture as the cache-hit stream: a client that
                # disconnects mid-stream still consumed a request.
                record_request_duration(
                    time.monotonic() - t_start, route_name, retrieval_only_outcome[0], tenant,
                )

        return StreamingResponse(retrieval_only_stream(), media_type="text/event-stream")

    # v2 P6 steps 1 + 3: allocate, then compose. Outside the retrieval lock
    # (it is pure CPU over strings already in hand) and after the ladder's
    # retrieval-only exit, which serves no prompt at all.
    #
    # The policy comes from `Config.ALLOCATION_POLICY` through the versioned
    # artifact -- no call site here names a policy class, which is what makes
    # swapping one a config change and nothing else.
    with tracer.start_as_current_span(STAGE_ALLOCATE) as span, StageTimer(route_name, STAGE_ALLOCATE):
        plan = active_policy().allocate(AllocationInputs(
            tenant=tenant,
            session_id=body.session_id,
            query=body.query,
            budget_tokens=context_budget_tokens(),
            passages_block=passages_block,
            recent_turns=recent_turns,
            profile_facts=profile_facts,
            episodes=episodes,
        ))
        # Per-block tokens and provenance onto the span -- P1's spine, through
        # the mechanism P1 already defines. Provenance is the point of step 1:
        # a bad answer has to be traceable to the block that caused it.
        for _attr, _value in plan.attributes().items():
            span.set_attribute(_attr, _value)
        set_cost(span)

    messages = build_messages(
        active_persona_head(),
        plan.text_for(TIER_PASSAGES),
        body.query,
        memory_block=plan.memory_block(),
    )
    # Filled in place as the stream runs, summed across every EMPTY_RETRIES
    # attempt. Stays empty if llama-server reports no usage, which is what
    # makes the request UNPRICED rather than estimated (P1 step 3).
    usage: dict = {}

    def _serving_model() -> str:
        """The arm that ACTUALLY served, for attribution.

        Provider-neutral by construction: this reads a field the gateway fills,
        it does not know what providers exist. `Config.SERVING_MODEL` is the
        fallback for the window before the stream has reported anything (and
        for a provider that reports no model at all) -- it is the configured
        intent, which is the right answer when nothing better is known and the
        wrong one to prefer once the gateway has said what really ran. Once
        routing lands (step 5) the two genuinely differ per request.
        """
        return usage.get("model") or Config.SERVING_MODEL

    gen = stream_answer(messages, usage)

    # Prime the stream before opening the HTTP response body: a connect/read
    # failure here still has a response to attach 503/504 to (design §IV.2).
    t_first_token: float | None = None
    gen_first_span = tracer.start_span(STAGE_GENERATE_FIRST)
    _t_gen_first = time.monotonic()
    try:
        with trace.use_span(gen_first_span, end_on_exit=True, record_exception=False):
            try:
                first_token = await gen.__anext__()
                t_first_token = time.monotonic()
                gen_first_span.set_attribute("generate.empty", False)
            except StopAsyncIteration:
                # No token ever arrived -- every EMPTY_RETRIES attempt came
                # back empty, and all of them were paid for inside this single
                # __anext__(). Leaving t_first_token unset is the point (R33):
                # setting it here reported "time to give up" as ttft_ms, which
                # is not a time-to-first-token at all, and it made
                # ttft_ms == total_ms on exactly the requests whose latency was
                # least trustworthy. ttft_ms is None when there is no first
                # token, which is what None is for. The span still records the
                # elapsed time, and the httpx child spans show the attempts --
                # so the cost stays visible without being mislabelled.
                first_token = None
                gen_first_span.set_attribute("generate.empty", True)
                record_empty_completion(route_name)
            gen_first_span.set_attribute("generate.model", Config.SERVING_MODEL)
            # Prefill DOES bill, but its token count is not knowable yet:
            # llama-server reports usage in a final chunk that arrives after
            # the whole stream, long after this span closes on the first
            # token. The charge is attributed to the sibling instead, and the
            # marker says so -- a bare 0.0 here would read as "prefill is
            # free", which is false. Every retry's prompt is included in that
            # sibling's count (R33), so the storm stays visible and priced.
            set_cost(
                gen_first_span,
                Cost(usd=0.0, metered=is_metered(Config.SERVING_MODEL)),
                deferred_to=STAGE_GENERATE_STREAM,
            )
        record_stage_duration(time.monotonic() - _t_gen_first, route_name, STAGE_GENERATE_FIRST)
    # Gateway vocabulary, not llama-server's (Batch C step 6). Each maps to the
    # status that describes what actually happened, because collapsing them
    # would make a policy refusal, a quota rejection and an outage
    # indistinguishable in the one place an operator looks first.
    except QuotaExhausted as exc:
        _abort()
        raise HTTPException(
            status_code=429, detail="provider quota exhausted", headers=rung_header,
        ) from exc
    except RequestTooLarge as exc:
        _abort()
        raise HTTPException(status_code=413, detail=str(exc), headers=rung_header) from exc
    except (BudgetExceeded, Degraded) as exc:
        # 503, not 402/429: charter IV puts a tripped spend guard into DEGRADED
        # mode, which is an availability statement about this deployment, not a
        # bill the caller can settle or a rate they can back off from.
        _abort()
        raise HTTPException(
            status_code=503, detail="serving degraded: spend guard", headers=rung_header,
        ) from exc
    except ContentRefused as exc:
        # R53/R57: Google returns HTTP 200 carrying a refusal. Surfacing it as
        # 502 would call a policy decision an upstream failure; 200-with-empty
        # would report a block as an answer. 422 says the request was
        # understood and declined.
        _abort()
        raise HTTPException(
            status_code=422, detail="provider refused to generate", headers=rung_header,
        ) from exc
    except TransportError as exc:
        _abort()
        raise HTTPException(
            status_code=502, detail="provider unreachable", headers=rung_header,
        ) from exc

    def _generation_cost() -> Cost:
        """Price the generation stage from whatever usage has accumulated.

        Both halves are priced here, not split across the two generation
        spans: llama-server's usage chunk arrives after the stream ends, by
        which time generate.first_token has long closed. Prompt tokens are the
        sum over every EMPTY_RETRIES attempt, so a four-attempt storm that
        produced no answer still prices its four prompts (R33).
        """
        return cost_usd(
            _serving_model(),
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )

    stream_outcome = ["error"]

    async def event_stream():
        answer_parts: list[str] = []
        stream_span = tracer.start_span(STAGE_GENERATE_STREAM)
        _t_stream = time.monotonic()
        try:
            if first_token:
                answer_parts.append(first_token)
                yield _sse("token", {"text": first_token})
            try:
                async for token in gen:
                    answer_parts.append(token)
                    yield _sse("token", {"text": token})
            except (TransportError, ContentRefused) as exc:
                # Mid-stream failure: the 200 status is already committed, so
                # this cannot become a 502/422 -- surface as a terminal SSE
                # error event instead of hanging (design §IV.2). ContentRefused
                # belongs here as well as in the pre-stream handler: Google can
                # emit a refusal partway through a stream that had already
                # started producing text (R57's in-band `error` event).
                logger.error("chat: mid-stream failure query=%r: %s", body.query, exc)
                stream_span.set_attribute("generate.mid_stream_error", type(exc).__name__)
                yield _sse("error", {"detail": str(exc)})
                return

            answer = "".join(answer_parts)
            stream_span.set_attribute("generate.tokens", len(answer_parts))
            stream_span.set_attribute("generate.answer_chars", len(answer))
            footer = "" if no_context else build_sources_footer(sources)
            yield _sse("sources", {"sources": sources, "footer": footer})

            if answer and not memory_bearing:
                # Config.SERVING_MODEL is the producing arm (v2 P2 step 9,
                # R51) -- the only one there is until Batch D's routing picks
                # per-request between more than one. Storing it now is what
                # lets a hit under per-request routing say which arm answered.
                #
                # `not memory_bearing` is the WRITE half of the bypass, and it
                # is the half that actually isolates: a memory-bearing answer
                # stored under a memory-free key would be served to a different
                # tenant asking the same question. Skipping only the read would
                # look identical from outside until the day it leaked.
                state.cache.set(
                    body.query, filters, route_name, config_version, corpus_version,
                    answer, sources, footer, _serving_model(),
                )
            if answer:
                # In-band, post-answer (G-P13's placement). Free here -- a dict
                # write, not a provider call -- so the admission slot is held
                # no measurably longer. The summarizer write that G-P13 was
                # actually about is Batch C's.
                state.turns.append(tenant, body.session_id, body.query, answer)

            total_ms = int((time.monotonic() - t_start) * 1000)
            ttft_ms = int((t_first_token - t_start) * 1000) if t_first_token is not None else None
            # The request cost IS the sum of its stage costs -- there is
            # deliberately no second, independently-computed total that could
            # disagree with the tree. Six stages are free by construction and
            # generate.first_token defers to this span, so the sum is this
            # span's cost; total_usd() is called anyway because it owns the
            # unpriced- and metered-propagation rules.
            cost = total_usd([_generation_cost()])

            # Metrics for the answer, recorded once, here (plan step 7 step 3).
            if ttft_ms is not None:
                record_ttft(ttft_ms / 1000, route_name)
            # G5's three-way split, kept three-way on purpose. An unmetered
            # answer must NOT enter the cost histogram as 0.0 -- a zero there
            # is indistinguishable from a real metered-but-free call, which is
            # exactly the false claim G5 forbids. An unpriced answer is a third
            # thing again: somebody billed us and we could not say how much.
            # All three make an aggregate provisional; only one of them is a
            # cost.
            if not cost.metered:
                record_unmetered_answer(_serving_model())
            elif cost.usd is None:
                record_unpriced_answer(_serving_model())
            else:
                # `tenant` here is what makes "share of the shared bill captured by
                # the heaviest tenant" (A12(1)'s fairness number) readable off
                # the operator plane rather than reconstructed from logs.
                record_answer_cost(cost.usd, _serving_model(), tenant)
            stream_outcome[0] = "empty" if not answer else "ok"

            yield _sse("meta", {
                "request_id": current_request_id(),
                "tenant": current_tenant(),
                "config_version": config_version,
                "corpus_version": corpus_version,
                "route_name": route_name,
                "chunk_ids": [p.id for p in points],
                "ttft_ms": ttft_ms,
                "total_ms": total_ms,
                "filter_relaxed": filter_relaxed,
                "no_context": no_context,
                "cache_hit": False,
                # The bypass predicate, computed once at :722. See the cached
                # branch for why it rides beside cache_hit.
                "memory_bearing": memory_bearing,
                # Ladder tier this request was served at (P4 Batch A0).
                "rung": rung.value,
                "cost_usd": cost.usd,
                "cost_metered": cost.metered,
                # Token counts on the meta event (v2 P6 Batch E prerequisites).
                # They were already logged and already priced; CP-6d's dry run
                # requires the standing four -- answer tail, TOKEN COUNTS, cost,
                # model id -- and the harness client can only report what the
                # server says. Absent stays absent (None), never 0: a 0 here
                # would read as a free draw rather than an unreported one.
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                # Producing arm (v2 P2 step 9, R51) -- same field the cache-hit
                # meta event carries, so the frontend/attribution reader does
                # not need a different lookup per cache_hit value.
                "model": _serving_model(),
                # The context plan the SERVER actually served, verbatim from
                # the same object the span was stamped from. Batch B's driver
                # must record the served plan rather than recompute one
                # client-side -- a client-recomputed plan agrees with the
                # server right up until the moment it matters, and looks
                # identical to success while disagreeing (R97).
                "context_plan": plan.attributes(),
            })
            logger.info(
                "chat: query=%r route=%s filters=%s entities=%s chunk_ids=%s "
                "cache_hit=false cache_population=%s ttft_ms=%s total_ms=%s "
                "filter_relaxed=%s no_context=%s "
                "prompt_tokens=%s completion_tokens=%s cost_usd=%s cost_metered=%s session_id=%s",
                body.query, route_name, filters, resolved_entities,
                [p.id for p in points],
                # Step 7(b): which POPULATION this request belongs to, on the
                # line that already carries `cache_hit`. Counting `cache_hit=false`
                # lines is the log-side way to compute a hit rate, and without
                # this field that count silently pools bypassed requests with
                # real misses -- the same defect the two metric names prevent.
                "memory_bearing" if memory_bearing else "memory_free",
                ttft_ms, total_ms, filter_relaxed, no_context,
                usage.get("prompt_tokens"), usage.get("completion_tokens"),
                cost.usd, cost.metered, body.session_id,
            )

            # --- v2 P6 step 4: the memory write, in band (G-P13) -------------
            #
            # AFTER the meta event, deliberately. G-P13's residual risk: latency
            # comparability with P4/P5 survives only if it is measured at the
            # answer-done SSE event and not at stream close, and `meta.total_ms`
            # IS that timestamp (Batch B's driver reads it as
            # `server_total_ms`). Writing before the meta yield would fold the
            # write's duration into every published p95 -- the R97
            # looks-identical-to-success shape.
            #
            # BEFORE the stream closes and the admission slot releases, equally
            # deliberately: this is the turn's own work, on the turn's own slot.
            # A second slot would be hold-and-wait under contention and would
            # count one user action twice against CHAT_MAX_INFLIGHT_TOTAL.
            #
            # The write's own duration is reported separately, which is what the
            # Verify table's serving-vs-summarizer USD/latency split already
            # demands. Slot occupancy grows by it on turns where it fires, so
            # `src/config.py:255-266`'s ~12.6 s admitted-wait arithmetic no
            # longer holds on those turns -- recorded, not retuned (R119 puts
            # the path at ~0.25 rps, where the queue never forms).
            #
            # `write_profile_from_turn` never raises: a failed write must not
            # fail a turn whose answer has already been delivered. It counts
            # instead (`mitya_memory_write_failures_total`), because "logged,
            # not metered" was ruled out here -- a silently stale memory changes
            # what the multi-turn metric measures.
            #
            # The QUESTION ONLY reaches the extractor (O-E), never `answer`.
            #
            # THE EPISODIC WRITE IS THE LAZY TRIGGER (CP-6d(a), G-P15 closed).
            # Session k-1 is summarized on the first turn of session k for the
            # same tenant: no session-end signal exists to trigger it from --
            # `ChatRequest` marks nothing finished and the auth session is a
            # different object on a 12h TTL -- but the LRU is already ordered by
            # use and keyed by tenant, so the predecessor is a LOOKUP, not an
            # event. No new endpoint, no client contract, no background task,
            # and it lands on a turn already in band, so G-P13 is unchanged.
            #
            # It fires at most once per session, and never for a session with no
            # successor: the last session of every tenant is not summarized (39
            # of 40 in a full replay) and a restart loses what RAM held. Both
            # accepted at CP-6d; there is no orphaned-write machinery.
            if answer:
                _t_write = time.monotonic()
                written = await write_profile_from_turn(tenant, body.query)
                episode_of = None
                if first_turn_of_session:
                    previous = state.turns.previous_session(tenant, body.session_id)
                    if previous is not None:
                        prior_id, prior_turns = previous
                        # `turn_count` is the LAST TURN'S INDEX, not the deque's
                        # length: the deque holds MAX_RECENT_TURNS and a digest
                        # reporting "4 turns" for a nine-turn session would
                        # misstate what it summarized. The transcript goes in
                        # fenced, through the same renderer the serving prompt
                        # uses -- one fence, not a second wording to drift.
                        episode_of = prior_id if await write_episode(
                            tenant, prior_id, render_turns(prior_turns), prior_turns[-1].index,
                        ) else None
                record_stage_duration(
                    time.monotonic() - _t_write, route_name, STAGE_MEMORY_WRITE,
                )
                logger.info(
                    "chat: memory_write rows=%d episode_of=%s session_id=%s write_ms=%d",
                    written, episode_of, body.session_id,
                    int((time.monotonic() - _t_write) * 1000),
                )
        finally:
            # Ended here, not by a context manager: the span opens in this
            # generator and must close whether the stream completed, errored
            # mid-flight, or the client disconnected.
            #
            # Cost is stamped in the `finally` for the same reason. A
            # mid-stream failure returns early and a client disconnect never
            # reaches the meta event, but both paid for every token that
            # arrived -- an abandoned stream is exactly where an unstamped
            # span would silently under-report the bill.
            stream_span.set_attribute("generate.model", _serving_model())
            for field in ("prompt_tokens", "completion_tokens"):
                if usage.get(field) is not None:
                    stream_span.set_attribute(f"generate.{field}", usage[field])
            set_cost(stream_span, _generation_cost())
            record_stage_duration(time.monotonic() - _t_stream, route_name, STAGE_GENERATE_STREAM)
            record_request_duration(time.monotonic() - t_start, route_name, stream_outcome[0], tenant)
            stream_span.end()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# Serve the built SPA (design §I.6, C8) -- must be the LAST statement in the
# module: Starlette matches routes in registration order, so /health, /books,
# /chat, and FastAPI's own /docs/openapi.json/redoc (registered above, at
# app-construction/decorator time) all win over this catch-all mount. Two
# caveats, documented here and in README.md's run notes: (1) html=True does
# NOT SPA-fallback unknown paths to index.html -- there is no client router,
# so an unmapped path 404s, which is fine; (2) directory="frontend/dist" is
# resolved relative to the uvicorn PROCESS CWD, not this file's location --
# the server must be launched from the repo root, or this mount raises
# RuntimeError at startup ("Directory 'frontend/dist' does not exist") if
# `npm run build` has not been run yet, in dev-proxy-only workflows included.
from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/", StaticFiles(directory="frontend/dist", html=True), name="ui")
