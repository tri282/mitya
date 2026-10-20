# test/test_gateway_guard.py
"""P2 step 4 verify (schedule Batch B): spend safety enforced BEFORE the
provider call -- src/gateway/guard.py. Updated at v3 P0 B1 for the request
ledger (src/gateway/ledger.py): `budget` is no longer a caller argument, the
ledger carries the per-call and per-request layers.

Plain-assert script, not pytest (matches test_gateway_contract.py's idiom).
Offline: FakeProvider only, no network, no real key, no clock sleeps (every
limiter/breaker here is built with an injectable, hand-advanced clock).

This file is the schedule's stated Batch B Verify(1) surface for step 4:
  * a request that would exceed the global budget never reaches a provider
    -- asserted against FakeProvider.calls, "exactly why the fake exists";
  * the spend-rate breaker trips into DEGRADED mode, not failure -- Degraded
    is a distinct exception from a hard failure, and it is raised before any
    provider call;
  * a `model` field has no path to influence routing -- the Provider
    contract has no such parameter at all.
"""

import asyncio
import inspect
import os
import sys
import tempfile

sys.path.insert(0, ".")

from src.db import init_schema  # noqa: E402
from src.gateway.fake import FakeProvider  # noqa: E402
from src.gateway.guard import (  # noqa: E402
    BudgetExceeded,
    Degraded,
    RequestTooLarge,
    SpendRateBreaker,
    guarded_generate,
)
from src.gateway.ledger import RequestLedger, SpendLayer, SpendStore  # noqa: E402
from src.limits import QuotaExhausted, RateLimiter  # noqa: E402
from src.limits_config import SpendLimits  # noqa: E402

_DB = os.path.join(tempfile.mkdtemp(prefix="mitya-guard-db-"), "t.sqlite3")
init_schema(_DB)

_WIDE = SpendLimits(
    per_call_usd=1000.0, per_request_usd=1000.0, tenant_day_usd=1e6, tenant_day_tokens=10**9,
    global_minute_usd=1e6, global_day_usd=1e6, global_day_tokens=10**9, global_month_usd=1e6,
    unpriced_call_tokens=10**9,
)


def _limiter(rpm=100, tpm=10_000, rpd=1000) -> RateLimiter:
    """A limiter that never has to wait, unless the test wants it to --
    generous defaults, overridden per test."""
    return RateLimiter(rpm=rpm, tpm=tpm, rpd=rpd, sleep=lambda s: None)


def _breaker(cap=1000.0) -> SpendRateBreaker:
    return SpendRateBreaker(max_usd_per_minute=cap, fairness_pressure=0.8)


def _ledger(**limits) -> RequestLedger:
    spend = _WIDE.model_copy(update=limits)
    return RequestLedger(spend, SpendStore(spend, _DB))


_MESSAGES = [{"role": "user", "content": "who are you?"}]


def _call(provider, messages=_MESSAGES, *, limiter=None, breaker=None, ledger=None,
          estimated_cost_usd=0.0, max_input_tokens=10_000, max_history_messages=10):
    return asyncio.run(
        guarded_generate(
            provider, messages, "default", "default", call_class="generation",
            limiter=limiter or _limiter(), breaker=breaker or _breaker(),
            ledger=ledger or _ledger(), estimated_cost_usd=estimated_cost_usd,
            max_input_tokens=max_input_tokens, max_history_messages=max_history_messages,
            max_output_tokens=0,
        )
    )


def test_provider_contract_has_no_body_controlled_routing_field() -> None:
    """charter IV: a `model` field in a request body is a cost-escalation
    primitive and must change nothing about routing. Enforced structurally,
    not by convention -- `Provider.generate()` has no `model`/`route`
    parameter a client-supplied body field could ever reach."""
    params = set(inspect.signature(FakeProvider.generate).parameters) - {"self"}
    assert params == {"messages", "tier", "tenant", "budget"}, params


def test_request_over_the_history_limit_never_reaches_the_provider() -> None:
    provider = FakeProvider()
    try:
        _call(provider, _MESSAGES * 5, max_history_messages=3)
    except RequestTooLarge:
        pass
    else:
        raise AssertionError("oversized history was not rejected")
    assert provider.calls == [], "provider must not be called for a rejected request"


def test_request_over_the_token_limit_never_reaches_the_provider() -> None:
    provider = FakeProvider()
    huge = [{"role": "user", "content": "word " * 10_000}]
    try:
        _call(provider, huge, max_input_tokens=100, max_history_messages=10)
    except RequestTooLarge:
        pass
    else:
        raise AssertionError("oversized input was not rejected")
    assert provider.calls == []


def test_a_request_over_budget_never_reaches_the_provider() -> None:
    """The named Verify(1) assertion, word for word: 'a request that would
    exceed the global budget never reaches a provider' -- checked against
    FakeProvider.calls, which is exactly why the fake carries that list."""
    provider = FakeProvider()
    try:
        _call(provider, ledger=_ledger(per_call_usd=0.0001), estimated_cost_usd=1.0)
    except BudgetExceeded as exc:
        assert exc.layer is SpendLayer.PER_CALL
    else:
        raise AssertionError("an over-budget request was not rejected")
    assert provider.calls == []


def test_exhausted_daily_quota_never_reaches_the_provider() -> None:
    """RateLimiter.acquire_async (v2 P2 step 4's stated addition) gates the
    call exactly like the sync acquire() does -- QuotaExhausted before the
    provider is ever touched."""
    provider = FakeProvider()
    limiter = _limiter(rpd=1)
    limiter.requests_today = 1  # quota already spent
    try:
        _call(provider, limiter=limiter)
    except QuotaExhausted:
        pass
    else:
        raise AssertionError("exhausted quota did not reject the request")
    assert provider.calls == []


def test_spend_rate_breaker_refuses_cleanly_not_failure() -> None:
    """The global per-minute layer refuses with a layer name (CP0 (iii)): Degraded is a
    BudgetExceeded, raised before any provider call."""
    provider = FakeProvider()
    breaker = _breaker(cap=0.50)
    try:
        _call(provider, breaker=breaker, estimated_cost_usd=1.0)
    except Degraded as exc:
        assert exc.layer is SpendLayer.GLOBAL_MINUTE
        assert isinstance(exc, BudgetExceeded)
        # 1.0 can never fit under a 0.50 cap: no retry time.
        assert exc.retry_after_s is None
    else:
        raise AssertionError("spend over the per-minute cap did not trip the breaker")
    assert provider.calls == [], "a tripped breaker must not let the call through"


def test_a_request_within_every_limit_reaches_the_provider_and_records_spend() -> None:
    provider = FakeProvider()
    breaker = _breaker(cap=10.0)
    result = _call(provider, breaker=breaker, ledger=_ledger(per_call_usd=10.0), estimated_cost_usd=0.5)
    assert len(provider.calls) == 1
    assert result is not None
    # Spend is only recorded AFTER a real call -- a rejected request must not
    # count against its own window (see guard.py's SpendRateBreaker.record).
    assert breaker.would_trip(9.6) is True   # 0.5 already spent + 9.6 > 10.0
    assert breaker.would_trip(9.4) is False  # 0.5 + 9.4 == 9.9 <= 10.0


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
