# test/test_gate_layers.py
"""P3 step 5 verify: the three layers, the three-valued verdict, the schema rules.

`src/rag/gate.py` carries its own `_demo()` self-check; this file runs it as part
of the suite (the suite executes test/test_*.py only, so a module self-check that
nobody invokes is a check that does not exist) and adds the end-to-end scenarios
the layers have to separate.

The scenario table is the point. A gate that returns the same disposition for a
one-row wobble and a six-row collapse is not a gate, and that distinction is
invisible in any aggregate -- both look like "recall went down".
"""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.rag import gate  # noqa: E402

IDS = [f"r-{i:04d}" for i in range(38)]


def _arm(hits: dict, version: str = "aaaaaaaaaaaa", route_checked: int = 16,
         route_matched: int = 16) -> dict:
    return {
        "config_version": version,
        "route_checked": route_checked,
        "route_matched": route_matched,
        "buckets": [{"bucket": "PRIMARY (headline)", "rows": len(hits)}],
        "rows": [{"id": i, "family": "primary", "hit": h, "rank": 1 if h else None}
                 for i, h in hits.items()],
    }


def test_module_self_check_passes() -> None:
    gate._demo()


def test_identical_arms_are_not_detected_and_not_an_improvement() -> None:
    hits = {i: True for i in IDS}
    result = gate.mcnemar_noninferiority(hits, dict(hits))
    assert result["verdict"] == gate.NOT_DETECTED
    assert result["recordable_as_improvement"] is False
    # No discordant pairs means no variance estimate. Reporting MDE 0.0 would
    # read as infinite sensitivity, which is the opposite of what it means.
    assert result["mde"] is None, "MDE must be undefined, not zero, with no discordant rows"


def test_one_row_deficit_is_not_detected_but_is_visible_as_a_flip() -> None:
    """The distinction Q2 rests on: not detected is not the same as no change.
    The aggregate cannot see this row; the flip list must."""
    prod = {i: True for i in IDS}
    cand = dict(prod); cand[IDS[0]] = False
    result = gate.mcnemar_noninferiority(prod, cand)
    assert result["verdict"] == gate.NOT_DETECTED
    assert result["b"] == 1 and result["c"] == 0
    assert result["mde"] > abs(result["delta"])

    moved = gate.flips(_arm(prod)["rows"], _arm(cand)["rows"])
    assert len(moved) == 1 and moved[0]["direction"] == "prod_hit_cand_miss"


def test_six_row_deficit_breaks() -> None:
    prod = {i: True for i in IDS}
    cand = dict(prod)
    for i in IDS[:6]:
        cand[i] = False
    assert gate.mcnemar_noninferiority(prod, cand)["verdict"] == gate.BREAK


def test_route_mismatch_breaks_with_no_statistics_involved() -> None:
    """The invariant layer is where the power actually is. R19 was one route
    mismatch; no sampling argument applies to it."""
    hits = {i: True for i in IDS}
    checks = gate.invariants(_arm(hits, route_matched=15), None, None)
    verdicts = {c["metric"]: c["verdict"] for c in checks}
    assert verdicts["route_match_prod"] == gate.BREAK


def test_first_run_of_a_version_reports_no_baseline_rather_than_passing() -> None:
    """'Nothing to compare' and 'compared and identical' are different claims."""
    hits = {i: True for i in IDS}
    checks = gate.invariants(_arm(hits), None, None)
    baseline_check = [c for c in checks if c["metric"] == "determinism_vs_baseline"][0]
    assert baseline_check["verdict"] == gate.NO_BASELINE


def test_flips_join_on_id_not_position() -> None:
    """An index join silently invents discordant counts when arms differ in
    order. Both arms here are identical but reversed."""
    rows = _arm({i: True for i in IDS})["rows"]
    assert gate.flips(rows, list(reversed(rows))) == []


def test_a_max_tokens_change_is_a_measurement_break_not_a_cost_delta() -> None:
    prod = {"usd": 0.0005, "completion_tokens": 1024, "basis": "b"}
    cand = {"usd": 0.0002, "completion_tokens": 400, "basis": "b"}
    record = gate.cost_layer(prod, cand)[0]
    assert record["verdict"] == gate.ERROR
    assert "measurement break" in record["reason"]


