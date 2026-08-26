# test/test_router.py
"""Phase 4 verify: router truth table (design III.1, ruling R14).

Plain-assert script, not pytest (pytest is not installed in .venv -- see
plans/mitya-rag/05-impl/phase-4.md). Offline, no GPU, no Qdrant: uses a small
hand-built structure.json fixture rather than the real 231-chapter one, so
this only exercises router.py's own logic.

Usage:
    python test/test_router.py
"""

import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.rag.router import route

logger = logging.getLogger(__name__)

STRUCTURE = {
    "Crime and Punishment": {
        "parts": {
            "1": {"chapters": [1, 2, 3, 4, 5, 6, 7], "unit_count": 7},
            "6": {"chapters": [1, 2, 3, 4, 5, 6, 7, 8], "unit_count": 8},
        },
    },
    "The Possessed": {
        "parts": {
            # part 1 chapter 1 added for the D1 regression case below --
            # without it, the pre-fix substring bug's wrong scope would fail
            # `_validate_scope` anyway (this fixture had no part "1"), and
            # the test would pass even with the bug still present.
            "1": {"chapters": [1], "unit_count": 1},
            "3": {"chapters": [1, 2, 3, 4, 5, 6, 7, 8], "unit_count": 8},
        },
    },
}

# summaries.json fixture -- keyed "Book|part|chapter". Deliberately omits
# "Crime and Punishment|6|8" (structurally valid, no summary) for the D4 case.
SUMMARIES = {
    "Crime and Punishment|1|7": "Raskolnikov confesses to Sonya.",
    "Crime and Punishment|6|1": "...",
    "Crime and Punishment|6|2": "...",
    "Crime and Punishment|6|3": "...",
    "Crime and Punishment|6|4": "...",
    "Crime and Punishment|6|5": "...",
    "Crime and Punishment|6|6": "...",
    "Crime and Punishment|6|7": "...",
    "The Possessed|3|6": "Stavrogin's confession.",
    "The Possessed|1|1": "Introductory chapter.",
}

CASES = [
    # (description, query, filters, expected_route, expected_scope_or_None)
    (
        "summary intent + resolvable regex scope -> STRUCTURAL",
        "summarize Crime and Punishment chapter 7 of part 1",
        None,
        "STRUCTURAL",
        {"book": "Crime and Punishment", "part": 1, "chapter": 7},
    ),
    (
        "summary intent + resolvable filters scope -> STRUCTURAL",
        "what happens in this chapter",
        {"book": "The Possessed", "part": 3, "chapter": 6},
        "STRUCTURAL",
        {"book": "The Possessed", "part": 3, "chapter": 6},
    ),
    (
        "book-title alias resolves through filters -> STRUCTURAL",
        "recap this",
        {"book": "Demons", "part": 3, "chapter": 6},
        "STRUCTURAL",
        {"book": "The Possessed", "part": 3, "chapter": 6},
    ),
    (
        "intent alone, no resolvable chapter -> DEFAULT (ruling R14 AND condition)",
        "summarize Raskolnikov's theory of the extraordinary man",
        None,
        "DEFAULT",
        None,
    ),
    (
        "resolvable scope alone, no summary intent -> DEFAULT",
        "Crime and Punishment chapter 7 of part 1",
        None,
        "DEFAULT",
        None,
    ),
    (
        "neither intent nor scope -> DEFAULT",
        "what is Raskolnikov's theory of the extraordinary man",
        None,
        "DEFAULT",
        None,
    ),
    (
        "intent + out-of-range chapter -> DEFAULT (not in structure fixture)",
        "summarize Crime and Punishment chapter 99 of part 1",
        None,
        "DEFAULT",
        None,
    ),
    (
        "intent + unmapped book title in filters -> DEFAULT, not a raise",
        "recap this",
        {"book": "Not A Real Book", "part": 1, "chapter": 1},
        "DEFAULT",
        None,
    ),
    (
        # D1 regression: "demons" (an alias for The Possessed) is a raw
        # substring of "demonstration". Pre-fix, the unanchored `alias in
        # lower` scan matched it, and with structure's Possessed part-1
        # chapter-1 fixture present, _validate_scope succeeded, wrongly
        # routing The Possessed I.1's summary as authoritative grounding for
        # a query about neither. Word-boundary matching must reject it.
        "book alias must not match as a bare substring (D1)",
        "recap the demonstration in part 1 chapter 1",
        None,
        "DEFAULT",
        None,
    ),
    (
        # D4 regression: a scope that resolves against structure.json but has
        # no entry in summaries.json (Crime and Punishment part 6 chapter 8,
        # deliberately omitted from the SUMMARIES fixture above) must fall
        # back to DEFAULT per R15, rather than route STRUCTURAL toward a
        # summary lookup that will come back empty.
        "resolved scope with no summary falls back to DEFAULT (D4, R15)",
        "summarize Crime and Punishment chapter 8 of part 6",
        None,
        "DEFAULT",
        None,
    ),
]


