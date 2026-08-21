"""Domain-gate score probe: the retrieval-score distributions R15/step 14 needs.

`eval_retrieval.py` skips the refusal families before retrieval ever runs -- they
have no correct chapter, so scoring them for accuracy would be a guaranteed miss
in a denominator they do not belong to. But step 14 needs exactly what that skip
throws away: **the retrieval scores themselves**, for `sensitive` (must be
answered) against `adversarial` (must be refused), to find a threshold that
separates them. This script is that missing path.

`primary` rows are probed too, as the must-pass control. A threshold that gates
out ordinary corpus questions is not a gate, it is an outage.

**Which score.** Not the fused one. `retrieve()` returns Qdrant's native RRF
scores, and RRF is computed from *ranks* -- `sum(1/(k + rank))` over the legs --
so the top-1 fused score is very nearly a constant no matter how well the query
matches the corpus. It orders results; it does not measure them. A gate needs a
similarity, so this probe reads the legs directly: cosine from the dense vector,
and the BM25 leg's own score. The fused score is recorded alongside precisely so
the "it carries no domain signal" claim is checkable rather than asserted.

Usage:

    python scripts/probe_gate_scores.py                     # summary table
    python scripts/probe_gate_scores.py --json probe.json   # + per-row scores
    python scripts/probe_gate_scores.py --self-check        # sweep math only, no DB

Determinism (A8b, R18): the query expansion this calls is the path R18 fixed, so
repeated runs must produce identical scores. If they do not, that is a bug
report, not a noise floor -- same rule as the retrieval eval.
"""

import argparse
import json
import logging
import os
import statistics
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient, models

from src.config import Config
from src.rag.entities import EntityMap
from src.rag.metrics import family_of
from src.rag.retrieve import _QUERY_PREFIX, retrieve

logger = logging.getLogger(__name__)

# Families this probe scores, and what the gate must do with each.
GATE_FAMILIES = {
    "sensitive": "pass",       # must be answered -- a refusal here is a false refusal
    "adversarial": "refuse",   # must be refused (see expected_behavior caveat below)
    "primary": "pass",         # must-pass control: ordinary corpus questions
}


def probe_row(client, embedder, bm25, entity_map, query: str, k: int = 5) -> dict:
    """Per-leg scores for one query. Mirrors serving: expansion runs first."""
    query_expanded, _ = entity_map.resolve(query)

    dense_vec = list(embedder.embed([_QUERY_PREFIX + query_expanded]))[0].tolist()
    dense = client.query_points(
        Config.QDRANT_COLLECTION, query=dense_vec, using="dense", limit=k, with_payload=False,
    ).points

    sparse_vec = list(bm25.query_embed(query_expanded))[0]
    sparse = client.query_points(
        Config.QDRANT_COLLECTION,
        query=models.SparseVector(indices=sparse_vec.indices.tolist(),
                                  values=sparse_vec.values.tolist()),
        using="bm25", limit=k, with_payload=False,
    ).points

    fused = retrieve(client, embedder, bm25, query_expanded, None)[:k]

    return {
        "dense_top1": dense[0].score if dense else 0.0,
        "dense_mean_topk": statistics.fmean(p.score for p in dense) if dense else 0.0,
        "bm25_top1": sparse[0].score if sparse else 0.0,
        "fused_top1": fused[0].score if fused else 0.0,
    }


def sweep(scores_by_family: dict[str, list[float]], statistic: str) -> list[dict]:
    """Threshold sweep. At each candidate cut, how much of each family falls below it.

    Gate semantics: score < threshold -> refuse. So for `adversarial` (must refuse)
    the fraction below is the catch rate, and for `sensitive`/`primary` (must pass)
    the fraction below is the false-refusal rate. A usable threshold has a high
    catch rate and a low false-refusal rate; whether one exists is the finding.
    """
    all_scores = sorted({s for scores in scores_by_family.values() for s in scores})
    if not all_scores:
        return []
    rows = []
    for cut in all_scores:
        row = {"threshold": cut, "statistic": statistic}
        for family, scores in scores_by_family.items():
            below = sum(1 for s in scores if s < cut)
            row[family] = below / len(scores) if scores else 0.0
        rows.append(row)
    return rows


def best_threshold(sweep_rows: list[dict], max_false_refusal: float = 0.10) -> dict | None:
    """Highest adversarial catch rate whose false-refusal cost stays under budget.

    Returns None when no threshold clears the budget -- which is a permitted and
    publishable outcome (R15): it means layer 1 is not viable alone.
    """
    viable = [
        r for r in sweep_rows
        if r.get("sensitive", 0.0) <= max_false_refusal and r.get("primary", 0.0) <= max_false_refusal
    ]
    return max(viable, key=lambda r: r.get("adversarial", 0.0)) if viable else None


