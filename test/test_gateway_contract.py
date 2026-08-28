# test/test_gateway_contract.py
"""P2 step 2 verify (schedule Batch A): the fake satisfies the gateway contract.

Plain-assert script, not pytest (pytest is not installed in .venv -- matches
test_entities.py, test_span_tree.py, etc.). Offline: no provider, no network,
no server.

This is Batch A's whole Verify(1) surface per the schedule: "type-check, and
a test that the fake satisfies the contract." Nothing downstream (budget
guard, routing, retry, real adapters) exists yet -- this file only checks
that `FakeProvider` really implements `Provider` and that its `Usage` carries
every field P1's spine needs to stamp a span with no second lookup.
"""

import asyncio
import sys

sys.path.insert(0, ".")

from src.config import Config  # noqa: E402
from src.gateway.contract import GenerationStream, Provider, Usage  # noqa: E402
from src.gateway.fake import FakeProvider  # noqa: E402
from src.pricing import UNPRICED  # noqa: E402


def test_fake_provider_satisfies_the_protocol() -> None:
    """Structural check: FakeProvider is a Provider by shape, with no
    explicit inheritance -- the point of using a Protocol here is that a real
    adapter at Batch C needs no shared base class either."""
    assert isinstance(FakeProvider(), Provider)


def test_generate_returns_a_generation_stream() -> None:
    provider = FakeProvider()
    result = asyncio.run(
        provider.generate(
            messages=[{"role": "user", "content": "who are you?"}],
            tier="default",
            tenant="default",
            budget=None,
        )
    )
    assert isinstance(result, GenerationStream)
    assert isinstance(result.usage, Usage)


def test_usage_is_unfilled_until_the_stream_is_drained() -> None:
    """Mirrors generate.py's stream_answer() out-param pattern: usage fields
    only become meaningful once every chunk has been consumed -- a caller
    that inspects usage before draining the stream sees the placeholder, not
    a partial answer."""
    provider = FakeProvider()
    stream = asyncio.run(provider.generate([], "default", "default", None))
    assert stream.usage.prompt_tokens is None
    assert stream.usage.completion_tokens is None
    assert stream.usage.cost is UNPRICED
    assert stream.usage.cache_hit is False


def test_chunks_replay_and_usage_fills_after_drain() -> None:
    provider = FakeProvider()
    stream = asyncio.run(provider.generate([], "default", "default", None))

    async def _drain() -> list[str]:
        return [c async for c in stream.chunks]

    chunks = asyncio.run(_drain())
    assert chunks  # an empty fake replay would test nothing
    assert "".join(chunks).strip()

    usage = stream.usage
    assert usage.prompt_tokens is not None and usage.prompt_tokens > 0
    assert usage.completion_tokens is not None and usage.completion_tokens > 0
    assert usage.model == provider.model
    assert usage.cache_hit is False
    assert usage.cost.priced is True
    assert usage.cost.metered is True
    # Exact, not just > 0: rates are USD/Mtok like Config.MODEL_RATES, so a
    # missing 1e6 divide is a factor-of-a-million bug a positivity check
    # would wave through. 64 * 1.0 + 13 * 4.0 = 116, per million.
    assert usage.cost.usd == 116 / 1_000_000


def test_synthetic_cost_never_touches_the_real_rate_table() -> None:
    """The fake's price is declared on its own fixture and never routed
    through Config.MODEL_RATES -- there is no vendor yet (CP-alpha), and
    wiring a fake id into the real rate table would be exactly the kind of
    vendor commitment this batch must not make."""
    assert FakeProvider().model not in Config.MODEL_RATES


def test_tier_tenant_and_budget_reach_the_provider_unenforced() -> None:
    """The contract carries tier/tenant/budget end to end -- enforcing them
    (the budget guard, routing) is Batch B/D's job, not this one's. What this
    batch owes is that the shape exists and the fake records what it was
    called with, so Batch B's tests can assert against `.calls`."""
    provider = FakeProvider()
    asyncio.run(provider.generate([{"role": "user", "content": "x"}], "premium", "acme", 0.0))
    assert provider.calls[-1]["tier"] == "premium"
    assert provider.calls[-1]["tenant"] == "acme"
    assert provider.calls[-1]["budget"] == 0.0


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"test_gateway_contract: {len(tests)} passed")


if __name__ == "__main__":
    main()
