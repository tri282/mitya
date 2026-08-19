# scripts/validate_eval_set.py
"""Set validator for test/retrieval_eval.json (mitya-v2 P0 step 6+ -- makes
the defect rate measurable, and enforces the append-only invariant (A9)
mechanically instead of by discipline alone).

Four checks, each collecting errors rather than stopping at the first:

  1. Every `targets` entry resolves to a real "Book|part|chapter" combination
     in data/rag/structure.json. A target naming a nonexistent chapter is the
     single most likely labeling defect (per the task brief).
  2. Every row has an `id`; ids are unique; format is `r-\\d{4}` (the format
     step 4's migration established -- r-0001..r-0027). A row of a different
     shape is flagged, not silently accepted, since a new family adopting a
     different id shape without deciding so is exactly the kind of drift this
     check exists to catch.
  3. Append-only vs. the committed version (git HEAD): no existing id is
     missing from the current set, and no existing id's `query` or `targets`
     changed. An edited immutable row is an ERROR, not a warning -- that
     invariant is what makes two eval-set versions comparable on their
     intersection (R13).
  4. Family integrity: every `family` is a known one (src.rag.metrics.FAMILIES),
     `entity_pair` rows carry a `pair_id` and every `pair_id` links exactly two
     rows (entity_fold_rate compares a Garnett query's rank against its
     alt-spelling twin, so a half-pair is unusable), retrieval-scored families
     carry at least one target, and the refusal families carry none.
  5. Denominator counts, PER FAMILY, reported separately so one can never
     silently bleed into another (the 0.731->0.720 correction was exactly this
     bug at 1/6th the scale, with 1/3rd as many families).

No network, no Qdrant, no judge call -- reads test/retrieval_eval.json,
data/rag/structure.json, and `git show HEAD:...` only. Exit code is nonzero on
any error, so this can become a pre-commit / CI gate later.

Usage:
    python scripts/validate_eval_set.py
    python scripts/validate_eval_set.py --pairs test/retrieval_eval.json
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config
from src.rag.metrics import FAMILIES, RETRIEVAL_SCORED_FAMILIES, family_of

logger = logging.getLogger(__name__)

ID_RE = re.compile(r"^r-\d{4}$")


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_pairs(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_head_pairs(path: str) -> list[dict] | None:
    """The set as committed at git HEAD, or None if the file has no HEAD
    version yet (new/untracked) -- the append-only check is then skipped
    with a warning rather than failing on a nonexistent baseline."""
    try:
        out = subprocess.run(
            ["git", "show", f"HEAD:{path}"], capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        return None
    return json.loads(out.stdout)


def check_targets_resolve(pairs: list[dict], structure: dict) -> list[str]:
    """Check 1: every target names a real book/part/chapter."""
    errors = []
    for row in pairs:
        rid = row.get("id", "<no id>")
        for t in row.get("targets", []):
            book, part, chapter = t.get("book"), t.get("part"), t.get("chapter")
            book_entry = structure.get(book)
            if book_entry is None:
                errors.append(f"{rid}: target book {book!r} not in structure.json")
                continue
            part_entry = book_entry["parts"].get(str(part))
            if part_entry is None:
                errors.append(f"{rid}: target part {part!r} not in {book!r} (structure.json)")
                continue
            if chapter not in part_entry["chapters"]:
                errors.append(
                    f"{rid}: target chapter {chapter!r} not in {book!r} part {part!r} "
                    f"(structure.json) -- likely labeling defect"
                )
    return errors


def check_ids(pairs: list[dict]) -> list[str]:
    """Check 2: every row has an id, ids are unique, format is r-\\d{4}."""
    errors = []
    seen: dict[str, int] = {}
    for i, row in enumerate(pairs):
        rid = row.get("id")
        if not rid:
            errors.append(f"row at index {i} (query={row.get('query')!r}) has no id")
            continue
        if not ID_RE.match(rid):
            errors.append(f"{rid}: id does not match the established format r-\\d{{4}}")
        if rid in seen:
            errors.append(f"{rid}: duplicate id (also at index {seen[rid]})")
        else:
            seen[rid] = i
    return errors


def check_append_only(pairs: list[dict], head_pairs: list[dict] | None) -> list[str]:
    """Check 3: no id removed, no existing id's query/targets edited, vs HEAD."""
    if head_pairs is None:
        logger.warning("no git HEAD version of the eval set found -- append-only check skipped")
        return []
    current_by_id = {row["id"]: row for row in pairs if row.get("id")}
    errors = []
    for head_row in head_pairs:
        rid = head_row.get("id")
        if rid is None:
            continue
        current_row = current_by_id.get(rid)
        if current_row is None:
            errors.append(f"{rid}: present at HEAD, missing now -- append-only (A9) forbids removal")
            continue
        if current_row.get("query") != head_row.get("query"):
            errors.append(f"{rid}: query changed since HEAD -- immutable rows must not be edited")
        if current_row.get("targets") != head_row.get("targets"):
            errors.append(f"{rid}: targets changed since HEAD -- immutable rows must not be edited")
    return errors


