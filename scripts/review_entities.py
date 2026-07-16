# scripts/review_entities.py
"""Triage aid for the design II.4 human review gate.

Reads data/rag/entities.raw.json (machine output) and writes a markdown report
ranking what a reviewer should look at first. It never writes entities.json --
that stays a human act, by design.

The gate exists because the teacher over-extracts: it emits an entry for anyone
named in passing, and it fragments one person across several keys when the text
calls them different things. Both are invisible in the raw file, which is
alphabetical and carries no occurrence counts. This script supplies the two
signals that make the difference obvious:

  - mention counts, from the actual novel text, so one-off mentions sort to the
    bottom and the real cast to the top;
  - merge candidates, from alias collisions and name containment, so fragments
    of one person land next to each other instead of 300 entries apart.

Judgement stays with the reviewer: this only decides reading order.

Usage:
    python scripts/review_entities.py                    # write the report
    python scripts/review_entities.py --rare-max 2       # tune the rare cutoff
"""

import argparse
import json
import logging
import os
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from itertools import combinations

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config
from src.rag.ingest import parse_all

logger = logging.getLogger(__name__)

REPORT_PATH = os.path.join(Config.RAG_DIR, "entities.review.md")

# Canonicals this similar are probably the same person spelled two ways.
# Tuned by eye on this corpus: 0.85 pairs "Marfa Petrovna"/"Marfa Petrovna
# Svidrigailov" without pairing the Karamazov brothers with each other.
SIMILARITY_THRESHOLD = 0.85

# Aliases that match half the corpus. The teacher gave "Jesus" the aliases
# "He", "Him" and "Lord", which scored it 1292 mentions in C&P -- every pronoun
# in the book. Harmless in the raw file, actively wrong in P4, where aliases are
# what a name query matches on: one of these drags in every chapter ever written.
UNUSABLE_ALIASES = {
    "he", "him", "his", "she", "her", "hers", "they", "them", "their", "it", "its",
    "i", "me", "my", "you", "your", "we", "us", "our", "who", "one", "man", "woman",
    "the", "a", "an", "lord", "god", "sir", "madam", "mother", "father", "brother",
    "sister", "son", "daughter", "prince", "princess", "general", "captain",
}


def name_forms(key: str, body: dict) -> list[str]:
    """Every string the text might use for this person, longest first so the
    mention regex prefers the fullest match and does not double-count."""
    forms = {body["canonical"], key, *body.get("aliases", [])}
    return sorted((f for f in forms if isinstance(f, str) and f.strip()), key=len, reverse=True)


def count_mentions(text: str, forms: list[str]) -> int:
    """Whole-word occurrences of any name form. One alternation, longest-first,
    so 'Rodion Romanovich Raskolnikov' consumes the span instead of scoring
    once for the full name and again for 'Rodion'."""
    if not forms:
        return 0
    pattern = "|".join(re.escape(f) for f in forms)
    return len(re.findall(rf"(?<!\w)(?:{pattern})(?!\w)", text))


def tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^\w]+", s.lower()) if t}


def merge_candidates(entries: dict) -> list[tuple[str, str, str]]:
    """(key_a, key_b, why) for pairs worth a reviewer's eye.

    Three signals, cheapest and most decisive first. Deliberately noisy on the
    side of showing too much: a false pair costs one glance, a missed fragment
    costs a wrong retrieval every time that person is asked about.
    """
    found: dict[tuple[str, str], str] = {}

    # 1. The same alias under two keys. Near-certain fragmentation: the text
    #    called one person "Rodya" and the teacher filed it twice.
    by_alias: dict[str, set[str]] = defaultdict(set)
    for key, body in entries.items():
        for alias in body.get("aliases", []):
            if isinstance(alias, str) and alias.strip():
                # A set, not a list: one entity can carry two aliases that
                # differ only in case, which would otherwise pair it with itself.
                by_alias[alias.lower()].add(key)
    for alias, keys in by_alias.items():
        for a, b in combinations(sorted(keys), 2):
            found[(a, b)] = f"share alias {alias!r}"

    for (a, body_a), (b, body_b) in combinations(sorted(entries.items()), 2):
        if (a, b) in found:
            continue
        ca, cb = body_a["canonical"], body_b["canonical"]

        # 2. Identical canonical under two keys -- the teacher filed one person
        #    twice, differing only in how it shortened the reference name.
        if ca.lower() == cb.lower():
            found[(a, b)] = f"identical canonical {ca!r}"
            continue

        # 3. One canonical's words fully contained in the other's. Catches
        #    "Raskolnikov" vs "Rodion Romanovich Raskolnikov". Also catches a
        #    surname shared by a family, which is a real ambiguity to rule on.
        ta, tb = tokens(ca), tokens(cb)
        if ta and tb and (ta < tb or tb < ta):
            found[(a, b)] = f"{ca!r} contained in {cb!r}" if ta < tb else f"{cb!r} contained in {ca!r}"
            continue

        # 4. Near-identical spelling: transliteration drift (Svidrigailov /
        #    Svidrigaïlov) rather than two people.
        if SequenceMatcher(None, ca.lower(), cb.lower()).ratio() >= SIMILARITY_THRESHOLD:
            found[(a, b)] = f"{ca!r} ~ {cb!r}"

    return [(a, b, why) for (a, b), why in sorted(found.items())]


