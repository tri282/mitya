"""Corpus ingest for the Dostoevsky RAG pipeline.

Phase 1 (this file, done): per-book structural parsing. Each novel's plain-text
file uses its own heading scheme (no single generic regex — see docs/RAG.md
§II.1) and is parsed into ordered ``Unit`` records (book/part/chapter/order/text),
then rolled up into ``structure.json`` — the canonical (book, part, chapter) key
space that phase 2 (chunking), phase 3 (summaries), and phase 4 (router scope
resolution) all key off of.

Phase 2 (chunking, embedding (dense + BM25), and Qdrant upsert) is not yet
implemented here.
"""

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass

from src.config import Config

logger = logging.getLogger(__name__)


# --- roman numerals -----------------------------------------------------
# All headings in this corpus use I/V/X/L/C only (max value seen: Book XII = 12).

_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def roman_to_int(numeral: str) -> int:
    numeral = numeral.upper()
    total = 0
    prev = 0
    for ch in reversed(numeral):
        val = _ROMAN_VALUES[ch]
        if val < prev:
            total -= val
        else:
            total += val
            prev = val
    return total


# --- unit -----------------------------------------------------------------

@dataclass
class Unit:
    book: str            # display title, e.g. "Crime and Punishment"
    part: int | None      # Part N, or Book N for BK (see BOOK_META notes)
    chapter: int | None
    order: int            # global sequence within the file
    text: str


# --- heading patterns -------------------------------------------------------
# One small handler per book below reuses these; schemes differ per novel and
# are documented per-book in BOOK_META (emitted into structure.json).

PART_RE = re.compile(r"^PART\s+([IVXLC]+)\s*$")                    # "PART I"
CHAPTER_RE_PLAIN = re.compile(r"^CHAPTER\s+([IVXLC]+)\s*$")        # "CHAPTER I"            (C&P)
CHAPTER_RE_TITLED = re.compile(r"^CHAPTER\s+([IVXLC]+)\.\s*.*$")   # "CHAPTER I. TITLE"     (Possessed)
BOOK_RE = re.compile(r"^Book\s+([IVXLC]+)\.\s*.*$")                # "Book II. Title"       (BK)
BK_CHAPTER_RE = re.compile(r"^Chapter\s+([IVXLC]+)\.\s*$")         # "Chapter I."           (BK)
EPILOGUE_RE = re.compile(r"^EPILOGUE\s*$")
BARE_ROMAN_RE = re.compile(r"^([IVXLC]+)\.?\s*$")                  # "I" / "I."  (Idiot, Notes, C&P epilogue)


def _read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return f.readlines()


def _segments(lines: list[str], heading_re: re.Pattern) -> list[tuple[str, int, int]]:
    """Find heading_re matches in lines; return (label, content_start, content_end)
    per match, content_end exclusive of the next heading (or end of lines)."""
    matches = []
    for i, line in enumerate(lines):
        m = heading_re.match(line.rstrip())
        if m:
            label = m.group(1) if m.groups() else m.group(0)
            matches.append((label, i))
    segments = []
    for idx, (label, start) in enumerate(matches):
        end = matches[idx + 1][1] if idx + 1 < len(matches) else len(lines)
        segments.append((label, start + 1, end))
    return segments


# --- per-book handlers -------------------------------------------------------

