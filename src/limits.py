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

from src.config import Config

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


def quota_scope(model: str, provider: str | None) -> tuple[str, ...]:
    """The key a limiter's window belongs to: the (provider, model) pair.

    **Measured, not read off the docs.** Groq's rate-limit page says "rate
    limits apply at the organization level, not individual users", which reads
    like one shared bucket across models -- and an earlier version of this file
    keyed Groq on the provider alone because of it. That was wrong. The phrase
    answers *who shares a limit* (every user in the org), not *whether models
    share one pool*, and per-model limit numbers in that same table would be
    meaningless if they did.

    Settled on the wire 2026-08-28 by reading `x-ratelimit-remaining-requests`:
    one call to `gpt-oss-120b` left it at 999/1000; three calls to
    `gpt-oss-20b` decremented only that model's counter (999, 998, 997); a
    second `gpt-oss-120b` call returned **998**, not 996. Separate buckets.

    Returns a tuple rather than a string so a provider name can never collide
    with a model id that happens to equal it.
    """
    return (provider or "unknown", model)


class ScopedRateLimiter:
    """One limiter object holding a window per quota scope.

    Deliberately NOT the defect `RateLimiter`'s docstring warns about. That one
    was a *silent default* -- `RateLimiter()` with no arguments meaning "the
    teacher's quota", so a judge on a different model burned the teacher's rows.
    Here every window's limits come from that model's own `Config.limits_for()`
    row, explicitly, on first use. There is no default to be silently wrong
    about, which is what makes one shared object safe here.

    What it buys is bookkeeping, not a different accounting model: one place
    that owns every window, created from explicit rows, instead of callers each
    holding their own limiter and each having to remember which row it came
    from. Every provider measured so far meters per model (`quota_scope()`), so
    the windows are one-per-model.

    What it does NOT fix is **R26** -- the windows still live in one process, so
    N uvicorn workers hold N sets of them and the effective quota is N times
    what is configured. Unchanged by this class, still open, and it needs shared
    state (Redis or equivalent) rather than a better key.
    """

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self._limiters: dict[tuple[str, ...], RateLimiter] = {}
        self._clock, self._sleep = clock, sleep

    def limiter_for(self, model: str) -> RateLimiter:
        """The window for `model`'s quota scope, created on first use from that
        model's own limits row."""
        limits = Config.limits_for(model)
        scope = quota_scope(model, limits.get("provider"))
        limiter = self._limiters.get(scope)
        if limiter is None:
            limiter = RateLimiter(
                rpm=limits["rpm"],
                # A None tpm means "this provider publishes no token limit"
                # (see NO_TPM_LIMIT's comment in config_artifact.py) -- not
                # "zero tokens allowed", which would reject every request.
                tpm=limits.get("tpm") or Config.NO_TPM_LIMIT,
                rpd=limits["rpd"],
                clock=self._clock,
                sleep=self._sleep,
            )
            self._limiters[scope] = limiter
            logger.info(
                "rate limit: opened window for scope %s (model=%s, rpm=%s tpm=%s rpd=%s)",
                "/".join(scope), model, limits["rpm"], limits.get("tpm"), limits["rpd"],
            )
        return limiter

    async def acquire_async(self, model: str, tokens: int) -> None:
        await self.limiter_for(model).acquire_async(tokens)

    def true_up(self, model: str, actual_tokens: int) -> None:
        self.limiter_for(model).true_up(actual_tokens)

    def remaining_today(self, model: str) -> int:
        return self.limiter_for(model).remaining_today()