# --- phrasing invariance (v2 P1 step 3b, defect 2) -----------------------
#
# A property, not a table. The CASES above are examples, and examples are what
# let this defect live: the `structural` eval family is three rows of ONE
# template varying the book, so it could not see a phrasing sensitivity at all.
# Generating the positions here keeps working for phrasings nobody authored.
#
# The property: a rephrasing that preserves BOTH the intent and the reference
# must not change the route. Not a quality judgement and not a sampled metric --
# it either holds for every position or the router is phrasing-dependent, which
# from P2 on means a rephrasing silently changes which model is paid for.

# One question about one chapter, written the way four different users would
# actually type it. Hand-written rather than generated: the connector between an
# intent phrase and its reference ("what happens IN x", "overview OF x") is
# grammar, not string concatenation, and a generated template produces
# ungrammatical text that agrees on DEFAULT for the wrong reason -- a test that
# fails without discriminating is worse than no test.
#
# Every phrasing below resolves the SAME (book, part, chapter) and asks for the
# SAME thing. Any disagreement in route is the defect.
INVARIANCE_SCOPE = {"book": "Crime and Punishment", "part": 1, "chapter": 7}

PHRASING_SETS = {
    "what happens": [
        ("trailing", "What happens in Crime and Punishment part 1 chapter 7?"),
        ("leading", "In Crime and Punishment part 1 chapter 7, what happens?"),
        ("parenthetical", "Crime and Punishment part 1 chapter 7 - what happens?"),
    ],
    "summarize": [
        ("trailing", "Summarize Crime and Punishment part 1 chapter 7."),
        ("leading", "In Crime and Punishment part 1 chapter 7, summarize the chapter."),
        ("parenthetical", "Crime and Punishment part 1 chapter 7 - summarize it."),
    ],
    "overview of": [
        ("trailing", "Give an overview of Crime and Punishment part 1 chapter 7."),
        ("leading", "In Crime and Punishment part 1 chapter 7, give an overview of the chapter."),
        ("parenthetical", "Crime and Punishment part 1 chapter 7 - overview of the chapter?"),
    ],
    "recap": [
        ("trailing", "Recap Crime and Punishment part 1 chapter 7."),
        ("leading", "In Crime and Punishment part 1 chapter 7, recap the events."),
        ("parenthetical", "Crime and Punishment part 1 chapter 7 - recap it."),
    ],
}


def check_phrasing_invariance() -> int:
    """Assert one route and one scope across every phrasing. Returns failures.

    Asserting STRUCTURAL specifically, not merely 'all three agree': a router
    that regressed to DEFAULT everywhere would satisfy agreement while being
    entirely broken.
    """
    failures = 0
    for intent, phrasings in PHRASING_SETS.items():
        results = [
            (position, query) + route(query, None, STRUCTURE, SUMMARIES)
            for position, query in phrasings
        ]
        ok = all(r == "STRUCTURAL" and sc == INVARIANCE_SCOPE for _, _, r, sc in results)
        logger.info("[%s] phrasing invariance: %r", "PASS" if ok else "FAIL", intent)
        if not ok:
            failures += 1
            for position, query, actual_route, actual_scope in results:
                logger.error(
                    "  %-14s -> %-10s scope=%-5s %r",
                    position, actual_route, actual_scope is not None, query,
                )
    return failures


def main() -> None:
    failures = 0
    for description, query, filters, expected_route, expected_scope in CASES:
        actual_route, actual_scope = route(query, filters, STRUCTURE, SUMMARIES)
        ok = actual_route == expected_route and actual_scope == expected_scope
        status = "PASS" if ok else "FAIL"
        logger.info("[%s] %s", status, description)
        if not ok:
            failures += 1
            logger.error(
                "  query=%r filters=%r -> got (%r, %r), expected (%r, %r)",
                query, filters, actual_route, actual_scope, expected_route, expected_scope,
            )

    logger.info("-" * 60)
    invariance_failures = check_phrasing_invariance()
    failures += invariance_failures

    logger.info("=" * 60)
    total = len(CASES) + len(PHRASING_SETS)
    logger.info("%d/%d checks passed", total - failures, total)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
