# test/test_optimizer.py
"""P7 Batch A verify: the search space's two enforcing tests (D-5), the
no-promotion property (D9), and the corpus_version assertion (schedule §5).

Runnable now -- no Qdrant, no provider, no gate call, no CURRENT_POINTER
write. The zero-change candidate known-answer case (`test_optimizer_gate_run.py`)
is a SEPARATE file precisely because it needs the gate, which needs approval.
"""

import os
import re
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config_artifact import CorpusManifest, ServingConfig  # noqa: E402
from src.optimizer import (  # noqa: E402
    ALLOWED,
    EXCLUDED,
    assert_no_promotion_path,
    churns_only_config_version,
    forbidden_instrument_fields,
    partition_gaps,
    propose_one,
)


def test_allowlist_and_exclusions_sum_to_every_serving_config_field() -> None:
    """D-5's partition-completeness test. A field added to ServingConfig later
    shows up here as unplaced -- a loud, readable failure naming the field --
    until a human places it in ALLOWED or EXCLUDED."""
    unplaced, in_both = partition_gaps()
    assert not unplaced, f"unplaced ServingConfig field(s), not yet ruled on: {sorted(unplaced)}"
    assert not in_both, f"field(s) in both lists: {sorted(in_both)}"
    total = len(ALLOWED) + len(EXCLUDED)
    assert total == len(ServingConfig.model_fields), (
        f"ALLOWED({len(ALLOWED)}) + EXCLUDED({len(EXCLUDED)}) = {total}, expected "
        f"{len(ServingConfig.model_fields)}"
    )


def test_every_allowed_entry_carries_all_three_properties() -> None:
    for name, entry in ALLOWED.items():
        assert entry.p1_proposable.strip(), f"{name}: empty P1"
        assert entry.p2_behavior_not_belief.strip(), f"{name}: empty P2"
        assert entry.p3_instrument.strip(), f"{name}: empty P3"


def test_no_p3_record_cites_an_instrument_that_does_not_exist() -> None:
    """D-5's P3 is "an instrument with MEASURED power", named in the entry.

    An entry whose P3 cites a guard that has not been built fails P3 by
    definition -- and reads, at a glance, exactly like one that passes. That is
    the whole failure mode D-5 was written against, so it is a build break and
    not a review item. `persona_head` was admitted this way once (its P3 said
    "NOT YET BUILT" in as many words) and is excluded until Batch D lands step
    3b; this check is what stops the next one.
    """
    for name, entry in ALLOWED.items():
        lowered = entry.p3_instrument.lower()
        for phrase in ("not yet built", "not built", "does not exist", "not yet landed",
                       "has not landed", "pending", "tbd"):
            assert phrase not in lowered, (
                f"{name}'s P3 cites an instrument that does not exist "
                f"({phrase!r}): {entry.p3_instrument}"
            )


def test_every_excluded_entry_carries_a_reason() -> None:
    for name, reason in EXCLUDED.items():
        assert reason.strip(), f"{name}: empty exclusion reason"


def test_instrument_integrity_by_construction() -> None:
    """D-5's second enforcing test. No allowlisted name may be a field read by
    the pricing, limits, gate, or metrics machinery."""
    forbidden = forbidden_instrument_fields()
    assert {"model_rates", "model_limits", "unknown_model_limits", "no_tpm_limit"} <= forbidden, (
        f"the four known price-card/quota-table fields must all be detected from source, "
        f"got {sorted(forbidden)}"
    )
    leaked = set(ALLOWED) & forbidden
    assert not leaked, f"allowlisted field(s) read by instrument machinery: {sorted(leaked)}"


def test_a_hand_added_model_rates_entry_still_fails() -> None:
    """The zero-trust clause, stated as a test that survives a careless future
    edit: even a HUMAN adding `model_rates` to the allowlist must not pass,
    because the check runs against source-derived forbidden fields, not
    against ALLOWED's own membership."""
    tampered = dict(ALLOWED)
    tampered["model_rates"] = ALLOWED["top_k_fused"]  # any entry; the name is what matters
    forbidden = forbidden_instrument_fields()
    assert set(tampered) & forbidden == {"model_rates"}


def test_proposer_rejects_an_override_outside_the_space() -> None:
    base = ServingConfig.from_env()
    try:
        propose_one(base, {"model_rates": {}})
    except ValueError as exc:
        assert "model_rates" in str(exc)
    else:
        raise AssertionError("propose_one accepted a non-allowlisted override")


def test_proposer_emits_exactly_one_candidate_and_changes_only_allowed_fields() -> None:
    base = ServingConfig.from_env()
    candidate = propose_one(base, {"top_k_fused": 13})
    assert candidate.top_k_fused == 13
    diffs = {
        name for name in ServingConfig.model_fields
        if getattr(base, name) != getattr(candidate, name)
    }
    assert diffs == {"top_k_fused"}
    assert diffs <= set(ALLOWED), f"a proposal changed a non-allowlisted field: {diffs}"


def test_no_promotion_assertion_is_on_pointer_content() -> None:
    assert_no_promotion_path("abc123", "abc123")  # unchanged: fine
    try:
        assert_no_promotion_path("abc123", "def456")
    except AssertionError:
        pass
    else:
        raise AssertionError("assert_no_promotion_path did not catch a pointer move")


def test_gate_invocation_never_carries_promote_true() -> None:
    """Source-level check, mirroring test_gate_promote.py's 'exactly one entry
    point' style: `src/optimizer.py` calls `p3_gate.main` exactly once, and
    that one call passes a literal `False`."""
    with open(os.path.join("src", "optimizer.py"), encoding="utf-8") as f:
        source = f.read()
    calls = re.findall(r"p3_gate\.main\(([^)]*)\)", source)
    assert len(calls) == 1, f"expected exactly one p3_gate.main(...) call site, found {len(calls)}"
    assert calls[0].strip().endswith("False"), f"gate call does not pass promote=False: {calls[0]!r}"
    assert re.search(r"main\([^)]*,\s*True\s*\)", source) is None


def test_corpus_version_is_never_churned_by_the_space() -> None:
    assert churns_only_config_version()
    assert not (set(ALLOWED) & set(CorpusManifest.model_fields))


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