def _parse_part_chapter(
    path: str,
    book_title: str,
    part_re: re.Pattern,
    chapter_re: re.Pattern,
    epilogue_re: re.Pattern | None = None,
    epilogue_chapter_re: re.Pattern | None = None,
) -> list[Unit]:
    """Part N + Chapter N scheme (C&P, Possessed, Idiot, Notes), with an
    optional trailing EPILOGUE block (C&P only) that gets its own part number
    (max part + 1) and is parsed with its own chapter heading scheme."""
    lines = _read_lines(path)
    part_segments = _segments(lines, part_re)

    epilogue_start_idx = None
    if epilogue_re is not None:
        epi_hits = [i for i, line in enumerate(lines) if epilogue_re.match(line.rstrip())]
        if epi_hits:
            epilogue_start_idx = epi_hits[0]
            # clip the last part's content range so it doesn't swallow the epilogue
            if part_segments:
                label, start, end = part_segments[-1]
                if end > epilogue_start_idx >= start:
                    part_segments[-1] = (label, start, epilogue_start_idx)

    units: list[Unit] = []
    order = 0
    for part_label, start, end in part_segments:
        part_num = roman_to_int(part_label)
        for chap_label, c_start, c_end in _segments(lines[start:end], chapter_re):
            chap_num = roman_to_int(chap_label)
            text = "".join(lines[start + c_start:start + c_end]).strip()
            order += 1
            units.append(Unit(book_title, part_num, chap_num, order, text))

    if epilogue_start_idx is not None and epilogue_chapter_re is not None:
        epi_content_start = epilogue_start_idx + 1
        epi_part_num = max((u.part for u in units), default=0) + 1
        for chap_label, c_start, c_end in _segments(lines[epi_content_start:], epilogue_chapter_re):
            chap_num = roman_to_int(chap_label)
            text = "".join(lines[epi_content_start + c_start:epi_content_start + c_end]).strip()
            order += 1
            units.append(Unit(book_title, epi_part_num, chap_num, order, text))

    return units


def _parse_brothers_karamazov(path: str) -> list[Unit]:
    """Book N scheme with chapter numbering reset per book.

    The source has no explicit "Book I." heading — Book I starts at the top of
    the file and runs to the first "Book II." heading. "Book N" is folded into
    Unit.part (payload-mapping decision, documented in BOOK_META below). Book
    count is derived from the file (1 implicit + however many "Book N."
    headings are present), never hardcoded. EPILOGUE is treated as book
    book_count + 1 and reuses the same "Chapter N." scheme as regular books.
    """
    book_title = "The Brothers Karamazov"
    lines = _read_lines(path)
    book_segments = _segments(lines, BOOK_RE)  # explicit "Book N." headings (II..)

    epi_hits = [i for i, line in enumerate(lines) if EPILOGUE_RE.match(line.rstrip())]
    epilogue_start_idx = epi_hits[0] if epi_hits else None

    book_i_end = book_segments[0][1] - 1 if book_segments else (epilogue_start_idx or len(lines))
    blocks: list[tuple[int, int, int]] = [(1, 0, book_i_end)]
    for label, start, end in book_segments:
        blocks.append((roman_to_int(label), start, end))

    if epilogue_start_idx is not None and blocks:
        num, start, end = blocks[-1]
        if end > epilogue_start_idx >= start:
            blocks[-1] = (num, start, epilogue_start_idx)

    book_count = len(blocks)  # read from file at parse time, not hardcoded

    units: list[Unit] = []
    order = 0
    for book_num, start, end in blocks:
        for chap_label, c_start, c_end in _segments(lines[start:end], BK_CHAPTER_RE):
            chap_num = roman_to_int(chap_label)
            text = "".join(lines[start + c_start:start + c_end]).strip()
            order += 1
            units.append(Unit(book_title, book_num, chap_num, order, text))

    if epilogue_start_idx is not None:
        epi_content_start = epilogue_start_idx + 1
        epi_book_num = book_count + 1
        for chap_label, c_start, c_end in _segments(lines[epi_content_start:], BK_CHAPTER_RE):
            chap_num = roman_to_int(chap_label)
            text = "".join(lines[epi_content_start + c_start:epi_content_start + c_end]).strip()
            order += 1
            units.append(Unit(book_title, epi_book_num, chap_num, order, text))

    return units


def parse_all() -> dict[str, list[Unit]]:
    return {
        "Crime and Punishment": _parse_part_chapter(
            os.path.join(Config.RAG_DIR, "crime_and_punishment.txt"),
            "Crime and Punishment", PART_RE, CHAPTER_RE_PLAIN,
            epilogue_re=EPILOGUE_RE, epilogue_chapter_re=BARE_ROMAN_RE,
        ),
        "The Brothers Karamazov": _parse_brothers_karamazov(
            os.path.join(Config.RAG_DIR, "the_brothers_karamazov.txt"),
        ),
        "The Idiot": _parse_part_chapter(
            os.path.join(Config.RAG_DIR, "the_idiot.txt"),
            "The Idiot", PART_RE, BARE_ROMAN_RE,
        ),
        "The Possessed": _parse_part_chapter(
            os.path.join(Config.RAG_DIR, "the_possessed.txt"),
            "The Possessed", PART_RE, CHAPTER_RE_TITLED,
        ),
        "Notes from the Underground": _parse_part_chapter(
            os.path.join(Config.RAG_DIR, "notes_from_the_underground.txt"),
            "Notes from the Underground", PART_RE, BARE_ROMAN_RE,
        ),
    }


