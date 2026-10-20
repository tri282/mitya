# test/test_ledger.py
"""v3 P0 B1 verify (OI8, OI45): the request ledger and the six spend layers on known-cost fakes.

Plain-assert script. Offline: FakeProvider, FaultProvider and a stub provider only; SQLite in a
temp dir; clocks injected. Run from the repo root.
"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, ".")

from src.db import init_schema  # noqa: E402
from src.gateway import guard as guard_mod  # noqa: E402
from src.gateway.contract import GenerationStream, Usage  # noqa: E402
from src.gateway.fake import FakeProvider, Fixture  # noqa: E402
from src.gateway.fault import FaultProvider, FaultSpec  # noqa: E402
from src.gateway.guard import Degraded, SpendRateBreaker, guarded_generate  # noqa: E402
from src.gateway.ledger import (  # noqa: E402
    BudgetExceeded,
    LedgerMissing,
    RequestLedger,
    SpendLayer,
    SpendStore,
    current_ledger,
    seconds_to_next_utc_midnight,
    seconds_to_next_utc_month,
)
from src.gateway.retry import TransportError  # noqa: E402
from src.limits import RateLimiter  # noqa: E402
from src.limits_config import SpendLimits  # noqa: E402
from src.pricing import TOKENS_PER_RATE_UNIT, UNPRICED, Cost, total_usd  # noqa: E402

WIDE = SpendLimits(
    per_call_usd=1000.0, per_request_usd=1000.0, tenant_day_usd=1e6, tenant_day_tokens=10**9,
    global_minute_usd=1e6, global_day_usd=1e6, global_day_tokens=10**9, global_month_usd=1e6,
    unpriced_call_tokens=10**9,
)
NOON = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
MESSAGES = [{"role": "user", "content": "who are you?"}]


def _db() -> str:
    path = os.path.join(tempfile.mkdtemp(prefix="mitya-ledger-db-"), "t.sqlite3")
    init_schema(path)
    return path


def _clock(now: datetime):
    box = [now]

    def clock() -> datetime:
        return box[0]

    clock.box = box
    return clock


def _ledger(db: str, now: datetime = NOON, **limits) -> RequestLedger:
    spend = WIDE.model_copy(update=limits)
    return RequestLedger(spend, SpendStore(spend, db, _clock(now)))


class StubProvider:
    """A provider whose call cost, billed amount and finish reason are declared."""

    model = "stub/known-cost"

    def __init__(self, cost: Cost, billed: float | None = None, finish: str = "stop",
                 prompt: int = 10, completion: int = 5) -> None:
        self.calls: list[dict] = []
        self._args = (cost, billed, finish, prompt, completion)

    async def generate(self, messages, tier, tenant, budget):
        self.calls.append({"messages": messages, "tenant": tenant, "budget": budget})
        cost, billed, finish, prompt, completion = self._args
        usage = Usage(model=self.model)

        async def _chunks():
            yield "x"
            usage.prompt_tokens, usage.completion_tokens = prompt, completion
            usage.cost, usage.billed_usd, usage.finish_reason = cost, billed, finish

        return GenerationStream(chunks=_chunks(), usage=usage)


async def _send(provider, ledger, *, tenant="default", estimate=0.001, max_output_tokens=0,
                messages=MESSAGES, breaker=None, drain=True):
    stream = await guarded_generate(
        provider, messages, "default", tenant, call_class="generation",
        limiter=RateLimiter(rpm=10**6, tpm=10**9, rpd=10**9, sleep=lambda s: None),
        breaker=breaker or SpendRateBreaker(1e6, 0.8), ledger=ledger,
        estimated_cost_usd=estimate, max_input_tokens=10**6, max_history_messages=10,
        max_output_tokens=max_output_tokens,
    )
    if drain:
        return [c async for c in stream.chunks]
    return stream


def _run(coro):
    return asyncio.run(coro)


def _exact(usd: float, tokens: int = 3) -> StubProvider:
    """Settles at exactly `usd` and `tokens`, so the persisted sums keep what was reserved."""
    return StubProvider(Cost(usd, True), prompt=tokens - 1, completion=1)


def _refused(coro) -> BudgetExceeded:
    try:
        _run(coro)
    except BudgetExceeded as exc:
        return exc
    raise AssertionError("expected a BudgetExceeded refusal")


# --- ledger vs reconstructed cost (CP1 (a)) ------------------------------------

def _fake_cost(f: Fixture) -> Cost:
    """Reconstructed independently of the ledger, from the fixture's own declared rates."""
    usd = (f.prompt_tokens * f.prompt_rate + f.completion_tokens * f.completion_rate) / TOKENS_PER_RATE_UNIT
    return Cost(usd=usd, metered=True)


