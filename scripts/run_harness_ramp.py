# scripts/run_harness_ramp.py
"""Driver for phase 4 step 10(a): the ramp, the saturation observation, and the
concurrency corruption check (mitya-v2 P4 Batch C-run, R96).

One entry point for three of 10(a)'s five run shapes, because they share one
body of traffic: `saturation.observe_sweep()` and the `chunk_ids` corruption
check both read the ramp's OWN records rather than issuing requests of their
own (`05-impl/phase-4.md`, shapes 2 and 5). Driving them separately would
triple the traffic and make the three views disagree about which run they
describe.

**Provenance is recorded before any traffic.** `/ready` is read first and its
body is stored in the output, so a curve measured against `MITYA_FAKE_PROVIDER`
can never be mistaken later for one measured against a vendor -- R96's whole
argument is that the fake ramp answers CP-4d and a real one does not, and that
distinction is worthless if the artifact does not say which was run.

Usage:
    # provenance + 2 requests, prints what it got. ALWAYS run this first.
    python scripts/run_harness_ramp.py --dry

    # the zero-spend curve that answers CP-4d (server started with
    # MITYA_FAKE_PROVIDER=1)
    python scripts/run_harness_ramp.py --levels 1,2,4,8,16 --per-level 20 \
        --json out/ramp-fake.json

    # the small metered curve for the charter II headline
    python scripts/run_harness_ramp.py --levels 1,2,4 --per-level 5 \
        --json out/ramp-real.json
"""

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx

from src.harness.client import DEFAULT_BASE_URL, HarnessClient
from src.harness.ramp import summarize_curve, sweep
from src.harness.saturation import observe_sweep

logger = logging.getLogger(__name__)


def _load_queries(path: str) -> list[dict]:
    """Turn either eval-set rows or the frozen session corpus into flat queries."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "sessions" in data:
        # The frozen corpus (R91/R92): one entry per TURN, tagged with its
        # session and family so a curve can be sliced the way every other P4
        # number is -- per family, never pooled.
        return [
            {"query": turn["query"], "session_id": s["id"], "family": s["family"]}
            for s in data["sessions"] for turn in s["turns"]
        ]
    return [{"query": row["query"], "row_id": row.get("id"), "family": row.get("family")}
            for row in data]


async def _provenance(base_url: str) -> dict:
    """Read `/ready` before any traffic. Which arm served the curve is not
    recoverable afterwards, so it is captured rather than assumed."""
    async with HarnessClient(base_url=base_url) as client:
        resp = await client.ready()
    body = resp.json()
    logger.info("provenance: %s", json.dumps(body, sort_keys=True)[:400])
    if isinstance(body, dict) and body.get("fake_provider"):
        logger.warning("this run is served by the FAKE PROVIDER -- latency and cost "
                       "rows describe the SERVER, not a vendor (R96)")
    return body


def _corruption_check(levels: dict) -> dict:
    """CP-4d shape 5: identical queries run concurrently must return identical
    `chunk_ids`. Reads the ramp's own records; issues nothing."""
    by_query: dict[str, set] = {}
    for level in levels.values():
        for rec in level.records:
            ids = rec.get("chunk_ids")
            if rec.get("status_code") == 200 and ids is not None:
                by_query.setdefault(rec["query"], set()).add(tuple(ids))
    divergent = {q: [list(v) for v in variants]
                 for q, variants in by_query.items() if len(variants) > 1}
    logger.info("corruption check: %d distinct queries, %d returned differing chunk_ids",
                len(by_query), len(divergent))
    return {"queries_checked": len(by_query), "divergent": divergent}


