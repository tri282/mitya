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
from fastapi.responses import JSONResponse, StreamingResponse
from fastembed import SparseTextEmbedding, TextEmbedding
from pydantic import BaseModel
from qdrant_client import QdrantClient

from src.config import Config
from src.observability import (
    RequestIdMiddleware,
    configure_logging,
    current_request_id,
)
from src.rag.context import build_context
from src.rag.entities import EntityMap
from src.rag.generate import (
    PERSONA_HEAD,
    LlamaServerTimeout,
    LlamaServerUnavailable,
    build_messages,
    build_sources_footer,
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

# Registered here rather than after the routes: ASGI middleware wraps the whole
# app, so ordering against route registration is irrelevant -- but it must be
# added before the first request, and this is the only statement that runs at
# import time between app construction and the route decorators.
app.add_middleware(RequestIdMiddleware)


def _sse(event: str, payload: dict) -> str:
    """Format one SSE frame. All payloads are JSON so the client has a single
    parse path regardless of event type."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """llama-server reachability + Qdrant presence/count + artifact load
    status. 200 only if all green (design §IV.2)."""
    state = request.app.state
    healthy = True
    result: dict = {}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            resp = await client.get(f"{Config.LLAMA_SERVER_URL}/health")
            resp.raise_for_status()
        result["llama_server"] = "ok"
    except httpx.HTTPError as exc:
        result["llama_server"] = f"unreachable: {exc}"
        healthy = False

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
    result["status"] = "ok" if healthy else "degraded"
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

    await state.sem.acquire()

    # --- cache check (inside the semaphore, per design §III.4/§III.6) ---
    cached = state.cache.get(body.query, filters)
    if cached is not None:
        logger.info(
            "chat: query=%r filters=%s session_id=%s cache_hit=true", body.query, filters, body.session_id,
        )

        async def cached_stream():
            try:
                yield _sse("token", {"text": cached["answer"]})
                yield _sse("sources", {"sources": cached["sources"], "footer": cached["footer"]})
                yield _sse("meta", {
                    "request_id": current_request_id(),
                    "route_name": "CACHE",
                    "chunk_ids": [],
                    "ttft_ms": None,
                    "total_ms": int((time.monotonic() - t_start) * 1000),
                    "filter_relaxed": False,
                    "no_context": not cached["sources"],
                    "cache_hit": True,
                })
                yield _sse("done", {})
            finally:
                state.sem.release()

        return StreamingResponse(cached_stream(), media_type="text/event-stream")

    # --- route -> resolve/expand -> retrieve -> rerank -> build_context ---
    try:
        route_name, scope = route(body.query, filters, state.structure, state.summaries)
        active_book = filters.get("book") if filters else None
        expanded_query, resolved_entities = state.entities.resolve(body.query, active_book=active_book)

        filter_relaxed = False
        if route_name == "STRUCTURAL" and scope is not None:
            points = retrieve_structural_excerpts(
                state.qdrant, state.embedder, expanded_query,
                scope["book"], scope["part"], scope["chapter"],
            )
        else:
            hard_filter = build_hard_filter(filters)
            points = retrieve(state.qdrant, state.embedder, state.bm25, expanded_query, hard_filter)
            if not points and hard_filter is not None:
                # Fallback (design §IV.3): a hard filter that matches zero
                # chunks retries once unfiltered rather than dead-ending.
                filter_relaxed = True
                logger.info(
                    "chat: filter_relaxed=true query=%r filters=%s", body.query, filters,
                )
                points = retrieve(state.qdrant, state.embedder, state.bm25, expanded_query, None)
            points = rerank(body.query, points, state.ranker)

        passages_block, sources = build_context(route_name, points, state.summaries, scope)
        no_context = not sources
        if no_context:
            logger.info("chat: no_context=true query=%r", body.query)
    except ValueError as exc:
        # Unmapped book title / non-integral part-chapter filter -- reject
        # loudly (matches Config.resolve_book_title()'s own contract) rather
        # than let it surface as a 500.
        state.sem.release()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        state.sem.release()
        raise

    messages = build_messages(PERSONA_HEAD, passages_block, body.query)
    gen = stream_answer(messages)

    # Prime the stream before opening the HTTP response body: a connect/read
    # failure here still has a response to attach 503/504 to (design §IV.2).
    t_first_token: float | None = None
    try:
        first_token = await gen.__anext__()
        t_first_token = time.monotonic()
    except StopAsyncIteration:
        first_token = None
        t_first_token = time.monotonic()
    except LlamaServerUnavailable as exc:
        state.sem.release()
        raise HTTPException(status_code=503, detail="llama-server unreachable") from exc
    except LlamaServerTimeout as exc:
        state.sem.release()
        raise HTTPException(status_code=504, detail="llama-server timed out") from exc

    async def event_stream():
        answer_parts: list[str] = []
        try:
            if first_token:
                answer_parts.append(first_token)
                yield _sse("token", {"text": first_token})
            try:
                async for token in gen:
                    answer_parts.append(token)
                    yield _sse("token", {"text": token})
            except (LlamaServerUnavailable, LlamaServerTimeout) as exc:
                # Mid-stream failure: the 200 status is already committed, so
                # this cannot become a 503/504 -- surface as a terminal SSE
                # error event instead of hanging (design §IV.2).
                logger.error("chat: mid-stream failure query=%r: %s", body.query, exc)
                yield _sse("error", {"detail": str(exc)})
                return

            answer = "".join(answer_parts)
            footer = "" if no_context else build_sources_footer(sources)
            yield _sse("sources", {"sources": sources, "footer": footer})

            if answer:
                state.cache.set(body.query, filters, answer, sources, footer)

            total_ms = int((time.monotonic() - t_start) * 1000)
            ttft_ms = int((t_first_token - t_start) * 1000) if t_first_token is not None else None
            yield _sse("meta", {
                "request_id": current_request_id(),
                "route_name": route_name,
                "chunk_ids": [p.id for p in points],
                "ttft_ms": ttft_ms,
                "total_ms": total_ms,
                "filter_relaxed": filter_relaxed,
                "no_context": no_context,
                "cache_hit": False,
            })
            logger.info(
                "chat: query=%r route=%s filters=%s entities=%s chunk_ids=%s "
                "cache_hit=false ttft_ms=%s total_ms=%s filter_relaxed=%s no_context=%s session_id=%s",
                body.query, route_name, filters, resolved_entities,
                [p.id for p in points], ttft_ms, total_ms, filter_relaxed, no_context, body.session_id,
            )
        finally:
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
