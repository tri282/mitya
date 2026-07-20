# scripts/eval_retrieval.py
"""Phase 4 verify: retrieval eval (design VII.1) -- recall@5 + MRR over the
labeled query -> (book, part, chapter) pairs in test/retrieval_eval.json.

This is also the RR5 tripwire: several labeled pairs use a non-Garnett
spelling ("Dunya", "Razumikhin", "Sonya", "Myshkin") on purpose, so a fold
regression or a missing "search_query: " prefix shows up as a recall drop on
exactly those rows, not just as a lower aggregate number.

Measures the HYBRID + FUSION leg (pre-rerank, top Config.TOP_K_FUSED sliced
to 5) -- Config.TOP_K_RERANK is only 3, smaller than 5, so recall@5 has
nothing to measure post-rerank. Rerank quality is a separate, qualitative
check (not scored here).

IMPORTANT -- same Qdrant local-mode lock as ingest_corpus.py: run only while
the API server (if any) is stopped.

Usage:
    python scripts/eval_retrieval.py
    python scripts/eval_retrieval.py --pairs test/retrieval_eval.json
"""

import argparse
import json
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient

from src.config import Config
from src.rag.entities import EntityMap
from src.rag.retrieve import retrieve

logger = logging.getLogger(__name__)


def load_pairs(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def evaluate(pairs: list[dict], entity_map: EntityMap, client: QdrantClient, embedder, bm25, k: int = 5) -> None:
    reciprocal_ranks: list[float] = []
    hits = 0
    per_row = []

    for pair in pairs:
        target = (pair["book"], pair["part"], pair["chapter"])
        expanded_query, resolved_names = entity_map.resolve(pair["query"], active_book=None)

        # Unfiltered eval on purpose: the labeled set exercises retrieval
        # quality, not the hard-filter path (that's exercised separately by
        # the STRUCTURAL-scope pairs, which check router.route() only).
        fused = retrieve(client, embedder, bm25, expanded_query, hard_filter=None)
        top_k = fused[:k]

        rank = None
        for i, point in enumerate(top_k, start=1):
            payload = point.payload or {}
            if (payload.get("book"), payload.get("part"), payload.get("chapter")) == target:
                rank = i
                break

        if rank is not None:
            hits += 1
            reciprocal_ranks.append(1.0 / rank)
        else:
            reciprocal_ranks.append(0.0)

        per_row.append((pair["query"], target, rank, resolved_names))
        logger.info(
            "query=%r target=%s rank=%s resolved_entities=%s",
            pair["query"], target, rank, resolved_names,
        )

    recall_at_k = hits / len(pairs)
    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks)

    logger.info("=" * 60)
    logger.info("recall@%d = %.3f (%d/%d)", k, recall_at_k, hits, len(pairs))
    logger.info("MRR = %.3f", mrr)

    misses = [row for row in per_row if row[2] is None]
    if misses:
        logger.info("misses:")
        for query, target, _rank, resolved in misses:
            logger.info("  %r -> expected %s (resolved entities: %s)", query, target, resolved)


def main(pairs_path: str) -> None:
    logger.info("loading dense embedder: %s", Config.EMBED_MODEL)
    embedder = TextEmbedding(model_name=Config.EMBED_MODEL)

    logger.info("loading sparse embedder: Qdrant/bm25")
    bm25 = SparseTextEmbedding(model_name="Qdrant/bm25")

    logger.info("loading entity map: %s", Config.ENTITIES_PATH)
    entity_map = EntityMap()

    client = QdrantClient(path=Config.VECTOR_DB_DIR)

    pairs = load_pairs(pairs_path)
    logger.info("loaded %d labeled pairs from %s", len(pairs), pairs_path)

    evaluate(pairs, entity_map, client, embedder, bm25)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Retrieval eval: recall@5 + MRR (design VII.1).")
    ap.add_argument("--pairs", default=os.path.join("test", "retrieval_eval.json"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main(args.pairs)
