#!/usr/bin/env python3
"""Batch H: score neighbour expansion's collision policy over the retrieval set.

Three arms, ONE CONFIG PROMOTION EACH -- the arm is read from the versioned
artifact (`Config.NEIGHBOR_*`), never from an env var or a flag, because
`Config.*` resolving through the artifact is what makes a number attributable
(CLAUDE.md; R64).

Scores what the SERVED spans contain, not the fused pool: recall@k over each
row's targets, `distinct_target_chapters_hit@k`, and the RENDERED prompt cost.
Read-only, in process, no uvicorn (which would hold the vector_db lock), no
provider call, $0.00.

KNOWN-ANSWER CHECK, stated before the run: ALIGN-ONLY must score IDENTICALLY to
the baseline on every chapter-level metric. Rule 1 keeps every admitted
neighbour inside its hit's chapter, so expansion cannot add a chapter, and only
BACKFILL -- which admits a new RANK -- can. If align-only diverges on recall,
the guard is leaking and the run is void rather than interesting.
"""
import json, sys, time
sys.path.insert(0, ".")
from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient
from flashrank import Ranker

from src.config import Config
from src.limits import estimate_tokens
from src.rag.context import expand_spans, _chapter_of
from src.rag.retrieve import retrieve, rerank

rows = [r for r in json.load(open("test/retrieval_eval.json")) if r.get("targets")]
embedder = TextEmbedding(model_name=Config.EMBED_MODEL)
bm25 = SparseTextEmbedding(model_name="Qdrant/bm25")
ranker = Ranker(model_name=Config.RERANK_MODEL, cache_dir="models/flashrank")
client = QdrantClient(path=Config.VECTOR_DB_DIR)

radius, policy = Config.NEIGHBOR_EXPANSION, Config.NEIGHBOR_COLLISION_POLICY
arm = ("baseline" if policy == "off"
       else f"merge-{policy}" if radius == 0 else f"r{radius}-{policy}")
print(f"arm={arm} config_version={Config.config_version() if hasattr(Config,'config_version') else 'n/a'} "
      f"radius={radius} policy={policy} max_span_tokens={Config.MAX_SPAN_TOKENS} k={Config.TOP_K_RERANK}")

out, t0 = [], time.time()
for row in rows:
    pts = retrieve(client, embedder, bm25, row["query"], None)
    ranked = rerank(row["query"], pts, ranker, top_k=len(pts))
    hits = ranked[:Config.TOP_K_RERANK]
    spans = expand_spans(client, Config.QDRANT_COLLECTION, hits, ranked,
                         radius, Config.MAX_SPAN_TOKENS, policy,
                         max_context_tokens=Config.MAX_CONTEXT_TOKENS)
    gold = {(t["book"], t["part"], t["chapter"]) for t in row["targets"]}
    got = {_chapter_of(s.payload) for s in spans}
    out.append({
        "id": row["id"], "n_targets": len(gold), "spans": len(spans),
        "hit": bool(got & gold),
        "coverage": len(got & gold) / len(gold),          # distinct-target-chapters-hit@k
        "tokens": sum(estimate_tokens(s.payload.get("text", "")) for s in spans),
    })

n = len(out)
multi = [r for r in out if r["n_targets"] > 1]
res = {
    "arm": arm, "radius": radius, "policy": policy,
    "config_version": Config.CONFIG_VERSION if hasattr(Config, "CONFIG_VERSION") else None,
    "rows": n, "wall_s": round(time.time() - t0, 1),
    "recall_at_k": round(sum(r["hit"] for r in out) / n, 4),
    "distinct_target_chapters_hit_at_k": round(sum(r["coverage"] for r in out) / n, 4),
    "multi_target_rows": len(multi),
    "multi_target_coverage": round(sum(r["coverage"] for r in multi) / len(multi), 4) if multi else None,
    "mean_prompt_tokens": round(sum(r["tokens"] for r in out) / n, 1),
    "total_prompt_tokens": sum(r["tokens"] for r in out),
    "mean_spans": round(sum(r["spans"] for r in out) / n, 3),
    "per_row": out,
}
path = f"out/batchh-{arm}.json"
json.dump(res, open(path, "w"), indent=1)
print(f"  rows={n} recall@k={res['recall_at_k']} coverage={res['distinct_target_chapters_hit_at_k']} "
      f"multi({len(multi)})={res['multi_target_coverage']} mean_tokens={res['mean_prompt_tokens']} "
      f"mean_spans={res['mean_spans']} wall={res['wall_s']}s -> {path}")
