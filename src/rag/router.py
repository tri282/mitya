"""Deterministic query router: DEFAULT vs STRUCTURAL (ruling R14).

STRUCTURAL only when BOTH hold: a summary-intent keyword, AND a resolvable
(book, part, chapter) scope -- from the caller's hard filter, or a light
"book title + part/chapter N" regex resolved against `structure.json`. The
AND matters: STRUCTURAL is a `summaries.json` lookup keyed by chapter, so
intent without a resolvable chapter has nothing to look up ("summarize
Raskolnikov's philosophy" stays DEFAULT, correctly).

Pure function, zero latency, no ML classifier -- two routes, one of which is
only valid with a resolvable chapter key, so a keyword+scope test is exact
where a classifier would be merely probabilistic (design III.1).
"""

import logging
import re

from src.config import Config
from src.rag.ingest import roman_to_int

logger = logging.getLogger(__name__)

# Trailing prepositions are the hazard here, and it is not obvious. These are
# matched as bare lowercased substrings, so a phrase ending in a preposition
# that binds to its OBJECT only matches when the object follows -- which welds
# intent detection to word order.
#
# "what happens in" was exactly that (v2 P1 step 3b, defect 2): the reference
# had to trail the verb phrase for the substring to exist, so
#   "What happens in part 4 chapter 4 of C&P?"  -> STRUCTURAL
#   "In part 4 chapter 4 of C&P, what happens?" -> DEFAULT
# on the same question with the same resolvable scope. Scope extraction was
# never at fault -- it resolved both -- but route() early-returns below before
# extraction ever runs. Dropped to "what happens".
#
# "overview of" is deliberately kept as-is: "of" binds to the noun `overview`
# and trails its head in every phrasing ("an overview OF the chapter" reads
# naturally with the reference anywhere), so it carries no positional
# dependence. "summarize" and "recap" are single tokens.
#
# Adding an entry: if it ends in a preposition governing the reference, it will
# reintroduce this defect. test_router.py's phrasing-invariance check is what
# catches that.
SUMMARY_INTENT = ("summarize", "what happens", "overview of", "recap")

# Light "book title + part/chapter N" regex. Deliberately does not try to
# parse book titles itself -- title matching goes through
# Config.BOOK_TITLE_ALIASES so "Demons"/"The Devils" resolve the same as in
# retrieve.py's hard-filter builder.
_PART_RE = re.compile(r"\b(?:part|book)\s+([0-9]+|[ivxlc]+)\b", re.IGNORECASE)
_CHAPTER_RE = re.compile(r"\bchapter\s+([0-9]+|[ivxlc]+)\b", re.IGNORECASE)


def route(query: str, filters: dict | None, structure: dict, summaries: dict) -> tuple[str, dict | None]:
    """Route `query` to ("STRUCTURAL", scope) or ("DEFAULT", None).

    `filters` is the caller's UI-supplied hard filter (book/part/chapter, may
    be partial or absent). `structure` is the loaded structure.json content.
    `summaries` is the loaded summaries.json content -- D4 fix: R15 requires
    a resolved (book, part, chapter) scope with no summary to fall back to
    DEFAULT (logged), and `structure.json` alone cannot tell us that; only
    `summaries.json` can. This is a signature change from the phase-4 sketch
    (`route(query, filters, structure)`), made because validating against
    summaries.json at route time is the correct seam -- see
    plans/mitya-rag/05-impl/phase-4.md for the deviation note.
    """
    if not _has_summary_intent(query):
        return "DEFAULT", None

    scope = (_scope_from_filters(filters, structure, summaries)
              or _scan_query_for_scope(query, structure, summaries))
    if scope is None:
        return "DEFAULT", None
    return "STRUCTURAL", scope


def _has_summary_intent(query: str) -> bool:
    lower = query.lower()
    return any(keyword in lower for keyword in SUMMARY_INTENT)


def _scope_from_filters(filters: dict | None, structure: dict, summaries: dict) -> dict | None:
    """Scope from an explicit UI-supplied filter. Requires book + part +
    chapter all present -- a bare chapter number is ambiguous (chapter
    numbering resets per part/book in this corpus, e.g. Brothers Karamazov).

    Unlike retrieve.py's hard-filter builder, an unresolvable filter book
    here is not a loud-reject case -- it just means this candidate has no
    scope, so route() falls back to DEFAULT. The filter still reaches
    retrieve.py's builder regardless of route, and *that* is where an
    explicitly-selected but unmapped title is rejected loudly.
    """
    if not filters:
        return None
    raw_book = filters.get("book")
    part = filters.get("part")
    chapter = filters.get("chapter")
    if raw_book is None or part is None or chapter is None:
        return None
    try:
        book = Config.resolve_book_title(raw_book)
    except ValueError:
        logger.debug("router: filters book %r is not in the title alias map", raw_book)
        return None
    return _validate_scope(book, part, chapter, structure, summaries)


def _scan_query_for_scope(query: str, structure: dict, summaries: dict) -> dict | None:
    """Scope from a light regex scan of the query text itself."""
    lower = query.lower()
    canonical_book = None
    # Longest alias first so "the possessed" matches before "possessed" would
    # short-circuit inside it. \b-anchored so "demons" cannot match inside
    # "demonstration" (D1 fix -- same substring hazard names.fold_tokens()
    # guards against for entity names, generalized here to book titles).
    for alias in sorted(Config.BOOK_TITLE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", lower):
            canonical_book = Config.BOOK_TITLE_ALIASES[alias]
            break
    if canonical_book is None:
        return None

    part_match = _PART_RE.search(query)
    chapter_match = _CHAPTER_RE.search(query)
    if not part_match or not chapter_match:
        return None

    part = _parse_number(part_match.group(1))
    chapter = _parse_number(chapter_match.group(1))
    if part is None or chapter is None:
        return None

    return _validate_scope(canonical_book, part, chapter, structure, summaries)


def _validate_scope(book: str, part: int, chapter: int, structure: dict, summaries: dict) -> dict | None:
    """Resolve (book, part, chapter) against structure.json; None if the
    tuple doesn't exist -- STRUCTURAL has nothing to look up otherwise.

    Also checks summaries.json (R15, D4 fix): a scope that resolves
    structurally but has no summary text has nothing for the STRUCTURAL
    branch to show either, so it falls back to DEFAULT here -- logged,
    rather than reaching context.py and citing a chapter with no content.
    """
    book_entry = structure.get(book)
    if book_entry is None:
        logger.debug("router: book %r not in structure.json", book)
        return None
    part_entry = book_entry.get("parts", {}).get(str(part))
    if part_entry is None or chapter not in part_entry.get("chapters", []):
        logger.debug("router: unresolved scope book=%r part=%r chapter=%r", book, part, chapter)
        return None
    summary_key = f"{book}|{part}|{chapter}"
    if summary_key not in summaries:
        logger.warning(
            "router: resolved scope book=%r part=%r chapter=%r has no summary at key %r "
            "-- falling back to DEFAULT (R15)", book, part, chapter, summary_key,
        )
        return None
    return {"book": book, "part": part, "chapter": chapter}


def _parse_number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    try:
        return roman_to_int(token)
    except KeyError:
        return None
