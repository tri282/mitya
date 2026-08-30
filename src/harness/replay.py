"""Replay mode (mitya-v2 P4 step 2).

Drives an existing eval set's queries through the live API via
`HarnessClient` and records what came back. This is deliberately the mode
that overlaps the eval gate (P3) in *input* only -- the gate judges quality;
this module measures behaviour. It does not import or reimplement anything
from `src.rag.metrics` (recall, MRR, judge scores). If a change here starts
computing a metric, it has drifted into the gate's job.

Rows come from `test/retrieval_eval.json` or `test/quality_eval.json`
unmodified -- both are read-only inputs; neither is written to.
"""

import json
import logging
from dataclasses import asdict

from src.harness.client import DEFAULT_BASE_URL, HarnessClient

logger = logging.getLogger(__name__)


def load_rows(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def replay(rows: list[dict], base_url: str = DEFAULT_BASE_URL) -> list[dict]:
    """Drive every row's `query` (and `filters`, if the row carries one)
    through `/chat`. One row -> one behavioral record; no aggregation, no
    scoring here."""
    records: list[dict] = []
    async with HarnessClient(base_url=base_url) as client:
        for row in rows:
            result = await client.chat(row["query"], filters=row.get("filters"))
            record = {"row_id": row.get("id"), **asdict(result)}
            records.append(record)
            logger.info(
                "replay: id=%s status=%s route=%s ttft_ms=%s total_ms=%.0f "
                "fault_phase=%s cache_hit=%s",
                row.get("id"), result.status_code, result.route, result.ttft_ms,
                result.total_ms, result.fault_phase, result.cache_hit,
            )
    return records


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * p))
    return s[idx]


def summarize(records: list[dict]) -> dict:
    """Behavioral summary: status/fault-phase counts and latency percentiles.
    No quality judgment, no recall, no pass/fail -- that is the gate's job
    (P3), not this one's."""
    n = len(records)
    status_counts: dict = {}
    fault_phase_counts: dict = {}
    for r in records:
        status_counts[r["status_code"]] = status_counts.get(r["status_code"], 0) + 1
        fault_phase_counts[r["fault_phase"]] = fault_phase_counts.get(r["fault_phase"], 0) + 1

    ttfts = [r["ttft_ms"] for r in records if r["ttft_ms"] is not None]
    totals = [r["total_ms"] for r in records]
    cache_hits = [r for r in records if r["cache_hit"] is not None]

    return {
        "n": n,
        "status_counts": status_counts,
        "fault_phase_counts": fault_phase_counts,
        # p50/p95/p99, never a mean (charter §II).
        "ttft_ms": {p: _percentile(ttfts, f) for p, f in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))},
        "total_ms": {p: _percentile(totals, f) for p, f in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))},
        "cache_hit_rate": (
            sum(1 for r in cache_hits if r["cache_hit"]) / len(cache_hits) if cache_hits else None
        ),
    }


def run(pairs_path: str, base_url: str, out_path: str | None) -> dict:
    """Orchestration used by the CLI driver."""
    import asyncio

    rows = load_rows(pairs_path)
    logger.info("replay: loaded %d rows from %s against %s", len(rows), pairs_path, base_url)
    records = asyncio.run(replay(rows, base_url))
    summary = summarize(records)
    logger.info("replay: summary=%s", json.dumps(summary))
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"records": records, "summary": summary}, f, indent=2)
        logger.info("replay: wrote %s", out_path)
    return {"records": records, "summary": summary}
