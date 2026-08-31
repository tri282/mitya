"""Spend safety, enforced before the provider call (v2 P2 step 4, charter IV;
schedule Batch B).

Charter IV, restated because this module exists to hold it: "per-tenant token
budget hard-capped before the provider call, never reconciled after" and
"global circuit breaker trips on spend rate, into degraded mode not
failure." Both apply globally at P2 -- per-tenant enforcement is P5, once
tenant identity is authenticated (phase-2-gateway.md's explicit deferral).

**Amended at v2 P5 step 5 (charter A12(1)).** Per-tenant enforcement arrives
here, but NOT in the shape phase-2-gateway.md deferred ("per-tenant budget").
A12 supersedes it: the deployment is one instance, many tenants, ONE shared
provider account -- so there are shared rate limits and one bill, and the
failure mode is a noisy neighbour starving the shared pool, not one tenant
reading another's budget. What P5 adds is therefore a **fair-share check on the
shared pool**, enforced before the provider call like everything else in this
module, and it EXTENDS the existing window rather than adding a second one.

The unit question, stated because it is the one that could have made this
control inert (R108's lesson). Charter II fixes USD as the *reporting* unit,
and `src/pricing` supplies it. But the pool that actually starves at this
project's arms is the provider QUOTA window, not a dollar ceiling: R48 already
records that at the free tier there is no USD ceiling to breach (Google's
rolling cap is literally $0), so a fairness rule denominated in dollars would
divide zero by the tenant count on every request and never fire. Fairness is
therefore enforced on the pool's own meter -- reserved tokens against
`RateLimiter.tpm` -- and reported in USD, which the same window carries per
tenant.

Boundary: this wraps a `Provider.generate()` call; it is not itself a
`Provider` and never appears on the `Provider` Protocol, so a real adapter
(Batch C) and `FakeProvider` (Batch A) are both untouched by this module
existing. `guarded_generate()` is the ONE place all four checks run, in the
order charter IV requires -- request size, then fair share, then budget, then
spend rate -- so a request that fails any of them never reaches `provider.generate()`
(Batch B's Verify assertion, checked against `FakeProvider.calls`).
"""

from __future__ import annotations

import logging
import time
from collections import deque

from src.config import Config
from src.gateway.contract import GenerationStream, Provider
from src.limits import LimitError, QuotaExhausted, RateLimiter
from src.limits import estimate_tokens as _estimate_tokens
from src.metrics import record_quota_rejection

logger = logging.getLogger(__name__)


class RequestTooLarge(LimitError):
    """Input tokens or history length over the configured ceiling. Rejected
    before the provider call -- billing for an oversized prompt and only
    THEN refusing it is exactly the "reconciled after" failure mode IV
    forbids."""


class BudgetExceeded(LimitError):
    """This request's estimated cost would exceed the remaining budget."""


class Degraded(RuntimeError):
    """The spend-rate breaker has tripped. Not a failure (IV: "into degraded
    mode not failure") -- a caller catches this and walks the degradation
    ladder (step 10, `src/gateway/ladder.py`); it must not become a 5xx by
    itself."""


class SpendRateBreaker:
    """The shared-pool window: a sliding-minute USD/minute cap (charter II's
    fixed spend-rate unit) AND, since v2 P5, the per-tenant fair-share view of
    the same window (charter A12(1)).

    Same window shape as `src.limits.RateLimiter` -- a sliding deque evicted
    on read -- but over a running dollar total rather than a request/token
    count, so it is its own small class rather than a third constructor
    argument bent onto `RateLimiter`, whose `rpm`/`tpm`/`rpd` accounting does
    not describe "how many dollars in the last 60 seconds".

    **Two policies, ONE window, deliberately.** The spend-rate breaker and the
    fair-share check ask different questions of exactly the same minute of
    history -- "how much has this deployment spent" and "how much of the pool
    has this tenant taken". Giving the second its own deque would mean a second
    copy of the eviction rule, which is the drift `src.limits.RateLimiter._try_reserve`
    already documents having to avoid. Each entry therefore carries
    `(timestamp, tenant, usd, tokens)`: `usd` answers the first question,
    `tenant`/`tokens` the second.

    Reports whether a request WOULD trip the cap (`would_trip`); it does not
    raise by itself, so the caller decides what "degraded" means (step 10's
    ladder), matching charter IV's "into degraded mode, not failure".
    """

    def __init__(
        self,
        max_usd_per_minute: float,
        clock=time.monotonic,
        fairness_pressure: float | None = None,
    ) -> None:
        self.max_usd_per_minute = max_usd_per_minute
        # Below this fraction of the pool there is spare capacity, so nobody is
        # being starved and a heavy tenant is not costing anyone anything --
        # rejecting it then would throw away throughput to enforce a fairness
        # nobody needed. Fairness is a contention rule, not a quota.
        self.fairness_pressure = (
            Config.POOL_FAIRNESS_PRESSURE if fairness_pressure is None else fairness_pressure
        )
        self._clock = clock
        # (timestamp, tenant, usd, pool_tokens_reserved)
        self._window: deque[tuple[float, str, float, int]] = deque()

    def _evict(self, now: float) -> None:
        while self._window and now - self._window[0][0] >= 60.0:
            self._window.popleft()

    def would_trip(self, projected_usd: float) -> bool:
        now = self._clock()
        self._evict(now)
        spent = sum(entry[2] for entry in self._window)
        return (spent + projected_usd) > self.max_usd_per_minute

    def record(self, usd: float, tenant: str, tokens: int) -> None:
        """Only called once a call is actually made -- a rejected request
        spent nothing, took none of the pool, and must not count against its
        own window."""
        self._window.append((self._clock(), tenant, usd, tokens))


