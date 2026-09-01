"""Check that §X of the P7 audit partitions every ServingConfig field.

The audit's original check tested whether each field appeared *anywhere* in the
document and passed while four fields sat in no §X list at all (G-P19). Presence
is not placement: a field classified in the body and absent from the in/out/
records/calibration lists cannot be turned into a proposer allowlist. This
checks placement, and is expected to FAIL until design-decide resolves G-P19.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config_artifact import ServingConfig  # noqa: E402

AUDIT = "plans/mitya-v2/04-plan/phase-7-audit.md"
BOUNDS = [
    ("### 1. In — cost axes", "### 2. Out"),
    ("### 2. Out —", "### 3. Neither"),
    ("### 3. Neither in nor out", "### 4. Not a search axis"),
    ("### 4. Not a search axis", "### 5. P-23"),
]


def main() -> int:
    doc = open(AUDIT, encoding="utf-8").read()
    lists = [doc[doc.index(a):doc.index(b)] for a, b in BOUNDS]
    fields = list(ServingConfig.model_fields)
    unplaced = [f for f in fields if not any(f in t for t in lists)]
    dupes = [f for f in fields if sum(1 for t in lists if f in t) > 1]
    print(f"fields={len(fields)} "
          f"unplaced={unplaced or 'none'} "
          f"in_two_lists={dupes or 'none'}")
    return 1 if (unplaced or dupes) else 0


if __name__ == "__main__":
    sys.exit(main())
