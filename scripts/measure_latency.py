# scripts/measure_latency.py
"""Latency measurement on the real 6GB card (design VI, VII.3) -- Phase 7.

[RUN -- needs approval]: hits the live `src/api/app.py` server over HTTP
(`Config`'s default `http://127.0.0.1:8000`, override with `--base-url`) and
therefore the real llama-server behind it. Never invoked by the agent.

P6 already measured generation end-to-end on the real GTX 1060 6GB card
(plans/mitya-rag/05-impl/phase-6.md, "Measured latency" section) via the
`/chat` request log line's `ttft_ms`/`total_ms` fields:

    KNOWN (P6, do not re-measure):
        TTFT   ~= 7.9s   (ttft_ms=7922, ~2k-token prefill)
        TOTAL  ~= 21.3s  (total_ms=21320, <=400 decode tokens)
        cache hit: near-instant (single cached `token` + `done`, no llama-server call)

Both exceed the *provisional* docs/RAG.md VI targets (TTFT <=3s, total
<=20s) -- that is P6's measurement, not this script's job to explain. This
script fills in the two numbers P6's run did NOT isolate:

  1. Retrieval-only latency (embed + Qdrant fused query + rerank-25), the
     part of the request before generation starts at all. Design target
     <= 0.6s.
  2. A cache-hit round trip, to confirm the < 50ms target with an actual
     timer rather than "near-instant" by eyeball.

It does this by timing two consecutive POST /chat calls with the SAME query
(first = miss, includes retrieval; second = cache hit) and reading retrieval
latency back out of the first request's own timing plus the server log's
ttft_ms (retrieval time ~= wall time to first byte of the SSE stream MINUS
the time the server log attributes to llama-server's own first-token
latency is not separable client-side -- see the printed caveat below). For
an exact retrieval-only number with no generation in the loop at all, this
script also offers `--retrieval-only`, which calls the same retrieval
primitives P4's `scripts/eval_retrieval.py` exercises (embed, Qdrant query,
rerank) directly, with NO llama-server call -- the cleanest isolation of the
design VI "Retrieval" line item.

After running, fill in `docs/RAG.md` §VI yourself from the printed numbers
(the phase file: revise the table ONCE, from real measurements -- this
script does not edit docs/RAG.md).

Usage:
    # End-to-end (miss + cache-hit) against a running `uvicorn src.api.app:app`:
    python scripts/measure_latency.py --query "Why did Raskolnikov confess?"

    # Retrieval-only, no llama-server needed (embed + Qdrant + rerank):
    python scripts/measure_latency.py --retrieval-only --query "Why did Raskolnikov confess?"
"""

import argparse
import json
import logging
import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx

from src.config import Config

logger = logging.getLogger(__name__)

# P6's already-measured generation numbers (real GTX 1060 6GB card,
# plans/mitya-rag/05-impl/phase-6.md) -- printed for context, not
# re-measured here. Kept as named constants (not re-derived) so this
# script's own printed summary can be read next to them directly.
P6_TTFT_MS = 7922
P6_TOTAL_MS = 21320


def measure_retrieval_only(query: str, filters: dict | None = None) -> float:
    """Time embed + Qdrant fused query + rerank-25 directly, no HTTP, no
    llama-server -- the cleanest isolation of design VI's "Retrieval" line
    item. Mirrors scripts/eval_retrieval.py's construction exactly so the
    timed path is the real one, not a shortcut."""
    from fastembed import SparseTextEmbedding, TextEmbedding
    from qdrant_client import QdrantClient

    from src.rag.entities import EntityMap
    from src.rag.retrieve import build_hard_filter, build_ranker, rerank, retrieve

    logger.info("loading embedder/ranker/qdrant (not timed -- lifespan cost, one-time in the real server)")
    embedder = TextEmbedding(model_name=Config.EMBED_MODEL)
    bm25 = SparseTextEmbedding(model_name="Qdrant/bm25")
    ranker = build_ranker()
    client = QdrantClient(path=Config.VECTOR_DB_DIR)
    entity_map = EntityMap()
    hard_filter = build_hard_filter(filters) if filters else None

    t0 = time.monotonic()
    expanded_query, _ = entity_map.resolve(query, active_book=(filters or {}).get("book"))
    points = retrieve(client, embedder, bm25, expanded_query, hard_filter)
    points = rerank(query, points, ranker)
    elapsed_s = time.monotonic() - t0

    logger.info("retrieval-only: %.3fs (%d points after rerank)", elapsed_s, len(points))
    return elapsed_s


