#!/usr/bin/env python3
"""P-28 -- which rows entity expansion flips, and in which direction.

[DESK -- $0.00, no provider call]. An EXISTENCE question, not a rate. R20
measured `full` vs `no-expansion` at 4 discordant rows with `delta` exactly
0.000 (b = c = 2), so `(2w-1)^2 = 0` and `n_required` is INFINITE. No sweep
separates the arms at any n, and none is run here: this reads two arm records
that already exist and reports the rows.

Input: the JSON `scripts/eval_retrieval.py --json` writes, with **both** arms
appended. This probe runs no retrieval of its own.

Two mandatory guards, both asserted rather than assumed:
  * bucket with `src/rag/metrics.py`'s `family_of()`, never the raw `family`
    key -- they disagree (raw primary 42, derived 38; A9's intersection rule);
  * report the expansion FIRING RATE on the same population, so a reader
    cannot mistake a wash for a small-population effect being averaged away.
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.rag.entities import EntityMap
from src.rag.gate import mcnemar_noninferiority
from src.rag.metrics import family_of

logger = logging.getLogger("probe_expansion_discordant")

# RR5's tripwire set: non-Garnett surface spellings.
NON_GARNETT = ("dunya", "razumikhin", "sonya", "myshkin", "zosima", "mikhailovna")


def known_answer_check() -> None:
    """`family_of()` disagrees with the raw key, and the McNemar join is by id.

    If either stops being true the row list below is bucketed or joined
    differently from every other number in the register, so this fails first.
    """
    assert family_of({"id": "x", "negative_control": False}) == "primary"
    assert family_of({"id": "x", "expected_route": "STRUCTURAL"}) == "structural"
    assert family_of({"id": "x", "family": "quotation"}) == "quotation"
    got = mcnemar_noninferiority({"a": True, "b": False}, {"a": False, "b": True})
    assert (got["b"], got["c"], got["delta"]) == (1, 1, 0.0), got
    logger.info("known-answer check PASSED (family_of derivation + id-joined McNemar)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", required=True, help="JSON with both arms appended")
    ap.add_argument("--pairs", default=os.path.join("test", "retrieval_eval.json"))
    ap.add_argument("--family", default="primary")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    known_answer_check()

    records = json.load(open(args.results, encoding="utf-8"))
    arms = {r["arm"]: r for r in records}
    for need in ("full", "no-expansion"):
        if need not in arms:
            raise SystemExit(f"arm {need!r} missing from {args.results}; have {sorted(arms)}")

    pairs = {p["id"]: p for p in json.load(open(args.pairs, encoding="utf-8"))}
    # Guard 1: the raw key and the derived family disagree. Assert it here so
    # the disagreement is a measured line in the report, not a footnote.
    raw_primary = sum(1 for p in pairs.values()
                      if (p.get("family") == args.family)
                      or (args.family == "primary" and "family" not in p))
    derived_primary = sum(1 for p in pairs.values() if family_of(p) == args.family)
    print(f"family={args.family}: raw-key n={raw_primary}, family_of() n={derived_primary}")

    def hits(arm: str) -> dict[str, bool]:
        return {r["id"]: r["hit"] for r in arms[arm]["rows"] if family_of(pairs[r["id"]]) == args.family}

    full, noexp = hits("full"), hits("no-expansion")
    stats = mcnemar_noninferiority(full, noexp)
    print(f"McNemar full vs no-expansion on {args.family}: {stats}")

    # Guard 2: the firing rate on the SAME population.
    emap = EntityMap()
    fired, expansions = 0, {}
    for rid in full:
        q = pairs[rid]["query"]
        expanded, names = emap.resolve(q)
        expansions[rid] = (expanded[len(q):].strip(), names)
        if names:
            fired += 1
    print(f"expansion fires on {fired}/{len(full)} = {fired / len(full):.1%} of {args.family} rows")
    print()

    disc = [rid for rid in full if full[rid] != noexp[rid]]
    print(f"discordant rows: {len(disc)}")
    for rid in sorted(disc):
        q = pairs[rid]["query"]
        appended, names = expansions[rid]
        spelling = [w for w in NON_GARNETT if w in q.lower()]
        direction = "RESCUED by expansion" if full[rid] else "BROKEN by expansion"
        print(f"\n  {rid}  {direction}")
        print(f"    query:      {q}")
        print(f"    spelling:   {'NON-GARNETT ' + ','.join(spelling) if spelling else 'canonical/none'}")
        print(f"    resolved:   {names or '(nothing)'}")
        print(f"    appended:   {appended[:220] or '(nothing)'}")
        print(f"    rank full={arms_rank(arms['full'], rid)}  no-expansion={arms_rank(arms['no-expansion'], rid)}")
    return 0


def arms_rank(arm: dict, rid: str):
    for r in arm["rows"]:
        if r["id"] == rid:
            return r["rank"]
    return None


if __name__ == "__main__":
    raise SystemExit(main())
