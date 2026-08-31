"""Least-served fairness in the admission gate (P5 G-P10, R117/R118).

Replaces `test_pool_fairness.py`, deleted with the mechanism it tested. That
suite passed nine assertions about a guard R117 then measured firing zero
times in production, because admission control caps in-flight work at ~3% of
the token pressure the guard required. The lesson is in the shape of these
tests: every one below drives `acquire()`, the same entry point the request
path uses, at contention levels the gate can actually reach.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.limits import AtCapacity, TenantConcurrency

PASSED = []


def check(name, fn):
    fn()
    PASSED.append(name)
    print(f"  ok  {name}")


class FakeClock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


def _gate(**kw):
    clock = FakeClock()
    defaults = dict(per_tenant=2, total=6, reserve=2, window_seconds=60.0,
                    tolerance=1.25, clock=clock)
    defaults.update(kw)
    return TenantConcurrency(**defaults), clock


def test_fairness_is_off_while_there_is_room():
    """The rule must not fire when slots are plentiful -- it decides who yields
    the SCARCE tail, not who gets served at all."""
    gate, _ = _gate()
    for _ in range(2):
        gate.acquire("tenant-a")
    gate.release("tenant-a")
    gate.release("tenant-a")
    # tenant-a is now the most-served, but 6 of 6 slots are free.
    gate.acquire("tenant-a")   # must not raise
    assert gate.inflight_for("tenant-a") == 1


def test_the_ahead_tenant_yields_the_last_slots():
    """R118's actual defect: with the pool nearly full, FCFS handed the last
    slot to whoever arrived first. Now it is withheld from whoever is ahead."""
    gate, _ = _gate()
    for _ in range(6):                      # tenant-a builds a served history
        gate.acquire("tenant-a"); gate.release("tenant-a")
    gate.acquire("tenant-b")                # b is behind and gets in
    for t in ("tenant-c", "tenant-d", "tenant-e"):
        gate.acquire(t)
    # 4 in flight, 2 free == reserve, so the rule is live.
    try:
        gate.acquire("tenant-a")
        raise AssertionError("the ahead tenant must yield the reserved tail")
    except AtCapacity as exc:
        assert exc.reason == "fairness", exc.reason


def test_a_behind_tenant_still_gets_the_reserved_slots():
    """The other half, and the one that makes it fairness rather than a cap:
    the withheld slots must remain AVAILABLE to whoever is behind."""
    gate, _ = _gate()
    for _ in range(6):
        gate.acquire("tenant-a"); gate.release("tenant-a")
    for t in ("tenant-b", "tenant-c", "tenant-d", "tenant-e"):
        gate.acquire(t)
    gate.acquire("tenant-b")   # behind the mean; must be admitted
    assert gate.inflight_total == 5


def test_one_tenant_alone_is_never_rejected_for_fairness():
    """R110's edge, in its new home: a single tenant's fair share is
    everything, so this rule can only restate 'the box is full' -- which is
    `reason=total`, with a different meaning."""
    gate, _ = _gate()
    for _ in range(20):
        gate.acquire("tenant-a"); gate.release("tenant-a")
    for _ in range(2):
        gate.acquire("tenant-a")
    try:
        gate.acquire("tenant-a")
        raise AssertionError("expected the per-tenant cap")
    except AtCapacity as exc:
        assert exc.reason == "tenant", exc.reason


def test_history_ages_out_of_the_window():
    """Fairness is over a WINDOW, not for all time: a tenant that was heavy an
    hour ago must not be paying for it now."""
    gate, clock = _gate()
    for _ in range(6):
        gate.acquire("tenant-a"); gate.release("tenant-a")
    for t in ("tenant-b", "tenant-c", "tenant-d", "tenant-e"):
        gate.acquire(t)
    clock.advance(61.0)
    gate.acquire("tenant-a")   # history expired; no longer ahead
    assert gate.inflight_for("tenant-a") == 1


def test_per_tenant_cap_still_takes_precedence():
    """Attribution: a tenant at its own limit is over its OWN limit, and must
    not be told the box is full or that it is being throttled for fairness."""
    gate, _ = _gate()
    gate.acquire("tenant-a")
    gate.acquire("tenant-a")
    try:
        gate.acquire("tenant-a")
        raise AssertionError("expected AtCapacity")
    except AtCapacity as exc:
        assert exc.reason == "tenant", exc.reason


def test_total_cap_still_rejects_when_genuinely_full():
    gate, _ = _gate(reserve=0)
    for t in ("tenant-a", "tenant-b", "tenant-c"):
        gate.acquire(t); gate.acquire(t)
    try:
        gate.acquire("tenant-d")
        raise AssertionError("expected AtCapacity")
    except AtCapacity as exc:
        assert exc.reason == "total", exc.reason


def test_reserve_zero_restores_fcfs():
    """The knob has to be able to turn the rule OFF, or the pre-G-P10
    behaviour is not reproducible for comparison."""
    gate, _ = _gate(reserve=0)
    for _ in range(6):
        gate.acquire("tenant-a"); gate.release("tenant-a")
    for t in ("tenant-b", "tenant-c", "tenant-d", "tenant-e"):
        gate.acquire(t)
    gate.acquire("tenant-a")   # ahead, but the rule is disabled
    assert gate.inflight_for("tenant-a") == 1


def test_released_slots_do_not_erase_served_history():
    """The count that decides fairness is SERVED, not in-flight. A tenant that
    churns fast would otherwise look permanently behind."""
    gate, _ = _gate()
    for _ in range(6):
        gate.acquire("tenant-a"); gate.release("tenant-a")
    assert gate.inflight_for("tenant-a") == 0
    for t in ("tenant-b", "tenant-c", "tenant-d", "tenant-e"):
        gate.acquire(t)
    try:
        gate.acquire("tenant-a")
        raise AssertionError("served history must survive release()")
    except AtCapacity as exc:
        assert exc.reason == "fairness", exc.reason


def test_served_history_does_not_grow_without_bound():
    """Same unbounded-key argument as `release()` and the metric label rule."""
    gate, clock = _gate()
    for i in range(50):
        gate.acquire(f"tenant-{i % 5}"); gate.release(f"tenant-{i % 5}")
    clock.advance(61.0)
    gate.acquire("tenant-0"); gate.release("tenant-0")
    assert len(gate._served) == 1, gate._served


for name, fn in sorted(list(globals().items())):
    if name.startswith("test_"):
        check(name, fn)
print(f"\n{len(PASSED)} passed")
