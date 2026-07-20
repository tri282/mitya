"""Assemble the prompt-context block + deduped Sources list (design III.3, R20).

DEFAULT: top-3 reranked chunks. STRUCTURAL: chapter summary + top-2 dense
excerpts hard-filtered to that chapter. Sources are deduped on
(book, part, chapter), preserving rank order (top chunks often share a
chapter); STRUCTURAL lists the summarized chapter first.

Pure formatter -- does not retrieve anything itself. `points` must already be
the caller's chosen set for the route: retrieve.retrieve()+rerank() output
for DEFAULT, retrieve.retrieve_structural_excerpts() output for STRUCTURAL.
"""

import logging

from src.rag.ingest import BOOK_META

logger = logging.getLogger(__name__)


def build_context(
    route: str,
    points: list,
    summaries: dict,
    scope: dict | None,
) -> tuple[str, list[dict]]:
    """Build (prompt_context_block, deduped_sources) for the given route."""
    if route == "STRUCTURAL" and scope is not None:
        return _build_structural(points, summaries, scope)
    return _build_default(points)


def _build_default(points: list) -> tuple[str, list[dict]]:
    passages: list[str] = []
    sources: list[dict] = []
    seen: set[tuple] = set()
    for i, point in enumerate(points, start=1):
        payload = point.payload or {}
        passages.append(f'[{i}] ({_label(payload)}) "{payload.get("text", "")}"')
        key = (payload.get("book"), payload.get("part"), payload.get("chapter"))
        if key not in seen:
            seen.add(key)
            sources.append({"book": key[0], "part": key[1], "chapter": key[2]})
    return "\n".join(passages), sources


def _build_structural(points: list, summaries: dict, scope: dict) -> tuple[str, list[dict]]:
    summary_key = f"{scope['book']}|{scope['part']}|{scope['chapter']}"
    summary = summaries.get(summary_key)
    if summary is None:
        # R15 (D4 fix): router.route() now validates the scope against
        # summaries.json before ever returning STRUCTURAL, so this branch
        # should be unreachable in the normal flow -- kept as defense in
        # depth in case a caller builds a scope some other way. Fall back to
        # the DEFAULT rendering of `points` rather than fabricating an empty
        # quotation and citing a chapter that contributed nothing.
        logger.error(
            "build_context: STRUCTURAL scope %s has no summary at key %r -- "
            "falling back to DEFAULT rendering (R15)", scope, summary_key,
        )
        return _build_default(points)

    passages = [f'[1] ({_label(scope)}) "{summary}"']
    scope_key = (scope["book"], scope["part"], scope["chapter"])
    sources = [{"book": scope_key[0], "part": scope_key[1], "chapter": scope_key[2]}]
    seen = {scope_key}

    for i, point in enumerate(points, start=2):
        payload = point.payload or {}
        passages.append(f'[{i}] ({_label(payload)}) "{payload.get("text", "")}"')
        key = (payload.get("book"), payload.get("part"), payload.get("chapter"))
        if key not in seen:
            seen.add(key)
            sources.append({"book": key[0], "part": key[1], "chapter": key[2]})

    return "\n".join(passages), sources


def _label(entry: dict) -> str:
    """"(Book Title, Part N, Chapter M)" -- part_label ("Part"/"Book") comes
    from src.rag.ingest.BOOK_META so Brothers Karamazov reads "Book 5" not
    "Part 5", matching design III.4's example verbatim."""
    book = entry.get("book")
    part_label = BOOK_META.get(book, {}).get("part_label", "Part")
    return f"{book}, {part_label} {entry.get('part')}, Chapter {entry.get('chapter')}"
