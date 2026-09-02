"""P7 Batch B -- the held-out split (charter D-4, schedule-p7.md §7 Batch B).

A version-stamped MASK over `test/retrieval_eval.json`'s immutable row ids
(A9, `notes/eval-set-versioning.md`) -- never an edit to the eval set itself.
Stratified per `family_of()` (`src/rag/metrics.py`) so each scored family
splits ~50/50 rather than the pooled set splitting 50/50 and leaving a family
lopsided (CP-7c). Fixed and stamped before any search touches it; D-4's
decision is that the split is built regardless of which half of the overfit
number (cost vs quality) can use it -- see D-4 item 1.

RATIO_RATIONALE travels with the mask id wherever a number is quoted from it
(CP-7c's inconclusive-branch rule, applied mechanically): 50/50 keeps three
scored families (primary, structural, adversarial) above R119's single-digit
line on the test half; 70/30 keeps only two. The ratio was chosen on
denominator survival, not on statistical convention.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import random

from src.rag.metrics import FAMILIES, family_of

logger = logging.getLogger(__name__)

RETRIEVAL_EVAL_PATH = os.path.join("test", "retrieval_eval.json")
SPLIT_ARTIFACT_PATH = os.path.join("test", "held_out_split.json")

SEED = 20260901  # recorded, not secret -- regenerating with this seed must
                  # reproduce the mask id byte-for-byte (Batch B's test).
RATIO = 0.5
RATIO_RATIONALE = (
    "50/50, chosen on denominator survival (CP-7c's inconclusive-branch rule), "
    "not on statistical convention: at 50/50 three scored families keep a "
    "double-digit test half (primary 19, structural 16, adversarial 12); at "
    "70/30 only two do (primary 11, structural 10)."
)


# R119: "a number computed from single-digit counts is not a number", and
# CP-7c states the consequence for this artifact directly -- "a split that
# drives any scored family into single digits is not admissible for that
# family". Recording the counts alone leaves that judgement to whoever reads
# the file later, which is how a fraction over 7 held-out rows gets quoted. So
# the verdict is stored, not the raw n alone.
R119_MIN_HELD_OUT = 10


@dataclasses.dataclass(frozen=True)
class FamilyCounts:
    search: int
    held_out: int

    @property
    def r119_admissible(self) -> bool:
        """May an overfit gap be REPORTED for this family at this split?

        False is not a defect in the split -- at n=141 most families cannot
        carry a held-out gap and D-4 already established that none can carry a
        QUALITY one at any ratio. It is the honest label, and it is what stops
        the count from being read as a licence.
        """
        return self.held_out >= R119_MIN_HELD_OUT


@dataclasses.dataclass(frozen=True)
class HeldOutSplit:
    """The mask, plus everything CP-7c requires travel with it."""

    mask_id: str
    seed: int
    ratio: float
    ratio_rationale: str
    eval_set_stamp: dict  # {"path", "rows", "sha256"} -- same shape as
                           # scripts/gate.py's _eval_set_stamp(), computed
                           # independently here (that module is read-only,
                           # per instruction, and importing it would pull in
                           # qdrant_client for no reason this module needs).
    families: dict[str, FamilyCounts]
    not_split: tuple[str, ...]
    search_ids: frozenset[str]
    held_out_ids: frozenset[str]

    def to_dict(self) -> dict:
        return {
            "mask_id": self.mask_id,
            "seed": self.seed,
            "ratio": self.ratio,
            "ratio_rationale": self.ratio_rationale,
            "eval_set_stamp": self.eval_set_stamp,
            "families": {
                name: {"search": c.search, "held_out": c.held_out,
                       "r119_admissible": c.r119_admissible}
                for name, c in sorted(self.families.items())
            },
            "not_split": list(self.not_split),
            "search_ids": sorted(self.search_ids),
            "held_out_ids": sorted(self.held_out_ids),
        }


def _eval_set_stamp(raw: bytes) -> dict:
    """Row count + content hash, same shape as `scripts/gate.py`'s
    `_eval_set_stamp()` (read, not imported -- see HeldOutSplit's docstring)."""
    return {
        "path": RETRIEVAL_EVAL_PATH,
        "rows": len(json.loads(raw)),
        "sha256": hashlib.sha256(raw).hexdigest()[:16],
    }


def _mask_id(search_ids: frozenset[str], held_out_ids: frozenset[str],
             ratio: float, seed: int) -> str:
    """Content hash over the sorted id lists plus ratio and seed -- so a
    number can be quoted with an id that changes iff the split does."""
    blob = json.dumps(
        {
            "search": sorted(search_ids),
            "held_out": sorted(held_out_ids),
            "ratio": ratio,
            "seed": seed,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_split(seed: int = SEED, ratio: float = RATIO,
                 eval_path: str = RETRIEVAL_EVAL_PATH) -> HeldOutSplit:
    """Stratified 50/50 (by default) mask over `family_of()`'s buckets.

    Deterministic: `random.Random(seed)` shuffles each family's own sorted id
    list, so the same seed always reproduces the same mask id regardless of
    row order in the source file. A family with fewer than 2 rows cannot
    produce two non-empty halves (`entity_pair`, 0 rows on disk today) and is
    recorded in `not_split` by name rather than silently absent -- CP-7c's
    rule, not an edge case to paper over.
    """
    with open(eval_path, "rb") as f:
        raw = f.read()
    rows = json.loads(raw)

    by_family: dict[str, list[str]] = {name: [] for name in FAMILIES}
    for row in rows:
        by_family[family_of(row)].append(row["id"])

    search_ids: set[str] = set()
    held_out_ids: set[str] = set()
    families: dict[str, FamilyCounts] = {}
    not_split: list[str] = []

    for name, ids in sorted(by_family.items()):
        ids_sorted = sorted(ids)
        n = len(ids_sorted)
        if n < 2:
            not_split.append(name)
            families[name] = FamilyCounts(search=0, held_out=0)
            continue
        shuffled = list(ids_sorted)
        random.Random(f"{seed}:{name}").shuffle(shuffled)
        split_at = round(n * ratio)
        split_at = max(1, min(n - 1, split_at))  # keep both halves non-empty
        fam_search, fam_held_out = shuffled[:split_at], shuffled[split_at:]
        search_ids.update(fam_search)
        held_out_ids.update(fam_held_out)
        families[name] = FamilyCounts(search=len(fam_search), held_out=len(fam_held_out))

    mask_id = _mask_id(frozenset(search_ids), frozenset(held_out_ids), ratio, seed)
    return HeldOutSplit(
        mask_id=mask_id,
        seed=seed,
        ratio=ratio,
        ratio_rationale=RATIO_RATIONALE,
        eval_set_stamp=_eval_set_stamp(raw),
        families=families,
        not_split=tuple(sorted(not_split)),
        search_ids=frozenset(search_ids),
        held_out_ids=frozenset(held_out_ids),
    )


def save(split: HeldOutSplit, path: str = SPLIT_ARTIFACT_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(split.to_dict(), f, indent=2, sort_keys=True)
        f.write("\n")
    logger.info("held-out split saved: mask_id=%s path=%s", split.mask_id, path)


def load(path: str = SPLIT_ARTIFACT_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --- the search-side entry point ---------------------------------------------
#
# Batch B's security property: the object a search loop can obtain from this
# module structurally cannot carry the held-out ids. `SearchIds` has exactly
# one field, and it is not `held_out_ids` -- there is no attribute, method, or
# closure on the returned object that can produce them. This is the property
# `test_held_out_split.py` asserts (dataclass fields, not docstring intent).

@dataclasses.dataclass(frozen=True)
class SearchIds:
    """What the search side is handed. Nothing else -- see module docstring."""

    ids: frozenset[str]


def search_view(split: HeldOutSplit) -> SearchIds:
    """The ONLY function a search-loop caller (Batch C's proposer, eventually)
    should hold a reference to. It closes over `split.search_ids` and returns
    an object with no path back to `split.held_out_ids`."""
    return SearchIds(ids=split.search_ids)
