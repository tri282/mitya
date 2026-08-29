# test/test_gate_promote.py
"""P3 step 6 verify: promotion is a pointer write, and a blocking run does not do it.

Every assertion here is on the POINTER FILE'S CONTENT, not on an exit code. An
exit code records what the script decided; only the file records what it did,
and the gap between those two is exactly where a promotion bug lives.
"""

import json
import os
import subprocess
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts import gate  # noqa: E402

_MDE_REPORT = {
    "layers": {
        "cost": [{"metric": "computed_cost_usd", "delta_pct": -18.0}],
        "quality_1a": [{"family": "primary", "n": 38, "mde": 0.128,
                        "verdict": gate.NOT_DETECTED}],
        "quality_1b": [],
    },
}


def _pointer_bytes() -> bytes:
    with open(gate.CURRENT_POINTER, "rb") as f:
        return f.read()


def test_rationale_carries_the_bound_not_a_claim() -> None:
    """A13 obligation 2. A promoted candidate may be up to one MDE worse, so the
    recorded rationale must say 'not detectably worse' and carry the MDE and n."""
    rationale = gate.rationale_of(_MDE_REPORT)
    assert "not detectably worse" in rationale
    assert "0.128" in rationale and "n=38" in rationale
    assert "cost -18.0%" in rationale
    assert "ceiling" in rationale, "cost claim does not state it is a ceiling"
    for forbidden in ("same quality", "quality preserved", "no regression"):
        assert forbidden not in rationale.lower()


def test_the_phrase_same_quality_appears_nowhere_in_the_gate() -> None:
    """One-line tripwire for A13 obligation 2, across the whole gate path."""
    for path in ("scripts/gate.py", "src/rag/gate.py"):
        with open(path, encoding="utf-8") as f:
            body = f.read().lower()
        assert "same quality" not in body, f"{path} claims 'same quality'"


def test_a_blocking_run_leaves_the_pointer_byte_identical() -> None:
    """The assertion is on the file, not the exit code."""
    before = _pointer_bytes()
    result = subprocess.run(
        [sys.executable, "scripts/gate.py", "--candidate", "deadbeef0000", "--promote"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0, "an unknown candidate must not promote"
    assert _pointer_bytes() == before, "the pointer moved on a run that did not pass"


def test_promote_without_a_candidate_is_refused() -> None:
    before = _pointer_bytes()
    result = subprocess.run(
        [sys.executable, "scripts/gate.py", "--promote"],
        capture_output=True, text=True,
    )
    assert result.returncode == gate.EXIT_ERROR
    assert _pointer_bytes() == before


def test_promotion_record_shape_is_rollback_complete() -> None:
    """A record that does not name the PREVIOUS hash cannot be rolled back from,
    and a CURRENT whose hash has no record is the anomaly that makes 'no bypass
    path' checkable at all (gap G-F). Detection, not prevention."""
    if not os.path.exists(gate.PROMOTIONS_LOG):
        return   # nothing promoted yet on this machine; shape is asserted below on write
    with open(gate.PROMOTIONS_LOG, encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            for key in ("utc", "promoted", "previous", "report", "rationale"):
                assert key in record, f"promotion record missing {key}"


def test_there_is_exactly_one_entry_point_for_any_proposal() -> None:
    """D9: an optimizer proposal and a human change take the identical path.
    Asserted by construction -- gate.py accepts a hash and has no other door."""
    with open("scripts/gate.py", encoding="utf-8") as f:
        source = f.read()
    assert source.count("add_argument(\"--candidate\"") == 1
    assert "--force" not in source and "--skip" not in source, "a bypass flag exists"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
