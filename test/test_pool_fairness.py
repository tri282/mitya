# test/test_pool_fairness.py
"""v2 P5 step 5 verify: the shared-pool fair-share guard (src/gateway/guard.py).

Charter **A12(1)** is what this implements, superseding the phase file's
"per-tenant budget" wording (R104): one instance, many tenants, ONE shared
provider account -- so shared rate limits and one bill, and the failure to
prevent is a noisy neighbour starving the pool, not one tenant reading
another's budget. There is therefore no per-tenant cap here at all. Fair share
is `pool_tokens / active_tenants`, computed from the same sliding minute the
spend-rate breaker already keeps.

R108 is the standard these tests are written to: every assertion drives the
ENFORCEMENT path, against `FakeProvider.calls`, not the code's shape. A guard
that has never rejected anything is indistinguishable from an absent one.

Plain-assert script, not pytest (matches test_gateway_guard.py's idiom).
Offline: FakeProvider only, no network, no real key, hand-advanced clocks.
"""

import asyncio
import sys

sys.path.insert(0, ".")

from src.gateway.fake import FakeProvider  # noqa: E402
from src.gateway.guard import (  # noqa: E402
    PoolShareExceeded,
    SpendRateBreaker,
    guarded_generate,
)
from src.limits import RateLimiter  # noqa: E402

# ~6 tokens by the char/4 heuristic; MAX_OUT below dominates the reservation.
_MESSAGES = [{"role": "user", "content": "who are you?"}]
MAX_OUT = 100
# Sized so the tests run inside the band that matters and never outside it.
# One request reserves 103 tokens (3 estimated prompt + MAX_OUT). The pressure
# floor is 0.8 x 1000 = 800 and the limiter itself binds at 1000, so every test
# below stays under 1000 committed tokens: if the fairness guard failed to
# fire, the assertion fails on a SERVED call rather than hanging in
# `RateLimiter.acquire_async`, which waits for pool capacity in real seconds.
# Real pools are this shape at a different scale (Groq gpt-oss-120b: tpm 8000).
POOL = 1000


class _Clock:
    """Hand-advanced monotonic clock -- the window is a minute long and no test
    is allowed to take a minute."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _limiter(tpm=POOL) -> RateLimiter:
    """Generous rpm/rpd, real tpm: the pool under test IS the token window, so
    only that one may bind."""
    return RateLimiter(rpm=10_000, tpm=tpm, rpd=100_000, sleep=lambda s: None)


def _call(provider, breaker, tenant, limiter=None):
    return asyncio.run(
        guarded_generate(
            provider, _MESSAGES, "default", tenant, None,
            limiter=limiter or _limiter(), breaker=breaker,
            estimated_cost_usd=0.001,
            max_input_tokens=10_000, max_history_messages=10,
            max_output_tokens=MAX_OUT,
        )
    )


def test_one_tenant_alone_gets_the_whole_pool() -> None:
    """The rule is max-min fairness, not a per-tenant cap: with a single active
    tenant its fair share IS the pool, so a heavy tenant on an idle deployment
    is never rejected by this guard. If this fails the guard has become a cap
    and A12(1) has been implemented as the thing it explicitly is not."""
    clock = _Clock()
    provider = FakeProvider()
    breaker = SpendRateBreaker(1000.0, clock=clock)
    limiter = _limiter()
    # 7 x 103 = 721 tokens: past nothing yet. The 7th call crosses the pressure
    # floor when its own reservation is counted (824 > 800), so this also
    # covers the case where pressure IS met and the tenant is still alone.
    for _ in range(7):
        _call(provider, breaker, "tenant-a", limiter)
    assert len(provider.calls) == 7, "a lone tenant was rejected by the fairness guard"


def test_a_noisy_tenant_is_rejected_once_a_second_tenant_contends() -> None:
    """The headline behaviour. tenant-a fills the pool; tenant-b then arrives,
    which halves both tenants' fair share; tenant-a's next request is refused
    BEFORE reaching the provider, and tenant-b's is served."""
    clock = _Clock()
    provider = FakeProvider()
    breaker = SpendRateBreaker(1000.0, clock=clock)
    limiter = _limiter()

    # 7 x 103 = 721 of 1000. tenant-a is alone, so its fair share is the whole
    # pool and every one is served.
    for _ in range(7):
        _call(provider, breaker, "tenant-a", limiter)
    assert len(provider.calls) == 7

    # tenant-b arrives: active = 2, fair share = 500, and tenant-a is at 721.
    # tenant-b itself is at 0, so it is served; tenant-a's next one is not.
    served_before_b = len(provider.calls)
    _call(provider, breaker, "tenant-b", limiter)
    assert len(provider.calls) == served_before_b + 1, "the quiet tenant was starved"

    try:
        _call(provider, breaker, "tenant-a", limiter)
    except PoolShareExceeded as exc:
        assert exc.retry_after >= 1, exc.retry_after
        assert exc.status == 429, exc.status
    else:
        raise AssertionError("the noisy tenant was not held to its share")
    assert len(provider.calls) == served_before_b + 1, (
        "a rejected request still reached the provider -- enforcement is after "
        "the call, which is exactly what charter IV forbids"
    )