async def _time_chat(base_url: str, query: str, filters: dict | None) -> tuple[float, float | None]:
    """POST /chat and time wall-clock to first SSE byte and to stream end.
    Returns (ttfb_s, total_s). ttfb includes retrieval + llama-server TTFT
    combined (this script cannot separate them client-side -- the server's
    own request log line, printed by src/api/app.py, is the source of truth
    for the ttft_ms/total_ms split; this script's numbers are a client-side
    cross-check on the same request)."""
    payload = {"query": query}
    if filters:
        payload["filters"] = filters

    t0 = time.monotonic()
    ttfb_s: float | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=5.0, read=120.0)) as client:
        async with client.stream("POST", f"{base_url}/chat", json=payload) as resp:
            resp.raise_for_status()
            async for _ in resp.aiter_lines():
                if ttfb_s is None:
                    ttfb_s = time.monotonic() - t0
    total_s = time.monotonic() - t0
    return ttfb_s or total_s, total_s


async def measure_end_to_end(base_url: str, query: str, filters: dict | None) -> None:
    import asyncio  # local: only this path needs the event loop

    logger.info("request 1/2 (expect cache MISS): POST %s/chat", base_url)
    ttfb1, total1 = await _time_chat(base_url, query, filters)
    logger.info("miss: time-to-first-byte=%.3fs total=%.3fs", ttfb1, total1)

    logger.info("request 2/2 (expect cache HIT, same query+filters): POST %s/chat", base_url)
    ttfb2, total2 = await _time_chat(base_url, query, filters)
    logger.info("cache hit: time-to-first-byte=%.3fs total=%.3fs (target < 0.05s)", ttfb2, total2)

    print(_summary(ttfb1, total1, ttfb2, total2))


def _summary(miss_ttfb: float, miss_total: float, hit_ttfb: float, hit_total: float) -> str:
    return (
        "\n" + "=" * 72 + "\n"
        "Phase 7 latency measurement -- fill docs/RAG.md sec VI from these numbers\n"
        + "=" * 72 + "\n"
        f"Known from P6 (real GTX 1060 6GB card, not re-measured here):\n"
        f"  TTFT (llama-server prefill)      ~= {P6_TTFT_MS / 1000:.1f}s\n"
        f"  Total (<=400 decode tokens)      ~= {P6_TOTAL_MS / 1000:.1f}s\n\n"
        f"Measured just now (this run):\n"
        f"  Cache MISS  time-to-first-byte   =  {miss_ttfb:.3f}s  (retrieval + TTFT combined)\n"
        f"  Cache MISS  total                =  {miss_total:.3f}s\n"
        f"  Cache HIT   time-to-first-byte   =  {hit_ttfb:.3f}s   (target < 0.05s)\n"
        f"  Cache HIT   total                =  {hit_total:.3f}s\n\n"
        "For an exact retrieval-only number (no llama-server in the loop), also run:\n"
        "  python scripts/measure_latency.py --retrieval-only --query '...'\n\n"
        "Cross-check the split between retrieval and llama-server TTFT against the\n"
        "server's own request log line (route/ttft_ms/total_ms), which is the\n"
        "authoritative source -- this script's ttfb numbers are client-side wall time\n"
        "on the same requests, not a re-derivation of the server's internal timers.\n"
        + "=" * 72
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Phase 7 latency measurement (design VI/VII.3).")
    ap.add_argument("--base-url", default=Config.LLAMA_SERVER_URL.replace(":8080", ":8000"),
                     help="the FastAPI server's base URL (default: derived from LLAMA_SERVER_URL's host, port 8000)")
    ap.add_argument("--query", required=True)
    ap.add_argument("--filters", type=json.loads, default=None, help='JSON, e.g. \'{"book": "Crime and Punishment"}\'')
    ap.add_argument("--retrieval-only", action="store_true",
                     help="time embed+Qdrant+rerank directly, no HTTP, no llama-server")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.retrieval_only:
        elapsed = measure_retrieval_only(args.query, args.filters)
        print(f"\nretrieval-only: {elapsed:.3f}s (target <= 0.6s per docs/RAG.md sec VI)")
    else:
        import asyncio
        asyncio.run(measure_end_to_end(args.base_url, args.query, args.filters))