def _fixtures() -> list[Fixture]:
    return [
        Fixture(("a",), 100, 20, 1.0, 4.0),
        Fixture(("b",), 250, 60, 0.5, 2.0),
        Fixture(("c",), 40, 400, 2.0, 8.0),
    ]


def test_ledger_total_equals_reconstructed_total_for_usd_calls() -> None:
    fixtures = _fixtures()
    results = []
    for _ in range(2):  # repeat spread must be 0
        ledger = _ledger(_db())
        for f in fixtures:
            _run(_send(FakeProvider(fixture=f), ledger, estimate=0.005))
        results.append(ledger.totals())
    expected = total_usd([_fake_cost(f) for f in fixtures])
    for totals in results:
        assert totals.list_cost == expected, (totals.list_cost, expected)
        assert totals.calls == 3 and totals.failed_calls == 0
    assert results[0] == results[1]


def test_ledger_keeps_metered_and_unpriced_semantics() -> None:
    db = _db()
    unmetered = _ledger(db)
    _run(_send(StubProvider(Cost(0.002, metered=False)), unmetered))
    _run(_send(StubProvider(Cost(0.003, metered=True)), unmetered))
    assert unmetered.totals().list_cost == total_usd([Cost(0.002, False), Cost(0.003, True)])
    assert unmetered.totals().list_cost.metered is False

    unknown = _ledger(db)
    _run(_send(StubProvider(Cost(0.003, metered=True)), unknown))
    _run(_send(StubProvider(UNPRICED), unknown))
    assert unknown.totals().list_cost == UNPRICED, "an unpriced call makes the request unpriced"
    assert unknown.totals().list_cost.usd is None


def test_billed_and_list_are_kept_apart() -> None:
    ledger = _ledger(_db())
    _run(_send(StubProvider(Cost(0.004, True), billed=0.003), ledger))
    _run(_send(StubProvider(Cost(0.004, True), billed=0.005), ledger))
    totals = ledger.totals()
    assert abs(totals.list_cost.usd - 0.008) < 1e-12
    assert abs(totals.billed.usd - 0.008) < 1e-12
    assert totals.billed.metered is True

    only_list = _ledger(_db())
    _run(_send(StubProvider(Cost(0.004, True), billed=None), only_list))
    assert abs(only_list.totals().list_cost.usd - 0.004) < 1e-12
    assert only_list.totals().billed == UNPRICED, "no billed source: unknown, never 0"

    free = _ledger(_db())
    _run(_send(StubProvider(Cost(0.004, False), billed=None), free))
    assert free.totals().billed.usd == 0.0 and free.totals().billed.metered is False


def test_no_overshoot_when_estimates_are_ceilings() -> None:
    fixtures = _fixtures()
    ledger = _ledger(_db())
    estimates = [0.005, 0.005, 0.005]
    for f, est in zip(fixtures, estimates):
        assert _fake_cost(f).usd <= est
        _run(_send(FakeProvider(fixture=f), ledger, estimate=est))
    assert ledger.totals().list_cost.usd <= sum(estimates)


def test_unpriced_actual_is_charged_at_its_estimate_not_zero() -> None:
    db = _db()
    ledger = _ledger(db)
    _run(_send(StubProvider(UNPRICED), ledger, estimate=0.004))
    day = f"day:{NOON:%Y-%m-%d}"
    usd, _ = ledger._store.totals("tenant:default", day)
    assert abs(usd - 0.004) < 1e-12, usd


def test_settle_replaces_the_estimate_with_the_actual() -> None:
    db = _db()
    ledger = _ledger(db)
    _run(_send(StubProvider(Cost(0.001, True), prompt=10, completion=5), ledger, estimate=0.008,
               max_output_tokens=500))
    usd, tokens = ledger._store.totals("global", f"day:{NOON:%Y-%m-%d}")
    assert abs(usd - 0.001) < 1e-12 and tokens == 15, (usd, tokens)


# --- budget before every send ----------------------------------------------------

def test_a_call_over_the_remaining_request_budget_is_refused_before_send() -> None:
    ledger = _ledger(_db(), per_call_usd=0.01, per_request_usd=0.01)
    provider = StubProvider(Cost(0.006, True))
    _run(_send(provider, ledger, estimate=0.006))
    assert len(provider.calls) == 1
    exc = _refused(_send(provider, ledger, estimate=0.006))
    assert exc.layer is SpendLayer.PER_REQUEST and exc.retry_after_s is None
    assert len(provider.calls) == 1, "the fake must record zero sends after the refusal"


