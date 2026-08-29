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

import argparse
import datetime
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from qdrant_client import QdrantClient  # noqa: E402

from src.config import Config  # noqa: E402
from src.config_artifact import (  # noqa: E402
    CURRENT_POINTER,
    VERSION_ENV,
    ConfigError,
    active_corpus_manifest,
    assert_embed_model_matches,
    load,
    read_pointer,
)
from src.rag.gate import (  # noqa: E402
    BREAK,
    ERROR,
    NOT_DETECTED,
    computed_cost_usd,
    cost_layer,
    disposition_of,
    flips,
    invariants,
    mcnemar_noninferiority,
    validate_report,
)

logger = logging.getLogger(__name__)

# The eval sets are tracked (contrary to 02-decide §2's premise -- see G-A), but
# their paths live in the harness scripts as argparse defaults rather than on
# Config. Restated here rather than added to Config: the gate is one more
# consumer, not the owner.
RETRIEVAL_EVAL_PATH = os.path.join("test", "retrieval_eval.json")
GATE_DIR = os.path.join("test", "gate")
FROZEN_REFERENCE = os.path.join(GATE_DIR, "local-q4km-frozen.json")

# Exit codes. ERROR is deliberately distinct from BLOCK: a provider or
# environment failure is a re-run condition, not a verdict about the candidate.
# Collapsing them is how a flaky network becomes "the change is bad" and,
# eventually, how the gate gets disabled (charter VIII).
EXIT_PASS, EXIT_BLOCK, EXIT_ERROR = 0, 1, 2


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


def run_arm(version: str | None, label: str) -> dict:
    """Score one arm in its own process.

    Subprocess, not a function call, and not negotiable: `eval_retrieval.py`
    imports the pipeline and reads a process-lifetime cached serving config
    (`active_serving_config()`), so two arms in one process would score against
    the same singleton. Mutating that global between arms is precisely the
    contamination P0 spent a phase learning to detect. One embedder load per
    arm is the price, amortised over 106 rows.
    """
    env = dict(os.environ)
    if version:
        env[VERSION_ENV] = version
    else:
        env.pop(VERSION_ENV, None)   # production resolves through CURRENT (A6)

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "arm.json")
        proc = subprocess.run(
            [sys.executable, os.path.join("scripts", "eval_retrieval.py"),
             "--arm", "full", "--json", out],
            env=env, capture_output=True, text=True,
        )
        if proc.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(
                f"arm {label!r} did not produce a result (exit {proc.returncode}): "
                f"{proc.stderr.strip()[-400:]}"
            )
        with open(out, encoding="utf-8") as f:
            record = json.load(f)[-1]

    record["arm_label"] = label
    record["config_version"] = version or read_pointer()
    record["serving_model"] = load(record["config_version"]).serving_model
    logger.info("arm %s: config=%s model=%s", label, record["config_version"],
                record["serving_model"])
    return record


def assert_comparable(production: dict, candidate: dict) -> None:
    """Refuse to compare arms whose serving model differs (P1 step 5's rule).

    Without this the gate attributes a quality delta to a config change that
    never happened -- the model moved underneath it. Fail-closed: a refusal is
    an ERROR, not a BLOCK, because nothing was measured about the candidate.
    """
    if production["serving_model"] != candidate["serving_model"]:
        raise RuntimeError(
            f"refusing to compare arms on different models: production is "
            f"{production['serving_model']!r}, candidate is {candidate['serving_model']!r}. "
            f"A delta between them is not attributable to the config change."
        )


def frozen_reference() -> dict:
    """The `local-q4km` chart line, read from its record and never executed (A3)."""
    with open(FROZEN_REFERENCE, encoding="utf-8") as f:
        reference = json.load(f)
    assert reference["executed"] is False, "the frozen arm must never be marked executed"
    return reference


def _baseline_path(version: str) -> str:
    return os.path.join(GATE_DIR, f"baseline-{version}.json")


