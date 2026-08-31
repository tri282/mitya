"""Load-ramp driver (mitya-v2 P4 step 5, schedule Batch C).

Sweeps concurrency, drives `/chat` at each level, and records the
throughput/latency curve with its knee (charter §II: rps at a STATED
concurrency, never unqualified). This module produces raw per-request
records only -- `src/harness/saturation.py` (step 6) turns the same records
into the backpressure OBSERVATION (D2), and step 10(a)'s corruption check
(identical concurrent queries must return identical `chunk_ids`) reads
`records[*]["chunk_ids"]` directly rather than through a third module.

**The one thing this module exists to get right, named because getting it
wrong looks identical to success**: the `concurrency` parameter must be what
actually reaches the server, not a number the client's own connection pool
quietly caps first. `HarnessClient(limits=...)` is sized to the requested
concurrency for exactly this reason -- see `test/test_harness_ramp.py` for
the offline proof (a fake transport that counts its own peak concurrent
invocations).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import asdict, dataclass, field

import httpx

from src.harness.client import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, HarnessClient

logger = logging.getLogger(__name__)


@dataclass
class LevelResult:
    concurrency: int
    n_requests: int
    wall_s: float
    rps: float
    records: list[dict] = field(default_factory=list)


async def run_at_concurrency(
    queries: list[dict],
    concurrency: int,
    n_requests: int,
    *,
    base_url: str = DEFAULT_BASE_URL,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> LevelResult:
    """Issue `n_requests` `/chat` calls with at most `concurrency` in flight
    at once, and record everything `HarnessClient.chat()` observed for each.

    `queries` is cycled if `n_requests` exceeds its length -- the ramp's job
    is a stated load shape, not a fixed corpus walk (that is replay's, step
    2). One `HarnessClient` (one connection pool, sized to `concurrency`) is
    shared by every request in this level; `asyncio.Semaphore(concurrency)`
    is the only thing gating how many run at once, so the number that reaches
    the transport IS the number requested here.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")
    if not queries:
        raise ValueError("queries must be non-empty")

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    sem = asyncio.Semaphore(concurrency)
    records: list[dict] = []
    query_cycle = itertools.cycle(queries)

    async def _one(client: HarnessClient, row: dict) -> None:
        async with sem:
            result = await client.chat(row["query"], filters=row.get("filters"))
            # `concurrency` travels on every record, not just the LevelResult
            # it came from -- `saturation.py`'s `observe()` reads it off the
            # record so a caller can pool records across levels without
            # losing which level each one belongs to.
            records.append({"row_id": row.get("id"), "concurrency": concurrency, **asdict(result)})

    wall_start = time.monotonic()
    async with HarnessClient(
        base_url=base_url, timeout=timeout, transport=transport, limits=limits,
    ) as client:
        tasks = [asyncio.create_task(_one(client, next(query_cycle))) for _ in range(n_requests)]
        await asyncio.gather(*tasks)
    wall_s = time.monotonic() - wall_start

    rps = n_requests / wall_s if wall_s > 0 else 0.0
    logger.info(
        "ramp: concurrency=%d n_requests=%d wall_s=%.3f rps=%.3f",
        concurrency, n_requests, wall_s, rps,
    )
    return LevelResult(concurrency=concurrency, n_requests=n_requests, wall_s=wall_s, rps=rps, records=records)


async def sweep(
    queries: list[dict],
    concurrencies: list[int],
    requests_per_level: int,
    *,
    base_url: str = DEFAULT_BASE_URL,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> dict[int, LevelResult]:
    """Run `run_at_concurrency` at every level in `concurrencies`, in order.

    Sequential across levels on purpose: overlapping two levels would make
    each level's own concurrency parameter a lie (the server would see the
    SUM). One level finishing fully before the next starts is what keeps
    "concurrency=8" meaning exactly 8, everywhere this curve is read.
    """
    levels: dict[int, LevelResult] = {}
    for c in concurrencies:
        levels[c] = await run_at_concurrency(
            queries, c, requests_per_level, base_url=base_url, transport=transport,
            timeout=timeout,
        )
    return levels


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * p))
    return s[idx]


def _latency_by_route(records: list[dict], field_name: str) -> dict:
    """p50/p95/p99 of `field_name`, sliced by `route` (R30/R42: an unsliced
    percentile measures the traffic mix, not the system)."""
    by_route: dict = {}
    for r in records:
        if r.get(field_name) is None:
            continue
        by_route.setdefault(r.get("route"), []).append(r[field_name])
    return {
        route: {p: _percentile(vals, f) for p, f in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))}
        for route, vals in by_route.items()
    }


def _find_knee(rps_by_concurrency: dict[int, float]) -> int | None:
    """The smallest concurrency past which adding another concurrent client
    stops buying meaningful throughput -- a plateau, per CP-4d's own reading
    rule ("if p95 grows linearly with concurrency from the first added
    client, the semaphore is the wall"). Under today's `Semaphore(1)` this is
    expected to be concurrency 1 itself (D2's unbounded-pile baseline) --
    reported as a normal reading, not a failure of this function.

    A 5% growth threshold is a stated, arbitrary bound for what counts as
    "still gaining" -- not a claim about the true elasticity of the curve.
    """
    levels = sorted(rps_by_concurrency)
    if not levels:
        return None
    prev_c, prev_rps = levels[0], rps_by_concurrency[levels[0]]
    for c in levels[1:]:
        rps = rps_by_concurrency[c]
        if prev_rps <= 0 or rps <= prev_rps * 1.05:
            return prev_c
        prev_c, prev_rps = c, rps
    return levels[-1]


def summarize_curve(levels: dict[int, LevelResult]) -> dict:
    """The throughput/latency curve, with the knee identified (step 5's
    deliverable). Every rps figure carries its own concurrency key; nothing
    here reports an unqualified throughput number (charter §II)."""
    curve = {}
    for c, level in sorted(levels.items()):
        curve[c] = {
            "rps": level.rps,
            "n_requests": level.n_requests,
            "total_ms": _latency_by_route(level.records, "total_ms"),
            "ttft_ms": _latency_by_route(level.records, "ttft_ms"),
        }
    knee = _find_knee({c: v["rps"] for c, v in curve.items()})
    return {"curve": curve, "knee_concurrency": knee}
