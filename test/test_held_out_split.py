# test/test_held_out_split.py
"""P7 Batch B verify: the mask covers every scored family with the counts
recorded, search ∪ held-out == the full id set with an empty intersection,
the search-side entry point cannot read the held-out ids, A9-conformance
(every masked id exists in the eval set, no eval-set row modified), and
regenerating from the recorded seed reproduces the same mask id.

Runnable now -- no Qdrant, no provider, no network. Reads
`test/retrieval_eval.json` and `test/held_out_split.json` only.
"""

import dataclasses
import json
import os
import subprocess
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.held_out_split import (  # noqa: E402
    R119_MIN_HELD_OUT,
    RETRIEVAL_EVAL_PATH,
    SPLIT_ARTIFACT_PATH,
    build_split,
    load,
    search_view,
)
from src.rag.metrics import FAMILIES  # noqa: E402


def _eval_rows() -> list[dict]:
    with open(RETRIEVAL_EVAL_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_split_covers_every_scored_family_with_recorded_counts() -> None:
    split = build_split()
    assert set(split.families) == FAMILIES
    for name, counts in split.families.items():
        if name in split.not_split:
            assert (counts.search, counts.held_out) == (0, 0), name
        else:
            assert counts.search > 0 and counts.held_out > 0, name


def test_search_union_held_out_equals_full_id_set_with_empty_intersection() -> None:
    split = build_split()
    all_ids = {row["id"] for row in _eval_rows()}
    assert split.search_ids | split.held_out_ids == all_ids
    assert split.search_ids & split.held_out_ids == set()


def test_family_that_cannot_be_split_is_named_not_split() -> None:
    split = build_split()
    assert "entity_pair" in split.not_split  # 0 rows on disk today (P-22)
    assert "negative_control" not in split.not_split  # 2 rows -> splits 1/1


def test_search_side_entry_point_cannot_read_held_out_ids() -> None:
    """The property, asserted structurally -- not the intention.

    `search_view()` returns an object with exactly one field, `ids`, holding
    the search half. There is no attribute on the returned object that is or
    contains `held_out_ids`.
    """
    split = build_split()
    view = search_view(split)
    fields = {f.name for f in dataclasses.fields(view)}
    assert fields == {"ids"}, f"search view exposes more than the search ids: {fields}"
    assert view.ids == split.search_ids
    assert view.ids.isdisjoint(split.held_out_ids)
    # Belt-and-suspenders: nothing reachable from the object's own __dict__
    # contains a held-out id.
    exposed: set[str] = set()
    for value in dataclasses.asdict(view).values():
        exposed |= set(value)
    assert exposed.isdisjoint(split.held_out_ids)


def test_mask_is_a9_conformant() -> None:
    """Every masked id exists in the eval set, and the eval set on disk is
    byte-identical to HEAD's committed copy -- the mask is a view over
    immutable rows, never an edit to them (A9)."""
    split = build_split()
    all_ids = {row["id"] for row in _eval_rows()}
    assert split.search_ids <= all_ids
    assert split.held_out_ids <= all_ids

    committed = subprocess.run(
        ["git", "show", f"HEAD:{RETRIEVAL_EVAL_PATH}"],
        capture_output=True, text=True, check=True,
    ).stdout
    with open(RETRIEVAL_EVAL_PATH, encoding="utf-8") as f:
        on_disk = f.read()
    assert json.loads(on_disk) == json.loads(committed), (
        "test/retrieval_eval.json differs from HEAD -- A9 forbids editing eval-set rows"
    )


def test_regenerating_from_the_recorded_seed_reproduces_the_mask_id() -> None:
    split_a = build_split()
    split_b = build_split(seed=split_a.seed, ratio=split_a.ratio)
    assert split_a.mask_id == split_b.mask_id
    assert split_a.search_ids == split_b.search_ids
    assert split_a.held_out_ids == split_b.held_out_ids


def test_saved_artifact_matches_a_fresh_build() -> None:
    """The artifact on disk (`test/held_out_split.json`) is not recomputed by
    whoever reads it -- it must already hold what a fresh build produces."""
    saved = load(SPLIT_ARTIFACT_PATH)
    fresh = build_split().to_dict()
    assert saved == fresh


def test_every_family_carries_an_r119_admissibility_verdict() -> None:
    """CP-7c: a split driving a scored family into single digits is not
    admissible FOR THAT FAMILY. The artifact must carry that verdict, because
    a counts table alone reads as a licence to compute a fraction over seven
    rows -- the exact thing R119 names as not a number.
    """
    d = load()
    for name, row in d["families"].items():
        assert "r119_admissible" in row, f"{name} has no admissibility verdict"
        assert row["r119_admissible"] == (row["held_out"] >= R119_MIN_HELD_OUT)
    admissible = sorted(n for n, r in d["families"].items() if r["r119_admissible"])
    assert admissible == ["adversarial", "primary", "structural"], admissible


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
