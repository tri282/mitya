#!/usr/bin/env python3
"""Find which chapter an event is in -- the labeling instrument for P0 steps 6-9.

Keyword-searches data/rag/summaries.json (231 chapter summaries) and prints the
best-matching chapter keys with a snippet, so authoring a row is "search, read,
confirm" instead of scrolling a 1.9MB novel. --text additionally greps the raw
corpus for an exact phrase, which is the stronger check when a summary is
ambiguous.

This decides the `targets` of an eval row, so it is deliberately dumb and
inspectable: term-overlap scoring, no embeddings, no model. It proposes; you
confirm against the text.

Usage:
    python scripts/find_chapter.py myshkin ivolgin lodge
    python scripts/find_chapter.py --book "The Idiot" --top 5 nastasya birthday
    python scripts/find_chapter.py --text "Rogozhin"
"""

import argparse
import json
import logging
import os
import re
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config

logger = logging.getLogger(__name__)

# Words too common in these summaries to carry signal; scoring them rewards
# length instead of relevance.
STOPWORDS = frozenset("""a an the and or but of to in on at for with from by is are was were
he she it his her him they them this that as into about what who which when where does do how""".split())


def score(summary: str, terms: list[str]) -> int:
    """Count distinct query terms present as whole words. Distinct, not total:
    a summary repeating one term ten times is not a better match than one
    mentioning all three terms once."""
    low = summary.lower()
    return sum(1 for t in terms if re.search(rf"\b{re.escape(t)}", low))


def snippet(summary: str, terms: list[str], width: int = 220) -> str:
    low = summary.lower()
    hits = [m.start() for t in terms for m in [re.search(rf"\b{re.escape(t)}", low)] if m]
    start = max(0, min(hits) - 60) if hits else 0
    return ("..." if start else "") + " ".join(summary[start:start + width].split()) + "..."


def search(summaries: dict, terms: list[str], book: str | None, top: int) -> list[tuple]:
    scored = []
    for key, summary in summaries.items():
        book_name = key.split("|")[0]
        if book and book_name != book:
            continue
        s = score(summary, terms)
        if s:
            scored.append((s, key, summary))
    # Sort by score, then by key, so repeated runs print the same order.
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[:top]


def grep_corpus(structure: dict, phrase: str, book: str | None, limit: int = 5) -> None:
    """Exact-phrase grep over the raw text. Reports line offsets only -- chapter
    attribution stays the summary search's job, since chapter boundaries live in
    the ingestion parser, not here."""
    for book_name, meta in structure.items():
        if book and book_name != book:
            continue
        path = os.path.join(Config.DATA_DIR, "rag", meta["file"])
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        hits = [i for i, line in enumerate(lines, 1) if phrase.lower() in line.lower()]
        logger.info("%s: %d line(s) contain %r%s", book_name, len(hits), phrase,
                    f" (first {limit}: {hits[:limit]})" if hits else "")


def main() -> None:
    ap = argparse.ArgumentParser(description="Locate the chapter an event belongs to.")
    ap.add_argument("terms", nargs="*", help="keywords; scored by distinct whole-word hits")
    ap.add_argument("--book", default=None, help="restrict to one canonical book title")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--text", default=None, help="also grep the raw corpus for this exact phrase")
    args = ap.parse_args()

    with open(Config.SUMMARIES_PATH, encoding="utf-8") as f:
        summaries = json.load(f)

    if args.book:
        args.book = Config.resolve_book_title(args.book)

    if args.terms:
        terms = [t.lower() for t in args.terms if t.lower() not in STOPWORDS]
        if not terms:
            logger.error("every term was a stopword; give something distinctive")
            sys.exit(2)
        results = search(summaries, terms, args.book, args.top)
        if not results:
            logger.info("no chapter summary contains any of %s", terms)
        for s, key, summary in results:
            book, part, chapter = key.split("|")
            logger.info("[%d/%d] %s part %s chapter %s", s, len(terms), book, part, chapter)
            logger.info('    {"book": "%s", "part": %s, "chapter": %s}', book, part, chapter)
            logger.info("    %s", snippet(summary, terms))

    if args.text:
        with open(Config.STRUCTURE_PATH, encoding="utf-8") as f:
            structure = json.load(f)
        grep_corpus(structure, args.text, args.book)

    if not args.terms and not args.text:
        ap.error("give keywords, --text, or both")


def _demo() -> None:
    """Self-check: scoring and snippeting on a fixture, no corpus needed."""
    fixture = {
        "The Idiot|1|8": "Myshkin arrives at the Ivolgin household and takes a room.",
        "The Idiot|1|9": "Ganya quarrels with his sister about Nastasya.",
    }
    hits = search(fixture, ["myshkin", "ivolgin"], None, 8)
    assert len(hits) == 1 and hits[0][1] == "The Idiot|1|8", hits
    assert hits[0][0] == 2, hits
    # Book filter excludes everything from another book.
    assert search(fixture, ["myshkin"], "Crime and Punishment", 8) == []
    # Distinct terms, not repeats: a term repeated scores once.
    assert score("Myshkin, Myshkin, Myshkin.", ["myshkin"]) == 1
    assert "Ivolgin" in snippet(fixture["The Idiot|1|8"], ["ivolgin"])
    logger.info("scripts/find_chapter.py self-check passed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--demo" in sys.argv:
        _demo()
    else:
        main()
