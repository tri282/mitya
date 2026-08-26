"""Route-intent probe: the wholesale-vs-targeted evidence step 3b defers on.

Step 3b fixed defect 2 by dropping a trailing preposition from `SUMMARY_INTENT`
("what happens in" -> "what happens"), which makes routing invariant to where
the chapter reference sits. That fix also **widens** intent, and the widening
admits a class the router did not previously take:

    wholesale   "In part 4 chapter 4 of C&P, what happens?"
                asks for the chapter. STRUCTURAL is right -- that IS a
                summaries.json lookup.

    targeted    "What happens to Raskolnikov's conscience in part 4 chapter 4?"
                is scoped to a chapter but asks about one thread inside it.
                STRUCTURAL serves 2 excerpts and the chapter summary, which may
                simply not contain the answer.

This is NOT the same question as defect 2, and conflating them is why this is a
separate script rather than more eval rows. Phrasing invariance is a *property*:
a rephrasing must not change the route, no judgement involved, and
test_router.py pins it. Wholesale-vs-targeted is a *design decision* with no
correct answer derivable from the code -- so it gets probed and decided, not
asserted.

Why not eval rows: an eval row carries an `expected_route`, and the whole point
is that nobody has decided what these rows' expected route is. Committing them
with a guessed label would launder the open question into a measurement, and
then the eval set would enforce the guess. Same reason step 14 used a probe
script for R15's threshold rather than pre-labelling the rows.

Cosmetic today -- both routes answer, one 45% faster. From P2 on, `route`
selects which model is paid for, so this line has a price.

Usage:

    python scripts/probe_route_intent.py            # table
    python scripts/probe_route_intent.py --json out.json

Offline: router only, no Qdrant, no GPU, no API calls.
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config  # noqa: E402
from src.rag.router import route  # noqa: E402

logger = logging.getLogger(__name__)

# One resolvable chapter, held fixed. The variable under test is the SHAPE of
# the question, so anything else that could move the route is pinned.
REF = "Crime and Punishment part 4 chapter 4"

# `shape` is the label the decision is about; `expect_hint` is what the plan's
# option (a) would predict, recorded so the table can be read as agreement or
# disagreement rather than a bare list of routes. It is a hypothesis, NOT an
# expected value -- nothing fails if a row disagrees.
PROBES = [
    # --- wholesale: the chapter itself is the subject ---------------------
    ("wholesale", f"What happens in {REF}?", "STRUCTURAL"),
    ("wholesale", f"In {REF}, what happens?", "STRUCTURAL"),
    ("wholesale", f"Summarize {REF}.", "STRUCTURAL"),
    ("wholesale", f"Give an overview of {REF}.", "STRUCTURAL"),

    # --- targeted: scoped to the chapter, about one thread inside it ------
    ("targeted", f"What happens to Raskolnikov's conscience in {REF}?", "STRUCTURAL"),
    ("targeted", f"What happens to Sonya in {REF}?", "STRUCTURAL"),
    ("targeted", f"In {REF}, what happens to the theme of confession?", "STRUCTURAL"),
    ("targeted", f"Summarize Raskolnikov's state of mind in {REF}.", "STRUCTURAL"),

    # --- thematic, scoped, but with no summary verb at all ----------------
    # Control: these must stay DEFAULT under every option on the table. If one
    # of them routes STRUCTURAL, the AND-with-scope guard has broken and the
    # decision below is moot.
    ("control-thematic", f"Why does Raskolnikov visit Sonya in {REF}?", "DEFAULT"),
    ("control-thematic", f"What is the significance of the lamp in {REF}?", "DEFAULT"),

    # --- unscoped: no resolvable chapter, must stay DEFAULT ---------------
    # The other half of the guard: summary intent alone has nothing to look up.
    ("control-unscoped", "What happens to Raskolnikov after the murder?", "DEFAULT"),
    ("control-unscoped", "Summarize Raskolnikov's theory of the extraordinary man.", "DEFAULT"),
]


def run() -> list[dict]:
    structure = json.load(open(os.path.join(Config.RAG_DIR, "structure.json")))
    summaries = json.load(open(os.path.join(Config.RAG_DIR, "summaries.json")))

    results = []
    for shape, query, hint in PROBES:
        actual_route, scope = route(query, None, structure, summaries)
        results.append({
            "shape": shape,
            "query": query,
            "route": actual_route,
            "scope_resolved": scope is not None,
            "hypothesis": hint,
            "agrees": actual_route == hint,
        })
    return results


def report(results: list[dict]) -> None:
    logger.info("%-18s%-12s%-7s%-8squery", "shape", "route", "scope", "vs (a)")
    logger.info("-" * 100)
    for r in results:
        # scope="no" on a control row means extraction never RAN -- route()
        # early-returns on absent summary intent, before scope is looked at.
        # It is not a claim that the reference is unresolvable.
        logger.info(
            "%-18s%-12s%-7s%-8s%s",
            r["shape"], r["route"],
            "yes" if r["scope_resolved"] else "no",
            "=" if r["agrees"] else "DIFFERS",
            r["query"],
        )

    controls = [r for r in results if r["shape"].startswith("control")]
    broken = [r for r in controls if r["route"] != "DEFAULT"]
    logger.info("")
    if broken:
        logger.warning(
            "GUARD BROKEN: %d control row(s) routed STRUCTURAL. The AND-with-scope condition "
            "is not holding; the wholesale/targeted decision is moot until it does.", len(broken),
        )
    else:
        logger.info("guard holds: all %d control rows DEFAULT.", len(controls))

    targeted = [r for r in results if r["shape"] == "targeted"]
    n_struct = sum(r["route"] == "STRUCTURAL" for r in targeted)
    logger.info(
        "targeted rows routing STRUCTURAL: %d/%d -- this is the count the decision is about. "
        "Every one of them is served the chapter summary rather than hybrid retrieval over "
        "that chapter's chunks.", n_struct, len(targeted),
    )
    logger.info(
        "\nThis probe does not decide anything. Read it beside the quality set's matched pair "
        "(q-0037/q-0038, same scene by description vs by chapter reference) and the Textual "
        "theme added at P0 step 9b, which is what actually measures STRUCTURAL's answers."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", help="write per-row results to this path")
    args = ap.parse_args()

    results = run()
    report(results)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2, ensure_ascii=False)
        logger.info("wrote %s", args.json)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
