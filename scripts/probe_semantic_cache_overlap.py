"""P-7b -- does query cosine predict RETRIEVED-CHUNK equivalence? (R49, R50)

P-7a measured cosine between queries and used *disjoint eval targets* as the
must-miss criterion. That is a proxy twice over: targets are chapter-level, the
cache serves chunk-level, and two queries with different target chapters can
still retrieve largely the same chunks -- in which case reusing them is
harmless and the proxy called a defect that is not one.

This probe asks the direct question instead. For a pair (A, B): if the cache
served A's reranked top-3 in answer to B, how much of what B would have
retrieved for itself does B still get?

    served_recall(A -> B) = |topk(A) & topk(B)| / |topk(B)|

That is the quantity a cache hit actually risks. A high query cosine with
served_recall 0.0 is the failure mode; a *low* cosine with high served_recall
is a missed hit, which costs money but breaks nothing.

Asymmetric on purpose: the cache serves A's chunks to B, so B's own top-k is
the denominator. Reported both directions.

**Runs the real pipeline** -- EntityMap expansion, hybrid dense+bm25 with
server-side RRF, FlashRank rerank to TOP_K_RERANK -- because a cache sits in
front of exactly that, not in front of a bare vector search.

Qdrant local mode takes an exclusive lock on data/vector_db/ (RUNBOOK-P6):
nothing else may be running against it, and this process must exit cleanly.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import sys

import numpy as np
from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config  # noqa: E402
from src.rag.entities import EntityMap  # noqa: E402
from src.rag.retrieve import (  # noqa: E402
    _QUERY_PREFIX,
    build_ranker,
    rerank,
    retrieve,
)
from src.rag.router import route  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("probe_overlap")
logger.setLevel(logging.INFO)

_BM25_MODEL = "Qdrant/bm25"

# Must-hit, widened from P-7a's five. Same rule: a paraphrase changes wording,
# never the information need. Spread across question form, register, synonym,
# and word order so the class is not five variations of one trick.
PARAPHRASES: list[tuple[str, str]] = [
    ("r-0001", "How does the scene where Raskolnikov murders the old pawnbroker with an axe unfold?"),
    ("r-0001", "Walk me through Raskolnikov's killing of the pawnbroker."),
    ("r-0002", "What happens when Raskolnikov finally admits the killing to Sonia?"),
    ("r-0002", "Raskolnikov's confession to Sonia -- how does it go?"),
    ("r-0003", "The scene where Raskolnikov bows down and kisses the ground in the Hay Market before giving himself up"),
    ("r-0004", "In what ways does Razumikhin look after Raskolnikov once the murder is done?"),
    ("r-0013", "The train journey where Myshkin first encounters Rogojin"),
    ("r-0017", "The scene at the grotto where the group kills Shatov"),
    ("r-0090", "In The Idiot, how does the text portray the epileptic fit?"),
    ("r-0092", "In Crime and Punishment, who kills himself, and by what means?"),
    ("r-0096", "In Crime and Punishment, what effect does a father being publicly shamed have on his household?"),
    ("r-0098", "In The Idiot, where does a character confess before a hostile crowd?"),
]


def topk_ids(client, embedder, bm25, ranker, entity_map, query: str) -> list:
    """One full DEFAULT-route retrieval, through rerank. Returns point ids."""
    expanded, _ = entity_map.resolve(query)
    points = retrieve(client, embedder, bm25, expanded, None)
    return [p.id for p in rerank(query, points, ranker)]


def unit(embedder, texts: list[str]) -> np.ndarray:
    v = np.array([x for x in embedder.embed([_QUERY_PREFIX + t for t in texts])])
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def served_recall(a_ids: list, b_ids: list) -> float:
    """Fraction of B's own top-k that B still gets when served A's top-k."""
    return len(set(a_ids) & set(b_ids)) / len(b_ids) if b_ids else 0.0


def main() -> int:
    rows = {r["id"]: r for r in json.load(open(os.path.join("test", "retrieval_eval.json")))}
    structure = json.load(open(os.path.join(Config.RAG_DIR, "structure.json")))
    summaries = json.load(open(os.path.join(Config.RAG_DIR, "summaries.json")))

    # L2's population: DEFAULT-routed AND retrieval-scored (a refusal row has no
    # chunks that must not be reused -- the P-7a defect, not repeated here).
    pop = [
        rid for rid, r in rows.items()
        if r.get("targets") and route(r["query"], None, structure, summaries)[0] == "DEFAULT"
    ]
    logger.info("population: %d DEFAULT retrieval-scored rows", len(pop))

    embedder = TextEmbedding(model_name=Config.EMBED_MODEL)
    bm25 = SparseTextEmbedding(model_name=_BM25_MODEL)
    ranker = build_ranker()
    entity_map = EntityMap()
    client = QdrantClient(path=Config.VECTOR_DB_DIR)
    try:
        logger.info("retrieving top-%d for %d eval rows + %d paraphrases ...",
                    Config.TOP_K_RERANK, len(pop), len(PARAPHRASES))
        topk = {rid: topk_ids(client, embedder, bm25, ranker, entity_map, rows[rid]["query"])
                for rid in pop}
        para_topk = [
            (src, text, topk_ids(client, embedder, bm25, ranker, entity_map, text))
            for src, text in PARAPHRASES
        ]
    finally:
        client.close()  # release the exclusive lock on data/vector_db/

    # --- must-hit: paraphrase served from its source row --------------------
    pv = unit(embedder, [t for _, t, _ in para_topk])
    sv = unit(embedder, [rows[s]["query"] for s, _, _ in para_topk])
    print(f"\n=== MUST HIT: serve source row's chunks to its paraphrase (k={Config.TOP_K_RERANK}) ===")
    print(f"{'src':8s} {'cos':>7s} {'served_recall':>14s}   paraphrase")
    hits = []
    for k, (src, text, ids) in enumerate(para_topk):
        cos = float(pv[k] @ sv[k])
        sr = served_recall(topk[src], ids) if src in topk else float("nan")
        hits.append((src, cos, sr))
        print(f"{src:8s} {cos:7.4f} {sr:14.2f}   {text[:60]}")

    # --- must-miss: every disjoint-target DEFAULT pair -----------------------
    dv = unit(embedder, [rows[r]["query"] for r in pop])
    pairs = []
    for i, j in itertools.combinations(range(len(pop)), 2):
        a, b = pop[i], pop[j]
        ta = {(t["book"], t.get("part"), t.get("chapter")) for t in rows[a]["targets"]}
        tb = {(t["book"], t.get("part"), t.get("chapter")) for t in rows[b]["targets"]}
        if ta & tb:
            continue
        pairs.append((a, b, float(dv[i] @ dv[j]),
                      served_recall(topk[a], topk[b]), served_recall(topk[b], topk[a])))
    pairs.sort(key=lambda t: -t[2])

    print("\n=== MUST MISS: worst 12 by cosine, with what serving them would actually cost ===")
    print(f"{'pair':22s} {'cos':>7s} {'a->b':>6s} {'b->a':>6s}")
    for a, b, c, ab, ba in pairs[:12]:
        print(f"{a+'/'+b:22s} {c:7.4f} {ab:6.2f} {ba:6.2f}")

    # --- does cosine predict served_recall at all? --------------------------
    cs = np.array([p[2] for p in pairs])
    rs = np.array([(p[3] + p[4]) / 2 for p in pairs])
    print(f"\n  corr(cosine, mean served_recall) over {len(pairs)} disjoint pairs = "
          f"{float(np.corrcoef(cs, rs)[0, 1]):+.3f}")

    # --- threshold sweep: the real tradeoff ---------------------------------
    # A "damaging" admission is a pair the threshold would serve whose
    # served_recall is poor -- a hit that returns chunks the other query would
    # not have retrieved. 0.34 = at k=3, fewer than 2 of 3 chunks survive.
    print("\n=== THRESHOLD SWEEP ===")
    print(f"{'T':>6s} {'hits kept':>10s} {'bad admits':>11s} {'worst admit sr':>15s}")
    for T in (0.80, 0.83, 0.85, 0.88, 0.90, 0.92, 0.95):
        kept = sum(1 for _, cos, _ in hits if cos >= T)
        admitted = [p for p in pairs if p[2] >= T]
        bad = [p for p in admitted if min(p[3], p[4]) < 0.34]
        worst = min((min(p[3], p[4]) for p in admitted), default=float("nan"))
        print(f"{T:6.2f} {kept:>4d}/{len(hits):<5d} {len(bad):>11d} {worst:15.2f}")

    print("\n  Provenance: eval rows and their retrievals [measured]; paraphrases [authored].")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
