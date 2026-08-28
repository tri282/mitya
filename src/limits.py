# src/limits.py
"""Rate limiting, not RAG-specific (P1 5b, extraction half).

Moved out of `src/rag/teacher.py` because P2's gateway needs this on the
request path, not only on the offline artifact-build path teacher.py serves.

`LimitError` is the base that used to live in teacher.py as `TeacherError`
(re-exported there under that name so every existing caller's `except
TeacherError` arm still resolves to exactly the class it resolves to today --
see teacher.py's module docstring for the compatibility shim). It moved down
rather than teacher.py's exception moving up, because a shared limiter module
cannot import teacher.py to raise its exception.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Callable

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0


class LimitError(RuntimeError):
    """Call failed and should not be retried by the caller."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class QuotaExhausted(LimitError):
    """Daily request budget spent. Re-run tomorrow; builders resume from disk.

    Raised by the local counter (which only knows about requests *this process*
    made) and by callers when the server keeps returning 429 -- quota spent by
    an earlier run is invisible to us until the API says so.
    """


def estimate_tokens(text: str) -> int:
    """Rough char/4 heuristic, only used to reserve TPM budget *before* a call.
    Actual usage is trued up from usage_metadata afterwards."""
    return max(1, len(text) // 4)


class RateLimiter:
    """Sliding-window limiter over requests/min, tokens/min and requests/day.

    rpm/tpm/rpd are required, not defaulted to any one caller's quota row --
    a *shared* module whose no-argument default silently meant "the teacher's
    quota" was the exact defect src/config.py's judge_limits() comment records
    having already been hit once: before 2026-08-21 a judge pointed at a
    different model ran against the teacher's quota row. Every caller now
    states its own limits explicitly.

    clock/sleep are injectable so the window logic is testable without burning
    real time or real quota.
    """

    def __init__(
        self,
        rpm: int,
        tpm: int,
        rpd: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rpm, self.tpm, self.rpd = rpm, tpm, rpd
        self._clock, self._sleep = clock, sleep
        self._window: deque[list[float]] = deque()  # [timestamp, tokens]
        self.requests_today = 0

    def _evict(self, now: float) -> None:
        while self._window and now - self._window[0][0] >= WINDOW_SECONDS:
            self._window.popleft()

    def _tokens_in_window(self) -> int:
        return int(sum(entry[1] for entry in self._window))

    def _check_capacity(self, tokens: int) -> None:
        """The two instant-fail checks, shared by both the sync and async
        acquire -- neither is a window wait, so there is nothing to gain by
        sleeping/awaiting first."""
        if tokens > self.tpm:
            raise LimitError(
                f"single request needs ~{tokens} tokens, over the {self.tpm} tokens/min "
                f"limit; it can never be scheduled"
            )
        if self.requests_today >= self.rpd:
            raise QuotaExhausted(
                f"daily limit of {self.rpd} requests reached; re-run after quota "
                f"resets -- completed work is already on disk and will be skipped"
            )

    def _try_reserve(self, tokens: int) -> float | None:
        """One attempt to fit `tokens` in the window. Reserves and returns
        `None` on success (window/`requests_today` updated in place -- this
        is the ONE place either is mutated, so the sync and async callers
        below share state by construction, not by convention); otherwise
        returns the seconds to wait before retrying.

        Factored out of what used to be `acquire()`'s body so `acquire_async`
        (schedule Batch B step 4) is a second caller of the same window logic
        instead of a second copy of it -- two copies of an eviction/window
        rule are how they drift apart."""
        now = self._clock()
        self._evict(now)
        if len(self._window) < self.rpm and self._tokens_in_window() + tokens <= self.tpm:
            self._window.append([now, float(tokens)])
            self.requests_today += 1
            return None
        # Oldest entry leaving the window is the soonest anything frees up.
        return WINDOW_SECONDS - (now - self._window[0][0]) + 0.05

    def acquire(self, tokens: int) -> None:
        """Block until a request of `tokens` fits inside every limit."""
        self._check_capacity(tokens)
        while True:
            wait = self._try_reserve(tokens)
            if wait is None:
                return
            logger.debug(
                "rate limit: %d/%d req, %d/%d tok in window; sleeping %.1fs",
                len(self._window), self.rpm, self._tokens_in_window(), self.tpm, wait,
            )
            self._sleep(max(wait, 0.05))

    async def acquire_async(self, tokens: int) -> None:
        """Non-blocking twin of `acquire()` (v2 P2 step 4). `RateLimiter` was
        sync-only through P1, correct for the offline teacher/judge/eval
        callers -- wrong for `/chat`, where `acquire()`'s `time.sleep` would
        block the event loop for every OTHER concurrent request while one
        caller waits out its own window.

        Shares `self._window`/`self.requests_today` with `acquire()` through
        `_try_reserve` -- the same instance, not a second limiter -- so an
        offline batch run and a live request in one process cannot each
        believe they own the full rpm/tpm/rpd budget. (The *per-process*
        split this does not close -- two separate processes still keep
        separate windows -- is R26's tracked, separate limitation.)
        """
        self._check_capacity(tokens)
        while True:
            wait = self._try_reserve(tokens)
            if wait is None:
                return
            logger.debug(
                "rate limit (async): %d/%d req, %d/%d tok in window; sleeping %.1fs",
                len(self._window), self.rpm, self._tokens_in_window(), self.tpm, wait,
            )
            await asyncio.sleep(max(wait, 0.05))

    def true_up(self, actual_tokens: int) -> None:
        """Replace the last estimate with the real count from usage_metadata."""
        if self._window:
            self._window[-1][1] = float(actual_tokens)

    def remaining_today(self) -> int:
        return max(0, self.rpd - self.requests_today)
