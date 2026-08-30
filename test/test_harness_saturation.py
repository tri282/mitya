# test/test_harness_saturation.py
"""Verify: src/harness/saturation.py (mitya-v2 P4 step 6, D2's observation
rewrite).

Plain-assert script, not pytest. Pure arithmetic over hand-built records --
no network, no server, nothing run.

Covers: the client-side wait-time inference, the 429/502 split staying two
numbers (R87), and D2's own named expected reading -- an unbounded pile at
effective concurrency 1 with a local-429 rate of exactly zero -- coming back
as a CLEAN result (no exception, no special-cased failure flag), which is
the whole point of rewriting step 6 from verification to observation.
"""

import sys

sys.path.insert(0, ".")

from src.harness.saturation import (  # noqa: E402
    QUEUE_DEPTH_UNOBSERVABLE,
    infer_semaphore_wait_ms,
    observe,
)


def _record(total_ms, status_code=200, fault_phase=None, concurrency=4):
    return {
        "total_ms": total_ms, "status_code": status_code, "fault_phase": fault_phase,
        "concurrency": concurrency, "ttft_ms": None,
    }


def test_wait_time_is_total_minus_baseline_clamped_at_zero() -> None:
    records = [_record(100.0), _record(250.0), _record(50.0)]
    waits = infer_semaphore_wait_ms(records, baseline_service_ms=80.0)
    assert waits == [20.0, 170.0, 0.0], waits  # third clamps rather than going negative


def test_expected_reading_at_effective_concurrency_one_is_a_clean_result() -> None:
    """D2, verbatim: 'an unbounded pile at effective concurrency 1 with a
    local-429 rate of exactly zero ... is a datapoint, not a failure.' This
    test asserts `observe()` reports exactly that shape without raising."""
    # Every request succeeds (200), but total_ms grows with each request's
    # position in the pile -- the unbounded-pile signature.
    records = [_record(100.0 * (i + 1), concurrency=1) for i in range(5)]
    result = observe(records, baseline_service_ms=100.0)
    assert result["local_429_rate"] == 0.0, result
    assert result["provider_502_rate"] == 0.0, result
    assert result["n"] == 5
    assert result["queue_depth"] == QUEUE_DEPTH_UNOBSERVABLE
    # The pile shows up as growing wait time, not as an error of any kind.
    assert result["wait_ms"]["p99"] > 0


def test_local_429_and_provider_502_are_reported_as_two_separate_numbers() -> None:
    """R87: QuotaExhausted -> 429 and TransportError -> 502 are disjoint
    code paths. A single merged rate would be wrong."""
    records = (
        [_record(100.0, status_code=429, fault_phase="pre_first_token") for _ in range(2)]
        + [_record(100.0, status_code=502, fault_phase="pre_first_token") for _ in range(1)]
        + [_record(100.0) for _ in range(7)]
    )
    result = observe(records, baseline_service_ms=100.0)
    assert result["local_429_rate"] == 0.2, result
    assert result["provider_502_rate"] == 0.1, result


def test_p99_degrades_without_error_flag() -> None:
    # Nine fast requests, one very slow -- all succeed, but p99 is well past
    # 3x baseline: a silent degradation (D2's own wording).
    records = [_record(100.0) for _ in range(9)] + [_record(1000.0)]
    result = observe(records, baseline_service_ms=100.0)
    assert result["p99_degrades_without_error"] is True, result

    # Same shape but the last one is a real error -- no longer "silent".
    records_with_error = [_record(100.0) for _ in range(9)] + [
        _record(1000.0, status_code=502, fault_phase="pre_first_token")
    ]
    result2 = observe(records_with_error, baseline_service_ms=100.0)
    assert result2["p99_degrades_without_error"] is False, result2


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    test_wait_time_is_total_minus_baseline_clamped_at_zero()
    print("ok: wait time inference clamps at zero")
    test_expected_reading_at_effective_concurrency_one_is_a_clean_result()
    print("ok: the D2-expected pile reads as a clean result, not a failure")
    test_local_429_and_provider_502_are_reported_as_two_separate_numbers()
    print("ok: local 429 and provider 502 are two separate numbers")
    test_p99_degrades_without_error_flag()
    print("ok: p99-degrades-without-error flag")
    print("ALL SATURATION OBSERVER CHECKS PASSED")
