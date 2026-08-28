"""Bootstrap the first serving config artifact from today's `Config` (P1 step 5).

Writes `config/serving/<hash>.json` and points `config/serving/CURRENT` at it.
Safe to re-run: the artifact is content-addressed, so an unchanged config
rewrites the same file and the same pointer.

The field-by-field assertion against `Config` is the point of the script, not
a formality -- the bootstrap version has to BE today's config, or every
baseline recorded against it is recorded against a fiction. It self-disables
once the facade lands (the versioned knobs stop being real class attributes at
that point, and comparing the artifact to itself would pass for the wrong
reason).
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config
from src.config_artifact import (
    CURRENT_POINTER,
    SERVING_DIR,
    CorpusManifest,
    ServingConfig,
    save,
    save_corpus_manifest,
)
from src.observability import configure_logging

logger = logging.getLogger(__name__)

# artifact field -> the `Config` attribute it was lifted from.
_FIELD_SOURCES = {
    "embed_model": "EMBED_MODEL",
    "rerank_model": "RERANK_MODEL",
    "rerank_enabled": "RERANK_ENABLED",
    "prefetch_limit": "PREFETCH_LIMIT",
    "top_k_fused": "TOP_K_FUSED",
    "top_k_rerank": "TOP_K_RERANK",
    "top_k_structural_excerpts": "TOP_K_STRUCTURAL_EXCERPTS",
    "ctx_size": "CTX_SIZE",
    "max_tokens": "MAX_TOKENS",
    "gen_temperature": "GEN_TEMPERATURE",
    "gen_top_p": "GEN_TOP_P",
    "prompt_composition": "PROMPT_COMPOSITION",
    "serving_model": "SERVING_MODEL",
    "book_title_aliases": "BOOK_TITLE_ALIASES",
    "model_limits": "MODEL_LIMITS",
    "unknown_model_limits": "UNKNOWN_MODEL_LIMITS",
    "no_tpm_limit": "NO_TPM_LIMIT",
    "model_rates": "MODEL_RATES",
}


def assert_matches_config(cfg: ServingConfig) -> int:
    """Compare the artifact against the pre-facade `Config`. Returns the
    number of fields actually compared, so a caller can tell a clean run from
    a vacuous one."""
    compared = 0
    mismatches = []
    for field, attr in _FIELD_SOURCES.items():
        if attr not in vars(Config):
            continue  # the facade has landed; Config now reads THROUGH the artifact
        compared += 1
        expected = vars(Config)[attr]
        actual = getattr(cfg, field)
        if actual != expected:
            mismatches.append(f"  {field}: artifact={actual!r} Config.{attr}={expected!r}")

    # summary_intent's source is the router, not Config.
    from src.rag import router
    if hasattr(router, "SUMMARY_INTENT"):
        compared += 1
        if tuple(cfg.summary_intent) != tuple(router.SUMMARY_INTENT):
            mismatches.append(
                f"  summary_intent: artifact={cfg.summary_intent!r} "
                f"router.SUMMARY_INTENT={router.SUMMARY_INTENT!r}"
            )

    if mismatches:
        raise SystemExit("bootstrap artifact does not match today's config:\n" + "\n".join(mismatches))
    return compared


# Duplicated from scripts/ingest_corpus.py rather than imported: that module
# pulls fastembed/onnxruntime at import time, and adoption embeds nothing.
# _BM25_MODEL's existing duplication (app.py, eval_retrieval.py,
# ingest_corpus.py) is recorded P2 debt -- see docs/RUNBOOK-P4.md.
_BM25_MODEL = "Qdrant/bm25"


def _summaries_provenance() -> tuple[str, str]:
    """(teacher_model, generated_at) for the manifest's derived-artifact
    provenance. summaries.json carries none of its own -- it is a bare
    {"book|part|chapter": text} dict -- so `teacher_model` is the CONFIGURED
    value and `generated_at` is the file's mtime, the closest proxy for when
    it was produced."""
    mtime = os.path.getmtime(Config.SUMMARIES_PATH)
    return Config.TEACHER_MODEL, datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

def adopt_corpus_manifest() -> str:
    """Write a corpus manifest for an index that already exists (P1 step 8).

    The manifest is normally written by the ingest run that built the index.
    An index built before step 8 has none, and the startup assertion refuses
    to serve without one -- so without this, adopting step 8 costs a full
    re-ingest to certify an index that is already correct.

    What is read and what is asserted, since the difference is the whole
    point of recording `provenance="adopted"`:
      * READ from Qdrant -- collection name, dense dimension, point count.
      * READ from structure.json -- the book set.
      * ASSERTED from today's Config -- `dense_model`, `chunk_tokens`,
        `chunk_overlap`. The index records none of these; that absence IS the
        bug the manifest closes. So adoption pins today's values as the
        index's identity: it catches every future swap, and cannot detect a
        mismatch that already existed at adoption time.
    """
    import json

    from qdrant_client import QdrantClient

    client = QdrantClient(path=Config.VECTOR_DB_DIR)
    try:
        info = client.get_collection(Config.QDRANT_COLLECTION)
        dense_dim = info.config.params.vectors["dense"].size
        point_count = client.count(Config.QDRANT_COLLECTION).count
    finally:
        client.close()

    if point_count == 0:
        raise SystemExit(
            f"collection {Config.QDRANT_COLLECTION!r} is empty -- nothing to adopt. "
            f"Run scripts/ingest_corpus.py."
        )

    with open(Config.STRUCTURE_PATH, encoding="utf-8") as f:
        books = sorted(json.load(f))

    teacher_model, summaries_generated_at = _summaries_provenance()
    manifest = CorpusManifest(
        chunk_tokens=Config.CHUNK_TOKENS,
        chunk_overlap=Config.CHUNK_OVERLAP,
        dense_model=Config.EMBED_MODEL,
        dense_dim=dense_dim,
        sparse_model=_BM25_MODEL,
        qdrant_collection=Config.QDRANT_COLLECTION,
        point_count=point_count,
        books=books,
        ingested_at=datetime.fromtimestamp(
            os.path.getmtime(Config.VECTOR_DB_DIR), tz=timezone.utc
        ).isoformat(),
        teacher_model=teacher_model,
        summaries_generated_at=summaries_generated_at,
        provenance="adopted",
    )
    version = save_corpus_manifest(manifest)
    logger.warning(
        "adopted the existing index as corpus %s -- dense_model=%r and the chunk "
        "params are TODAY'S Config values, not values read from the index. Future "
        "swaps are caught; a mismatch already present today is not.",
        version, manifest.dense_model,
    )
    return version


def main(promote: bool, adopt_corpus: bool) -> None:
    configure_logging(logging.INFO)
    if adopt_corpus:
        adopt_corpus_manifest()
    cfg = ServingConfig.from_env()

    compared = assert_matches_config(cfg)
    if compared == len(_FIELD_SOURCES) + 1:
        logger.info("artifact matches today's Config on all %d fields", compared)
    else:
        logger.warning(
            "compared only %d/%d fields -- the facade has landed, so this run does NOT "
            "re-verify the bootstrap against a pre-facade Config",
            compared, len(_FIELD_SOURCES) + 1,
        )

    version = save(cfg)
    logger.info("artifact: %s", os.path.join(SERVING_DIR, f"{version}.json"))

    if promote:
        with open(CURRENT_POINTER, "w", encoding="utf-8") as f:
            f.write(version + "\n")
        logger.info("pointer %s -> %s", CURRENT_POINTER, version)
    else:
        logger.info("pointer left alone (pass --promote to point CURRENT at %s)", version)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--promote", action="store_true",
                        help="point config/serving/CURRENT at the generated version")
    parser.add_argument("--adopt-corpus", action="store_true",
                        help="write a corpus manifest for the index that already exists, "
                             "instead of requiring a re-ingest to produce one")
    args = parser.parse_args()
    main(args.promote, args.adopt_corpus)
