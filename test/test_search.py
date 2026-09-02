# test/test_search.py
"""P7 Batch C verify: the mutation domain, the budget enforcer's known-answer
case, the cost-first funnel, and CP-7e's forced-failure fault test.

Runnable now -- no Qdrant, no provider, no gate call, no `CURRENT_POINTER`
write, no real `config/serving/` artifact write (`save_fn` is faked). All of
`src.search.run_search()`'s external machinery is injected
(`price_fn`/`gate_runner`/`save_fn`/`clock`), so CP-7e's fault test can force
a gate ERROR mid-search at $0.00 without opening anything.
"""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config_artifact import ServingConfig  # noqa: E402
from src.optimizer import ALLOWED  # noqa: E402
from src.search import (  # noqa: E402
    Budget,
    BudgetExceeded,
    EvolutionaryFamily,
    FIELD_DOMAIN,
    GridRandomFamily,
    SurrogateFamily,
    run_search,
)


# --- fakes: no Qdrant, no gate subprocess, no config/serving/ write ---------

class _Priced:
    def __init__(self, config_version: str, mean_usd: float, mean_prompt_tokens: float = 0.0):
        self.config_version = config_version
        self.mean_usd = mean_usd
        self.mean_prompt_tokens = mean_prompt_tokens


def _fake_save_fn():
    """Deterministic, content-free version stamps -- no config/serving/ write.
    A closure so each test gets its own counter/registry."""
    counter = [0]
    versions: dict[int, ServingConfig] = {}

    def save(cfg: ServingConfig) -> str:
        counter[0] += 1
        version = f"fake-v{counter[0]}"
        versions[counter[0]] = cfg
        return version

    return save


def _fake_price_fn(usd_by_version: dict[str, float], default: float = 1.0):
    """A price_fn whose USD is read from a dict the test controls -- the
    'mutate -> price -> funnel' order is exercised without touching
    src.p7_objective or Qdrant at all."""
    def price_fn(version: str) -> _Priced:
        return _Priced(version, usd_by_version.get(version, default))
    return price_fn


def _gate_report(disposition: str, cand_usd: float = 1.0) -> dict:
    return {
        "disposition": disposition,
        "layers": {
            "cost": [{"metric": "computed_cost_usd", "cand": cand_usd}],
            "quality_1a": [{"metric": "recall_at_5", "family": "primary", "verdict": "NOT-DETECTED"}],
        },
    }


def _clock_from(sequence):
    """A fake clock: pops timestamps off `sequence` (a list) each call, so
    wall-clock math is exact and test-controlled rather than real-timed."""
    it = iter(sequence)

    def clock():
        return next(it)
    return clock


# --- FIELD_DOMAIN / mutation --------------------------------------------------

def test_field_domain_covers_every_allowed_field() -> None:
    """D-5's failure mode one layer down: an ALLOWED field with no mutation
    domain would be unmutable by construction. `src.search` already asserts
    this at import time; restated here as a readable, standalone check."""
    assert set(FIELD_DOMAIN) == set(ALLOWED)


def test_evolutionary_and_grid_random_only_touch_allowlisted_fields() -> None:
    base = ServingConfig.from_env()
    for family in (EvolutionaryFamily(), GridRandomFamily(), SurrogateFamily()):
        import random
        rng = random.Random(0)
        for _ in range(20):
            point = family.next_point([], base, rng)
            assert point.field in ALLOWED, f"{family.name} proposed {point.field!r}, not allowlisted"


# --- budget enforcer (D-7c) ---------------------------------------------------

def test_budget_refuses_to_start_below_one_points_cost() -> None:
    """Schedule §5's standing known-answer case: a cap below the cost of a
    single point must refuse to start, not run one point and stop."""
    budget = Budget(max_wall_clock_s=10.0, max_points=5)
    try:
        budget.assert_affordable(per_point_wall_clock_s=38.9)  # Batch A's measured gate-run cost
    except BudgetExceeded as exc:
        assert "refusing to start" in str(exc)
    else:
        raise AssertionError("a 10s cap accepted a 38.9s-per-point cost")


def test_budget_refuses_to_start_with_zero_points() -> None:
    budget = Budget(max_wall_clock_s=1000.0, max_points=0)
    try:
        budget.assert_affordable(per_point_wall_clock_s=1.0)
    except BudgetExceeded:
        pass
    else:
        raise AssertionError("a 0-point cap accepted a run")


