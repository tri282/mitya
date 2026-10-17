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

v3 P0 B1: the six spend layers and the request ledger live in `ledger.py` (OI8, OI45); this
module is still the one admission point.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from collections.abc import AsyncIterator

from src.gateway.contract import GenerationStream, Provider, Usage, normalize_finish_reason
from src.gateway.ledger import BudgetExceeded, RequestLedger, Reservation, SpendLayer
from src.limits import LimitError, QuotaExhausted, RateLimiter
from src.limits import estimate_tokens as _estimate_tokens
from src.metrics import record_budget_refusal, record_finish_reason, record_quota_rejection

logger = logging.getLogger(__name__)


class RequestTooLarge(LimitError):
    """Input tokens or history length over the configured ceiling. Rejected
    before the provider call -- billing for an oversized prompt and only
    THEN refusing it is exactly the "reconciled after" failure mode IV
    forbids."""


class Degraded(BudgetExceeded):
    """The global per-minute layer refused (CP0: a clean decline, never a ladder rung)."""

    def __init__(self, retry_after_s: int | None) -> None:
        super().__init__(SpendLayer.GLOBAL_MINUTE, retry_after_s)


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
        fairness_pressure: float,
        clock=time.monotonic,
    ) -> None:
        self.max_usd_per_minute = max_usd_per_minute
        # Below this fraction of the pool there is spare capacity, so nobody is
        # being starved and a heavy tenant is not costing anyone anything --
        # rejecting it then would throw away throughput to enforce a fairness
        # nobody needed. Fairness is a contention rule, not a quota.
        self.fairness_pressure = fairness_pressure
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

    def seconds_until_fits(self, projected_usd: float) -> int | None:
        """Seconds until the oldest entries leave the window enough for `projected_usd` to fit;
        None when it cannot fit even in an empty window."""
        if projected_usd > self.max_usd_per_minute:
            return None
        now = self._clock()
        self._evict(now)
        running = sum(entry[2] for entry in self._window)
        for stamp, _, usd, _ in self._window:
            running -= usd
            if running + projected_usd <= self.max_usd_per_minute:
                return max(1, math.ceil(stamp + 60.0 - now))
        return 60

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


async def _settling(
    chunks: AsyncIterator[str], usage: Usage, ledger: RequestLedger, res: Reservation, model: str,
) -> AsyncIterator[str]:
    """Settle the ledger entry when the stream ends, however it ends. A stream that raises
    before its first chunk produced nothing and releases its reservation."""
    produced = False
    try:
        async for chunk in chunks:
            produced = True
            yield chunk
    except BaseException:
        if not produced:
            ledger.fail(res)
            raise
        ledger.settle(res, usage)
        raise
    else:
        ledger.settle(res, usage)
    finally:
        finish = normalize_finish_reason(usage.finish_reason, usage.native_finish_reason)
        if res.entry.outcome != "failed":
            record_finish_reason(finish.value)
            if finish.value == "other":
                logger.warning(
                    "gateway: unrecognised finish reason %r from %s counted as other",
                    (usage.finish_reason or usage.native_finish_reason or "")[:40], model,
                )


async def guarded_generate(
    provider: Provider,
    messages: list[dict],
    tier: str,
    tenant: str,
    *,
    limiter: RateLimiter,
    breaker: SpendRateBreaker,
    ledger: RequestLedger,
    estimated_cost_usd: float,
    max_input_tokens: int,
    max_history_messages: int,
    max_output_tokens: int,
) -> GenerationStream:
    """Enforce spend safety, then delegate to `provider.generate()`.

    Order: request size, the per-model quota window, per-call and per-request budget, the
    global per-minute window, then the persisted tenant-day, global-day and global-month
    sums (reserved atomically). All before `provider.generate()` runs; a refusal at any
    layer raises `BudgetExceeded` naming the layer and sends nothing (CP0 (iii)). The
    returned stream settles the ledger entry with the actual cost when it ends.
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

    # Fairness lives in the admission gate (`TenantConcurrency._ahead_of_share`, R117).
    model = getattr(provider, "model", "unknown")
    pool_tokens_reserved = input_tokens + max_output_tokens

    try:
        # Reserve input plus the completion ceiling: provider TPM counts both directions
        # (measured on Groq gpt-oss-120b: input-only reservation overshoots by 24%).
        await limiter.acquire_async(pool_tokens_reserved)
    except QuotaExhausted:
        record_quota_rejection(model, tenant)
        raise

    try:
        ledger.check(estimated_cost_usd)
        if breaker.would_trip(estimated_cost_usd):
            logger.warning(
                "gateway: spend-rate layer refused (tenant=%s, projected=%s)",
                tenant, estimated_cost_usd,
            )
            raise Degraded(breaker.seconds_until_fits(estimated_cost_usd))
        remaining = ledger.remaining_usd()
        res = ledger.reserve(tenant, model, estimated_cost_usd, pool_tokens_reserved)
    except BudgetExceeded as exc:
        logger.warning(
            "gateway: budget refused at %s (tenant=%s, estimated=%s)",
            exc.layer.value, tenant, estimated_cost_usd,
        )
        record_budget_refusal(exc.layer.value)
        raise

    try:
        stream = await provider.generate(messages, tier, tenant, remaining)
    except BaseException:
        ledger.fail(res)
        raise
    # One record, both policies: `estimated_cost_usd` feeds the spend-rate cap,
    # `tenant`/`pool_tokens_reserved` feed the fair-share view of the same minute.
    breaker.record(estimated_cost_usd, tenant, pool_tokens_reserved)
    return GenerationStream(
        chunks=_settling(stream.chunks, stream.usage, ledger, res, model), usage=stream.usage,
    )
