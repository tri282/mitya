"""Saturation observer (mitya-v2 P4 step 6, rewritten from verification to
OBSERVATION by D2, `plans/mitya-v2/02-decide/decisions-p4.md`).

Consumes the same per-request records `src/harness/ramp.py`'s sweep produces
-- this module adds no traffic of its own, it reads what already happened at
each concurrency level. Reports, per level: semaphore wait time (inferred,
client-side, since nothing server-side publishes it -- R90 gap 3), the LOCAL
429 rate and the provider-surfaced-as-502 rate as TWO separate numbers (R87:
`QuotaExhausted` -> 429 at `app.py:551` and `TransportError` -> 502 at
`app.py:570-572` are disjoint code paths; a single merged "429 rate" would be
wrong), and whether p99 degrades without any request erroring.

**The expected reading under today's server is a clean result, not an
error.** D2: "an unbounded pile at effective concurrency 1 with a local-429
rate of exactly zero ... is a datapoint, not a failure." Nothing in this
module raises or asserts on that condition -- it is arithmetic over records,
and the interpretation belongs to whoever reads the evidence log, not to a
pass/fail check buried in the observer.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# R90 gap 3, verbatim reason, always attached rather than a bare "null" or a
# silently-omitted key: `src/metrics.py` defines four histograms and five
# counters and ZERO gauges, so a waiting request is invisible until it
# finishes. Queue depth is exactly the quantity this step needs and exactly
# the one nothing publishes -- this label is the honest substitute for a
# number that does not exist, not a number itself.
QUEUE_DEPTH_UNOBSERVABLE = (
    "unobservable: src/metrics.py defines nine instruments (four histograms, "
    "five counters) and zero gauges (R90 gap 3) -- a waiting request appears "
    "only as latency after it finishes, so queue depth cannot be read from "
    "outside the process. Reported as this label, never guessed and never "
    "silently omitted."
)


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * p))
    return s[idx]


def infer_semaphore_wait_ms(records: list[dict], baseline_service_ms: float) -> list[float]:
    """Client-side ESTIMATE of time spent waiting on `state.sem`, since there
    is no gauge to read it from (R90 gap 3).

    `baseline_service_ms` is the request's own service time with nothing
    queued ahead of it -- in practice the median `total_ms` for the same
    route measured at the sweep's concurrency-1 level. Under a strict FIFO
    `Semaphore(1)`, a request's `total_ms` at higher concurrency is
    (wait time ahead of it) + (its own service time); subtracting the
    baseline is the best available inference, not a measurement -- there is
    no way, from outside, to attribute the wait to a specific position in the
    queue. Negative results are clamped to zero rather than reported (a
    request that happened to be faster than the baseline waited nothing, it
    did not wait a negative amount).
    """
    return [max(0.0, r["total_ms"] - baseline_service_ms) for r in records]


def observe(records: list[dict], baseline_service_ms: float) -> dict:
    """One concurrency level's saturation reading.

    `records` is one level's raw per-request list, in `ramp.LevelResult`'s
    shape (`ChatResult` fields plus `row_id`). Nothing here mutates or
    re-issues traffic -- pure arithmetic over what the ramp already recorded.
    """
    n = len(records)
    if n == 0:
        raise ValueError("observe() needs at least one record")

    waits = infer_semaphore_wait_ms(records, baseline_service_ms)
    local_429 = sum(1 for r in records if r["status_code"] == 429)
    provider_502 = sum(1 for r in records if r["status_code"] == 502)
    # "Errored" for the silent-degradation check below: any status outside
    # 200, or a transport failure with no status at all. Deliberately not the
    # fault_phase field alone -- a 200 with a post-first-token SSE error still
    # counts as an error here, even though its status_code reads 200.
    errored = sum(
        1 for r in records
        if r["status_code"] != 200 or r["fault_phase"] is not None
    )
    totals = [r["total_ms"] for r in records]
    p99_total_ms = _percentile(totals, 0.99)

    return {
        "n": n,
        "concurrency": records[0].get("concurrency"),
        "wait_ms": {
            "p50": _percentile(waits, 0.50),
            "p95": _percentile(waits, 0.95),
            "p99": _percentile(waits, 0.99),
        },
        # Two numbers, never one (R87).
        "local_429_rate": local_429 / n,
        "provider_502_rate": provider_502 / n,
        "total_ms_p50": _percentile(totals, 0.50),
        "total_ms_p99": p99_total_ms,
        # D2's own wording: "A pile that degrades p99 silently is reported as
        # such." The 3x multiplier is a stated, arbitrary bound for what
        # counts as "degrades" -- not a pass/fail threshold; this step
        # observes, it does not verify (D2).
        "p99_degrades_without_error": (
            errored == 0 and p99_total_ms is not None and p99_total_ms > baseline_service_ms * 3
        ),
        "queue_depth": QUEUE_DEPTH_UNOBSERVABLE,
    }


def observe_sweep(levels: dict[int, list[dict]], baseline_service_ms: float) -> dict[int, dict]:
    """`observe()` at every concurrency in a sweep's records, keyed by
    concurrency -- the shape `ramp.sweep()`'s `LevelResult.records` come in
    once tagged with their own level (callers pass `{c: level.records}`)."""
    return {c: observe(records, baseline_service_ms) for c, records in levels.items()}
