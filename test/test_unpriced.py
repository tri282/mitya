# test/test_unpriced.py
"""v3 P0 B2 verify (schedule-p0 V rulings 2026-10-20, row 4 (a)): a call with no list price is
estimated UNPRICED, never $0. It counts toward the tenant-day and global-day token layers and the
per-call `unpriced_call_tokens` cap; the USD layers do not apply to it; `meta` shows UNPRICED.

Plain-assert script. Offline: stub providers and an in-memory transport, temp SQLite. Run from the
repo root after `python scripts/scrub_fixtures.py`.
"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, ".")
sys.path.insert(0, "test")

import provider_replay as pr  # noqa: E402

import src.rag.generate as gen  # noqa: E402
from src.api.app import _ledger_meta  # noqa: E402
from src.config import Config  # noqa: E402
from src.db import init_schema  # noqa: E402
from src.gateway.contract import GenerationStream, Usage  # noqa: E402
from src.gateway.fake import FakeProvider  # noqa: E402
from src.gateway.guard import SpendRateBreaker, guarded_generate  # noqa: E402
from src.gateway.ledger import BudgetExceeded, RequestLedger, SpendLayer, SpendStore  # noqa: E402
from src.limits import RateLimiter  # noqa: E402
from src.limits_config import SpendLimits  # noqa: E402
from src.pricing import UNPRICED, Cost  # noqa: E402

CAP = 100
NOON = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
DAY = "day:2026-10-20"
WIDE = SpendLimits(
    per_call_usd=1000.0, per_request_usd=1000.0, tenant_day_usd=1e6, tenant_day_tokens=10**9,
    global_minute_usd=1e6, global_day_usd=1e6, global_day_tokens=10**9, global_month_usd=1e6,
    unpriced_call_tokens=CAP,
)
MESSAGES = [{"role": "user", "content": "who are you?"}]  # 3 estimated input tokens
# Each send below reserves 3 + 10 = 13 tokens and, for `Unpriced`, settles at 10 + 5 = 15.
OUT = 10


class Unpriced:
    """A provider with no list price: usage arrives, cost stays UNPRICED."""

    model = "stub/unpriced"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def generate(self, messages, tier, tenant, budget):
        self.calls.append({"tenant": tenant, "budget": budget})
        usage = Usage(model=self.model)

        async def _chunks():
            yield "x"
            usage.prompt_tokens, usage.completion_tokens, usage.finish_reason = 10, 5, "stop"

        return GenerationStream(chunks=_chunks(), usage=usage)


def _ledger(**limits) -> tuple[RequestLedger, SpendStore]:
    path = os.path.join(tempfile.mkdtemp(prefix="mitya-unpriced-db-"), "t.sqlite3")
    init_schema(path)
    spend = WIDE.model_copy(update=limits)
    store = SpendStore(spend, path, lambda: NOON)
    return RequestLedger(spend, store), store


async def _send(provider, ledger, *, estimate=None, max_output_tokens=OUT, tenant="tenant-a"):
    stream = await guarded_generate(
        provider, MESSAGES, "default", tenant, call_class="generation",
        limiter=RateLimiter(rpm=10**6, tpm=10**9, rpd=10**9, sleep=lambda s: None),
        breaker=SpendRateBreaker(1e6, 0.8), ledger=ledger, estimated_cost_usd=estimate,
        max_input_tokens=10**6, max_history_messages=10, max_output_tokens=max_output_tokens,
    )
    return [c async for c in stream.chunks]


def _ok(provider, ledger, **kw) -> list[str]:
    return asyncio.run(_send(provider, ledger, **kw))


def _refused(provider, ledger, **kw) -> BudgetExceeded:
    try:
        asyncio.run(_send(provider, ledger, **kw))
    except BudgetExceeded as exc:
        return exc
    raise AssertionError("expected a refusal")


def test_per_call_token_cap_can_fire() -> None:
    ledger, _ = _ledger()
    provider = Unpriced()
    exc = _refused(provider, ledger, max_output_tokens=CAP)  # 3 + 100 > 100
    assert exc.layer is SpendLayer.PER_CALL and exc.retry_after_s is None
    assert provider.calls == [], "a refused call must not send"
    # Exactly at the cap passes: 3 + 97 = 100 is not over.
    assert _ok(provider, ledger, max_output_tokens=CAP - 3) == ["x"]
    assert len(provider.calls) == 1


def test_the_cap_is_moved_by_its_input() -> None:
    """Raising the cap admits the call the lower cap refused."""
    provider = Unpriced()
    low, _ = _ledger(unpriced_call_tokens=50)
    _refused(provider, low, max_output_tokens=60)
    high, _ = _ledger(unpriced_call_tokens=500)
    assert _ok(provider, high, max_output_tokens=60) == ["x"]


def test_usd_layers_do_not_apply_to_an_unpriced_call() -> None:
    tiny = 1e-9
    ledger, store = _ledger(
        per_call_usd=tiny, per_request_usd=tiny, tenant_day_usd=tiny, global_day_usd=tiny,
        global_month_usd=tiny, global_minute_usd=tiny,
    )
    provider = Unpriced()
    for _ in range(3):
        assert _ok(provider, ledger) == ["x"]
    assert len(provider.calls) == 3
    # Control: the same limits refuse a priced call, so the USD layers are in force.
    assert _refused(Unpriced(), ledger, estimate=0.001).layer is SpendLayer.PER_CALL
    # The unpriced calls put tokens in the persisted sums and no USD.
    assert store.totals("global", DAY) == (0.0, 45)


def test_token_layers_bind_an_unpriced_call() -> None:
    provider = Unpriced()
    ledger, store = _ledger(tenant_day_tokens=20)
    assert _ok(provider, ledger) == ["x"]
    assert store.totals("tenant:tenant-a", DAY) == (0.0, 15)
    exc = _refused(provider, ledger)  # 15 settled + 13 reserved > 20
    assert exc.layer is SpendLayer.TENANT_DAY and exc.retry_after_s is not None
    assert len(provider.calls) == 1

    provider = Unpriced()
    ledger, _ = _ledger(global_day_tokens=20)
    assert _ok(provider, ledger) == ["x"]
    assert _refused(provider, ledger).layer is SpendLayer.GLOBAL_DAY
    assert len(provider.calls) == 1


def test_meta_shows_unpriced_not_zero() -> None:
    ledger, _ = _ledger()
    _ok(Unpriced(), ledger)
    assert ledger.entries[0].estimate_usd is None
    totals = ledger.totals()
    assert totals.list_cost == UNPRICED and totals.billed == UNPRICED and totals.unpriced_calls == 1
    meta = _ledger_meta(ledger)
    assert meta["list_usd"] is None and meta["billed_usd"] is None and meta["unpriced_calls"] == 1


def test_a_priced_actual_on_an_unpriced_estimate_is_still_counted() -> None:
    """No estimate does not mean no bill: the actual USD enters the persisted sums."""
    ledger, store = _ledger()
    _ok(FakeProvider(), ledger)
    usd, tokens = store.totals("global", DAY)
    assert abs(usd - 1.16e-4) < 1e-12 and tokens == 77
    assert ledger.totals().unpriced_calls == 0


def test_the_spend_rate_breaker_does_not_trip_on_none() -> None:
    breaker = SpendRateBreaker(1e-9, 0.8)
    assert breaker.would_trip(None) is False
    assert breaker.would_trip(1.0) is True


def test_generation_estimate_is_none_for_a_model_with_no_rate_row() -> None:
    assert gen._estimate_usd_or_none(MESSAGES, "stub/no-rate-row") is None
    assert gen._estimated_cost_usd(MESSAGES, "stub/no-rate-row") == 0.0  # P7's objective keeps its number
    assert Config.SERVING_MODEL in Config.MODEL_RATES
    assert gen._estimate_usd_or_none(MESSAGES, Config.SERVING_MODEL) > 0.0


def test_an_openrouter_replay_is_billed_but_list_unpriced() -> None:
    """The adapter has no rate row bound (the rate table is keyed by model id, not surface), so the
    call is UNPRICED at list and carries the provider-reported charge as billed."""
    fx = pr.fixture("openrouter", "stream_ok")
    ledger, store = _ledger()
    prov = pr.provider("openrouter", fx["request"]["model"], pr.transport(fx))
    with pr.key_set("openrouter"):
        assert _ok(prov, ledger) == ["ok"]
    totals = ledger.totals()
    assert totals.list_cost == UNPRICED and totals.unpriced_calls == 1
    assert totals.billed == Cost(usd=0.00003527, metered=True)
    assert store.totals("global", DAY) == (0.0, 115)


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
