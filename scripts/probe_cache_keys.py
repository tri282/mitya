#!/usr/bin/env python3
"""P6 P-17 -- the answer cache's hit rate over the frozen session corpus.

Desk simulation, not a run: no server, no provider, no spend. It reads
`test/session_corpus.json`, applies R91's repeat-rate dial at its two
pre-registered points (0.0 and 0.30), and counts key collisions under the key
`src/rag/cache.py` builds today.

**One simplification, derived rather than assumed.** `route` is a pure function
of (query, filters, structure, summaries) (`src/rag/router.py:57`) and the
artifacts are fixed for a replay, so two identical queries always get the same
route. Replayed turns carry only `query` (R120), so `filters` is None, and
`config_version`/`corpus_version` are constant within a run. Collisions over
the full key therefore equal collisions over the normalized query alone -- so
this needs neither the router nor the loaded artifacts. It still calls
`AnswerCache._key` rather than re-implementing `strip().lower()`, so there is
no second normalization to diverge from the real one (R88's defect).

**The known-answer case is baked in.** R125 measured, by `jq`, that the corpus
carries 57 duplicate turns of 108 at rate 0.0 -- all 40 openers distinct, all 68
follow-ups drawn from a pool of 11 strings. `--check` asserts exactly that. A
driver that silently produced a different number is normalizing differently
from the cache, and its 0.30 number would be worthless.

**The memory-bearing half is arithmetic, not a run.** A key component that
varies per turn collides never, so the rate is 0 by construction and is
labelled as such. What CP-6c actually needs is the curve over WEAKER
fingerprints -- session-scoped, turn-index-scoped, and hashed into N buckets --
which is what this reports beside the zero.

Usage:
    python scripts/probe_cache_keys.py --dry
    python scripts/probe_cache_keys.py --json out/p6-cache-keys.json
"""

import argparse
import hashlib
import json
import logging
import os
import random
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.harness import sessions as S
from src.rag.cache import AnswerCache

logger = logging.getLogger("probe_cache_keys")

# R91's two pre-registered points, and the seed that makes the stochastic one a
# measurement rather than a sample. Both travel into the artifact.
RATES = (0.0, 0.30)
SEED = 91
EXPECTED_CORPUS_VERSION = "4155c97b0a2b"
# R125, measured by jq over the frozen corpus. The known-answer case.
EXPECTED_BASELINE_HITS = 57
EXPECTED_TURNS = 108

# Constant within a replay, so their values cannot change a collision count.
# Named rather than inlined so the artifact can say what was held fixed.
FIXED = {"filters": None, "route": "default", "config_version": "fixed", "corpus_version": "fixed"}


def _key(query: str, extra: str = "") -> str:
    """The real cache key. `_key` never touches `self`, so it is called unbound
    rather than constructing an AnswerCache -- construction would open diskcache
    at Config.CACHE_DIR, which is a side effect this probe has no use for."""
    return AnswerCache._key(
        None, query + extra, FIXED["filters"], FIXED["route"],
        FIXED["config_version"], FIXED["corpus_version"],
    )


