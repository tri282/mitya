"""Transport retry only (v2 P2 step 8(a), schedule Batch B).

TRANSPORT failures -- connect/timeout/5xx-class errors a real provider
adapter raises -- get a bounded, jittered, budget-aware retry here. The
QUALITY retry (the existing empty-completion retry, tuned to the local q4
7B's specific failure mode) is step 8(b) and C4 may delete it outright;
building a policy a checkpoint might delete is the waste the schedule exists
to avoid ("Why one batch" in schedule.md's Batch B section). Keeping the two
retry kinds in two functions, never one, is also what stops them from
silently doubling up on one failure (phase-2-gateway.md step 8's explicit
requirement).
"""

from __future__ import annotations

import asyncio
import logging
import random

from src.gateway.contract import GenerationStream, Provider, Usage
from src.metrics import record_empty_completion

logger = logging.getLogger(__name__)


class TransportError(RuntimeError):
    """A transport-level failure worth retrying: connect, timeout, 5xx, 429.

    `retry_after` carries the provider's own instruction when it sends one, in
    seconds. Groq returns `retry-after` on every 429; Google returns no
    rate-limit headers at all (R53's survey), so it stays None there and the
    caller falls back to its own backoff.

    Honouring it matters more than it looks. Groq meters with a **leaky
    bucket** that refills continuously (`x-ratelimit-reset-tokens: 547ms`),
    while `src.limits.RateLimiter` is a **60-second sliding window**. The
    window admits a burst that fits under the per-minute total; the bucket
    refuses it because the tokens are not available at that instant. No amount
    of reserving the right token count fixes that -- the two disagree about
    *when*, not *how much* -- so the provider's own wait is the only correct
    answer available.
    """

    def __init__(self, *args, retry_after: float | None = None) -> None:
        super().__init__(*args)
        self.retry_after = retry_after


async def retry_transport(
    provider: Provider,
    messages: list[dict],
    tier: str,
    tenant: str,
    budget: float | None,
    *,
    max_attempts: int = 3,
    base_delay: float = 0.25,
    max_delay: float = 4.0,
    cost_per_attempt_usd: float = 0.0,
    remaining_budget_usd: float | None = None,
) -> GenerationStream:
    """Retry `provider.generate()` on `TransportError`, bounded and jittered.

    Budget-aware (phase-2-gateway.md step 4/8: "retries are a cost multiplier
    here, so they are bounded and budget-aware"): a next attempt whose
    cumulative cost would exceed `remaining_budget_usd` is refused rather
    than taken, so a retry storm cannot silently multiply the bill past the
    cap the budget guard (`src/gateway/guard.py`) already enforced once. This
    check is deliberately BEFORE the call, same "before spending, not
    reconciled after" posture as step 4 (charter IV).

    Jitter is full jitter over `[0.5x, 1.5x)` of an exponential backoff, so N
    concurrent retries do not all wake and retry in lockstep.
    """
    attempt = 0
    while True:
        attempt += 1
        projected = attempt * cost_per_attempt_usd
        if remaining_budget_usd is not None and projected > remaining_budget_usd:
            raise TransportError(
                f"retry attempt {attempt} (projected {projected} USD) would exceed "
                f"remaining budget {remaining_budget_usd}"
            )
        try:
            return await provider.generate(messages, tier, tenant, budget)
        except TransportError:
            if attempt >= max_attempts:
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay *= 0.5 + random.random()
            logger.warning(
                "gateway: transport retry %d/%d in %.2fs (tenant=%s)",
                attempt, max_attempts, delay, tenant,
            )
            await asyncio.sleep(delay)


def record_if_empty_completion(route: str, usage: Usage) -> None:
    """Zero-token completion counter for the hosted path (v2 P2 step 8(a)).

    Reuses the SAME instrument the local llama-server path already
    increments (`record_empty_completion`, `src/api/app.py`'s
    `STAGE_GENERATE_FIRST` handling on a `StopAsyncIteration`) rather than a
    second, hosted-only counter -- one closed-set counter across every arm is
    what lets P2's per-arm sweep (11a) compare local vs. hosted zero-token
    rates on the same series. Call after a `GenerationStream` has been fully
    drained; before that, `usage.completion_tokens` is unfilled (`None`), not
    zero, and `is None` must not be read as "empty".
    """
    if usage.completion_tokens == 0:
        record_empty_completion(route)
