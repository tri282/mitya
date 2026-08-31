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


# ---------------------------------------------------------------------------
# Admission control (v2 P5 step 6, charter A12(3), D2)
# ---------------------------------------------------------------------------


class AtCapacity(LimitError):
    """The request was not admitted. Nothing was queued and nothing was spent.

    `reason` is `"tenant"` (this tenant already holds its maximum in-flight
    requests) or `"total"` (the deployment is full). The two are separate
    because they are the two halves of the starvation question P5 has to
    answer: a `"tenant"` rejection is the noisy neighbour being held back, a
    `"total"` rejection is somebody else losing a request to it.
    """

    def __init__(self, message: str, reason: str, retry_after: int = 1):
        super().__init__(message, status=429)
        self.reason = reason
        self.retry_after = retry_after


class TenantConcurrency:
    """Bounded in-flight requests, per tenant and in total, over a SINGLE worker.

    This is what bounds the queue P4 measured as unbounded. R98/R99: the system
    never rejected anything at any concurrency level on either arm -- it queued
    silently behind the retrieval lock, reaching 62 s p99 with a 100% success
    rate. `knee_concurrency = 1` on both arms, so concurrency past the knee buys
    exactly zero throughput and costs proportional latency; every request past
    it is pure queue. An honest 429 is strictly better than a 62-second 200.

    **Rejects, never waits.** There is deliberately no semaphore and no `await`
    in the acquire path: a limiter that sleeps until capacity frees is how an
    unbounded queue is spelled, and it is exactly what `RateLimiter.acquire_async`
    does for a provider quota (correct there, wrong here -- the same distinction
    `src.auth`'s login limiter records). Check-and-increment is atomic without a
    lock because the acquire path contains no await point and this is one event
    loop in one worker -- which A12(3) makes an assumption with a stated reason,
    not an accident.

    It does NOT replace `app.state.sem`. That lock is mutual exclusion over
    non-re-entrant retrieval components (D7/R100/R101 narrowed it to exactly
    that); this is admission control over whole requests, held for the entire
    call including the SSE body. Different questions, different scopes.
    """

    def __init__(self, per_tenant: int, total: int, reserve: int = 0,
                 window_seconds: float = 60.0, tolerance: float = 1.25,
                 clock=time.monotonic):
        self.per_tenant = per_tenant
        self.total = total
        # Least-served fairness on the GLOBAL slots (P5 G-P10, R117/R118).
        # `reserve` is how many of the last slots are withheld from a tenant
        # that is already ahead; 0 disables the rule and restores plain FCFS.
        self.reserve = reserve
        self.window_seconds = window_seconds
        self.tolerance = tolerance
        self._clock = clock
        self._inflight: dict[str, int] = {}
        self._total = 0
        self._served: dict[str, list[float]] = {}

    @property
    def inflight_total(self) -> int:
        return self._total

    def inflight_for(self, tenant: str) -> int:
        return self._inflight.get(tenant, 0)

    def acquire(self, tenant: str) -> None:
        """Take a slot or raise `AtCapacity`. Pair with `release()` in a finally.

        The per-tenant check runs FIRST so attribution is right when both caps
        are hit at once: a tenant at its own limit is over its own limit, and
        reporting that as "the box is full" would hide the noisy neighbour
        inside everyone else's rejection count.
        """
        if self._inflight.get(tenant, 0) >= self.per_tenant:
            raise AtCapacity(
                f"tenant {tenant} already has {self.per_tenant} requests in flight",
                reason="tenant",
            )
        if self._total >= self.total:
            raise AtCapacity(
                f"{self.total} requests already in flight", reason="total",
            )
        now = self._clock()
        # Prune on EVERY admission, not only when the fairness rule is
        # consulted. `_ahead_of_share` returns early whenever there is room or
        # `reserve` is 0, so evicting only from inside it left the history
        # growing without bound on an uncontended gate -- the same unbounded-key
        # problem `release()` avoids, reintroduced one function away.
        self._evict(now)
        if self._ahead_of_share(tenant, now):
            raise AtCapacity(
                f"tenant {tenant} is ahead of its share; the last "
                f"{self.reserve} slots are held for less-served tenants",
                reason="fairness",
            )
        self._served.setdefault(tenant, []).append(now)
        self._inflight[tenant] = self._inflight.get(tenant, 0) + 1
        self._total += 1

    def _ahead_of_share(self, tenant: str, now: float) -> bool:
        """Should this tenant yield one of the last free slots?

        **Least-served, decided at arrival, without a queue** (G-P10). R118
        measured the global cap distributing by arrival time: five tenants
        offering identical load received shares from 0.273 to 0.091, and in the
        noisy-neighbour run two LIGHT tenants were rejected eight times each
        while two others at the same offered load were rejected none. FCFS on a
        shared cap is arbitrary under contention, and arbitrary is not fair.

        R117 is the other half. The token-pressure fair-share guard could never
        fire, because admission caps in-flight work at ~3% of the pressure that
        guard needed -- the offered load that would raise pressure is exactly
        what admission refuses. Fairness therefore belongs HERE, at the only
        place where contention is actually observable, and the second mechanism
        is retired rather than left green-tested and unreachable.

        Deliberately still reject-never-wait. Ordering waiters by served count
        would be the textbook least-served scheduler and it would reintroduce
        the queue P4 measured at 62 s p99. Instead the scarce tail of the pool
        is simply withheld from whoever is already ahead: the last `reserve`
        slots are available only to tenants at or below the mean served count
        in the window. Under contention that hands the next slot to a
        less-served tenant without anyone waiting for it.
        """
        if self.reserve <= 0:
            return False
        if (self.total - self._total) > self.reserve:
            return False  # plenty of room; fairness is not the binding question
        active = set(self._served) | {tenant}
        if len(active) < 2:
            return False  # nobody to be fair to
        counts = {t: len(self._served.get(t, ())) for t in active}
        mean = sum(counts.values()) / len(counts)
        return counts[tenant] > mean * self.tolerance

    def _evict(self, now: float) -> None:
        """Drop served timestamps outside the window, and tenants with none
        left -- same unbounded-key argument as `release()` below."""
        cutoff = now - self.window_seconds
        for t in list(self._served):
            kept = [ts for ts in self._served[t] if ts >= cutoff]
            if kept:
                self._served[t] = kept
            else:
                del self._served[t]

    def release(self, tenant: str) -> None:
        remaining = self._inflight.get(tenant, 0) - 1
        if remaining > 0:
            self._inflight[tenant] = remaining
        else:
            # Drop the key rather than leaving a 0 behind: the dict would
            # otherwise grow one entry per tenant that has ever connected and
            # never shrink, which is the same unbounded-key problem the metric
            # label rule exists for.
            self._inflight.pop(tenant, None)
        self._total = max(0, self._total - 1)


