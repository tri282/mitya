"""P3 eval gate (mitya-v2, charter §III CP3).

This module is built one step at a time. Step 3 owns `preflight()`; step 4 owns
the entry point that calls it; step 5 owns the three metric layers.

WHY A PREFLIGHT AT ALL. A gate that runs against a missing or mismatched corpus
does not fail -- it measures a *different system* and reports green. That is
R19's failure mode (a harness scoring a pipeline production does not run)
promoted from a code path to a deployment condition, and a green check is the
worst possible way to discover it.

`data/` is gitignored (D8: shipping the corpus is ceremony this deploy does not
need), so a fresh checkout has the code and the eval sets but no vector DB. The
gate does not rebuild it -- re-ingestion is a minutes-long GPU operation with
its own script and its own manifest. The gate refuses, loudly, naming the
command.
"""

import hashlib
import json
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from qdrant_client import QdrantClient  # noqa: E402

from src.config import Config  # noqa: E402
from src.config_artifact import (  # noqa: E402
    CURRENT_POINTER,
    ConfigError,
    active_corpus_manifest,
    assert_embed_model_matches,
)

logger = logging.getLogger(__name__)

# The eval sets are tracked (contrary to 02-decide §2's premise -- see G-A), but
# their paths live in the harness scripts as argparse defaults rather than on
# Config. Restated here rather than added to Config: the gate is one more
# consumer, not the owner.
RETRIEVAL_EVAL_PATH = os.path.join("test", "retrieval_eval.json")


class PreflightError(RuntimeError):
    """The checkout cannot produce the numbers the gate would report."""


def _eval_set_stamp(path: str) -> dict:
    """Row count + content hash of an eval set.

    Recorded into every report so two reports can never be compared across
    different sets (§II; A9 makes the ids immutable, which is what gives the
    hash its meaning -- a changed hash means rows were appended, never edited).
    """
    with open(path, "rb") as f:
        raw = f.read()
    return {
        "path": path,
        "rows": len(json.loads(raw)),
        "sha256": hashlib.sha256(raw).hexdigest()[:16],
    }


def preflight() -> dict:
    """Refuse to gate on a checkout that cannot produce the numbers.

    Mirrors `src/api/app.py`'s lifespan startup (validate_rag -> manifest ->
    embed-model agreement) deliberately: the gate must refuse on exactly the
    conditions serving refuses on, or it certifies a config the server would
    not accept.

    Returns the provenance stamps to embed in the report. Raises
    `PreflightError` with the fixing command on any unmet condition.
    """
    # 1. The four RAG artifacts. Config.validate_rag() already owns this check
    # and names the rebuild scripts -- do not restate the list here, or it
    # drifts from the one serving uses.
    try:
        Config.validate_rag()
    except FileNotFoundError as exc:
        raise PreflightError(
            f"{exc} Corpus artifacts live under data/, which is gitignored (D8), "
            f"so a fresh checkout has none of them."
        ) from exc

    # 2. The serving config pointer. Without it there is no production arm to
    # compare a candidate against, and "production" is the A6 pointer by
    # definition (§V).
    if not os.path.exists(CURRENT_POINTER):
        raise PreflightError(
            f"no serving config pointer at {CURRENT_POINTER} -- the gate has no "
            f"production arm to compare against. Run: python scripts/bootstrap_config.py"
        )

    # 3. Corpus/config agreement. The index and the query embedder must be the
    # same model or retrieval degrades silently, with no error and no log line
    # (P1 step 1's reproduced bug). Reuse the assertion serving uses.
    try:
        manifest = active_corpus_manifest()
        assert_embed_model_matches(Config.EMBED_MODEL, manifest)
    except ConfigError as exc:
        raise PreflightError(str(exc)) from exc

    # 4. The vector DB must be openable. Local-mode Qdrant takes an exclusive
    # lock on the directory, so a running uvicorn makes every eval script fail
    # deep inside a retrieval call (docs/RUNBOOK-P6.md:16). Detect it here, by
    # opening rather than by port-scanning: the lock is the real condition, and
    # a port says nothing about who holds the file.
    try:
        QdrantClient(path=Config.VECTOR_DB_DIR).close()
    except Exception as exc:  # qdrant-client raises its own storage errors
        raise PreflightError(
            f"cannot open the vector DB at {Config.VECTOR_DB_DIR}: {exc} -- if the API "
            f"is running, stop it: local-mode Qdrant holds an exclusive lock on that "
            f"directory and the gate cannot read it while uvicorn does."
        ) from exc

    stamps = {
        "corpus_version": manifest.version(),
        "embed_model": Config.EMBED_MODEL,
        "config_version": open(CURRENT_POINTER, encoding="utf-8").read().strip(),
        "retrieval_eval": _eval_set_stamp(RETRIEVAL_EVAL_PATH),
    }
    for key, value in stamps.items():
        logger.info("preflight ok: %s = %s", key, value)
    return stamps


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    preflight()
    logger.info("preflight passed")