def read_baseline(version: str) -> dict | None:
    """The recorded run for this config version, or None on its first sighting.

    F7's bit-equality check needs something to compare against. A first run
    RECORDS and reports NO-BASELINE rather than passing silently -- "nothing to
    compare" and "compared and identical" are different claims and must not
    print the same way. Retention is not decided; nothing prunes this directory
    (gap G-G).
    """
    path = _baseline_path(version)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_baseline(version: str, arm: dict) -> None:
    with open(_baseline_path(version), "w", encoding="utf-8") as f:
        json.dump({"config_version": version, "rows": arm["rows"],
                   "recorded_utc": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")},
                  f, indent=2)


def write_report(report: dict, candidate: str | None) -> str:
    os.makedirs(GATE_DIR, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(GATE_DIR, f"{stamp}-{candidate or 'production'}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return path


def build_layers(prod: dict, cand: dict | None, baseline: dict | None,
                 manifest) -> dict:
    """The three layers. Quality is evaluated PER FAMILY, never pooled -- a
    pooled denominator is the failure charter II was written against."""
    layers = {
        "invariants": invariants(prod, cand, baseline),
        "cost": cost_layer(
            computed_cost_usd(load(prod["config_version"]), manifest),
            computed_cost_usd(load(cand["config_version"]), manifest) if cand else None,
        ),
        "quality_1a": [],
        # Empty until its composition is chosen (G-B) and until any 1b metric
        # has an MDE (G-C). 1b is report-only at promotion time; it does not
        # block. This is the phase's largest open item, deliberately not closed.
        "quality_1b": [],
    }
    if cand is None:
        return layers

    families = sorted({r["family"] for r in prod["rows"]})
    for family in families:
        prod_hits = {r["id"]: r["hit"] for r in prod["rows"] if r["family"] == family}
        cand_hits = {r["id"]: r["hit"] for r in cand["rows"] if r["family"] == family}
        record = mcnemar_noninferiority(prod_hits, cand_hits)
        record["metric"] = "recall_at_5"
        record["family"] = family
        layers["quality_1a"].append(record)
    return layers


def build_caption(layers: dict, disposition: str, reason: str) -> str:
    """The green check's wording is a charter obligation, not a UX detail (A13).

    It must carry the BOUND, never the claim: a candidate can be up to one MDE
    worse and still pass, so "quality preserved" would be a false statement
    about a real promotion.
    """
    primary = next((r for r in layers["quality_1a"]
                    if r.get("family") == "primary" and r.get("n")), None)
    parts = [reason]
    cost = layers["cost"][0]
    if cost.get("delta_pct") is not None:
        parts.append(f"computed cost {cost['delta_pct']:+.1f}% ({cost['basis']})")
    if primary and primary.get("mde") is None:
        parts.append(f"retrieval identical on all {primary['n']} primary rows "
                     f"(no discordant pairs; MDE undefined)")
    elif primary and primary["verdict"] == NOT_DETECTED:
        parts.append(f"retrieval not detectably worse "
                     f"(delta {primary['delta']:+.4f}, MDE {primary['mde']:.3f} at n={primary['n']})")
    elif primary:
        parts.append(f"retrieval {primary['verdict']} (delta {primary['delta']:+.4f}, "
                     f"MDE {primary['mde']:.3f} at n={primary['n']})")
    if layers["quality_1b"] == []:
        parts.append("1b not evaluated (composition open, G-B)")
    return "; ".join(parts)
    os.makedirs(GATE_DIR, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(GATE_DIR, f"{stamp}-{candidate or 'production'}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return path


def main(candidate: str | None, promote: bool) -> int:
    stamps = preflight()

    # Production is re-run live every time, never read from a stored record
    # (F7). Its retrieval vector is free, and freezing it would trade a $0
    # re-measurement for blindness to environment drift -- and would retire the
    # cross-process determinism check that caught R18.
    arms = {"production": run_arm(None, "production")}
    if candidate:
        arms["candidate"] = run_arm(candidate, "candidate")
        assert_comparable(arms["production"], arms["candidate"])

    prod = arms["production"]
    cand = arms.get("candidate")
    baseline = read_baseline(prod["config_version"])
    layers = build_layers(prod, cand, baseline, active_corpus_manifest())
    disposition, reason = disposition_of(layers)

    report = {
        "schema_version": 1,
        "generated_utc": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "provenance": stamps,
        "arms": arms,
        "layers": layers,
        "flips": flips(prod["rows"], cand["rows"]) if cand else [],
        "local_q4km": frozen_reference(),
        "disposition": disposition,
        "caption": build_caption(layers, disposition, reason),
    }
    validate_report(report)
    path = write_report(report, candidate)
    logger.info("report written: %s", path)

    if baseline is None:
        write_baseline(prod["config_version"], prod)
        logger.info("no baseline for %s -- recorded this run as the baseline",
                    prod["config_version"])

    logger.info("flips: %d", len(report["flips"]))
    for flip in report["flips"]:
        logger.info("  %s %s (%s) rank %s -> %s", flip["id"], flip["direction"],
                    flip["family"], flip["prod_rank"], flip["cand_rank"])
    logger.info("DISPOSITION %s -- %s", disposition.upper(), report["caption"])

    if promote:
        # Step 6 owns promotion. Refusing loudly beats a flag that silently
        # does nothing -- an operator who believes a promotion happened is
        # worse off than one who gets an error.
        logger.error("--promote is not implemented until step 6; nothing was promoted")
        return EXIT_ERROR

    if disposition == "error":
        return EXIT_ERROR
    return EXIT_BLOCK if disposition == "blocks" else EXIT_PASS


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="P3 eval gate (charter CP3).")
    ap.add_argument("--candidate", default=None,
                    help="serving config version to gate; omit to run production against itself")
    ap.add_argument("--promote", action="store_true",
                    help="move the CURRENT pointer on a passing verdict (step 6)")
    args = ap.parse_args()
    try:
        sys.exit(main(args.candidate, args.promote))
    except (PreflightError, ConfigError, RuntimeError) as exc:
        logger.error("%s", exc)
        sys.exit(EXIT_ERROR)
