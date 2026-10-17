# test/test_tenant_concurrency.py
"""v2 P5 step 6 verify: bounded per-tenant concurrency and the bounded queue
(`src/limits.py`'s TenantConcurrency / ConcurrencyMiddleware).

What this is for. P4 measured the serving path as an UNBOUNDED queue (R99):
`local_429_rate = 0.00` at every concurrency level on both arms, p99 reaching
62 s, 100% success. The system never rejected anything -- it queued silently.
Charter A12(3) makes the replacement bounded per-tenant concurrency over a
single worker, and D2/CP4 ask for honest 429s rather than an async pile.

The two properties that matter, and both are here:

  * the bound REJECTS rather than waits -- there is no await in the acquire
    path, so a full deployment answers immediately instead of enqueuing;
  * the slot is released on EVERY exit, including a streamed body, an
    exception, and a client disconnect. `test_chat_semaphore.py` exists
    because that is the half people get wrong, and a leaked slot is a
    permanent capacity loss that only shows up as a deadlock later.

Plain-assert script, not pytest. Offline: the middleware is driven as plain
ASGI with hand-built scope dicts and a stub downstream app -- no server, no
Qdrant, no network, matching test_observability.py's posture.
"""

import asyncio
import sys

sys.path.insert(0, ".")

from src.config import Config  # noqa: E402
from src.limits import AtCapacity, ConcurrencyMiddleware, TenantConcurrency  # noqa: E402


def _scope(path="/chat", tenant="tenant-a"):
    return {"type": "http", "path": path, "state": {"tenant": tenant}, "headers": []}


class _Downstream:
    """Stub ASGI app. `gate` lets a test observe how many calls are inside it
    at once -- the only thing that actually proves a bound bounds anything."""

    def __init__(self, hold: asyncio.Event | None = None, raises: Exception | None = None):
        self.hold = hold
        self.raises = raises
        self.concurrent = 0
        self.peak = 0
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        try:
            if self.raises is not None:
                raise self.raises
            if self.hold is not None:
                await self.hold.wait()
        finally:
            self.concurrent -= 1


def _collect_send():
    sent = []

    async def send(message):
        sent.append(message)

    return sent, send


async def _noop_receive():
    return {"type": "http.request"}


# --- the gate itself --------------------------------------------------------


def test_a_tenant_is_held_to_its_own_bound() -> None:
    gate = TenantConcurrency(per_tenant=2, total=6, reserve=0)
    gate.acquire("tenant-a")
    gate.acquire("tenant-a")
    try:
        gate.acquire("tenant-a")
    except AtCapacity as exc:
        assert exc.reason == "tenant", exc.reason
        assert exc.status == 429, exc.status
    else:
        raise AssertionError("a tenant went past its per-tenant bound")


def test_one_tenant_at_its_bound_does_not_block_another() -> None:
    """The point of a PER-TENANT bound rather than a single global one: a noisy
    neighbour must not be able to consume the whole deployment."""
    gate = TenantConcurrency(per_tenant=2, total=6, reserve=0)
    gate.acquire("tenant-a")
    gate.acquire("tenant-a")
    gate.acquire("tenant-b")  # must not raise
    assert gate.inflight_total == 3


def test_the_deployment_total_is_bounded_too() -> None:
    """This is the queue bound. Without it, five tenants x per_tenant would be
    the real ceiling and the pile P4 measured would just be smaller."""
    gate = TenantConcurrency(per_tenant=2, total=4, reserve=0)
    for tenant in ("a", "b"):
        gate.acquire(tenant)
        gate.acquire(tenant)
    try:
        gate.acquire("c")
    except AtCapacity as exc:
        assert exc.reason == "total", exc.reason
    else:
        raise AssertionError("the deployment admitted past its total bound")


def test_the_two_rejection_reasons_are_attributed_correctly() -> None:
    """A tenant at its own cap when the box is ALSO full must be reported as
    "tenant", not "total". Otherwise the noisy neighbour's own rejections hide
    inside everyone else's count and the starvation number is unreadable."""
    gate = TenantConcurrency(per_tenant=2, total=2, reserve=0)
    gate.acquire("tenant-a")
    gate.acquire("tenant-a")
    try:
        gate.acquire("tenant-a")
    except AtCapacity as exc:
        assert exc.reason == "tenant"
    try:
        gate.acquire("tenant-b")
    except AtCapacity as exc:
        assert exc.reason == "total"


def test_release_frees_the_slot_and_does_not_leave_a_key_behind() -> None:
    gate = TenantConcurrency(per_tenant=1, total=1, reserve=0)
    gate.acquire("tenant-a")
    gate.release("tenant-a")
    assert gate.inflight_total == 0
    assert gate.inflight_for("tenant-a") == 0
    assert gate._inflight == {}, "a zero entry was left behind -- unbounded key growth"
    gate.acquire("tenant-a")  # the slot really did come back


# --- the middleware ---------------------------------------------------------


