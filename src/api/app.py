"""FastAPI app: lifespan-managed retrieval/generation clients, Semaphore(1), request handlers.

Composes P4 (retrieval) and P5 (generation) behind a single online path
(design §IV, rulings R4/R15/R22). Everything expensive -- embedder, BM25,
reranker, Qdrant client, entities/summaries/structure, disk cache -- is
constructed exactly once in `lifespan()` and hung off `app.state`; endpoints
only ever read `request.app.state`, never construct these themselves (design
§IV.3: "never lazily per query").

`asyncio.Semaphore(1)` serializes retrieval+generation end to end, including
the streamed token phase -- llama-server runs with `--parallel 1` and the
fastembed/FlashRank models are not re-entrant, so a second request must queue
rather than interleave. The semaphore is therefore acquired in `POST /chat`
before any pipeline work and released only in the `finally` of the SSE
generator (or explicitly on every early-exit branch before a 4xx/5xx is
raised) -- acquiring/releasing it as a `with` block scoped to the endpoint
function would release it as soon as the endpoint *returns* a
`StreamingResponse`, before the body is actually streamed, which would defeat
the point.

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

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastembed import SparseTextEmbedding, TextEmbedding
from opentelemetry import trace
from pydantic import BaseModel
from qdrant_client import QdrantClient

from src.config import Config
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
    record_cache_lookup,
    record_empty_completion,
    record_request_duration,
    record_stage_duration,
    record_ttft,
    record_unmetered_answer,
    record_unpriced_answer,
)
from src.observability import (
    STAGE_CACHE,
    STAGE_CONTEXT,
    STAGE_ENTITIES,
    STAGE_GENERATE_FIRST,
    STAGE_GENERATE_STREAM,
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
from src.rag.context import build_context
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
    session_id: str | None = None  # log-correlation only -- no conversation memory in v1


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load every RAG artifact/client ONCE (design §IV.3). Fails fast --
    missing artifacts (Config.validate_rag) or an empty Qdrant collection both
    raise before the server starts accepting requests."""
    Config.validate_rag()
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
    app.state.sem = asyncio.Semaphore(1)

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

# Order matters, and only this way round: FastAPIInstrumentor registers its own
# middleware, and Starlette runs the LAST-added middleware outermost. Ours must
# run first so the request_id ContextVar is set before the tracer mints the root
# span -- that is what lets RequestIdGenerator derive trace_id from request_id
# instead of falling back to random (R32).
tracer = configure_tracing(app)
app.add_middleware(RequestIdMiddleware)
# Second, independent reader on the same span/attribute data (P1 step 7,
# R32) -- see src/metrics.py's module docstring for why this is not a second
# measurement path.
configure_metrics()


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
async def books(request: Request) -> dict:
    """Serve structure.json verbatim -- feeds the UI's book/part/chapter filter."""
    return request.app.state.structure


@app.post("/chat")
async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
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

    await state.sem.acquire()

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
        """Release the semaphore and record the request as an error.

        Every pre-generation exit goes through here rather than calling
        `state.sem.release()` directly, because the request-duration histogram
        is what drives BOTH RED panels -- the rate and the error ratio. A path
        that releases without recording is invisible to both: measured live at
        P1 step 7, a 503 from an unreachable llama-server recorded all six
        stage durations and no request at all, so the error ratio read 0.000
        during a total outage. One helper, so there is one place that can be
        got wrong instead of four.
        """
        record_request_duration(time.monotonic() - t_start, route_name, "error")
        state.sem.release()
    # Root attribute is the slice key for a latency/cost percentile (P1 step
    # 4) -- the stage span above keeps its own route.name/scope_resolved
    # detail, this is the per-request dimension.
    root_span.set_attribute("route", route_name)

    with tracer.start_as_current_span(STAGE_CACHE) as span, StageTimer(route_name, STAGE_CACHE):
        cached = state.cache.get(body.query, filters, route_name, config_version, corpus_version)
        span.set_attribute("cache.hit", cached is not None)
        set_cost(span)
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
            "chat: query=%r filters=%s session_id=%s cache_hit=true", body.query, filters, body.session_id,
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
                cached_outcome[0] = "ok"
            finally:
                # In the `finally`, and with the outcome tracked in a cell, for
                # the same reason cost is stamped there on the generate path: a
                # client that disconnects mid-stream never reaches the line
                # above, but it did consume a request, and a duration histogram
                # that silently drops those measures only the happy path.
                record_request_duration(
                    time.monotonic() - t_start, route_name, cached_outcome[0],
                )
                state.sem.release()

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
        with tracer.start_as_current_span(STAGE_ENTITIES) as span, StageTimer(route_name, STAGE_ENTITIES):
            expanded_query, resolved_entities = state.entities.resolve(
                body.query, active_book=active_book
            )
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
                points = rerank(body.query, points, state.ranker)
                span.set_attribute("rerank.points_out", len(points))
                set_cost(span)

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
                    time.monotonic() - t_start, route_name, retrieval_only_outcome[0],
                )
                state.sem.release()

        return StreamingResponse(retrieval_only_stream(), media_type="text/event-stream")

    messages = build_messages(active_persona_head(), passages_block, body.query)
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

            if answer:
                # Config.SERVING_MODEL is the producing arm (v2 P2 step 9,
                # R51) -- the only one there is until Batch D's routing picks
                # per-request between more than one. Storing it now is what
                # lets a hit under per-request routing say which arm answered.
                state.cache.set(
                    body.query, filters, route_name, config_version, corpus_version,
                    answer, sources, footer, _serving_model(),
                )

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
                record_answer_cost(cost.usd, _serving_model())
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
                # Ladder tier this request was served at (P4 Batch A0).
                "rung": rung.value,
                "cost_usd": cost.usd,
                "cost_metered": cost.metered,
                # Producing arm (v2 P2 step 9, R51) -- same field the cache-hit
                # meta event carries, so the frontend/attribution reader does
                # not need a different lookup per cache_hit value.
                "model": _serving_model(),
            })
            logger.info(
                "chat: query=%r route=%s filters=%s entities=%s chunk_ids=%s "
                "cache_hit=false ttft_ms=%s total_ms=%s filter_relaxed=%s no_context=%s "
                "prompt_tokens=%s completion_tokens=%s cost_usd=%s cost_metered=%s session_id=%s",
                body.query, route_name, filters, resolved_entities,
                [p.id for p in points], ttft_ms, total_ms, filter_relaxed, no_context,
                usage.get("prompt_tokens"), usage.get("completion_tokens"),
                cost.usd, cost.metered, body.session_id,
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
            record_request_duration(time.monotonic() - t_start, route_name, stream_outcome[0])
            stream_span.end()
            state.sem.release()

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
