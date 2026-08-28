# test/test_gateway_retry.py
"""P2 step 8(a) verify (schedule Batch B): transport retry only --
src/gateway/retry.py.

Plain-assert script, not pytest. Offline: a hand-rolled `FlakyProvider`
stands in for "a real adapter that fails transport-wise N times then
succeeds" -- `FakeProvider` itself never raises TransportError, since it has
no transport to fail (contract.py's whole point).
"""

import asyncio
import sys

sys.path.insert(0, ".")

from src import metrics  # noqa: E402
from src.gateway.contract import GenerationStream, Usage  # noqa: E402
from src.gateway.fake import DEFAULT_FIXTURE, FakeProvider  # noqa: E402
from src.gateway.retry import TransportError, record_if_empty_completion, retry_transport  # noqa: E402

metrics.configure_metrics()

_MESSAGES = [{"role": "user", "content": "who are you?"}]


class FlakyProvider:
    """Fails with TransportError `fail_times` times, then delegates to a
    real FakeProvider call. `.attempts` records every call, succeeded or not
    -- what the tests assert the retry count against."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.attempts = 0
        self._delegate = FakeProvider()

    async def generate(self, messages, tier, tenant, budget) -> GenerationStream:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise TransportError(f"simulated transport failure #{self.attempts}")
        return await self._delegate.generate(messages, tier, tenant, budget)


def test_succeeds_after_bounded_retries() -> None:
    provider = FlakyProvider(fail_times=2)
    result = asyncio.run(
        retry_transport(
            provider, _MESSAGES, "default", "default", None,
            max_attempts=3, base_delay=0.001, max_delay=0.002,
        )
    )
    assert provider.attempts == 3
    assert result is not None


def test_gives_up_after_max_attempts() -> None:
    provider = FlakyProvider(fail_times=10)
    try:
        asyncio.run(
            retry_transport(
                provider, _MESSAGES, "default", "default", None,
                max_attempts=3, base_delay=0.001, max_delay=0.002,
            )
        )
    except TransportError:
        pass
    else:
        raise AssertionError("retry did not give up after max_attempts")
    assert provider.attempts == 3, "must not retry past the bound"


def test_a_retry_that_would_exceed_the_remaining_budget_is_refused() -> None:
    """Retries are a cost multiplier (phase-2-gateway.md step 4/8) -- an
    unbounded retry loop against a metered provider is itself a spend
    hazard, so a retry whose cumulative attempt cost would exceed what is
    left must not be taken."""
    provider = FlakyProvider(fail_times=10)
    try:
        asyncio.run(
            retry_transport(
                provider, _MESSAGES, "default", "default", None,
                max_attempts=5, base_delay=0.001, max_delay=0.002,
                cost_per_attempt_usd=1.0, remaining_budget_usd=1.5,
            )
        )
    except TransportError:
        pass
    else:
        raise AssertionError("a budget-exceeding retry was allowed")
    # attempt 1 costs 1.0 (<=1.5, taken and fails); attempt 2 would cost 2.0
    # (>1.5) and must be refused before it is made.
    assert provider.attempts == 1


def test_hosted_zero_token_completion_is_counted() -> None:
    """v2 P2 step 8(a)'s ~3-line counter: a hosted completion with zero
    tokens increments the SAME instrument the local llama-server path uses
    (mitya_empty_completions_total)."""
    before = _empty_completions_total("STRUCTURAL")
    record_if_empty_completion("STRUCTURAL", Usage(completion_tokens=0, model="fake/replay-v1"))
    after = _empty_completions_total("STRUCTURAL")
    assert after == before + 1


def test_a_nonzero_completion_is_not_counted_as_empty() -> None:
    before = _empty_completions_total("DEFAULT")
    record_if_empty_completion("DEFAULT", Usage(completion_tokens=13, model="fake/replay-v1"))
    after = _empty_completions_total("DEFAULT")
    assert after == before


def test_an_undrained_stream_is_not_counted_as_empty() -> None:
    """completion_tokens is None before a stream is drained -- None must
    never be read as zero (Usage's own contract, contract.py)."""
    before = _empty_completions_total("DEFAULT")
    record_if_empty_completion("DEFAULT", Usage())  # completion_tokens defaults to None
    after = _empty_completions_total("DEFAULT")
    assert after == before


def _empty_completions_total(route: str) -> float:
    from prometheus_client import REGISTRY, generate_latest

    lines = generate_latest(REGISTRY).decode().splitlines()
    for line in lines:
        if line.startswith(f'mitya_empty_completions_total{{route="{route}"}}'):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
