"""Transport failure vocabulary (v2 P2 step 8(a), schedule Batch B).

`TransportError` -- what a provider adapter raises for a connect/timeout/
5xx/429 failure -- and the hosted zero-token-completion counter. Both are
live in the request path (`src/api/app.py`, `src/rag/generate.py`).

**This module no longer owns a retry loop.** `retry_transport()` lived here
and had zero request-path call sites (R88): P2 shipped the gateway's adapters
into the request path and left its policy behind, and the two loops had
already diverged on the one field R68 added -- `generate.py` honours
`exc.retry_after`, `retry_transport()` never read it. P4 Batch A0 item 3
deleted it and blessed the loop in `src/rag/generate.py` (the one that
absorbed 242 real Groq 429s) as the implementation. Its only unique feature,
a linear `attempt * cost_per_attempt_usd` budget projection, was already
superseded per attempt by `guarded_generate()` (`src/gateway/guard.py`),
which re-runs request-size limits, the rate limiter, the budget check and the
spend-rate breaker before EVERY attempt -- so the deletion was a straight
subtraction with nothing to migrate.

The two retry kinds (transport, and the empty-completion QUALITY retry) stay
distinct and share one attempt budget, `Config.MAX_GENERATION_ATTEMPTS`;
`test/test_retry_classes.py` is where that property is asserted.
"""

from __future__ import annotations

from src.gateway.contract import Usage
from src.metrics import record_empty_completion


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

    def __init__(
        self, *args, retry_after: float | None = None, error_class: str | None = None,
    ) -> None:
        super().__init__(*args)
        self.retry_after = retry_after
        # Set by the generic OpenAI-dialect adapter (`ErrorClass` value); None from older adapters.
        self.error_class = error_class


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
