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
                Whether STRUCTURAL serves it well is the open question.

MEASURED 2026-08-26, and it corrects the framing above. STRUCTURAL is not
summary-only: retrieve_structural_excerpts() takes the QUERY and does a dense
nearest-neighbour search hard-filtered to the chapter, so the block is the
chapter summary PLUS 2 query-relevant excerpts of raw chapter text. Running
--context on the four targeted probes, STRUCTURAL's excerpts overlap DEFAULT's
reranked picks by 27-99%. The real difference on a chapter-scoped question is
narrower than "summary vs retrieval": summary + dense-top-2 against
hybrid + reranked-top-3, over the same chapter's chunks.

So the information is there, and the remaining question is generation quality --
which belongs at P2 on the hosted model that will actually serve, not on
`local-q4km` (D1/D2: it is leaving the request path). Recorded as R40.

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

**--context**: the half of this question that does NOT need a generator.

Whether a targeted question is well served by STRUCTURAL turns first on a
question no judge can answer better than a diff: **is the information even in
the context STRUCTURAL builds?** STRUCTURAL serves summaries.json for the
chapter plus 2 excerpts; DEFAULT serves hybrid retrieval + rerank over that
chapter's chunks. Comparing the two context blocks needs no generation.

The asymmetry is what makes it worth running before the judge:

  * summary demonstrably lacks the information -> STRUCTURAL is wrong for
    targeted questions no matter which model generates, and that verdict
    survives the move to hosted serving (D1/D2).
  * summary contains it -> the remaining question is generation quality, which
    belongs at P2 on the model that will actually serve, not on `local-q4km`
    (A3: running it means starting the hardware v2 exists to escape).

Same move as R22 / probe_gate_scores.py: probe the mechanism, not the output.

Usage:

    python scripts/probe_route_intent.py            # routing table only
    python scripts/probe_route_intent.py --json out.json
    python scripts/probe_route_intent.py --context  # + both context blocks

Default mode is offline: router only, no Qdrant, no GPU, no API calls.
--context additionally loads the embedders, the reranker and local-mode Qdrant.
Still no generation, no judge, no API calls, $0.00.
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


def _build_both_contexts(query: str, scope: dict, state) -> dict:
    """Build the context block each route WOULD produce for one query.

    Mirrors src/api/app.py's two branches rather than importing them: the
    handler interleaves this with spans, the semaphore and the cache, none of
    which belong in a probe. The retrieval calls themselves are the same.
    """
    from src.rag.context import build_context
    from src.rag.retrieve import (
        build_hard_filter, rerank, retrieve, retrieve_structural_excerpts,
    )

    expanded, _ = state["entities"].resolve(query, active_book=scope["book"])

    structural_points = retrieve_structural_excerpts(
        state["client"], state["embedder"], expanded,
        scope["book"], scope["part"], scope["chapter"],
    )
    structural_block, structural_sources = build_context(
        "STRUCTURAL", structural_points, state["summaries"], scope,
    )

    # DEFAULT restricted to the same chapter -- the comparison is about what
    # each route puts in the prompt for THIS chapter, so letting the hybrid leg
    # roam the whole corpus would be measuring a different question.
    hard_filter = build_hard_filter({
        "book": scope["book"], "part": scope["part"], "chapter": scope["chapter"],
    })
    default_points = retrieve(
        state["client"], state["embedder"], state["bm25"], expanded, hard_filter,
    )
    default_points = rerank(query, default_points, state["ranker"])
    default_block, default_sources = build_context(
        "DEFAULT", default_points, state["summaries"], None,
    )

    return {
        "structural_chars": len(structural_block),
        "structural_sources": len(structural_sources),
        "structural_block": structural_block,
        "default_chars": len(default_block),
        "default_sources": len(default_sources),
        "default_block": default_block,
    }


def load_retrieval_state() -> dict:
    """Everything --context needs. Expensive; only built when asked for."""
    from fastembed import SparseTextEmbedding, TextEmbedding
    from qdrant_client import QdrantClient

    from src.rag.entities import EntityMap
    from src.rag.retrieve import build_ranker

    logger.info("loading dense embedder: %s", Config.EMBED_MODEL)
    embedder = TextEmbedding(model_name=Config.EMBED_MODEL)
    logger.info("loading sparse embedder: Qdrant/bm25")
    bm25 = SparseTextEmbedding(model_name="Qdrant/bm25")
    logger.info("loading reranker: %s", Config.RERANK_MODEL)
    ranker = build_ranker()
    logger.info("loading entity map")
    entities = EntityMap()
    return {
        "embedder": embedder, "bm25": bm25, "ranker": ranker, "entities": entities,
        "client": QdrantClient(path=Config.VECTOR_DB_DIR),
        "summaries": json.load(open(os.path.join(Config.RAG_DIR, "summaries.json"))),
    }


def report_contexts(results: list[dict]) -> None:
    """Print both blocks for every targeted probe, for reading side by side.

    No verdict is computed. Whether a chapter summary "covers" a question about
    one character's interiority is a judgement about text, and a char-count
    heuristic dressed up as a coverage score would be exactly the kind of
    fabricated measurement charter II forbids. The probe puts the two blocks in
    front of a person; the person decides.
    """
    targeted = [r for r in results if r["shape"] == "targeted" and "context" in r]
    logger.info("")
    logger.info("=" * 100)
    logger.info("CONTEXT COMPARISON -- %d targeted probes", len(targeted))
    logger.info("=" * 100)
    for r in targeted:
        c = r["context"]
        logger.info("")
        logger.info("QUERY: %s", r["query"])
        logger.info(
            "  STRUCTURAL builds %d chars / %d sources | DEFAULT builds %d chars / %d sources",
            c["structural_chars"], c["structural_sources"],
            c["default_chars"], c["default_sources"],
        )
        logger.info("  --- STRUCTURAL context (chapter summary + excerpts) ---")
        logger.info("%s", c["structural_block"])
        logger.info("  --- DEFAULT context (hybrid retrieval + rerank, same chapter) ---")
        logger.info("%s", c["default_block"])
        logger.info("-" * 100)


def run(with_context: bool = False) -> list[dict]:
    structure = json.load(open(os.path.join(Config.RAG_DIR, "structure.json")))
    summaries = json.load(open(os.path.join(Config.RAG_DIR, "summaries.json")))

    state = load_retrieval_state() if with_context else None

    results = []
    for shape, query, hint in PROBES:
        actual_route, scope = route(query, None, structure, summaries)
        row = {
            "shape": shape,
            "query": query,
            "route": actual_route,
            "scope_resolved": scope is not None,
            "hypothesis": hint,
            "agrees": actual_route == hint,
        }
        # Only the targeted rows are worth the retrieval cost: the wholesale
        # rows are what STRUCTURAL is FOR, and the controls never reach it.
        if with_context and shape == "targeted" and scope is not None:
            row["context"] = _build_both_contexts(query, scope, state)
        results.append(row)
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
    ap.add_argument(
        "--context", action="store_true",
        help="also build and print the context block each route would produce "
             "for the targeted probes (loads embedders, reranker and Qdrant)",
    )
    args = ap.parse_args()

    results = run(with_context=args.context)
    report(results)
    if args.context:
        report_contexts(results)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2, ensure_ascii=False)
        logger.info("wrote %s", args.json)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
