"""Corpus ingest for the Dostoevsky RAG pipeline.

Phase 1 (done): per-book structural parsing. Each novel's plain-text file uses
its own heading scheme (no single generic regex — see docs/RAG.md §II.1) and
is parsed into ordered ``Unit`` records (book/part/chapter/order/text), then
rolled up into ``structure.json`` — the canonical (book, part, chapter) key
space that phase 2 (chunking), phase 3 (summaries), and phase 4 (router scope
resolution) all key off of.

Phase 2 (this file, done): token-window chunking within each ``Unit``, dense
(nomic, fastembed) + sparse (BM25) embedding, and Qdrant local-collection
upsert. Driven by ``scripts/ingest_corpus.py``.
"""

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from qdrant_client import QdrantClient, models

from src.config import Config

if TYPE_CHECKING:
    # Imported only for type hints -- keeps this module importable (e.g. by
    # scripts that just want parse_all()/Unit) without pulling in fastembed's
    # onnxruntime dependency unless chunking/embedding is actually used.
    from fastembed import SparseTextEmbedding, TextEmbedding
    from tokenizers import Tokenizer

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
    # utf-8-sig transparently strips a leading BOM if present, else behaves as utf-8.
    with open(path, encoding="utf-8-sig") as f:
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


# --- chunking ----------------------------------------------------------------
# Token-window chunking within each parsed Unit -- chunks never cross a
# chapter boundary, since a Unit already *is* one chapter. Windows are sized
# with the embedder's own tokenizer (not str.split/tiktoken) so chunk sizing
# matches what nomic actually sees at inference time (design R8).

@dataclass
class Chunk:
    id: int               # stable integer point id (global, deterministic parse order)
    book: str
    part: int | None
    chapter: int | None
    text: str


def get_tokenizer(embedder: "TextEmbedding") -> "Tokenizer":
    """Return the underlying `tokenizers.Tokenizer` fastembed loaded for this model.

    fastembed's public `TextEmbedding` API has no documented tokenizer
    accessor. Read from fastembed 0.8.0 source (common/onnx_model.py,
    text/onnx_text_model.py): the per-architecture inner model (here
    `PooledEmbedding`, used for nomic-embed-text-v1.5) is `OnnxTextModel`,
    which stores the HF `tokenizers.Tokenizer` on `self.tokenizer`, populated
    in `load_onnx_model()`. `TextEmbedding.__init__` calls
    `load_onnx_model()` eagerly unless `lazy_load=True` (we use the default,
    `lazy_load=False`), so the tokenizer is already loaded by the time this
    is called -- no dummy embed() call needed first.
    """
    return embedder.model.tokenizer


def chunk_units(
    units_by_book: dict[str, list[Unit]],
    tokenizer: "Tokenizer",
    chunk_tokens: int = Config.CHUNK_TOKENS,
    chunk_overlap: int = Config.CHUNK_OVERLAP,
) -> list[Chunk]:
    """Slide a chunk_tokens-wide / chunk_overlap-overlapping window over each
    Unit's text, using tokenizer offsets to map token windows back to char
    spans. Returns chunks in stable global order (book iteration order x unit
    order x window order), so point ids are deterministic across re-runs.
    """
    if chunk_overlap >= chunk_tokens:
        raise ValueError(f"CHUNK_OVERLAP ({chunk_overlap}) must be < CHUNK_TOKENS ({chunk_tokens})")
    stride = chunk_tokens - chunk_overlap

    chunks: list[Chunk] = []
    point_id = 0
    for book_title, units in units_by_book.items():
        for unit in sorted(units, key=lambda u: u.order):
            if not unit.text:
                continue
            # add_special_tokens=False: we want raw content-token offsets to
            # window over. The [CLS]/[SEP] fastembed adds at actual embed()
            # time carry (0, 0) offsets and would corrupt the char-slicing
            # below; they're re-added transparently when we embed the chunk
            # text later, so omitting them here doesn't affect what the
            # model sees at inference time.
            encoding = tokenizer.encode(unit.text, add_special_tokens=False)
            offsets = encoding.offsets
            n = len(offsets)
            if n == 0:
                continue
            start = 0
            while start < n:
                end = min(start + chunk_tokens, n)
                char_start = offsets[start][0]
                char_end = offsets[end - 1][1]
                chunk_text = unit.text[char_start:char_end]
                chunks.append(Chunk(point_id, book_title, unit.part, unit.chapter, chunk_text))
                point_id += 1
                if end == n:
                    break
                start += stride
    return chunks


