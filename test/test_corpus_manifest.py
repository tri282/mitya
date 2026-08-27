# test/test_corpus_manifest.py
"""P1 step 8 verify: the corpus manifest (src/config_artifact.py) and the
EMBED_MODEL startup assertion it exists to enforce.

Covers the live bug step 1 reproduced: EMBED_MODEL is read at ingest and at
query from the same global, with nothing checking they agree -- swap it and
restart without re-ingesting, and query vectors are compared against an index
built by a different model, silently. This checks the assertion actually
guards that: agreeing models pass (non-vacuity -- the guard's precondition
absent still succeeds), disagreeing models raise, and a missing manifest
raises.

Plain-assert script, not pytest. Offline: a hand-written manifest in a temp
dir, no real ingest.
"""

import contextlib
import json
import os
import sys
import tempfile

sys.path.insert(0, ".")
from src import config_artifact  # noqa: E402
from src.config_artifact import (  # noqa: E402
    ConfigError,
    CorpusManifest,
    assert_embed_model_matches,
    load_corpus_manifest,
    save_corpus_manifest,
)


def _manifest(**overrides) -> CorpusManifest:
    fields = dict(
        chunk_tokens=500,
        chunk_overlap=100,
        dense_model="nomic-ai/nomic-embed-text-v1.5",
        dense_dim=768,
        sparse_model="Qdrant/bm25",
        qdrant_collection="dostoevsky",
        point_count=1234,
        books=["Crime and Punishment", "The Idiot"],
        ingested_at="2026-08-27T00:00:00+00:00",
        teacher_model="models/gemini-3.1-flash-lite",
        summaries_generated_at="2026-08-20T00:00:00+00:00",
    )
    fields.update(overrides)
    return CorpusManifest(**fields)


@contextlib.contextmanager
def _manifest_path(tmp: str):
    """Point CORPUS_MANIFEST at a temp file, and drop the process cache."""
    saved = config_artifact.CORPUS_MANIFEST
    config_artifact.CORPUS_MANIFEST = os.path.join(tmp, "manifest.json")
    config_artifact.reset_active_corpus()
    try:
        yield
    finally:
        config_artifact.CORPUS_MANIFEST = saved
        config_artifact.reset_active_corpus()


# --- the assertion itself (no file I/O -- a pure function of two values) ---

def test_agreeing_models_pass() -> None:
    """Non-vacuity: the guard's precondition (a mismatch) absent, the call
    must succeed rather than raise for some unrelated reason."""
    manifest = _manifest(dense_model="nomic-ai/nomic-embed-text-v1.5")
    assert_embed_model_matches("nomic-ai/nomic-embed-text-v1.5", manifest)  # must not raise


def test_disagreeing_models_raise() -> None:
    manifest = _manifest(dense_model="nomic-ai/nomic-embed-text-v1.5")
    try:
        assert_embed_model_matches("BAAI/bge-small-en-v1.5", manifest)
        assert False, "expected a ConfigError"
    except ConfigError as exc:
        assert "nomic-ai/nomic-embed-text-v1.5" in str(exc)
        assert "BAAI/bge-small-en-v1.5" in str(exc)


# --- the loader -------------------------------------------------------------

def test_missing_manifest_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with _manifest_path(tmp):
            try:
                load_corpus_manifest()
                assert False, "expected a ConfigError"
            except ConfigError as exc:
                assert "no corpus manifest" in str(exc)


def test_present_manifest_loads_and_round_trips() -> None:
    """Non-vacuity's other half: with the missing-file precondition absent,
    the loader must actually succeed and return equal data, not merely fail
    to raise."""
    manifest = _manifest()
    with tempfile.TemporaryDirectory() as tmp:
        with _manifest_path(tmp):
            version = save_corpus_manifest(manifest)
            loaded = load_corpus_manifest()
            assert loaded == manifest
            assert loaded.version() == version


def test_invalid_manifest_raises() -> None:
    """A hand-corrupted file (missing field) fails validation, not silently
    substituting a default."""
    with tempfile.TemporaryDirectory() as tmp:
        with _manifest_path(tmp):
            os.makedirs(os.path.dirname(config_artifact.CORPUS_MANIFEST), exist_ok=True)
            with open(config_artifact.CORPUS_MANIFEST, "w", encoding="utf-8") as f:
                json.dump({"chunk_tokens": 500}, f)  # missing every other field
            try:
                load_corpus_manifest()
                assert False, "expected a ConfigError"
            except ConfigError as exc:
                assert "failed validation" in str(exc)


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