# --- structure.json ----------------------------------------------------------
# Per-book metadata documenting the heading scheme and payload-mapping choices.
# The (book, part, chapter) tuple emitted here is the canonical key format that
# phase 2 (chunk payloads) and phase 3 (summaries.json) must reuse verbatim.

BOOK_META = {
    "Crime and Punishment": {
        "file": "crime_and_punishment.txt",
        "scheme": "part_chapter",
        "part_label": "Part",
        "chapter_label": "Chapter",
        "notes": (
            "6 parts, 'PART N' / 'CHAPTER N' headings (no titles). Epilogue is "
            "mapped to part=7 (max part + 1); its 2 chapters are bare roman "
            "numerals ('I', 'II', no 'CHAPTER' keyword) in the source."
        ),
    },
    "The Brothers Karamazov": {
        "file": "the_brothers_karamazov.txt",
        "scheme": "book_chapter",
        "part_label": "Book",
        "chapter_label": "Chapter",
        "notes": (
            "Source has no explicit 'Book I.' heading -- Book I is inferred as "
            "everything before the first 'Book II.' heading. 'Book N' is folded "
            "into Unit.part (part = book number); chapter numbering resets per "
            "book. Book count is read from the file at parse time (11 explicit "
            "'Book N.' headings + implicit Book I = 12), not hardcoded. Epilogue "
            "is mapped to part=book_count+1 (13); its 3 chapters reuse the "
            "regular 'Chapter N.' scheme."
        ),
    },
    "The Idiot": {
        "file": "the_idiot.txt",
        "scheme": "part_chapter",
        "part_label": "Part",
        "chapter_label": None,
        "notes": (
            "GAP resolved by grep inspection: chapters are unlabeled bare roman "
            "numerals (e.g. 'IV.'), no 'CHAPTER' keyword. Confirmed 50 "
            "roman-numeral heading lines across 4 parts (16+12+10+12), matching "
            "the part boundaries exactly. Parsed with the same bare-roman-numeral "
            "handler used for Notes from the Underground and the Crime and "
            "Punishment epilogue."
        ),
    },
    "The Possessed": {
        "file": "the_possessed.txt",
        "scheme": "part_chapter",
        "part_label": "Part",
        "chapter_label": "Chapter",
        "notes": (
            "3 parts, 'CHAPTER N. TITLE' headings. No separate epilogue block: "
            "the final chapter is titled 'CONCLUSION' but is a regular numbered "
            "chapter (Part III, Chapter VIII)."
        ),
    },
    "Notes from the Underground": {
        "file": "notes_from_the_underground.txt",
        "scheme": "part_chapter",
        "part_label": "Part",
        "chapter_label": None,
        "notes": "2 parts, numbered sections (bare roman numerals) as chapters.",
    },
}


def build_structure(units_by_book: dict[str, list[Unit]]) -> dict:
    structure: dict = {}
    for book_title, units in units_by_book.items():
        parts: dict[str, dict] = {}
        for unit in sorted(units, key=lambda u: u.order):
            key = str(unit.part)
            entry = parts.setdefault(key, {"chapters": [], "unit_count": 0})
            if unit.chapter not in entry["chapters"]:
                entry["chapters"].append(unit.chapter)
            entry["unit_count"] += 1
        structure[book_title] = {
            **BOOK_META[book_title],
            "parts": parts,
            "total_units": len(units),
        }
    return structure


def emit_structure(output_path: str = Config.STRUCTURE_PATH) -> dict:
    units_by_book = parse_all()
    for book_title, units in units_by_book.items():
        logger.info("parsed %s: %d units", book_title, len(units))

    structure = build_structure(units_by_book)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(structure, f, indent=2, ensure_ascii=False)
    logger.info("wrote structure to %s", output_path)
    return structure


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Parse the Dostoevsky corpus into structural units.")
    ap.add_argument(
        "--parse-only", action="store_true",
        help="Emit structure.json only. Currently the only mode -- chunking/embedding land in phase 2.",
    )
    ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    emit_structure()