def check_families(pairs: list[dict]) -> list[str]:
    """Check 4: family names known, pairs complete, targets present iff scored."""
    errors = []
    pair_members: dict[str, list[str]] = {}
    for row in pairs:
        rid = row.get("id", "<no id>")
        family = family_of(row)
        if family not in FAMILIES:
            errors.append(f"{rid}: unknown family {family!r} (known: {sorted(FAMILIES)})")
            continue

        # A row cannot be in two denominators at once. The legacy boolean is
        # still authoritative for the pre-family rows, so the two must agree
        # wherever both are present.
        if row.get("negative_control") and family != "negative_control":
            errors.append(
                f"{rid}: negative_control=True but family={family!r} -- a row belongs to "
                "exactly one denominator"
            )

        targets = row.get("targets", [])
        if family in RETRIEVAL_SCORED_FAMILIES and not targets:
            errors.append(f"{rid}: family {family!r} is retrieval-scored but has no targets")
        if family not in RETRIEVAL_SCORED_FAMILIES and targets:
            errors.append(
                f"{rid}: family {family!r} is not retrieval-scored and must carry no targets "
                "(it has no correct chapter; scoring it would be a guaranteed miss)"
            )

        if family == "entity_pair":
            pair_id = row.get("pair_id")
            if not pair_id:
                errors.append(f"{rid}: family 'entity_pair' requires a pair_id")
            else:
                pair_members.setdefault(pair_id, []).append(rid)

    for pair_id, members in sorted(pair_members.items()):
        if len(members) != 2:
            errors.append(
                f"pair_id {pair_id!r}: links {len(members)} rows ({', '.join(members)}), "
                "expected exactly 2 -- entity_fold_rate compares a spelling against its twin"
            )
    return errors


def report_denominators(pairs: list[dict]) -> dict[str, int]:
    """Check 5: per-family counts, kept visibly separate."""
    counts = {f: 0 for f in sorted(FAMILIES)}
    for row in pairs:
        family = family_of(row)
        counts[family] = counts.get(family, 0) + 1
    counts["total"] = len(pairs)
    logger.info(
        "denominators: %s",
        " ".join(f"{f}={n}" for f, n in counts.items() if n or f == "primary"),
    )
    return counts


def validate(pairs_path: str, structure_path: str) -> tuple[bool, list[str]]:
    pairs = _load_pairs(pairs_path)
    structure = _load_json(structure_path)
    head_pairs = _load_head_pairs(pairs_path)

    errors: list[str] = []
    errors += check_ids(pairs)
    errors += check_targets_resolve(pairs, structure)
    errors += check_append_only(pairs, head_pairs)
    errors += check_families(pairs)

    report_denominators(pairs)

    for e in errors:
        logger.error(e)
    ok = not errors
    logger.info("validate_eval_set: %s (%d rows, %d errors)", "PASSED" if ok else "FAILED", len(pairs), len(errors))
    return ok, errors