def _input_tokens(messages: list[dict]) -> int:
    """Rough pre-call estimate, same heuristic `src.limits.estimate_tokens`
    already uses to reserve TPM budget before a call -- reused rather than a
    second estimator, so "input tokens" means the same thing in the rate
    limiter and in the request-size guard."""
    return sum(_estimate_tokens(m.get("content", "") or "") for m in messages)


async def guarded_generate(
    provider: Provider,
    messages: list[dict],
    tier: str,
    tenant: str,
    budget: float | None,
    *,
    limiter: RateLimiter,
    breaker: SpendRateBreaker,
    estimated_cost_usd: float,
    max_input_tokens: int,
    max_history_messages: int,
    max_output_tokens: int = 0,
) -> GenerationStream:
    """Enforce spend safety, then delegate to `provider.generate()`.

    Order, and the order IS charter IV's point: request-size limits, then the
    shared-pool fair-share check (P5 step 5 -- ahead of the limiter on purpose;
    see the comment at its call site), then the per-model rate limiter (`RateLimiter.acquire_async` -- v2 P2 step 4's
    stated reason it must be async: `time.sleep` inside this coroutine would
    block the event loop for every OTHER concurrent request), then the
    budget check, then the spend-rate breaker -- all before
    `provider.generate()` runs. Nothing here is reconciled after the call.
    """
    if len(messages) > max_history_messages:
        raise RequestTooLarge(
            f"{len(messages)} messages over the {max_history_messages}-message "
            f"history limit"
        )
    input_tokens = _input_tokens(messages)
    if input_tokens > max_input_tokens:
        raise RequestTooLarge(
            f"~{input_tokens} input tokens over the {max_input_tokens} limit"
        )

    # The fair-share guard that used to sit here is RETIRED (P5 G-P10, R117).
    # It never fired: it needed 80% of `limiter.tpm` in pressure, and admission
    # control (`src.limits.TenantConcurrency`) caps in-flight work at roughly 3%
    # of that -- the offered load that would raise pressure is precisely what
    # admission refuses. It passed nine tests and was unreachable in production,
    # which is a worse state than absent, because green tests asserted it worked.
    # Fairness now lives in the admission gate, the only place where contention
    # is observable: `TenantConcurrency._ahead_of_share()`.
    model = getattr(provider, "model", "unknown")
    pool_tokens_reserved = input_tokens + max_output_tokens

    try:
        # Reserve input PLUS the completion ceiling, not input alone. Provider
        # TPM counts tokens in both directions, so reserving only the prompt
        # lets more requests through than the provider will accept and the
        # difference comes back as 429s. Measured on Groq gpt-oss-120b (tpm
        # 8000): a ~1712-token prompt reserved alone permits 4.7 req/min while
        # the provider, counting the 400-token completion too, allows 3.8 --
        # a 24% overshoot that the transport retry then absorbs, turning a
        # limiter miss into latency and wasted rpd rather than an error.
        #
        # `_input_tokens` above stays input-only on purpose: that check is
        # about REQUEST SIZE (does this prompt fit at all), which is a
        # different question from how much quota the round trip consumes.
        await limiter.acquire_async(pool_tokens_reserved)
    except QuotaExhausted:
        # First live call site for mitya_quota_rejections_total (Q13,
        # src/metrics.py) -- QuotaExhausted has existed since P1 5b with no
        # caller on the request path to raise it.
        record_quota_rejection(model, tenant)
        raise

    if budget is not None and estimated_cost_usd > budget:
        # Deliberately NOT record_quota_rejection() here -- that counter's
        # description (src/metrics.py) is specifically QuotaExhausted
        # rejections. A budget-exceeded rejection is a different reason and
        # would blur the two under one series with no way to tell them
        # apart; adding a second, distinctly-named counter is out of this
        # step's "~3 lines" scope (schedule Batch B), so this is logged, not
        # metered, until a later phase asks for the metric.
        logger.warning(
            "gateway: budget guard rejected request (tenant=%s, estimated=%s, budget=%s)",
            tenant, estimated_cost_usd, budget,
        )
        raise BudgetExceeded(
            f"estimated cost {estimated_cost_usd} exceeds remaining budget {budget}"
        )

    if breaker.would_trip(estimated_cost_usd):
        logger.warning(
            "gateway: spend-rate breaker tripped (tenant=%s, projected=%s) -- degraded mode",
            tenant, estimated_cost_usd,
        )
        raise Degraded("spend rate over the per-minute cap")

    stream = await provider.generate(messages, tier, tenant, budget)
    # One record, both policies: `estimated_cost_usd` feeds the spend-rate cap,
    # `tenant`/`pool_tokens_reserved` feed the fair-share view of the same
    # minute. See SpendRateBreaker's "two policies, one window" note.
    breaker.record(estimated_cost_usd, tenant, pool_tokens_reserved)
    return stream
