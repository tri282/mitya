"""Spend safety, enforced before the provider call (v2 P2 step 4, charter IV;
schedule Batch B).

Charter IV, restated because this module exists to hold it: "per-tenant token
budget hard-capped before the provider call, never reconciled after" and
"global circuit breaker trips on spend rate, into degraded mode not
failure." Both apply globally at P2 -- per-tenant enforcement is P5, once
tenant identity is authenticated (phase-2-gateway.md's explicit deferral).

Boundary: this wraps a `Provider.generate()` call; it is not itself a
`Provider` and never appears on the `Provider` Protocol, so a real adapter
(Batch C) and `FakeProvider` (Batch A) are both untouched by this module
existing. `guarded_generate()` is the ONE place all three checks run, in the
order charter IV requires -- request size, then budget, then spend rate --
so a request that fails any of them never reaches `provider.generate()`
(Batch B's Verify assertion, checked against `FakeProvider.calls`).
"""

from __future__ import annotations

import logging
import time
from collections import deque

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
    """Sliding-window USD/minute cap (charter II's fixed spend-rate unit).

    Same window shape as `src.limits.RateLimiter` -- a sliding deque evicted
    on read -- but over a running dollar total rather than a request/token
    count, so it is its own small class rather than a third constructor
    argument bent onto `RateLimiter`, whose `rpm`/`tpm`/`rpd` accounting does
    not describe "how many dollars in the last 60 seconds".

    Reports whether a request WOULD trip the cap (`would_trip`); it does not
    raise by itself, so the caller decides what "degraded" means (step 10's
    ladder), matching charter IV's "into degraded mode, not failure".
    """

    def __init__(self, max_usd_per_minute: float, clock=time.monotonic) -> None:
        self.max_usd_per_minute = max_usd_per_minute
        self._clock = clock
        self._window: deque[tuple[float, float]] = deque()  # (timestamp, usd)

    def _evict(self, now: float) -> None:
        while self._window and now - self._window[0][0] >= 60.0:
            self._window.popleft()

    def would_trip(self, projected_usd: float) -> bool:
        now = self._clock()
        self._evict(now)
        spent = sum(usd for _, usd in self._window)
        return (spent + projected_usd) > self.max_usd_per_minute

    def record(self, usd: float) -> None:
        """Only called once a call is actually made -- a rejected request
        spent nothing and must not count against its own window."""
        self._window.append((self._clock(), usd))


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

    Order, and the order IS charter IV's point: request-size limits, then
    the per-model rate limiter (`RateLimiter.acquire_async` -- v2 P2 step 4's
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

    model = getattr(provider, "model", "unknown")
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
        await limiter.acquire_async(input_tokens + max_output_tokens)
    except QuotaExhausted:
        # First live call site for mitya_quota_rejections_total (Q13,
        # src/metrics.py) -- QuotaExhausted has existed since P1 5b with no
        # caller on the request path to raise it.
        record_quota_rejection(model)
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
    breaker.record(estimated_cost_usd)
    return stream