def main(rare_max: int) -> None:
    entities = json.load(open(Config.ENTITIES_RAW_PATH, encoding="utf-8"))
    people = sum(len(c) for c in entities.values())
    logger.info("loaded %d people across %d reference names from %s",
                people, len(entities), Config.ENTITIES_RAW_PATH)

    # Concatenate each book once; counting per chapter would multiply the regex
    # passes for a number nobody reads at chapter granularity.
    book_text = {book: "\n".join(u.text for u in units) for book, units in parse_all().items()}
    logger.info("loaded text for %d books", len(book_text))

    # entities maps a name to a list of people, one per book (RAG.md section 4).
    # Within a single book a name identifies one person, so the per-book views
    # below are still plain {name: body} and every check stays book-local --
    # which is the point: comparing a C&P Nikolay with a Possessed Nikolay was
    # never a merge candidate, it was two people.
    by_book: dict[str, dict] = defaultdict(dict)
    for key, candidates in entities.items():
        for body in candidates:
            by_book[body["book"]][key] = body

    mentions: dict[tuple[str, str], int] = {}
    for book, entries in by_book.items():
        text = book_text.get(book, "")
        if not text:
            logger.warning("no text for book %r; its mention counts will be 0", book)
        for key, body in entries.items():
            mentions[(book, key)] = count_mentions(text, name_forms(key, body))
        logger.info("%s: counted mentions for %d entities", book, len(entries))

    lines = [
        "# Entity review aid",
        "",
        f"Generated from `{os.path.relpath(Config.ENTITIES_RAW_PATH)}` "
        f"({people} people across {len(entities)} reference names, {len(by_book)} books).",
        "",
        "Reading order, not verdicts. The gate (design II.4) is yours: review, then save",
        f"a corrected copy as `{os.path.relpath(Config.ENTITIES_PATH)}`. The builder never",
        "writes that file, so re-runs cannot clobber your edits.",
        "",
        "**Mentions** = whole-word hits for any of the entity's name forms across the whole",
        "book. Low counts are the teacher extracting someone named once in passing.",
        "Counts are inflated wherever an alias matches an ordinary word — see the unusable",
        "alias section per book, and fix those first.",
        "",
        "This report can only rank what was extracted. For **absences** — a real character",
        "the teacher missed — check against `docs/CHARACTERS.md`.",
        "",
    ]

    total_rare = total_pairs = total_unusable = 0
    for book in sorted(by_book):
        entries = by_book[book]
        m = lambda k: mentions[(book, k)]  # noqa: E731 -- book-local view, used once per section
        ranked = sorted(entries, key=lambda k: (-m(k), k))
        rare = [k for k in ranked if m(k) <= rare_max]
        pairs = merge_candidates(entries)
        unusable = {
            k: sorted(a for a in entries[k].get("aliases", [])
                      if isinstance(a, str) and a.lower() in UNUSABLE_ALIASES)
            for k in ranked
        }
        unusable = {k: v for k, v in unusable.items() if v}
        total_rare += len(rare)
        total_pairs += len(pairs)
        total_unusable += len(unusable)

        lines += [
            f"## {book}",
            "",
            f"{len(entries)} entities | {len(pairs)} merge candidates | "
            f"{len(rare)} with <= {rare_max} mentions | {len(unusable)} with unusable aliases",
            "",
        ]

        if unusable:
            lines += [
                "### Unusable aliases — fix before P4",
                "",
                "These aliases match on ordinary words, so in P4 they would pull unrelated",
                "chapters into a name query. Drop the alias, keep the entity.",
                "",
                "| entity | unusable aliases | mentions (inflated) |",
                "|---|---|---|",
            ]
            lines += [f"| `{k}` | {', '.join(v)} | {m(k)} |" for k, v in unusable.items()]
            lines.append("")

        if pairs:
            lines += ["### Merge candidates", "", "| A | B | why |", "|---|---|---|"]
            for a, b, why in pairs:
                lines.append(f"| `{a}` ({m(a)}) | `{b}` ({m(b)}) | {why} |")
            lines.append("")

        if rare:
            lines += [
                f"### Rare (<= {rare_max} mentions) — likely drop",
                "",
                "| entity | canonical | mentions |",
                "|---|---|---|",
            ]
            lines += [f"| `{k}` | {entries[k]['canonical']} | {m(k)} |" for k in rare]
            lines.append("")

        keep = [k for k in ranked if m(k) > rare_max]
        lines += ["### Roster (by mentions)", "", "| entity | canonical | aliases | mentions |", "|---|---|---|---|"]
        lines += [
            f"| `{k}` | {entries[k]['canonical']} | {', '.join(entries[k]['aliases']) or '—'} | {m(k)} |"
            for k in keep
        ]
        lines.append("")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info("people=%d names=%d rare(<=%d)=%d merge_candidates=%d unusable_aliases=%d",
                people, len(entities), rare_max, total_rare, total_pairs, total_unusable)
    logger.info("wrote %s", REPORT_PATH)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the entity review aid for the II.4 gate.")
    ap.add_argument("--rare-max", type=int, default=2,
                    help="Entities with this many mentions or fewer are listed as likely drops.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    main(args.rare_max)
