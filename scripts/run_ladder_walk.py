# scripts/run_ladder_walk.py
"""Phase 4 step 8/10(a): walk the degradation ladder and record which rung
actually served (mitya-v2 P4 Batch C-run; R83, R93, R94, R95).

Two windows, because three of the five rungs are **operator-only** and cannot
be entered over HTTP by a harness that step 1 grants no privileged access:

    --warm    cache ON, credential PRESENT.  Costs 1 draw. Seeds the disk
              cache (`diskcache`, so it survives the restart) and confirms
              PRIMARY.
    --walk    cache ON, credential UNSET.    Costs 0 draws. `generation_available`
              is then False (R94), so the warmed query returns CACHED and any
              other query returns RETRIEVAL_ONLY.

`cached_answer` needs BOTH a cache hit and generation being unavailable:
`next_rung()` returns PRIMARY whenever generation is available, *regardless of
cache_hit*, so a cache hit on a healthy deployment is a PRIMARY-rung cache hit
and not the CACHED rung at all. R95's table said this rung was forceable over
HTTP; it is not, and `src/harness/rungs.py`'s `force_cached` docstring carries
the same wrong expectation.

`503`/UNAVAILABLE is deliberately NOT walked: it needs retrieval down as well,
which means breaking the vector store on purpose. Reported as unreached.

Usage:
    python scripts/run_ladder_walk.py --warm --json out/ladder-warm.json
    python scripts/run_ladder_walk.py --walk --json out/ladder-walk.json
"""

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx

from src.harness.client import DEFAULT_BASE_URL, HarnessClient
from src.harness.rungs import force_cached, force_primary, observe_rung

logger = logging.getLogger(__name__)

# Fixed so both windows address the same cache key: the key is
# (query, filters, config_version, route) per A12, and a different query in the
# second window would be a miss and prove nothing.
WARM_QUERY = "What is Raskolnikov struggling with in Crime and Punishment?"
# Deliberately NOT warmed, so the second window has an uncached query to fall
# through to RETRIEVAL_ONLY on.
COLD_QUERY = "What does Zosima teach about active love in The Brothers Karamazov?"


def _row(label: str, r) -> dict:
    out = {"label": label, "status_code": r.status_code, "rung": r.rung,
           "cache_hit": r.cache_hit, "route": r.route, "cost_usd": r.cost_usd,
           "ttft_ms": r.ttft_ms, "total_ms": r.total_ms,
           "answer_chars": r.answer_chars, "answer_tail": r.answer_tail}
    logger.info("%-22s status=%s rung=%-14s cache_hit=%-5s cost=%s chars=%s",
                label, r.status_code, r.rung, r.cache_hit, r.cost_usd, r.answer_chars)
    return out


async def main_async(args) -> None:
    timeout = httpx.Timeout(args.timeout, connect=5.0)
    async with HarnessClient(base_url=args.base_url, timeout=timeout) as client:
        provenance = (await client.ready()).json()
    logger.info("provenance: %s", json.dumps(provenance, sort_keys=True)[:400])

    rows = []
    async with HarnessClient(base_url=args.base_url, timeout=timeout) as client:
        if args.warm:
            # One paid draw, then the same query again: the second is a cache
            # hit but still the PRIMARY rung, which is the point being recorded.
            first, second = await force_cached(client, WARM_QUERY)
            rows.append(_row("warm/first (miss)", first))
            rows.append(_row("warm/second (hit)", second))
            rows.append(_row("primary", await force_primary(client, COLD_QUERY)))
        else:
            rows.append(_row("walk/warmed query", await observe_rung(client, WARM_QUERY)))
            rows.append(_row("walk/cold query", await observe_rung(client, COLD_QUERY)))

    out = {"mode": "warm" if args.warm else "walk", "provenance": provenance,
           "warm_query": WARM_QUERY, "cold_query": COLD_QUERY, "rows": rows,
           "unreached": ["503 (UNAVAILABLE): needs retrieval down as well; not "
                         "walked -- it would mean breaking the vector store on purpose"]}
    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        logger.info("wrote %s", args.json)


def main() -> None:
    ap = argparse.ArgumentParser(description="P4 step 8: walk the degradation ladder.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--warm", action="store_true", help="cache ON, credential PRESENT (1 draw)")
    g.add_argument("--walk", action="store_true", help="cache ON, credential UNSET (0 draws)")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
