# scripts/eval_retrieval.py
"""Phase 4/7 verify: retrieval eval (design VII.1) -- recall@5 + MRR over the
labeled query -> (book, part, chapter) pairs in test/retrieval_eval.json.

This is also the RR5 tripwire: several labeled pairs use a non-Garnett
spelling ("Dunya", "Razumikhin", "Sonya", "Myshkin", "Zosima", "Mikhailovna")
on purpose, so a fold regression or a missing "search_query: " prefix shows
up as a recall drop on exactly those rows, not just as a lower aggregate
number.

Measures the HYBRID + FUSION leg (pre-rerank, top Config.TOP_K_FUSED sliced
to 5) -- Config.TOP_K_RERANK is only 3, smaller than 5, so recall@5 has
nothing to measure post-rerank. The rerank arms below therefore score at k=3
against their own fused-top-3 baseline, and their numbers are NOT comparable
to the k=5 headline.

ABLATION ARMS (--arm, plans/mitya-ablation/plan.md). The default arm "full"
is the shipped config and reproduces the headline; the rest each disable one
stage so its contribution can be attributed:

  full             baseline: entity-expanded query, dense+bm25 RRF      (k=5)
  no-expansion     raw query, no EntityMap.resolve()                    (k=5)
  dense-only       drop the BM25 prefetch leg                           (k=5)
  sparse-only      drop the dense prefetch leg                          (k=5)
  rerank           fused[:3] vs FlashRank(raw query)[:3]                (k=3)
  rerank-expanded  fused[:3] vs FlashRank(expanded query)[:3]           (k=3)

The eval set is ~25 primary rows, so one row is worth 0.04 recall: read any
delta below ~0.08 as noise, and read the n/N counts rather than the ratio.
"rerank-expanded" also settles the P6 carry-forward (rerank scores the raw
query, not the entity-expanded one -- chosen at impl time, never measured).

Phase 7 fix (was a P4 review finding, docs/RUNBOOK-P4.md sec V): the driver
previously never called router.route() or retrieve.build_hard_filter(), so
the six labeled rows documented as router/alias/negative-control cases
verified nothing they claimed. Now:

  * Every row is routed through router.route() (structure.json + summaries.json
    loaded once). A row carrying "expected_route" (+ optional "expected_scope")
    is checked against the live result and a mismatch is logged as an error --
    this is the STRUCTURAL-routing regression tripwire (ruling R14).
  * A row carrying "filters" has build_hard_filter() called on it and the
    resulting Qdrant filter is threaded into retrieve() -- this is what
    actually exercises Config.BOOK_TITLE_ALIASES end to end (the two
    "Demons"/"The Devils" rows), rather than leaving build_hard_filter dead
    code in this script.
  * A row carrying "negative_control": true is excluded from the headline
    recall@5/MRR (per the same P4 review: two negative-control rows -- broad,
    intentionally-unresolvable-scope "recap"/"summarize X's philosophy"
    queries meant to prove router.route() stays DEFAULT -- have no retrieval
    diagnostic value and were dragging the headline number down with noise).
    They are still retrieved and scored, just reported in a separate section
    so they inform without polluting the primary metric.

IMPORTANT -- same Qdrant local-mode lock as ingest_corpus.py: run only while
the API server (if any) is stopped.

Usage:
    python scripts/eval_retrieval.py
    python scripts/eval_retrieval.py --pairs test/retrieval_eval.json
    python scripts/eval_retrieval.py --arm no-expansion --json test/ablation.json
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient

from src.config import Config
from src.rag.entities import EntityMap
from src.rag.retrieve import build_hard_filter, build_ranker, rerank, retrieve
from src.rag.router import route

logger = logging.getLogger(__name__)

# Ablation arms (see module docstring). Fields:
#   expand       -- run EntityMap.resolve() on the query
#   legs         -- prefetch legs handed to retrieve()
#   rerank_query -- None (no rerank bucket) | "raw" | "expanded"
#   k            -- cutoff; rerank arms must use <= Config.TOP_K_RERANK
ARMS: dict[str, dict] = {
    "full":            {"expand": True,  "legs": ("dense", "bm25"), "rerank_query": None,       "k": 5},
    "no-expansion":    {"expand": False, "legs": ("dense", "bm25"), "rerank_query": None,       "k": 5},
    "dense-only":      {"expand": True,  "legs": ("dense",),        "rerank_query": None,       "k": 5},
    "sparse-only":     {"expand": True,  "legs": ("bm25",),         "rerank_query": None,       "k": 5},
    "rerank":          {"expand": True,  "legs": ("dense", "bm25"), "rerank_query": "raw",      "k": Config.TOP_K_RERANK},
    "rerank-expanded": {"expand": True,  "legs": ("dense", "bm25"), "rerank_query": "expanded", "k": Config.TOP_K_RERANK},
}


def load_pairs(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _rank_of(points: list, target: tuple) -> int | None:
    """1-based rank of the target (book, part, chapter) in points, or None."""
    for i, point in enumerate(points, start=1):
        payload = point.payload or {}
        if (payload.get("book"), payload.get("part"), payload.get("chapter")) == target:
            return i
    return None


def _score_bucket(label: str, rows: list[tuple], k: int) -> dict | None:
    """Log recall@k + MRR for one bucket of (query, target, rank) rows, and
    return the headline numbers so --json can accumulate arms across runs."""
    if not rows:
        return None
    hits = sum(1 for _, _, rank in rows if rank is not None)
    mrr = sum(1.0 / rank if rank is not None else 0.0 for _, _, rank in rows) / len(rows)
    logger.info("-- %s (%d rows) --", label, len(rows))
    logger.info("recall@%d = %.3f (%d/%d)", k, hits / len(rows), hits, len(rows))
    logger.info("MRR = %.3f", mrr)
    misses = [(q, t) for q, t, rank in rows if rank is None]
    if misses:
        logger.info("%s misses:", label)
        for query, target in misses:
            logger.info("  %r -> expected %s", query, target)
    return {
        "bucket": label,
        "k": k,
        "rows": len(rows),
        "hits": hits,
        "recall": round(hits / len(rows), 4),
        "mrr": round(mrr, 4),
    }


def evaluate(
    pairs: list[dict],
    entity_map: EntityMap,
    client: QdrantClient,
    embedder,
    bm25,
    structure: dict,
    summaries: dict,
    arm: str = "full",
    ranker=None,
) -> dict:
    config = ARMS[arm]
    k = config["k"]
    legs = config["legs"]
    rerank_query = config["rerank_query"]

    primary_rows: list[tuple] = []
    negative_control_rows: list[tuple] = []
    reranked_rows: list[tuple] = []
    route_checked = 0
    route_mismatches = 0

    for pair in pairs:
        target = (pair["book"], pair["part"], pair["chapter"])
        filters = pair.get("filters")

        # Real router path (was skipped entirely pre-P7 -- see module docstring).
        route_name, scope = route(pair["query"], filters, structure, summaries)
        expected_route = pair.get("expected_route")
        if expected_route is not None:
            route_checked += 1
            expected_scope = pair.get("expected_scope")
            route_ok = route_name == expected_route and (
                expected_scope is None or scope == expected_scope
            )
            if not route_ok:
                route_mismatches += 1
                logger.error(
                    "ROUTE MISMATCH query=%r expected=(%s, %s) got=(%s, %s)",
                    pair["query"], expected_route, expected_scope, route_name, scope,
                )
            else:
                logger.info(
                    "route ok: query=%r -> (%s, %s)", pair["query"], route_name, scope,
                )

        # Real hard-filter path (was dead code pre-P7 -- also see module
        # docstring): a "filters" row now actually builds+applies the Qdrant
        # filter, exercising Config.BOOK_TITLE_ALIASES via build_hard_filter,
        # not just via route()'s independent regex scan.
        hard_filter = build_hard_filter(filters) if filters else None
        active_book = filters.get("book") if filters else None

        # The "no-expansion" arm is the whole point of measuring the entity
        # layer: same query, same legs, resolve() simply not called.
        if config["expand"]:
            query_used, resolved_names = entity_map.resolve(pair["query"], active_book=active_book)
        else:
            query_used, resolved_names = pair["query"], []

        fused = retrieve(client, embedder, bm25, query_used, hard_filter=hard_filter, legs=legs)
        rank = _rank_of(fused[:k], target)

        row = (pair["query"], target, rank)
        if pair.get("negative_control"):
            negative_control_rows.append(row)
        else:
            primary_rows.append(row)

        # Rerank arms score a second bucket over the SAME fused candidate pool,
        # so the arm is self-baselining in one pass (fused[:k] above vs
        # reranked[:k] here) -- rerank can never be compared to the k=5 headline.
        if rerank_query is not None and not pair.get("negative_control"):
            query_for_rerank = query_used if rerank_query == "expanded" else pair["query"]
            reranked = rerank(query_for_rerank, fused, ranker)
            reranked_rows.append((pair["query"], target, _rank_of(reranked[:k], target)))

        logger.info(
            "query=%r target=%s rank=%s route=%s resolved_entities=%s filters=%s",
            pair["query"], target, rank, route_name, resolved_names, filters,
        )

    logger.info("=" * 60)
    logger.info("arm=%s (expand=%s, legs=%s, rerank_query=%s, k=%d)",
                arm, config["expand"], ",".join(legs), rerank_query, k)
    logger.info("router: %d/%d expected-route rows matched", route_checked - route_mismatches, route_checked)

    buckets = [
        _score_bucket(f"PRIMARY (headline recall@{k} / MRR)", primary_rows, k),
        _score_bucket("NEGATIVE-CONTROL (reported separately, not headline)", negative_control_rows, k),
        _score_bucket(f"RERANKED (same candidates, {rerank_query} query)", reranked_rows, k),
    ]
    return {
        "arm": arm,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expand": config["expand"],
        "legs": list(legs),
        "rerank_query": rerank_query,
        "k": k,
        "route_matched": route_checked - route_mismatches,
        "route_checked": route_checked,
        "buckets": [b for b in buckets if b is not None],
    }


def _append_result(path: str, record: dict) -> None:
    """Append one arm's result to a JSON list, creating it if absent, so a
    six-arm ablation accumulates in a file instead of in scrollback."""
    results = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            results = json.load(f)
    results.append(record)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    logger.info("appended arm %r to %s (%d records)", record["arm"], path, len(results))


def main(pairs_path: str, arm: str, json_path: str | None) -> None:
    logger.info("loading dense embedder: %s", Config.EMBED_MODEL)
    embedder = TextEmbedding(model_name=Config.EMBED_MODEL)

    logger.info("loading sparse embedder: Qdrant/bm25")
    bm25 = SparseTextEmbedding(model_name="Qdrant/bm25")

    logger.info("loading entity map: %s", Config.ENTITIES_PATH)
    entity_map = EntityMap()

    logger.info("loading structure/summaries for router.route()")
    structure = _load_json(Config.STRUCTURE_PATH)
    summaries = _load_json(Config.SUMMARIES_PATH)

    # Only the rerank arms pay the FlashRank model load.
    ranker = None
    if ARMS[arm]["rerank_query"] is not None:
        logger.info("loading reranker: %s", Config.RERANK_MODEL)
        ranker = build_ranker()

    client = QdrantClient(path=Config.VECTOR_DB_DIR)

    pairs = load_pairs(pairs_path)
    logger.info("loaded %d labeled pairs from %s (arm=%s)", len(pairs), pairs_path, arm)

    record = evaluate(pairs, entity_map, client, embedder, bm25, structure, summaries,
                      arm=arm, ranker=ranker)
    if json_path:
        _append_result(json_path, record)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Retrieval eval: recall@5 + MRR (design VII.1).")
    ap.add_argument("--pairs", default=os.path.join("test", "retrieval_eval.json"))
    ap.add_argument("--arm", default="full", choices=sorted(ARMS),
                    help="ablation arm (see module docstring); 'full' is the shipped config")
    ap.add_argument("--json", dest="json_path", default=None,
                    help="append this arm's headline numbers to a JSON results file")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main(args.pairs, args.arm, args.json_path)
