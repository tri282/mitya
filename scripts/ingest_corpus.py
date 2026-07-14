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

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient

from src.config import Config
from src.rag.ingest import chunk_units, ensure_collection, get_tokenizer, parse_all, upsert_chunks

logger = logging.getLogger(__name__)


def main(rebuild: bool, batch_size: int) -> None:
    # Loading TextEmbedding downloads the ONNX model to the fastembed cache on
    # first run (network + disk), then loads it into onnxruntime -- this is
    # the expensive step the CRITICAL EXECUTION RULE gates on approval.
    logger.info("loading dense embedder: %s", Config.EMBED_MODEL)
    embedder = TextEmbedding(model_name=Config.EMBED_MODEL)
    tokenizer = get_tokenizer(embedder)

    logger.info("loading sparse embedder: Qdrant/bm25")
    bm25 = SparseTextEmbedding(model_name="Qdrant/bm25")

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


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Ingest the Dostoevsky corpus into the local Qdrant collection.")
    ap.add_argument("--rebuild", action="store_true", help="Drop and recreate the collection if it already exists.")
    ap.add_argument("--batch-size", type=int, default=64, help="Embedding/upsert batch size (default: 64).")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main(args.rebuild, args.batch_size)