def replay(expanded: list[dict], memory: str = "none") -> dict:
    """Walk turns in replay order, counting a hit whenever the key has been seen.

    `memory` selects the fingerprint mixed into the key:
      none        -- today's key
      per_turn    -- a component that varies every turn (Q6 option a, full fidelity)
      per_session -- session-scoped memory: repeats within a session still hit
      bucket:N    -- the memory state hashed into N buckets
    No TTL and no eviction: the corpus is one replay and the cache has no TTL
    (cache.py's docstring), so first-occurrence-is-a-miss is the whole model.
    """
    seen: set[str] = set()
    hits = turns = 0
    for session in expanded:
        for idx, turn in enumerate(session["turns"]):
            if memory == "none":
                extra = ""
            elif memory == "per_turn":
                extra = f"|mem:{session['id']}:{idx}"
            elif memory == "per_session":
                extra = f"|mem:{session['id']}"
            elif memory.startswith("bucket:"):
                n = int(memory.split(":", 1)[1])
                # NOT builtin hash(): PYTHONHASHSEED salts str hashing per
                # process, so the bucket assignment would differ between runs
                # and the curve would not be reproducible.
                digest = hashlib.sha256(f"{session['id']}:{idx}".encode()).hexdigest()
                extra = f"|mem:{int(digest, 16) % n}"
            else:
                raise ValueError(f"unknown memory fingerprint {memory!r}")
            key = _key(turn["query"], extra)
            turns += 1
            if key in seen:
                hits += 1
            else:
                seen.add(key)
    return {"turns": turns, "hits": hits, "distinct_keys": len(seen),
            "hit_rate": round(hits / turns, 4) if turns else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true",
                    help="3 turns only: print what they produced, per CLAUDE.md's batch rule")
    ap.add_argument("--json", help="write the artifact here")
    ap.add_argument("--corpus", default=S.CORPUS_PATH)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    corpus = S.load(args.corpus)  # raises if the frozen fixture changed silently
    if corpus["version"] != EXPECTED_CORPUS_VERSION:
        raise SystemExit(
            f"corpus version {corpus['version']} != pre-registered {EXPECTED_CORPUS_VERSION}. "
            f"This probe's known-answer case (57/108) was measured against that version only."
        )
    base = corpus["sessions"]

    if args.dry:
        # Read what it produced, not the exit code.
        for session in base[:1]:
            for idx, turn in enumerate(session["turns"][:3]):
                q = turn["query"]
                logger.info("dry turn %d: normalized=%r key=%s",
                            idx, q.strip().lower()[:60], _key(q)[:12])
        logger.info("dry: rate-0.0 replay -> %s", json.dumps(replay(base)))
        logger.info("dry: known answer is hits=%d of turns=%d", EXPECTED_BASELINE_HITS, EXPECTED_TURNS)
        return 0

    # Known-answer case FIRST. An instrument that has not been checked against a
    # case whose answer is known in advance is not evidence (P5's rule, six times).
    baseline = replay(base)
    if (baseline["turns"], baseline["hits"]) != (EXPECTED_TURNS, EXPECTED_BASELINE_HITS):
        raise SystemExit(
            f"known-answer check FAILED: rate-0.0 replay gave {baseline['hits']} hits over "
            f"{baseline['turns']} turns, expected {EXPECTED_BASELINE_HITS}/{EXPECTED_TURNS} "
            f"(R125, measured by jq). This driver is normalizing differently from the cache; "
            f"its 0.30 number is not usable."
        )
    logger.info("known-answer check PASSED: %d/%d hits at rate 0.0", baseline["hits"], baseline["turns"])

    result = {
        "probe": "P-17",
        "finding": "R125",
        "corpus_version": corpus["version"],
        "seed": SEED,
        "key_components_held_fixed": FIXED,
        "label": S.LABEL,
        "rates": {},
        "memory_fingerprints": {},
    }
    for rate in RATES:
        expanded = S.expand_with_repeats(base, rate, random.Random(SEED))
        observed = round(S.observed_repeat_rate(expanded), 4)
        row = replay(expanded)
        # A parameter nobody checks is a parameter that drifts (sessions.py:82).
        # The dial not firing looks identical to success in a hit-rate column.
        row["requested_repeat_rate"] = rate
        row["observed_repeat_rate"] = observed
        if rate > 0 and observed == 0.0:
            raise SystemExit(f"repeat dial did not fire at rate {rate}: observed {observed}")
        result["rates"][str(rate)] = row
        logger.info("rate=%.2f observed=%.4f -> %s", rate, observed, json.dumps(row))

    # The memory-bearing half. Zero is the arithmetic prior, not a measurement --
    # so it is labelled, and the weaker fingerprints are reported beside it so
    # CP-6c chooses from a curve rather than from one zero.
    for fp in ("per_turn", "per_session", "bucket:8", "bucket:32"):
        row = replay(base, memory=fp)
        row["provenance"] = "by_construction" if fp == "per_turn" else "computed"
        result["memory_fingerprints"][fp] = row
        logger.info("memory=%s -> %s", fp, json.dumps(row))

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        logger.info("wrote %s", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
