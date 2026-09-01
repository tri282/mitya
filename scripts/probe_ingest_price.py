#!/usr/bin/env python3
"""P-27 -- what a 2-3 arm chunking ablation costs, without running one.

[DESK -- $0.00, no provider call, NO Qdrant]. Times the two embedders the
ingest path uses (`fastembed` dense + BM25 sparse, both local) over a batch of
real chunk-sized text, then extrapolates to the arm point counts in
`phase-7-candidate-granularity.md` §III.

**It deliberately never opens `data/vector_db/`.** Local-mode Qdrant takes an
exclusive process-level file lock on that directory (`docs/RUNBOOK-P6.md:16`),
so pricing the ablation must not itself become the thing that blocks a gate
run. Disk is extrapolated from the existing collection's size on disk instead
of by building a second one.
"""

import argparse
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config
from src.config_artifact import active_corpus_manifest

logger = logging.getLogger("probe_ingest_price")
_BM25_MODEL = "Qdrant/bm25"  # mirrors scripts/ingest_corpus.py:40

# `phase-7-candidate-granularity.md` §III, iso-token by construction.
ARMS = [
    ("baseline 500/100", 500, 100, 3284, 14.2),
    ("B1 750/150", 750, 150, 2190, 9.5),
    ("B2 1500/300", 1500, 300, 1095, 4.7),
]


def _corpus_text() -> str:
    """Real corpus prose, so the tokenizer sees the distribution it will see."""
    root = Config.RAG_DIR
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.endswith((".txt", ".md")):
                with open(os.path.join(dirpath, name), encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                if len(text) > 200_000:
                    return text
    raise SystemExit(f"no corpus text under {root} -- cannot price on real prose")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", type=int, default=64, help="chunks to time")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from fastembed import SparseTextEmbedding, TextEmbedding
    from src.rag.ingest import embed_dense, embed_sparse

    manifest = active_corpus_manifest()
    text = _corpus_text()
    # ~4 chars/token, the estimator this repo uses everywhere else.
    chunk_chars = manifest.chunk_tokens * 4
    chunks = [text[i * chunk_chars:(i + 1) * chunk_chars] for i in range(args.batch)]
    assert all(len(c) == chunk_chars for c in chunks), "corpus too short for the batch"

    t0 = time.monotonic()
    embedder = TextEmbedding(model_name=manifest.dense_model)
    bm25 = SparseTextEmbedding(model_name=_BM25_MODEL)
    load_s = time.monotonic() - t0

    # Warm the ONNX session so the timed pass measures throughput, not the
    # first-call graph build.
    embed_dense(embedder, chunks[:2])
    embed_sparse(bm25, chunks[:2])

    t0 = time.monotonic()
    dense = embed_dense(embedder, chunks)
    dense_s = time.monotonic() - t0
    t0 = time.monotonic()
    embed_sparse(bm25, chunks)
    sparse_s = time.monotonic() - t0

    # Known-answer check: the vectors this probe timed must be the ones the
    # index holds, or the throughput is for a different model than the ablation
    # would run. Dimension is the cheapest assertion that catches a swap.
    assert len(dense) == args.batch, len(dense)
    assert len(dense[0]) == manifest.dense_dim, (len(dense[0]), manifest.dense_dim)
    # And the arms must fit the embedder's window, or B2 is silently truncated
    # and the ablation measures truncation instead of granularity.
    from src.rag.ingest import get_tokenizer
    max_len = get_tokenizer(embedder).truncation["max_length"]
    assert max_len >= max(a[1] for a in ARMS), (max_len, ARMS)
    logger.info("known-answer check PASSED: dense_dim=%d matches manifest %s; "
                "embedder max_length=%d >= largest arm chunk_tokens=%d (no arm truncates)",
                len(dense[0]), manifest.version(), max_len, max(a[1] for a in ARMS))

    # Price per TOKEN, not per point. The arms are iso-token by construction
    # (points x chunk_tokens is ~constant), so a per-point rate measured at 500
    # tokens would report B2 as 3x cheaper than it is. Transformer cost is
    # roughly linear in sequence length at these lengths and nomic's window is
    # 8192, so no arm truncates -- checked below.
    tokens_timed = args.batch * manifest.chunk_tokens
    per_token_s = (dense_s + sparse_s) / tokens_timed
    per_point = per_token_s * manifest.chunk_tokens
    disk_kb = int(subprocess.run(["du", "-sk", Config.VECTOR_DB_DIR], capture_output=True,
                                 text=True).stdout.split()[0])

    print(f"corpus={manifest.version()} dense_model={manifest.dense_model} "
          f"dense_dim={manifest.dense_dim} chunk_tokens={manifest.chunk_tokens}")
    print(f"model load: {load_s:.1f}s (once per arm)")
    print(f"embed {args.batch} chunks: dense {dense_s:.2f}s, sparse {sparse_s:.2f}s "
          f"-> {per_token_s * 1e6:.1f} us/token ({per_point * 1000:.1f} ms per 500-token point)")
    print(f"existing collection on disk: {disk_kb / 1024:.1f} MB over "
          f"{manifest.point_count} points = {disk_kb / manifest.point_count:.1f} KB/point")
    print()
    print(f"{'arm':20} {'points':>7} {'chunks/ch':>10} {'embed_s':>9} {'wall_min':>9} {'disk_MB':>9}")
    total_s, total_mb = 0.0, 0.0
    for label, ct, _ov, points, per_chapter in ARMS:
        # Payload text is the SAME corpus in every arm -- only the vector count
        # scales -- so disk splits into a per-point vector term and a constant
        # text term. Vector term = dense_dim * 4 bytes.
        vec_mb = points * manifest.dense_dim * 4 / 1024 / 1024
        base_vec_mb = manifest.point_count * manifest.dense_dim * 4 / 1024 / 1024
        text_mb = disk_kb / 1024 - base_vec_mb
        arm_mb = vec_mb + text_mb
        embed_s = points * ct * per_token_s
        wall = (load_s + embed_s) / 60
        total_s += load_s + embed_s
        total_mb += arm_mb
        print(f"{label:20} {points:>7} {per_chapter:>10.1f} {embed_s:>9.1f} {wall:>9.1f} {arm_mb:>9.1f}")
    print()
    print(f"three arms, embed+load only: {total_s / 60:.1f} min, {total_mb:.0f} MB of NEW collections")
    print("Excludes: parse+chunk, Qdrant upsert, and the retrieval-eval scoring pass per arm.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