def _summarize(label: str, scores: list[float]) -> str:
    if not scores:
        return f"{label:14s} n=0"
    q = statistics.quantiles(scores, n=4) if len(scores) > 1 else [scores[0]] * 3
    return (f"{label:14s} n={len(scores):3d}  min={min(scores):.4f}  p25={q[0]:.4f}  "
            f"med={statistics.median(scores):.4f}  p75={q[2]:.4f}  max={max(scores):.4f}")


def main(pairs_path: str, json_path: str | None, statistic: str) -> None:
    rows = [r for r in json.load(open(pairs_path, encoding="utf-8"))
            if family_of(r) in GATE_FAMILIES]
    logger.info("probing %d rows across %s", len(rows), sorted(GATE_FAMILIES))

    logger.info("loading embedders + vector db")
    embedder = TextEmbedding(model_name=Config.EMBED_MODEL)
    bm25 = SparseTextEmbedding(model_name="Qdrant/bm25")
    entity_map = EntityMap()
    client = QdrantClient(path=Config.VECTOR_DB_DIR)

    per_row = []
    for row in rows:
        scores = probe_row(client, embedder, bm25, entity_map, row["query"])
        per_row.append({"id": row["id"], "family": family_of(row),
                        "expected_behavior": row.get("expected_behavior"),
                        "query": row["query"], **scores})

    by_family: dict[str, list[float]] = {}
    for r in per_row:
        by_family.setdefault(r["family"], []).append(r[statistic])

    logger.info("=" * 78)
    for stat in ("dense_top1", "dense_mean_topk", "bm25_top1", "fused_top1"):
        logger.info("-- %s --", stat)
        for family in sorted(GATE_FAMILIES):
            logger.info("   %s", _summarize(family, [r[stat] for r in per_row
                                                     if r["family"] == family]))

    logger.info("=" * 78)
    logger.info("threshold sweep on %s (score < threshold -> refuse)", statistic)
    sweep_rows = sweep(by_family, statistic)
    chosen = best_threshold(sweep_rows)
    if chosen is None:
        logger.warning(
            "NO THRESHOLD separates the families within a 10%% false-refusal budget. "
            "Per R15 this is the finding, not a failure: layer 1 of the P2 guard is not "
            "viable alone and needs layers 2-3 to carry it."
        )
    else:
        logger.info(
            "best threshold=%.4f -- catches %.0f%% of adversarial, refuses %.0f%% of "
            "sensitive and %.0f%% of primary",
            chosen["threshold"], 100 * chosen["adversarial"],
            100 * chosen["sensitive"], 100 * chosen["primary"],
        )

    # The four adversarial rows whose expected_behavior is answer_without_leak are
    # NOT refusal rows; a score gate that fires on them is wrong even though they
    # sit in the adversarial family. Reported apart so the catch rate is honest.
    leak_rows = [r for r in per_row if r.get("expected_behavior") == "answer_without_leak"]
    if leak_rows:
        logger.info("-- %d adversarial rows are answer_without_leak, not refusal rows --",
                    len(leak_rows))
        for r in leak_rows:
            logger.info("   %s %s=%.4f  %s", r["id"], statistic, r[statistic], r["query"][:60])

    if json_path:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"statistic": statistic, "rows": per_row, "sweep": sweep_rows,
                       "best_threshold": chosen}, f, indent=2)
        logger.info("wrote %s", json_path)


def _self_check() -> None:
    """Sweep/threshold math, no DB. Separable and non-separable cases."""
    separable = {"adversarial": [0.1, 0.2, 0.15], "sensitive": [0.8, 0.9, 0.85], "primary": [0.7, 0.95]}
    chosen = best_threshold(sweep(separable, "dense_top1"))
    assert chosen is not None and chosen["adversarial"] == 1.0, chosen
    assert chosen["sensitive"] == 0.0 and chosen["primary"] == 0.0, chosen

    overlapping = {"adversarial": [0.5, 0.6], "sensitive": [0.5, 0.6], "primary": [0.5, 0.6]}
    chosen = best_threshold(sweep(overlapping, "dense_top1"))
    # Identical distributions: any cut costs as much as it catches, so the only
    # threshold inside the budget is the one that catches nothing.
    assert chosen is None or chosen["adversarial"] == 0.0, chosen

    assert sweep({}, "dense_top1") == []
    assert best_threshold([]) is None
    logger.info("probe_gate_scores self-check passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Domain-gate score probe (P0 step 14, R15).")
    ap.add_argument("--pairs", default=os.path.join("test", "retrieval_eval.json"))
    ap.add_argument("--json", dest="json_path", default=None, help="write per-row scores + sweep")
    ap.add_argument("--statistic", default="dense_top1",
                    choices=("dense_top1", "dense_mean_topk", "bm25_top1", "fused_top1"),
                    help="score the sweep runs on; fused_top1 is expected to carry no signal")
    ap.add_argument("--self-check", action="store_true", help="sweep math only, no DB")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.self_check:
        _self_check()
    else:
        main(args.pairs, args.json_path, args.statistic)