def test_the_quiet_tenant_keeps_being_served_while_the_noisy_one_is_refused() -> None:
    """Starvation is the failure mode A12(1) names, so the test has to show the
    victim being served, not only the offender being stopped. An "everyone is
    rejected" guard would pass a test that asserted only the rejection."""
    clock = _Clock()
    provider = FakeProvider()
    breaker = SpendRateBreaker(1000.0, clock=clock)
    limiter = _limiter()
    for _ in range(7):
        _call(provider, breaker, "tenant-a", limiter)

    served = 0
    refused = 0
    for tenant in ("tenant-b", "tenant-a", "tenant-b", "tenant-a"):
        try:
            _call(provider, breaker, tenant, limiter)
            served += 1
        except PoolShareExceeded:
            refused += 1
    assert refused >= 1, "the noisy tenant was never held back"
    assert served >= 1, "the quiet tenant got nothing -- this is starvation, not fairness"


def test_no_enforcement_below_the_pressure_floor() -> None:
    """Fairness is a contention rule. With a big pool and two tenants, one of
    them being far ahead of the other costs nobody anything, and rejecting it
    would throw away throughput to enforce a fairness nobody needed."""
    clock = _Clock()
    provider = FakeProvider()
    breaker = SpendRateBreaker(1000.0, clock=clock)
    limiter = _limiter(tpm=1_000_000)
    _call(provider, breaker, "tenant-b", limiter)
    for _ in range(5):
        _call(provider, breaker, "tenant-a", limiter)
    assert len(provider.calls) == 6, "the guard fired with the pool nearly empty"


def test_the_window_slides_so_a_rejection_is_not_permanent() -> None:
    """A tenant refused now must be served again once its usage ages out --
    otherwise the guard is a ban, and `retry_after` is a lie."""
    clock = _Clock()
    provider = FakeProvider()
    breaker = SpendRateBreaker(1000.0, clock=clock)
    limiter = _limiter()
    for _ in range(7):
        _call(provider, breaker, "tenant-a", limiter)
    _call(provider, breaker, "tenant-b", limiter)
    try:
        _call(provider, breaker, "tenant-a", limiter)
        raise AssertionError("expected the noisy tenant to be refused")
    except PoolShareExceeded as exc:
        wait = exc.retry_after
    clock.advance(wait + 61)
    _call(provider, breaker, "tenant-a", limiter)  # must not raise


def test_a_rejected_request_never_counts_against_the_pool() -> None:
    """A refusal consumed nothing, so it must not deepen the hole that caused
    it -- otherwise a rejected tenant digs itself permanently out of share."""
    clock = _Clock()
    breaker = SpendRateBreaker(1000.0, clock=clock)
    breaker.record(0.001, "tenant-a", 800)
    breaker.record(0.001, "tenant-b", 10)
    assert breaker.share_exceeded("tenant-a", 103, POOL) is True
    # Ask again: a pure query must not mutate the window.
    assert breaker.share_exceeded("tenant-a", 103, POOL) is True
    assert sum(e[3] for e in breaker._window) == 810, "share_exceeded() recorded usage"


def test_one_active_tenant_is_never_the_fairness_guard_s_business() -> None:
    """The `len(active) < 2` branch, driven directly: even wildly over the
    pool, a single tenant is not rejected HERE. Being over the pool is the rate
    limiter's fact and it has its own status code and its own counter; a
    fairness rejection that fired there would report the wrong cause."""
    clock = _Clock()
    breaker = SpendRateBreaker(1000.0, clock=clock)
    breaker.record(0.0, "tenant-a", 5_000)
    assert breaker.share_exceeded("tenant-a", 103, POOL) is False
    breaker.record(0.0, "tenant-b", 10)
    assert breaker.share_exceeded("tenant-a", 103, POOL) is True, (
        "non-vacuity: the same over-pool tenant IS rejected once a second one "
        "contends -- so the False above is the one-tenant rule, not a dead guard"
    )


def test_an_unlimited_pool_disables_the_rule_rather_than_dividing_by_it() -> None:
    """`Config.NO_TPM_LIMIT` (1e9) stands for "this provider publishes no token
    limit". There is then no pool to divide and no contention to arbitrate, and
    inventing a denominator would make the guard look like it was enforcing
    something it is not (the same posture src/pricing takes on an unknown
    rate). Zero is the degenerate case of the same statement."""
    clock = _Clock()
    breaker = SpendRateBreaker(1000.0, clock=clock)
    for _ in range(50):
        breaker.record(0.0, "tenant-a", 10_000)
    breaker.record(0.0, "tenant-b", 10_000)
    assert breaker.share_exceeded("tenant-a", 10_000, 0) is False
    assert breaker.share_exceeded("tenant-a", 10_000, 10 ** 9) is False


def test_the_spend_rate_breaker_still_works_on_the_shared_window() -> None:
    """P2's invariant, re-verified: the window now carries tenant and tokens as
    well as USD, and the dollar policy must be unaffected by that. Two policies,
    one window -- the risk of which is exactly this."""
    clock = _Clock()
    breaker = SpendRateBreaker(1.0, clock=clock)
    assert breaker.would_trip(0.5) is False
    breaker.record(0.6, "tenant-a", 100)
    breaker.record(0.3, "tenant-b", 100)
    assert breaker.would_trip(0.2) is True   # 0.9 + 0.2 > 1.0
    assert breaker.would_trip(0.05) is False
    clock.advance(61)
    assert breaker.would_trip(0.9) is False, "the window did not evict"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