def test_provider_is_handed_the_remaining_budget() -> None:
    ledger = _ledger(_db(), per_call_usd=0.01, per_request_usd=0.015)
    provider = StubProvider(Cost(0.006, True))
    _run(_send(provider, ledger, estimate=0.006))
    assert provider.calls[0]["budget"] == 0.01
    _run(_send(provider, ledger, estimate=0.006))
    assert abs(provider.calls[1]["budget"] - 0.009) < 1e-12


# --- one can-fire test per layer ---------------------------------------------------

def test_layer_per_call_can_fire() -> None:
    provider = FakeProvider()
    exc = _refused(_send(provider, _ledger(_db(), per_call_usd=0.01), estimate=0.011))
    assert exc.layer is SpendLayer.PER_CALL and exc.retry_after_s is None
    assert "exceeds its budget" in str(exc)
    assert provider.calls == []


def test_layer_per_request_can_fire() -> None:
    provider = _exact(0.015)
    ledger = _ledger(_db(), per_call_usd=0.02, per_request_usd=0.02)
    _run(_send(provider, ledger, estimate=0.015))
    exc = _refused(_send(provider, ledger, estimate=0.015))
    assert exc.layer is SpendLayer.PER_REQUEST and exc.retry_after_s is None
    assert len(provider.calls) == 1


def test_layer_tenant_day_can_fire_on_usd_and_on_tokens() -> None:
    provider = _exact(0.125)
    ledger = _ledger(_db(), tenant_day_usd=0.25)
    _run(_send(provider, ledger, estimate=0.125))
    _run(_send(provider, ledger, estimate=0.125))
    exc = _refused(_send(provider, ledger, estimate=0.125))
    assert exc.layer is SpendLayer.TENANT_DAY
    assert exc.retry_after_s == seconds_to_next_utc_midnight(NOON) == 12 * 3600
    assert len(provider.calls) == 2
    # Another tenant is not affected by the first one's day.
    _run(_send(provider, ledger, tenant="tenant-b", estimate=0.125))

    provider = _exact(0.001, tokens=400)
    ledger = _ledger(_db(), tenant_day_tokens=1000)
    big = [{"role": "user", "content": "word " * 80}]  # ~100 tokens + 300 reserved = 400
    for _ in range(2):
        _run(_send(provider, ledger, messages=big, max_output_tokens=300))
    exc = _refused(_send(provider, ledger, messages=big, max_output_tokens=300))
    assert exc.layer is SpendLayer.TENANT_DAY and len(provider.calls) == 2


def test_layer_global_minute_can_fire() -> None:
    now = [100.0]
    breaker = SpendRateBreaker(0.05, 0.8, clock=lambda: now[0])
    provider = FakeProvider()
    ledger = _ledger(_db())
    _run(_send(provider, ledger, estimate=0.03, breaker=breaker))
    now[0] = 110.0
    exc = _refused(_send(provider, ledger, estimate=0.03, breaker=breaker))
    assert isinstance(exc, Degraded) and exc.layer is SpendLayer.GLOBAL_MINUTE
    assert exc.retry_after_s == 50, "the window frees 60s after the first record"
    assert len(provider.calls) == 1
    now[0] = 161.0
    _run(_send(provider, ledger, estimate=0.03, breaker=breaker))
    assert len(provider.calls) == 2


def test_layer_global_day_can_fire_on_usd_and_on_tokens() -> None:
    provider = _exact(0.125)
    ledger = _ledger(_db(), global_day_usd=0.25)
    _run(_send(provider, ledger, tenant="tenant-a", estimate=0.125))
    _run(_send(provider, ledger, tenant="tenant-b", estimate=0.125))
    exc = _refused(_send(provider, ledger, tenant="tenant-c", estimate=0.125))
    assert exc.layer is SpendLayer.GLOBAL_DAY and exc.retry_after_s == 12 * 3600
    assert len(provider.calls) == 2

    provider = _exact(0.001, tokens=400)
    ledger = _ledger(_db(), global_day_tokens=1000)
    big = [{"role": "user", "content": "word " * 80}]
    _run(_send(provider, ledger, tenant="tenant-a", messages=big, max_output_tokens=300))
    _run(_send(provider, ledger, tenant="tenant-b", messages=big, max_output_tokens=300))
    exc = _refused(_send(provider, ledger, tenant="tenant-c", messages=big, max_output_tokens=300))
    assert exc.layer is SpendLayer.GLOBAL_DAY


