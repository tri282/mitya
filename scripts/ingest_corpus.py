# scripts/ingest_corpus.py
"""Phase 2 driver: parse -> chunk -> embed (dense + BM25) -> Qdrant upsert.

Populates the local Qdrant collection (Config.QDRANT_COLLECTION) with one
point per chunk, carrying a dense (nomic) vector, a sparse (BM25) vector, and
structural payload (book/part/chapter/canonical_entities/text). This is the
retrieval substrate for phase 4.

IMPORTANT -- Qdrant local mode (`QdrantClient(path=...)`) takes an exclusive
process-level file lock on Config.VECTOR_DB_DIR. Run this only while the API
server is stopped; running it concurrently with a live server pointed at the
same path raises a lock error (design docs/RAG.md §II.3).

Usage:
    python scripts/ingest_corpus.py               # refuses if the collection already exists
    python scripts/ingest_corpus.py --rebuild      # drops + recreates the collection first
    python scripts/ingest_corpus.py --batch-size 32
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient

from src.config import Config
from src.config_artifact import CorpusManifest, save_corpus_manifest
from src.rag.ingest import chunk_units, ensure_collection, get_tokenizer, parse_all, upsert_chunks

logger = logging.getLogger(__name__)

# BM25 model id: hardcoded here to match src/api/app.py's _BM25_MODEL and
# scripts/eval_retrieval.py -- P2 debt (not lifted into Config), see
# docs/RUNBOOK-P4.md.
_BM25_MODEL = "Qdrant/bm25"


def _summaries_provenance() -> tuple[str, str]:
    """(teacher_model, generated_at) for the corpus manifest's derived-
    artifact provenance.

    summaries.json carries no provenance of its own -- inspected directly, it
    is a bare {"book|part|chapter": text} dict with no metadata keys. So
    there is nothing to READ here: `teacher_model` is the CONFIGURED
    Config.TEACHER_MODEL (today's value, not a value recorded at generation
    time), and `generated_at` is the file's own mtime -- the closest proxy
    available for "when it was produced," not a timestamp summaries.json
    states itself.
    """
    mtime = os.path.getmtime(Config.SUMMARIES_PATH)
    generated_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    return Config.TEACHER_MODEL, generated_at


def main(rebuild: bool, batch_size: int) -> None:
    # Loading TextEmbedding downloads the ONNX model to the fastembed cache on
    # first run (network + disk), then loads it into onnxruntime -- this is
    # the expensive step the CRITICAL EXECUTION RULE gates on approval.
    logger.info("loading dense embedder: %s", Config.EMBED_MODEL)
    embedder = TextEmbedding(model_name=Config.EMBED_MODEL)
    tokenizer = get_tokenizer(embedder)

    logger.info("loading sparse embedder: %s", _BM25_MODEL)
    bm25 = SparseTextEmbedding(model_name=_BM25_MODEL)

    logger.info("parsing corpus from %s", Config.RAG_DIR)
    units_by_book = parse_all()

    logger.info("chunking (chunk_tokens=%d, chunk_overlap=%d)", Config.CHUNK_TOKENS, Config.CHUNK_OVERLAP)
    chunks = chunk_units(units_by_book, tokenizer, Config.CHUNK_TOKENS, Config.CHUNK_OVERLAP)

    per_book: dict[str, int] = {}
    for chunk in chunks:
        per_book[chunk.book] = per_book.get(chunk.book, 0) + 1
    for book, count in per_book.items():
        logger.info("  %s: %d chunks", book, count)
    logger.info("total chunks: %d", len(chunks))

    client = QdrantClient(path=Config.VECTOR_DB_DIR)
    ensure_collection(client, Config.QDRANT_COLLECTION, rebuild)

    total_upserted = upsert_chunks(client, Config.QDRANT_COLLECTION, chunks, embedder, bm25, batch_size)

    point_count = client.count(Config.QDRANT_COLLECTION).count
    logger.info("upserted %d points; collection point count = %d", total_upserted, point_count)
    if point_count != len(chunks):
        logger.warning(
            "point count mismatch: parsed %d chunks but collection reports %d points",
            len(chunks), point_count,
        )

    # Corpus manifest (v2 P1 step 8, D9): the version identity checked at
    # startup against the serving config's EMBED_MODEL. dense_dim is read
    # back from the collection actually created, not assumed, so the
    # manifest can never silently drift from ensure_collection's own choice.
    dense_dim = client.get_collection(Config.QDRANT_COLLECTION).config.params.vectors["dense"].size
    teacher_model, summaries_generated_at = _summaries_provenance()
    manifest = CorpusManifest(
        chunk_tokens=Config.CHUNK_TOKENS,
        chunk_overlap=Config.CHUNK_OVERLAP,
        dense_model=Config.EMBED_MODEL,
        dense_dim=dense_dim,
        sparse_model=_BM25_MODEL,
        qdrant_collection=Config.QDRANT_COLLECTION,
        point_count=point_count,
        books=sorted(per_book),
        ingested_at=datetime.now(timezone.utc).isoformat(),
        teacher_model=teacher_model,
        summaries_generated_at=summaries_generated_at,
    )
    manifest_version = save_corpus_manifest(manifest)
    logger.info("corpus manifest version: %s", manifest_version)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Ingest the Dostoevsky corpus into the local Qdrant collection.")
    ap.add_argument("--rebuild", action="store_true", help="Drop and recreate the collection if it already exists.")
    ap.add_argument("--batch-size", type=int, default=64, help="Embedding/upsert batch size (default: 64).")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main(args.rebuild, args.batch_size)
