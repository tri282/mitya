# test/test_gate_preflight.py
"""P3 step 3 verify: the gate refuses a checkout that cannot produce numbers.

The assertion that matters is NOT "preflight passes here" -- it passes here
because this machine has a built corpus. It is that each unmet condition raises
with a message naming the command that fixes it. A preflight that fails with a
bare KeyError deep inside qdrant_client is worse than no preflight: it looks
like a broken gate rather than a missing corpus, and the operator debugs the
wrong thing.

Every check below therefore asserts on the message, not just on the exception
type. Each is paired with the happy path, because a test asserting that
something raises passes trivially if the function raises for another reason.
"""

import os
import sys
import tempfile

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts import gate  # noqa: E402
from src.config import Config  # noqa: E402


def _expect_preflight_error(needle: str) -> str:
    """Run preflight, require PreflightError, return its message."""
    try:
        gate.preflight()
    except gate.PreflightError as exc:
        message = str(exc)
        assert needle in message, f"message does not name {needle!r}: {message}"
        return message
    raise AssertionError(f"preflight did not refuse; expected a message naming {needle!r}")


def test_missing_corpus_artifacts_name_the_ingest_command() -> None:
    original = Config.VECTOR_DB_DIR
    try:
        with tempfile.TemporaryDirectory() as empty:
            Config.VECTOR_DB_DIR = os.path.join(empty, "vector_db")
            message = _expect_preflight_error("ingest_corpus.py")
            assert "vector_db" in message, "the missing artifact is not named"
            assert "gitignored" in message, "does not explain why a fresh checkout lacks it"
    finally:
        Config.VECTOR_DB_DIR = original


def test_missing_config_pointer_names_the_bootstrap_command() -> None:
    original = gate.CURRENT_POINTER
    try:
        gate.CURRENT_POINTER = os.path.join(tempfile.gettempdir(), "no-such-CURRENT")
        message = _expect_preflight_error("bootstrap_config.py")
        assert "production arm" in message, "does not say what the gate loses without it"
    finally:
        gate.CURRENT_POINTER = original


def test_embed_model_mismatch_refuses() -> None:
    """The silent-degradation bug (P1 step 1): query vectors compared against an
    index built by a different model. Retrieval degrades with no error and no log
    line, so a gate that does not check this reports green on a broken retriever."""
    original = Config.EMBED_MODEL
    try:
        Config.EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
        message = _expect_preflight_error("EMBED_MODEL mismatch")
        assert original in message or "manifest names" in message
    finally:
        Config.EMBED_MODEL = original


def test_preflight_passes_here_and_stamps_provenance() -> None:
    """Non-vacuity for all three above, and the report's provenance contract:
    a report that does not record which corpus and which eval set produced it
    cannot be compared against any other report (charter II)."""
    stamps = gate.preflight()
    for key in ("corpus_version", "embed_model", "config_version", "retrieval_eval"):
        assert stamps.get(key), f"preflight did not stamp {key}"
    assert stamps["retrieval_eval"]["rows"] > 0
    assert len(stamps["retrieval_eval"]["sha256"]) == 16


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