async def main_async(args) -> None:
    queries = _load_queries(args.queries)
    logger.info("loaded %d queries from %s", len(queries), args.queries)
    provenance = await _provenance(args.base_url)

    levels = [int(x) for x in args.levels.split(",")]
    if args.dry:
        levels, per_level = [1], 2
        logger.info("DRY RUN: concurrency 1, 2 requests -- read the output before sizing anything")
    else:
        per_level = args.per_level
    total = len(levels) * per_level
    logger.info("about to issue %d requests (%s x %d). Budget: R96.", total, levels, per_level)

    results = await sweep(queries, levels, per_level, base_url=args.base_url,
                          timeout=httpx.Timeout(args.timeout, connect=5.0))
    curve = summarize_curve(results)
    # Concurrency 1 is the baseline service time saturation infers wait from;
    # without it there is nothing to subtract and the wait column is nonsense.
    # Taken as an UNSLICED median on purpose, unlike the curve's own
    # percentiles: `observe_sweep` takes one scalar, and a per-route baseline
    # has nothing to be per-route against. It is therefore a mix-weighted
    # figure -- fine as a subtrahend, not publishable as a latency number.
    baseline_records = results[min(results)].records
    service_ms = [r["total_ms"] for r in baseline_records
                  if r.get("status_code") == 200 and r.get("total_ms") is not None]
    baseline = statistics.median(service_ms) if service_ms else 0.0
    if min(results) != 1:
        logger.warning("no concurrency-1 level in this sweep -- inferred wait is measured "
                       "against concurrency %d, so it understates the true wait", min(results))
    logger.info("baseline service time (unsliced median at concurrency %d, n=%d): %.0f ms",
                min(results), len(service_ms), baseline)
    saturation = observe_sweep({c: r.records for c, r in results.items()}, baseline)

    # Per-level census BEFORE any aggregate, because the aggregates hide it: a
    # client-side read timeout lands as route=None at exactly DEFAULT_TIMEOUT,
    # which a percentile happily averages into the curve as if it were a
    # measurement. Counted and reported so a truncated level is visible.
    census = {}
    for c, level in sorted(results.items()):
        statuses: dict = {}
        timeouts = 0
        for rec in level.records:
            key = str(rec.get("status_code"))
            statuses[key] = statuses.get(key, 0) + 1
            if rec.get("route") is None and rec.get("status_code") is None:
                timeouts += 1
        census[c] = {"n": len(level.records), "status_codes": statuses,
                     "no_response": timeouts}
        if timeouts:
            logger.warning("concurrency %d: %d/%d requests got NO response (client timeout "
                           "at %ss) -- this level's latency is truncated, not measured",
                           c, timeouts, len(level.records), args.timeout)

    out = {
        "provenance": provenance,
        "levels": levels,
        "requests_per_level": per_level,
        "client_timeout_s": args.timeout,
        "census": census,
        "curve": curve,
        "saturation": saturation,
        "corruption": _corruption_check(results),
        # The raw records, kept because every aggregate above is lossy and the
        # metered runs cannot be repeated for free to re-derive one.
        "records": {str(c): level.records for c, level in sorted(results.items())},
    }

    if args.dry:
        for level in results.values():
            for rec in level.records:
                logger.info("  status=%s rung=%s route=%s cache_hit=%s ttft=%sms total=%sms",
                            rec.get("status_code"), rec.get("rung"), rec.get("route"),
                            rec.get("cache_hit"), rec.get("ttft_ms"), rec.get("total_ms"))
                logger.info("  model=%s metered=%s cost=%s chars=%s terminal=%s",
                            rec.get("model"), rec.get("cost_metered"), rec.get("cost_usd"),
                            rec.get("answer_chars"), rec.get("terminal_event"))
                logger.info("  answer tail: %r", rec.get("answer_tail"))
        logger.info("DRY RUN complete -- nothing written. Read the rows above before the real run.")
        return

    os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    logger.info("knee_concurrency=%s  wrote %s", curve.get("knee_concurrency"), args.json)


def main() -> None:
    ap = argparse.ArgumentParser(description="P4 step 10(a): ramp + saturation + corruption check.")
    ap.add_argument("--queries", default="test/session_corpus.json")
    ap.add_argument("--levels", default="1,2,4")
    ap.add_argument("--per-level", type=int, default=5)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--json", default=None)
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="client read timeout in seconds. The default 30s in "
                         "HarnessClient truncates any level slower than that; a ramp "
                         "behind a global lock is slower than that by design.")
    ap.add_argument("--dry", action="store_true",
                    help="provenance + 2 requests, printed. Run this first, always.")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.dry and not args.json:
        ap.error("give --json for a real run, or --dry")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
