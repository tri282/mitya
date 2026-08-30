"""Frozen session corpus: load, and the A5 repeat-rate dial (mitya-v2 P4, R91).

The corpus is frozen ONCE (`test/session_corpus.json`, written by
`scripts/freeze_sessions.py`) and the repeat rate is applied at replay time
rather than baked in. R91 pre-registers TWO points -- 0.0 and 0.30 -- and
baking the rate into the artifact would mean two corpora, two version stamps,
and two things to keep in step. The dial is a replay parameter; the corpus is
one file.

Every number derived from this corpus carries R91's label. See the module's
`LABEL` constant, which is the verbatim text that must travel with any
published figure.
"""

import hashlib
import json
import logging
import random

logger = logging.getLogger(__name__)

LABEL = (
    "Derived from a harness-authored synthetic session corpus (P4, R91). Its resemblance to "
    "real usage is unproven and was not tested: no real traffic exists to compare against (D3), "
    "and the corpus was declared unvalidated by choice rather than validated by a check. Its "
    "composition is a pre-registered design parameter -- 40 sessions across five families, 50% "
    "literal-bearing -- chosen for measurability, not realism, and its cache repeat rate is a "
    "dial, not an observation. Per-family figures are reported separately so any reweighting is "
    "arithmetic rather than a regeneration."
)

CORPUS_PATH = "test/session_corpus.json"


def corpus_stamp(sessions: list[dict]) -> str:
    """Content hash over the accepted sessions. Same content, same stamp."""
    payload = json.dumps(sessions, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def load(path: str = CORPUS_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        corpus = json.load(f)
    stamp = corpus_stamp(corpus["sessions"])
    if stamp != corpus["version"]:
        raise ValueError(
            f"session corpus {path} is modified: recomputed stamp {stamp} != "
            f"recorded {corpus['version']}. A frozen fixture that changed silently is "
            f"not a fixture -- re-freeze deliberately or restore the file."
        )
    logger.info("session corpus %s: %d sessions, %d turns, version %s",
                path, len(corpus["sessions"]),
                sum(len(s["turns"]) for s in corpus["sessions"]), stamp)
    return corpus


def expand_with_repeats(sessions: list[dict], rate: float, rng: random.Random) -> list[dict]:
    """Replace `rate` of turns with a query drawn from elsewhere in the corpus.

    This is what makes a cache hit possible at all: under A12 the key is
    (query, filters, config_version, route) and is deliberately NOT
    tenant-keyed while the corpus is shared, so a repeated query hits whoever
    issued it. `rate` is a recorded PARAMETER -- every cache-hit-rate claim
    downstream carries its value, per R91.
    """
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"repeat rate {rate} outside [0, 1]")
    pool = [t["query"] for s in sessions for t in s["turns"]]
    out = []
    for session in sessions:
        turns = []
        for turn in session["turns"]:
            if rng.random() < rate:
                turns.append({**turn, "query": rng.choice(pool), "repeat": True})
            else:
                turns.append({**turn, "repeat": False})
        out.append({**session, "turns": turns})
    return out


def observed_repeat_rate(sessions: list[dict]) -> float:
    """Fraction of turns actually marked as repeats. A parameter nobody checks
    is a parameter that drifts (schedule-p4.md, Batch B verify)."""
    turns = [t for s in sessions for t in s["turns"]]
    return sum(1 for t in turns if t.get("repeat")) / len(turns) if turns else 0.0