def test_layer_global_month_can_fire() -> None:
    provider = _exact(0.125)
    ledger = _ledger(_db(), global_month_usd=0.25)
    _run(_send(provider, ledger, tenant="tenant-a", estimate=0.125))
    _run(_send(provider, ledger, tenant="tenant-b", estimate=0.125))
    exc = _refused(_send(provider, ledger, tenant="tenant-c", estimate=0.125))
    assert exc.layer is SpendLayer.GLOBAL_MONTH
    expected = int((datetime(2026, 11, 1, tzinfo=timezone.utc) - NOON).total_seconds())
    assert exc.retry_after_s == expected, (exc.retry_after_s, expected)
    assert len(provider.calls) == 2


# --- persistence and UTC boundaries ----------------------------------------------

def test_daily_and_monthly_sums_survive_a_restart() -> None:
    db = _db()
    spend = WIDE.model_copy(update={"tenant_day_usd": 0.25, "global_month_usd": 0.5})
    first = SpendStore(spend, db, _clock(NOON))
    first.reserve("tenant-a", 0.125, 10)
    first.reserve("tenant-a", 0.125, 10)
    del first  # a new process opens a new store on a new connection

    second = SpendStore(spend, db, _clock(NOON))
    assert second.totals("tenant:tenant-a", "day:2026-10-17") == (0.25, 20)
    assert second.totals("global", "month:2026-10") == (0.25, 20)
    try:
        second.reserve("tenant-a", 0.125, 10)
    except BudgetExceeded as exc:
        assert exc.layer is SpendLayer.TENANT_DAY
    else:
        raise AssertionError("the restarted store forgot the day's spend")


def test_day_resets_at_utc_midnight_and_month_does_not() -> None:
    db = _db()
    spend = WIDE.model_copy(update={"tenant_day_usd": 0.25, "global_month_usd": 0.3})
    clock = _clock(datetime(2026, 10, 17, 23, 59, 59, tzinfo=timezone.utc))
    store = SpendStore(spend, db, clock)
    store.reserve("tenant-a", 0.25, 1)
    try:
        store.reserve("tenant-a", 0.01, 1)
    except BudgetExceeded as exc:
        assert exc.layer is SpendLayer.TENANT_DAY and exc.retry_after_s == 1
    else:
        raise AssertionError("tenant-day did not fire before midnight")
    clock.box[0] = datetime(2026, 10, 18, 0, 0, 0, tzinfo=timezone.utc)
    store.reserve("tenant-a", 0.04, 1)  # a fresh day
    try:
        store.reserve("tenant-a", 0.04, 1)
    except BudgetExceeded as exc:
        assert exc.layer is SpendLayer.GLOBAL_MONTH, "the month sum carried across midnight"
    else:
        raise AssertionError("month sum was reset at midnight")


def test_reset_hints_are_computed_in_utc() -> None:
    assert seconds_to_next_utc_midnight(datetime(2026, 10, 17, 23, 0, tzinfo=timezone.utc)) == 3600
    assert seconds_to_next_utc_midnight(datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)) == 86400
    assert seconds_to_next_utc_month(datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)) == 60
    assert seconds_to_next_utc_month(datetime(2026, 2, 28, 0, 0, tzinfo=timezone.utc)) == 86400


# --- failed sends and the contextvar ----------------------------------------------

def test_a_send_that_fails_before_output_is_released_and_counted_apart() -> None:
    db = _db()
    ledger = _ledger(db)
    faulty = FaultProvider(FakeProvider(), FaultSpec(kind="429", attempts=1))
    try:
        _run(_send(faulty, ledger, estimate=0.004))
    except TransportError:
        pass
    else:
        raise AssertionError("the injected fault did not surface")
    assert ledger.totals().failed_calls == 1 and ledger.totals().calls == 0
    assert ledger._store.totals("global", f"day:{NOON:%Y-%m-%d}")[0] == 0.0, "the hold was released"
    _run(_send(faulty, ledger, estimate=0.004))  # the retry succeeds
    assert ledger.totals().calls == 1 and ledger.totals().failed_calls == 1


def test_a_priced_call_outside_a_ledger_has_no_budget() -> None:
    try:
        current_ledger()
    except LedgerMissing:
        pass
    else:
        raise AssertionError("current_ledger() returned a ledger nobody opened")


def test_guard_module_exports_the_refusal_vocabulary() -> None:
    assert guard_mod.BudgetExceeded is BudgetExceeded
    assert issubclass(guard_mod.Degraded, BudgetExceeded)


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