# --- embedding -----------------------------------------------------------------
# Dense: nomic-embed-text-v1.5 via fastembed (ONNX/CPU, 768-dim, cosine).
# Nomic's training uses task-instruction prefixes baked into the input text
# rather than a model-side flag, so the prefix has to be applied by the
# caller. Read from fastembed 0.8.0 source: TextEmbedding.embed/query_embed/
# passage_embed and PooledEmbedding (grepped fastembed/text/*.py,
# fastembed/common/*.py for "search_document"/"search_query": no hits) never
# insert the prefixes themselves, and PooledEmbedding does not override
# query_embed/passage_embed to add them either -- so we must prefix
# explicitly, both here (ingest, "search_document:") and in P4 retrieval
# ("search_query:"). Silently getting this wrong degrades retrieval (RR5).
_DOC_PREFIX = "search_document: "


def embed_dense(embedder: "TextEmbedding", texts: list[str]) -> list[list[float]]:
    prefixed = [_DOC_PREFIX + t for t in texts]
    return [vec.tolist() for vec in embedder.embed(prefixed)]


def embed_sparse(bm25: "SparseTextEmbedding", texts: list[str]) -> list:
    # .embed() (document side) emits raw term-frequency weights, meant to be
    # combined with Qdrant's server-side Modifier.IDF at query time (design
    # §II.2, R9). Confirmed via fastembed/sparse/bm25.py: .embed() calls
    # raw_embed()/_term_frequency() (TF only, no IDF), while .query_embed()
    # (query side, used in P4) assigns a flat weight of 1.0 per token instead
    # -- the two are intentionally asymmetric, this is not a bug to "fix".
    return list(bm25.embed(texts))


# --- Qdrant collection + upsert -------------------------------------------------

def ensure_collection(client: QdrantClient, collection: str, rebuild: bool) -> None:
    """Create the local collection with named dense ("dense") + sparse
    ("bm25") vectors. Names are reused verbatim by P4's prefetch. Refuses to
    touch an existing collection unless `rebuild` is set.
    """
    exists = client.collection_exists(collection)
    if exists and not rebuild:
        raise RuntimeError(f"collection '{collection}' already exists -- pass --rebuild to recreate it")
    if exists and rebuild:
        logger.info("dropping existing collection %s (--rebuild)", collection)
        client.delete_collection(collection)

    client.create_collection(
        collection,
        vectors_config={"dense": models.VectorParams(size=768, distance=models.Distance.COSINE)},
        # Modifier.IDF is required -- without it fastembed's TF-only BM25
        # weights are never combined with corpus-level IDF and "BM25"
        # degrades to plain term frequency (design §II.2).
        sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    logger.info("created collection %s (dense=768/cosine, bm25=idf)", collection)


def upsert_chunks(
    client: QdrantClient,
    collection: str,
    chunks: list[Chunk],
    embedder: "TextEmbedding",
    bm25: "SparseTextEmbedding",
    batch_size: int,
) -> int:
    """Embed (dense + sparse) and upsert chunks in batches to bound memory.
    Returns the total number of points upserted.
    """
    total = 0
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        texts = [c.text for c in batch]
        dense_vecs = embed_dense(embedder, texts)
        sparse_vecs = embed_sparse(bm25, texts)

        points = [
            models.PointStruct(
                id=chunk.id,
                vector={
                    "dense": dense_vec,
                    "bm25": models.SparseVector(
                        indices=sparse_vec.indices.tolist(),
                        values=sparse_vec.values.tolist(),
                    ),
                },
                payload={
                    "book": chunk.book,
                    "part": chunk.part,
                    "chapter": chunk.chapter,
                    "canonical_entities": [],  # left empty in v1, see design §II.4
                    "text": chunk.text,
                },
            )
            for chunk, dense_vec, sparse_vec in zip(batch, dense_vecs, sparse_vecs)
        ]
        client.upsert(collection, points=points)
        total += len(points)
        logger.info("upserted points %d-%d (%d/%d)", i, i + len(batch) - 1, total, len(chunks))
    return total


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Parse the Dostoevsky corpus into structural units.")
    ap.add_argument(
        "--parse-only", action="store_true",
        help="Emit structure.json only. Chunking/embedding/upsert live in scripts/ingest_corpus.py.",
    )
    ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    emit_structure()