def test_the_middleware_rejects_with_429_and_retry_after() -> None:
    gate = TenantConcurrency(per_tenant=1, total=1, reserve=0)
    gate.acquire("tenant-a")  # deployment is full before the request arrives
    downstream = _Downstream()
    mw = ConcurrencyMiddleware(downstream, gate=gate)
    sent, send = _collect_send()
    asyncio.run(mw(_scope(), _noop_receive, send))
    assert sent[0]["status"] == 429, sent[0]["status"]
    headers = dict(sent[0]["headers"])
    assert b"retry-after" in headers
    assert b"detail" in sent[1]["body"]
    assert downstream.calls == 0, "a rejected request still reached the app"


def test_a_rejection_is_immediate_and_never_queues() -> None:
    """The distinction from a semaphore, asserted rather than argued: a bound
    that waits is an unbounded queue with extra steps, and that is precisely
    what P4 measured. Driven by holding the one slot open forever and checking
    the second request completes anyway."""
    gate = TenantConcurrency(per_tenant=1, total=1, reserve=0)
    hold = asyncio.Event()
    downstream = _Downstream(hold=hold)
    mw = ConcurrencyMiddleware(downstream, gate=gate)

    async def scenario():
        sent_a, send_a = _collect_send()
        first = asyncio.create_task(mw(_scope(tenant="tenant-a"), _noop_receive, send_a))
        await asyncio.sleep(0)  # let it get inside the downstream app
        sent_b, send_b = _collect_send()
        # No timeout needed: if this awaited a slot it would never return, and
        # the test would hang rather than fail -- so it is wrapped.
        await asyncio.wait_for(
            mw(_scope(tenant="tenant-b"), _noop_receive, send_b), timeout=2.0,
        )
        assert sent_b[0]["status"] == 429
        hold.set()
        await first
        return downstream.peak

    peak = asyncio.run(scenario())
    assert peak == 1, f"the bound admitted {peak} concurrent requests past its total of 1"


def test_the_slot_is_released_when_the_app_raises() -> None:
    """The leak that only shows up as a deadlock on a later request. Every
    /chat error path -- 400, 413, 422, 429, 502, 503, 504 -- exits this way."""
    gate = TenantConcurrency(per_tenant=1, total=1, reserve=0)
    downstream = _Downstream(raises=RuntimeError("boom"))
    mw = ConcurrencyMiddleware(downstream, gate=gate)
    _, send = _collect_send()
    try:
        asyncio.run(mw(_scope(), _noop_receive, send))
    except RuntimeError:
        pass
    else:
        raise AssertionError("the middleware swallowed a downstream exception")
    assert gate.inflight_total == 0, "the slot leaked on an exception path"


def test_the_slot_is_held_for_the_whole_call_then_released() -> None:
    """Held across the streamed body -- the reason this is a middleware and not
    a context manager inside chat(), which returns as soon as the
    StreamingResponse object is constructed."""
    gate = TenantConcurrency(per_tenant=1, total=1, reserve=0)
    hold = asyncio.Event()
    downstream = _Downstream(hold=hold)
    mw = ConcurrencyMiddleware(downstream, gate=gate)

    async def scenario():
        _, send = _collect_send()
        task = asyncio.create_task(mw(_scope(), _noop_receive, send))
        await asyncio.sleep(0)
        inflight_during = gate.inflight_total
        hold.set()
        await task
        return inflight_during, gate.inflight_total

    during, after = asyncio.run(scenario())
    assert during == 1, "the slot was not held while the body was still streaming"
    assert after == 0, "the slot was not released when the call finished"


def test_the_operator_plane_is_never_admission_controlled() -> None:
    """/health, /ready and /metrics must answer while the serving plane is
    full -- which is exactly when an operator is looking at them."""
    gate = TenantConcurrency(per_tenant=1, total=1, reserve=0)
    gate.acquire("tenant-a")
    downstream = _Downstream()
    mw = ConcurrencyMiddleware(downstream, gate=gate)
    for path in ("/health", "/ready", "/metrics"):
        _, send = _collect_send()
        asyncio.run(mw(_scope(path=path), _noop_receive, send))
    assert downstream.calls == 3, "an operator-plane route was throttled"


def test_the_configured_bounds_are_the_ones_the_plan_sized() -> None:
    """The numbers are load-bearing (see src/config.py's derivation from R98/R99
    and the stated 5-tenant maximum), so a silent change to either is a change
    to the queue bound P5 reports."""
    assert Config.CHAT_MAX_INFLIGHT_PER_TENANT == 2
    assert Config.CHAT_MAX_INFLIGHT_TOTAL == 6
    assert Config.CHAT_MAX_INFLIGHT_TOTAL < 5 * Config.CHAT_MAX_INFLIGHT_PER_TENANT, (
        "the total must bind before 5 tenants x per-tenant, or 'requests other "
        "tenants lose to a noisy one' is zero by construction"
    )


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