def test_budget_rejects_an_unmeasured_cost() -> None:
    """CP-7d: 'no cap from a cost that was never measured' -- a non-positive
    per-point cost is not a measurement."""
    budget = Budget(max_wall_clock_s=1000.0, max_points=5)
    for bad in (0.0, -1.0):
        try:
            budget.assert_affordable(per_point_wall_clock_s=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted per_point_wall_clock_s={bad}")


def test_check_before_point_refuses_a_point_that_would_exceed_the_cap() -> None:
    budget = Budget(max_wall_clock_s=100.0, max_points=5)
    budget.check_before_point(points_spent=0, wall_clock_spent_s=0.0, per_point_wall_clock_s=38.9)
    try:
        budget.check_before_point(points_spent=0, wall_clock_spent_s=70.0, per_point_wall_clock_s=38.9)
    except BudgetExceeded:
        pass
    else:
        raise AssertionError("70s spent + 38.9s next point (108.9s) exceeded a 100s cap silently")


# --- run_search: the cost-first funnel (D-7d) --------------------------------

def test_only_cheaper_candidates_reach_the_gate() -> None:
    """D-7d's order: mutate -> price (free) -> only a candidate that actually
    got cheaper consumes a gate run. Every price here equals the baseline
    (not strictly cheaper), so the gate must never be called."""
    base = ServingConfig.from_env()
    gate_calls = []

    def gate_runner(version: str) -> dict:
        gate_calls.append(version)
        return _gate_report("does_not_block")

    rows = run_search(
        budget=Budget(max_wall_clock_s=1.0, max_points=10),
        price_fn=_fake_price_fn({}, default=1.0),  # baseline AND every candidate price at 1.0
        gate_runner=gate_runner,
        base_config=base,
        base_version="base-v0",
        per_point_wall_clock_s=0.5,
        family=EvolutionaryFamily(),
        seed=0,
        save_fn=_fake_save_fn(),
        clock=_clock_from([0.0] + [i * 0.1 for i in range(1, 50)]),
    )
    assert gate_calls == [], f"gate was called on a non-improving candidate: {gate_calls}"
    assert rows, "expected some screened-out rows in the trace"
    assert all(not r.passed_cost_first_funnel for r in rows)


def test_a_cheaper_candidate_reaches_the_gate_and_can_be_blocked() -> None:
    """D-1's named collision, reproduced directly: a candidate can rank first
    on the objective and still be blocked by the gate's ceiling."""
    base = ServingConfig.from_env()
    gate_calls = []

    def gate_runner(version: str) -> dict:
        gate_calls.append(version)
        return _gate_report("blocks", cand_usd=0.002)

    rows = run_search(
        budget=Budget(max_wall_clock_s=1000.0, max_points=1),
        price_fn=_fake_price_fn({"base-v0": 1.0}, default=0.5),  # candidates cheaper than baseline
        gate_runner=gate_runner,
        base_config=base,
        base_version="base-v0",
        per_point_wall_clock_s=1.0,
        family=EvolutionaryFamily(),
        seed=0,
        save_fn=_fake_save_fn(),
        clock=_clock_from([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]),
    )
    assert gate_calls, "expected the one cheaper candidate to reach the gate"
    blocked = [r for r in rows if r.passed_cost_first_funnel and r.disposition == "blocks"]
    assert blocked, "expected at least one candidate to be blocked"
    assert blocked[0].ranked_first_but_blocked
    assert "RANKS BETTER ON THE OBJECTIVE BUT BLOCKED" in blocked[0].divergence_line()


# --- CP-7e: does the cap hold under a FORCED gate failure mid-search? --------

def test_forced_gate_error_mid_search_stops_and_does_not_exceed_the_cap() -> None:
    """CP-7e's own words: 'the cap is tested by breaking the loop, not by
    watching it succeed.' A gate ERROR is injected on the first call that
    reaches the gate; the loop must stop immediately (fail closed, A7) and
    the point count must never exceed the cap it was given."""
    base = ServingConfig.from_env()
    calls = {"n": 0}

    def failing_gate_runner(version: str) -> dict:
        calls["n"] += 1
        raise RuntimeError("forced gate ERROR (CP-7e injected fault)")

    budget = Budget(max_wall_clock_s=1000.0, max_points=5)
    rows = run_search(
        budget=budget,
        price_fn=_fake_price_fn({"base-v0": 1.0}, default=0.1),  # candidates always "improve"
        gate_runner=failing_gate_runner,
        base_config=base,
        base_version="base-v0",
        per_point_wall_clock_s=1.0,
        family=EvolutionaryFamily(),
        seed=0,
        save_fn=_fake_save_fn(),
        clock=_clock_from([i * 0.1 for i in range(5000)]),
    )
    assert calls["n"] == 1, f"the loop must stop on the FIRST forced failure, called {calls['n']} times"
    error_rows = [r for r in rows if r.disposition == "error"]
    assert len(error_rows) == 1
    gate_reaching_rows = [r for r in rows if r.passed_cost_first_funnel]
    assert len(gate_reaching_rows) == 1, "the cap (5 points) must not be exceeded after a forced failure"


def test_forced_gate_block_mid_search_also_stops_within_the_point_cap() -> None:
    """The non-exception failure shape: every candidate is BLOCKED, never
    ERRORs. Must still respect the point cap exactly -- no over-run."""
    base = ServingConfig.from_env()

    def always_blocks(version: str) -> dict:
        return _gate_report("blocks", cand_usd=9.0)

    budget = Budget(max_wall_clock_s=1000.0, max_points=3)
    rows = run_search(
        budget=budget,
        price_fn=_fake_price_fn({"base-v0": 1.0}, default=0.1),
        gate_runner=always_blocks,
        base_config=base,
        base_version="base-v0",
        per_point_wall_clock_s=1.0,
        family=GridRandomFamily(),  # never accumulates -- exercises repeated re-pricing of `base`
        seed=1,
        save_fn=_fake_save_fn(),
        clock=_clock_from([i * 0.1 for i in range(5000)]),
    )
    gate_reaching = [r for r in rows if r.passed_cost_first_funnel]
    assert len(gate_reaching) == budget.max_points, (
        f"expected exactly {budget.max_points} gate runs, got {len(gate_reaching)}"
    )
    assert all(r.disposition == "blocks" for r in gate_reaching)


def test_forced_wall_clock_exhaustion_stops_before_the_next_point() -> None:
    """The cap is enforced BEFORE the call, never reconciled after (D-7c) --
    a wall-clock budget that cannot afford one more point stops the loop
    rather than starting (and possibly overrunning) it."""
    base = ServingConfig.from_env()
    calls = {"n": 0}

    def gate_runner(version: str) -> dict:
        calls["n"] += 1
        return _gate_report("does_not_block")

    # One point completes (t0=0.0, then 1.0/2.0/3.0/4.0 for its four clock()
    # reads), then the SECOND iteration's wall-clock check reads 105.0 --
    # 105.0 - 0.0 = 105s >= the 100s cap, so the loop must stop there and
    # never call the gate a second time.
    budget = Budget(max_wall_clock_s=100.0, max_points=100)
    clock_seq = [0.0, 1.0, 2.0, 3.0, 4.0, 105.0] + [105.0 + i for i in range(1, 5000)]
    run_search(
        budget=budget,
        price_fn=_fake_price_fn({"base-v0": 1.0}, default=0.1),
        gate_runner=gate_runner,
        base_config=base,
        base_version="base-v0",
        per_point_wall_clock_s=10.0,
        family=EvolutionaryFamily(),
        seed=0,
        save_fn=_fake_save_fn(),
        clock=_clock_from(clock_seq),
    )
    assert calls["n"] <= 1, f"wall-clock exhaustion must stop the loop, got {calls['n']} gate calls"


def test_duplicate_candidate_stall_stops_without_spinning() -> None:
    """CP-7e's minimum injected set: 'a proposer emitting a duplicate
    candidate.' A family that always proposes the same override must not
    spin forever -- the loop detects the stall and stops."""
    base = ServingConfig.from_env()

    class _ConstantFamily:
        name = "constant"

        def next_point(self, trace, base, rng):
            from src.search import Point
            return Point("top_k_rerank", 2)

    same_version_save = lambda cfg: "always-the-same-version"  # noqa: E731

    rows = run_search(
        budget=Budget(max_wall_clock_s=1000.0, max_points=1000),
        price_fn=_fake_price_fn({}, default=0.1),
        gate_runner=lambda v: _gate_report("does_not_block"),
        base_config=base,
        base_version="base-v0",
        per_point_wall_clock_s=1.0,
        family=_ConstantFamily(),
        seed=0,
        save_fn=same_version_save,
        clock=_clock_from([i * 0.01 for i in range(5000)]),
    )
    assert rows[-1].disposition == "duplicate_candidate_stall"
    assert len(rows) <= 3, f"duplicate-candidate detection must stop quickly, got {len(rows)} rows"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
