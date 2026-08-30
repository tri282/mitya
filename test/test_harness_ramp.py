# test/test_harness_ramp.py
"""Verify: src/harness/ramp.py (mitya-v2 P4 step 5, schedule Batch C).

Plain-assert script, not pytest. Offline: `httpx.MockTransport` stands in for
the wire -- no network, no server.

**The load-bearing test in this file, named because it is the batch's most
important one (schedule-p4.md Batch C Verify's own wording): the ramp
driver's `concurrency` parameter is what actually reaches the server, not a
number the client's own connection pool quietly caps first.** A fake
transport counts its own peak concurrent invocations (it holds each request
open on an `asyncio.Event` until enough are in flight, then releases them
together) -- if `run_at_concurrency(concurrency=N, ...)` under-delivers, the
transport's own peak will read below N, and this test fails on THAT number,
not on a timing heuristic.
"""

import asyncio
import sys

sys.path.insert(0, ".")

import httpx  # noqa: E402

from src.harness.ramp import run_at_concurrency, summarize_curve, _find_knee  # noqa: E402

_QUERIES = [{"id": f"q{i}", "query": f"why did raskolnikov {i}"} for i in range(4)]


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def _meta(route: str = "DEFAULT") -> str:
    return _sse(
        "meta",
        '{"request_id": "r", "route_name": "%s", "cache_hit": false, "cost_usd": 0.0, '
        '"cost_metered": false, "model": "fake", "no_context": false, '
        '"filter_relaxed": false, "chunk_ids": []}' % route,
    )


def test_concurrency_parameter_reaches_the_transport() -> None:
    concurrency = 5
    n_requests = 10
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        # Yield control so other concurrently-scheduled tasks actually get a
        # chance to start their own request before this one finishes -- a
        # handler that returns instantly would never observe overlap even at
        # concurrency > 1, no matter how the driver is wired.
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        body = _sse("token", '{"text": "ok"}') + _meta() + _sse("done", "{}")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    result = asyncio.run(
        run_at_concurrency(
            _QUERIES, concurrency, n_requests, transport=httpx.MockTransport(handler),
        )
    )
    assert result.n_requests == n_requests
    assert len(result.records) == n_requests
    assert peak == concurrency, (
        f"the transport saw a peak concurrency of {peak}, not the requested {concurrency} -- "
        f"the ramp driver's own client pool (or its semaphore) is the bottleneck, exactly "
        f"the failure this test exists to catch"
    )


def test_records_cycle_through_queries_and_carry_the_concurrency_tag() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = _sse("token", '{"text": "ok"}') + _meta() + _sse("done", "{}")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    result = asyncio.run(
        run_at_concurrency([_QUERIES[0]], concurrency=2, n_requests=5, transport=httpx.MockTransport(handler))
    )
    assert len(result.records) == 5
    assert all(r["row_id"] == "q0" for r in result.records)
    assert all(r["concurrency"] == 2 for r in result.records)
    assert result.rps > 0


def test_summarize_curve_reports_rps_per_concurrency_and_a_knee() -> None:
    """Arithmetic-only: no run, just the curve-summarizing function fed
    synthetic levels standing in for a real sweep's output."""
    from src.harness.ramp import LevelResult

    def _level(c: int, rps: float, n: int = 4) -> LevelResult:
        records = [
            {"route": "DEFAULT", "total_ms": 100.0 * c, "ttft_ms": 50.0 * c}
            for _ in range(n)
        ]
        return LevelResult(concurrency=c, n_requests=n, wall_s=n / rps, rps=rps, records=records)

    # A flat curve from the first added client: Semaphore(1)'s expected shape
    # (D2) -- no throughput gain past concurrency 1.
    levels = {1: _level(1, 10.0), 2: _level(2, 10.0), 4: _level(4, 10.0)}
    summary = summarize_curve(levels)
    assert summary["knee_concurrency"] == 1, summary
    assert summary["curve"][1]["rps"] == 10.0
    assert summary["curve"][2]["total_ms"]["DEFAULT"]["p50"] == 200.0
    assert set(summary["curve"].keys()) == {1, 2, 4}


def test_find_knee_on_a_genuinely_scaling_curve() -> None:
    # A curve that keeps gaining should knee at the LAST level that still
    # gained, not the first.
    assert _find_knee({1: 5.0, 2: 9.5, 4: 18.0, 8: 18.5}) == 4


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    test_concurrency_parameter_reaches_the_transport()
    print("ok: concurrency parameter reaches the transport")
    test_records_cycle_through_queries_and_carry_the_concurrency_tag()
    print("ok: records cycle through queries and carry the concurrency tag")
    test_summarize_curve_reports_rps_per_concurrency_and_a_knee()
    print("ok: summarize_curve reports rps per concurrency and a knee")
    test_find_knee_on_a_genuinely_scaling_curve()
    print("ok: knee detection on a genuinely scaling curve")
    print("ALL RAMP CHECKS PASSED")