def _demo() -> None:
    """Smallest runnable check -- exercises each check function against small
    synthetic fixtures, not the real (immutable, do-not-touch) eval set."""
    structure = {
        "Book A": {"parts": {"1": {"chapters": [1, 2]}}},
    }
    good = [{"id": "r-0001", "query": "q1", "targets": [{"book": "Book A", "part": 1, "chapter": 1}]}]
    assert check_targets_resolve(good, structure) == []
    assert check_ids(good) == []

    bad_target = [{"id": "r-0001", "query": "q1", "targets": [{"book": "Book A", "part": 1, "chapter": 99}]}]
    assert len(check_targets_resolve(bad_target, structure)) == 1

    bad_book = [{"id": "r-0001", "query": "q1", "targets": [{"book": "Nonexistent", "part": 1, "chapter": 1}]}]
    assert len(check_targets_resolve(bad_book, structure)) == 1

    no_id = [{"query": "q1", "targets": []}]
    assert len(check_ids(no_id)) == 1

    bad_format = [{"id": "row1", "query": "q1", "targets": []}]
    assert len(check_ids(bad_format)) == 1

    dup = [
        {"id": "r-0001", "query": "q1", "targets": []},
        {"id": "r-0001", "query": "q2", "targets": []},
    ]
    assert len(check_ids(dup)) == 1  # only the second occurrence flags a duplicate

    head = [{"id": "r-0001", "query": "original", "targets": [{"book": "Book A", "part": 1, "chapter": 1}]}]
    unedited = [{"id": "r-0001", "query": "original", "targets": [{"book": "Book A", "part": 1, "chapter": 1}]}]
    assert check_append_only(unedited, head) == []

    edited = [{"id": "r-0001", "query": "CHANGED", "targets": [{"book": "Book A", "part": 1, "chapter": 1}]}]
    assert len(check_append_only(edited, head)) == 1

    removed: list[dict] = []
    assert len(check_append_only(removed, head)) == 1

    assert check_append_only(unedited, None) == []  # no HEAD baseline -> skip, not fail

    counts = report_denominators([
        {"negative_control": True}, {"negative_control": False}, {},
    ])
    assert counts["primary"] == 2 and counts["negative_control"] == 1 and counts["total"] == 3

    # family checks
    ok_pair = [
        {"id": "r-0031", "family": "entity_pair", "pair_id": "p-001", "targets": [{"book": "Book A", "part": 1, "chapter": 1}]},
        {"id": "r-0032", "family": "entity_pair", "pair_id": "p-001", "targets": [{"book": "Book A", "part": 1, "chapter": 1}]},
    ]
    assert check_families(ok_pair) == []
    assert len(check_families(ok_pair[:1])) == 1            # half a pair is unusable
    assert len(check_families([dict(ok_pair[0], pair_id=None)])) == 1  # missing pair_id; not counted as a half-pair
    assert check_families([{"id": "r-0040", "family": "adversarial", "targets": []}]) == []
    assert len(check_families([{"id": "r-0041", "family": "sensitive", "targets": [{"book": "Book A", "part": 1, "chapter": 1}]}])) == 1
    assert len(check_families([{"id": "r-0042", "family": "primary", "targets": []}])) == 1
    assert len(check_families([{"id": "r-0043", "family": "nonsense", "targets": []}])) == 1
    assert len(check_families([{"id": "r-0044", "family": "primary", "negative_control": True, "targets": [{"book": "Book A", "part": 1, "chapter": 1}]}])) == 1
    assert check_families([{"id": "r-0001", "targets": [{"book": "Book A", "part": 1, "chapter": 1}]}]) == []  # legacy row

    logger.info("scripts/validate_eval_set.py self-check passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Validate test/retrieval_eval.json (mitya-v2 P0).")
    ap.add_argument("--pairs", default=os.path.join("test", "retrieval_eval.json"))
    ap.add_argument("--structure", default=Config.STRUCTURE_PATH)
    ap.add_argument("--demo", action="store_true", help="run the self-check instead of validating")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.demo:
        _demo()
        sys.exit(0)

    ok, _ = validate(args.pairs, args.structure)
    sys.exit(0 if ok else 1)