def test_a_cost_increase_breaks_and_a_decrease_is_recordable() -> None:
    prod = {"usd": 0.0005, "completion_tokens": 1024, "basis": "b"}
    worse = gate.cost_layer(prod, {"usd": 0.0009, "completion_tokens": 1024, "basis": "b"})[0]
    better = gate.cost_layer(prod, {"usd": 0.0004, "completion_tokens": 1024, "basis": "b"})[0]
    assert worse["verdict"] == gate.BREAK and worse["recordable_as_improvement"] is False
    assert better["verdict"] == gate.DETECTED and better["recordable_as_improvement"] is True


def test_bootstrap_is_seeded_so_two_runs_agree() -> None:
    """Load-bearing, not tidy: an unseeded bootstrap makes two gate runs on
    identical inputs disagree, tripping the determinism invariant this same
    module asserts and making a report unreproducible from its own inputs."""
    prod = {i: 0.5 for i in IDS}
    cand = {i: 0.6 for i in IDS}
    assert gate.paired_bootstrap(prod, cand, draws=500) == \
           gate.paired_bootstrap(prod, cand, draws=500)


def test_bootstrap_sees_a_rank_change_that_recall_cannot() -> None:
    """The whole reason MRR is computed: a hit moving rank 4 -> 2 never crosses
    k, so hit/miss is blind to it while reciprocal rank is not."""
    prod = {i: 1 / 4 for i in IDS}
    cand = {i: 1 / 2 for i in IDS}
    result = gate.paired_bootstrap(prod, cand, draws=2000)
    assert result["delta"] > 0 and result["verdict"] == gate.DETECTED

    hits = {i: True for i in IDS}   # identical hit/miss for the same rows
    assert gate.mcnemar_noninferiority(hits, dict(hits))["verdict"] == gate.NOT_DETECTED


def test_reported_metrics_do_not_decide_the_disposition() -> None:
    """R74 / A13 rider: gating a second quality metric would pick weights nobody
    chose. A non-blocking BREAK must not block."""
    layers = {"quality_1a": [
        {"metric": "recall_at_5", "verdict": gate.NOT_DETECTED, "blocking": True},
        {"metric": "mrr", "verdict": gate.BREAK, "blocking": False},
    ]}
    assert gate.disposition_of(layers)[0] == "does_not_block"


def test_a_report_must_admit_it_cannot_see_grounding() -> None:
    """R77, measured: stripping every passage moved source_leak and
    assistant_tell by EXACTLY zero while the offline judge saw groundedness fall
    4.022 -> 1.000. A reader completes "did not block" as "the answers are fine",
    and the cheapest config the optimizer can reach is the ungrounded one
    (-32% shadow cost). The disclaimer is mandatory, not advisory."""
    bare = {"caption": "invariants hold, cost did not grow",
            "disposition": "does_not_block", "layers": {"quality_1a": []}}
    try:
        gate.validate_report(bare)
    except ValueError as exc:
        assert "grounding" in str(exc).lower()
        return
    raise AssertionError("a report claiming a pass without the grounding disclaimer was accepted")


def test_disposition_is_derived_separately_from_verdicts() -> None:
    """F5: a metric may be NOT-DETECTED while the run does not block."""
    layers = {"quality_1a": [{"metric": "recall_at_5", "verdict": gate.NOT_DETECTED}],
              "cost": [{"metric": "computed_cost_usd", "verdict": gate.NOT_DETECTED}]}
    assert gate.disposition_of(layers)[0] == "does_not_block"
    layers["invariants"] = [{"metric": "route_match_prod", "verdict": gate.BREAK}]
    assert gate.disposition_of(layers)[0] == "blocks"
    layers["invariants"] = [{"metric": "route_match_prod", "verdict": gate.ERROR}]
    assert gate.disposition_of(layers)[0] == "error"


def test_the_scalar_tripwire_fires_on_a_sort_key_and_not_on_a_count() -> None:
    """A13's rider. The false-positive half matters as much as the true-positive
    half: a tripwire that rejects `n_cells_scored` gets deleted by whoever hits
    it first, and then the rule is gone."""
    ok = {"caption": "invariants hold; GROUNDING NOT CHECKED (R77)",
          "disposition": "does_not_block", "layers": {"quality_1a": []}}
    gate.validate_report({**ok, "counts": {"n_cells_scored": 49}})
    for offender in ({"overall_score": 1}, {"composite": 1}, {"weighted_quality": 1}):
        try:
            gate.validate_report({**ok, **offender})
        except ValueError:
            continue
        raise AssertionError(f"scalar collapse accepted: {offender}")


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