class ConcurrencyMiddleware:
    """Pure-ASGI admission control on the expensive paths (P5 step 6).

    Plain ASGI, and that is the whole reason it is a middleware rather than a
    context manager inside `chat()`. The slot must be held for the entire call
    INCLUDING the streamed SSE body -- an in-flight request is in flight until
    the client has its last token -- and `chat()` returns as soon as the
    StreamingResponse is constructed. Wrapping `await self.app(...)` in one
    try/finally gives exactly one acquire and one release covering every path:
    the cache-hit stream, the retrieval-only stream, every pre-stream raise, a
    mid-stream failure, and a client disconnect. `test_chat_semaphore.py` exists
    because getting that right by hand across six exits is how a slot leaks.

    Must be installed INSIDE `RequestIdMiddleware` (i.e. added to the app
    BEFORE it, since Starlette runs the last-added middleware outermost): it
    reads the tenant that middleware resolved out of `scope["state"]`.

    Scoped to `paths` rather than every route on purpose -- `/health`,
    `/ready` and `/metrics` are the operator plane and must answer while the
    serving plane is full, which is precisely when an operator is looking.
    """

    def __init__(self, app, gate: TenantConcurrency, paths: tuple[str, ...] = ("/chat",)):
        self.app = app
        self.gate = gate
        self.paths = paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in self.paths:
            await self.app(scope, receive, send)
            return

        from src.config import Config
        from src.metrics import record_admission_rejection

        tenant = (scope.get("state") or {}).get("tenant") or Config.DEFAULT_TENANT
        try:
            self.gate.acquire(tenant)
        except AtCapacity as exc:
            record_admission_rejection(tenant, exc.reason)
            logger.warning(
                "admission: rejected %s (tenant=%s, reason=%s, inflight=%d/%d)",
                scope.get("path"), tenant, exc.reason, self.gate.inflight_total,
                self.gate.total,
            )
            await _send_429(send, str(exc), exc.retry_after)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self.gate.release(tenant)


async def _send_429(send, detail: str, retry_after: int) -> None:
    """A FastAPI-shaped `{"detail": ...}` body, hand-rolled because there is no
    `Request`/`Response` at this layer -- the SPA and the harness both parse
    that one shape and must not need a second error contract for this path."""
    import json

    body = json.dumps({"detail": detail}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 429,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"retry-after", str(retry_after).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})
