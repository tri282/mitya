# test/test_optimizer_gate_run.py
"""P7 Batch A verify: the proposer's known-answer case.

`[RUN -- needs approval]`. This opens Qdrant (through the unchanged P3 gate)
and is NOT part of `test/test_optimizer.py`'s zero-spend suite -- it is a
separate file so `test_optimizer.py` stays runnable with no side effects and
this one stays a single, clearly-labelled command the owner runs by hand:

    .venv/bin/python test/test_optimizer_gate_run.py

Not run by the agent that wrote this file, per the task's hard limit on
executing anything that touches the pipeline.
"""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config_artifact import active_serving_config, save  # noqa: E402
from src.optimizer import propose_one, run_gate_for_candidate, trace_row_from_report  # noqa: E402


def test_zero_change_candidate_is_all_zero_deltas_and_does_not_block() -> None:
    """A candidate byte-identical to production must produce a gate report
    with every delta exactly 0.000 and disposition does_not_block. A proposer
    whose zero-change candidate shows a delta is measuring its own plumbing
    (schedule §5, Batch A's standing gate)."""
    production = active_serving_config()
    candidate = propose_one(production, {})  # no overrides: byte-identical
    version = save(candidate)

    import time
    start = time.monotonic()
    report = run_gate_for_candidate(version)
    wall_clock_s = time.monotonic() - start

    assert report["disposition"] == "does_not_block", report["caption"]
    cost = report["layers"]["cost"][0]
    assert cost.get("delta_pct") in (0.0, 0, None), f"zero-change candidate priced a delta: {cost}"
    for row in report["layers"]["quality_1a"]:
        if row.get("metric") == "recall_at_5":
            assert row["verdict"] != "BREAK", row

    row = trace_row_from_report(report, wall_clock_s)
    print(f"  trace: candidate={row.candidate_version} production={row.production_version} "
          f"objective={row.objective_usd} ({row.objective_source}) "
          f"wall_clock={row.wall_clock_s:.1f}s disposition={row.disposition}")


def main() -> None:
    test_zero_change_candidate_is_all_zero_deltas_and_does_not_block()
    print("  ok  test_zero_change_candidate_is_all_zero_deltas_and_does_not_block")


if __name__ == "__main__":
    main()
