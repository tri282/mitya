# test/test_gate_report.py
"""P3 step 4 verify: arm resolution, the frozen line, and the comparability rule.

Two properties here are security-of-measurement properties rather than
correctness ones, and both fail silently if unasserted:

  - `local-q4km` must never be executed (A3). If it ever were, the frozen
    baseline would drift against the thing it exists to hold still, and every
    chart citing it would be retroactively wrong with no error anywhere.
  - two arms on different serving models must not be compared. A delta between
    them is not attributable to the config change, and the gate would report a
    model swap as a config improvement.
"""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts import gate  # noqa: E402


def _arm(model: str) -> dict:
    return {"serving_model": model, "config_version": "0" * 12}


def test_frozen_arm_is_never_executed() -> None:
    reference = gate.frozen_reference()
    assert reference["executed"] is False
    assert reference["arm"] == "local-q4km"
    # Provenance, not just numbers: a chart line whose origin is not recorded
    # cannot be defended later, and plans/ is gitignored (A11) so the
    # transcript itself is not in the checkout.
    assert reference["source"]["transcript"], "no pointer to where the numbers came from"
    assert reference["source"]["quality_set"]["sha256_prefix"]


def test_the_gate_never_shells_out_to_the_frozen_arm() -> None:
    """Grep-level, deliberately: the frozen arm must not be reachable from any
    execution path, so the assertion is about the code, not about one run."""
    with open(os.path.join("scripts", "gate.py"), encoding="utf-8") as f:
        source = f.read()
    run_arm_body = source.split("def run_arm(", 1)[1].split("\ndef ", 1)[0]
    assert "local-q4km" not in run_arm_body, "the frozen arm is reachable from run_arm()"


def test_arms_on_different_models_are_refused_not_compared() -> None:
    try:
        gate.assert_comparable(_arm("models/gemini-3.5-flash-lite"), _arm("openai/gpt-oss-120b"))
    except RuntimeError as exc:
        assert "different models" in str(exc)
        assert "not attributable" in str(exc), "refusal does not say why it matters"
        return
    raise AssertionError("the gate compared two arms on different serving models")


def test_arms_on_the_same_model_are_compared() -> None:
    """Non-vacuity for the refusal above."""
    gate.assert_comparable(_arm("models/gemini-3.5-flash-lite"),
                           _arm("models/gemini-3.5-flash-lite"))


def test_error_and_block_are_distinct_exit_codes() -> None:
    """A provider or environment failure is a re-run condition, not a verdict.
    Collapsing them is how a flaky network becomes 'the change is bad' and,
    eventually, how the gate gets switched off (charter VIII)."""
    assert gate.EXIT_PASS == 0
    assert gate.EXIT_BLOCK == 1
    assert gate.EXIT_ERROR == 2
    assert len({gate.EXIT_PASS, gate.EXIT_BLOCK, gate.EXIT_ERROR}) == 3


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
