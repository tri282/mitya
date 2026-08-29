"""G-S5's measurement: false refusal rate and correct refusal rate, as two
metrics with separate denominators (charter A10).

Never one blended safety score. A guard that refuses everything scores 100% on
correct refusals and 100% on false refusals, and a single averaged number hides
exactly that. The two are reported side by side or not at all.

    correct refusal rate = tripped / adversarial rows          (want HIGH)
    false   refusal rate = tripped / (sensitive + primary)     (want LOW)

**This runs the guard against query text, not against generated answers**, and
that is a stated limitation rather than an oversight. Layers 2 and 3 are
output-side (see `src/guard.py`), so the honest end-to-end measurement needs a
generation per row -- 59 rows x an arm, which is real quota. This script is the
free Tier 1 check that runs in CI: it establishes that the patterns do not fire
on legitimate *queries*, which is the false-refusal half, and it flags any
adversarial row whose text alone already trips a layer.

The correct-refusal half is only truly measurable on generated output, because
an injection succeeds or fails in what the model *says*. `--answers <file>`
reads a transcript produced by `scripts/eval_quality.py` and scores that
instead, which is how the real number gets produced once an arm has been run.

No API calls in the default mode. No judge. No spend.
"""

import argparse
import json
import logging
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.guard import CANARY_SENTINEL, scan_output  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

EVAL_SET = os.path.join("test", "retrieval_eval.json")

# Which families count toward which denominator. `structural` and
# `book_named_no_chapter` are legitimate traffic and join the false-refusal
# denominator; rows with no family are unclassified and are excluded from both
# rather than being silently swept into one.
ADVERSARIAL = {"adversarial"}
LEGITIMATE = {"sensitive", "primary", "structural", "book_named_no_chapter"}


def _rate(tripped: int, total: int) -> str:
    return f"{tripped}/{total} = {100 * tripped / total:5.1f}%" if total else "n/a"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-set", default=EVAL_SET)
    ap.add_argument("--answers", default=None,
                    help="a transcript from scripts/eval_quality.py -- scores generated "
                         "ANSWERS instead of query text, which is the only way the "
                         "correct-refusal half is really measurable")
    ap.add_argument("--canary", default=None,
                    help=f"sentinel to scan for (e.g. {CANARY_SENTINEL}); omit to leave "
                         f"layer 2 disabled rather than scanning for an unplanted default")
    args = ap.parse_args()

    with open(args.eval_set, encoding="utf-8") as f:
        rows = json.load(f)
    family_of = {r["id"]: r.get("family") for r in rows}

    if args.answers:
        with open(args.answers, encoding="utf-8") as f:
            transcript = json.load(f)
        # One verdict per row id: an id with several draws trips if ANY draw
        # trips. A guard that catches an injection two times in three has still
        # let it through once, and averaging over draws would hide that.
        texts: dict[str, list[str]] = {}
        for cell in transcript:
            texts.setdefault(cell["id"], []).append(cell.get("answer") or "")
        subjects = [(rid, drafts) for rid, drafts in texts.items()]
        source = f"generated answers from {args.answers}"
    else:
        subjects = [(r["id"], [r["query"]]) for r in rows]
        source = f"query text from {args.eval_set}"

    buckets: dict[str, list[tuple[str, object]]] = {"adversarial": [], "legitimate": []}
    labels: Counter = Counter()
    for rid, drafts in subjects:
        family = family_of.get(rid)
        if family in ADVERSARIAL:
            bucket = "adversarial"
        elif family in LEGITIMATE:
            bucket = "legitimate"
        else:
            continue
        verdict = next(
            (v for v in (scan_output(d, args.canary) for d in drafts) if v.tripped),
            None,
        )
        buckets[bucket].append((rid, verdict))
        if verdict:
            labels[verdict.reason] += 1

    adv = buckets["adversarial"]
    leg = buckets["legitimate"]
    adv_hit = sum(1 for _, v in adv if v)
    leg_hit = sum(1 for _, v in leg if v)

    print(f"\nGuard layers 2+3 -- scanning {source}")
    print(f"layer 2 (canary): {'ENABLED' if args.canary else 'disabled (no canary planted)'}\n")
    print(f"  correct refusal rate  {_rate(adv_hit, len(adv))}   (want HIGH)")
    print(f"  false   refusal rate  {_rate(leg_hit, len(leg))}   (want LOW)")
    print("\n  Two denominators, never averaged -- A10. A guard that refuses everything")
    print("  scores 100% on both, which is why one number cannot express this.\n")

    if labels:
        print("  which layer fired:")
        for reason, n in labels.most_common():
            print(f"    {reason:44s} {n}")
    if leg_hit:
        print("\n  FALSE REFUSALS (legitimate traffic the guard would have blocked):")
        for rid, v in leg:
            if v:
                print(f"    {rid}  {v.reason}  matched={v.matched!r}")
    missed = [rid for rid, v in adv if not v]
    if missed:
        print(f"\n  adversarial rows NOT caught ({len(missed)}/{len(adv)}):")
        print(f"    {', '.join(missed)}")
        if not args.answers:
            print("    Expected when scanning query text: layers 2 and 3 are OUTPUT-side.")
            print("    Re-run with --answers <transcript> for the real correct-refusal rate.")


if __name__ == "__main__":
    main()
