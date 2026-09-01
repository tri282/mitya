#!/usr/bin/env python3
"""P-22 -- per-family counts and the MDE a held-out split would leave.

[DESK -- $0.00, no provider call, no Qdrant]. Reads `test/retrieval_eval.json`,
buckets with `src/rag/metrics.py`'s `family_of()` (never the raw `family` key --
they disagree), and computes the gate's own MDE form `2.8 * sqrt(p_disc / n)`
at 50/50, 70/30 and k-fold rotation.

`_MDE_Z = 2.8` carries the 80% power term; 1.96 is significance only and would
understate every MDE by 43%. Both are printed so the difference is visible
rather than asserted.
"""

import json
import logging
import math
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.rag.gate import _MDE_Z, mcnemar_noninferiority
from src.rag.metrics import RETRIEVAL_SCORED_FAMILIES, family_of

logger = logging.getLogger("probe_split_arithmetic")
EVAL = "test/retrieval_eval.json"

# p_disc values R20 measured on the primary family, spanning the observed range.
P_DISC = [0.075, 0.100, 0.175]


def mde(p_disc: float, n: int, z: float = _MDE_Z) -> float:
    return z * math.sqrt(p_disc / n) if n else float("inf")


def known_answer_check() -> None:
    """Reproduce R20's published MDE row and the gate's own ERROR contract.

    R20: full vs no-expansion, primary n=40, 4 discordant -> p_disc 0.100,
    MDE 0.140, delta 0.000. If either the formula or `_MDE_Z` moves, this fails
    instead of the table quietly reporting a different quantity.
    """
    assert _MDE_Z == 2.8, _MDE_Z
    assert abs(mde(0.100, 40) - 0.140) < 5e-4, mde(0.100, 40)
    assert abs(mde(0.075, 40) - 0.121) < 5e-4
    assert abs(mde(0.175, 40) - 0.185) < 5e-4

    # b = c = 2 on n = 40 is R20's own discordant structure: delta 0.000.
    prod = {f"r{i}": True for i in range(40)}
    cand = dict(prod)
    prod["r0"], prod["r1"] = True, True
    cand["r0"], cand["r1"] = False, False      # b = 2
    prod["r2"], prod["r3"] = False, False
    cand["r2"], cand["r3"] = True, True        # c = 2
    got = mcnemar_noninferiority(prod, cand)
    assert (got["b"], got["c"], got["delta"]) == (2, 2, 0.0), got
    assert abs(got["mde"] - 0.140) < 5e-4, got

    # The row-id contract: a split that hands the arms different rows is an
    # ERROR, not a delta. This is the check P-22 asks about.
    half = {k: v for k, v in list(cand.items())[:20]}
    assert mcnemar_noninferiority(prod, half)["verdict"] == "ERROR"
    logger.info("known-answer check PASSED (R20 MDE row + b=c=2 + row-id ERROR contract)")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    known_answer_check()

    rows = json.load(open(EVAL, encoding="utf-8"))
    derived = Counter(family_of(r) for r in rows)
    raw = Counter(r.get("family") or "<absent>" for r in rows)
    print(f"rows={len(rows)}")
    print(f"derived (family_of): {dict(sorted(derived.items()))}")
    print(f"raw     (r['family']): {dict(sorted(raw.items()))}")
    # Only the retrieval-scored families can carry a recall@5 MDE at all; the
    # refusal families deliberately carry `targets: []`.
    scored = {f: derived.get(f, 0) for f in sorted(RETRIEVAL_SCORED_FAMILIES)}
    print(f"retrieval-scored: {scored}  (sum={sum(scored.values())} of {len(rows)})")
    print(f"unscored (refusal families): "
          f"{ {f: n for f, n in derived.items() if f not in RETRIEVAL_SCORED_FAMILIES} }")
    print()

    header = f"{'family':26} {'n':>4} {'50/50':>9} {'70/30':>9} {'5-fold':>9}"
    for p in P_DISC:
        print(f"--- MDE at p_disc={p:.3f}  (z={_MDE_Z}; z=1.96 in brackets) ---")
        print(header)
        for fam, n in scored.items():
            splits = {"50/50": n // 2, "70/30": int(n * 0.3), "5-fold": n // 5}
            cells = "".join(
                f"{mde(p, v):>9.3f}" if v else f"{'inf':>9}" for v in splits.values()
            )
            print(f"{fam:26} {n:>4}{cells}   full={mde(p, n):.3f} "
                  f"[1.96: {mde(p, n, 1.96):.3f}, understated by "
                  f"{100 * (1 - 1.96 / _MDE_Z):.0f}%]")
        print()

    # The headline the split would break against.
    total_primary = derived.get("primary", 0)
    print(f"primary n = {total_primary}; a 70/30 test half is n = {int(total_primary * 0.3)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
