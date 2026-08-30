# test/test_session_corpus.py
"""Verify: the frozen session corpus and the A5 repeat-rate dial (P4 Batch B,
R91).

Plain-assert script, not pytest (pytest is not installed in .venv). Offline,
no GPU, no Qdrant, no API, no server. Checks the three properties Batch B's
verify names, each of which fails silently if it breaks:

    1. The corpus is deterministic and re-readable -- same version stamp, same
       ids, same counts on a second read. `load()` recomputes the stamp, so a
       silently edited fixture raises instead of being replayed as if frozen.
    2. The recorded repeat rate matches the corpus's ACTUAL repetition. "A
       parameter nobody checks is a parameter that drifts"
       (schedule-p4.md, Batch B).
    3. The composition matches R91's pre-registration exactly. A drifted
       composition is residual risk 4 -- an accreted distribution -- in a new
       costume.

Usage:
    python test/test_session_corpus.py
"""

import logging
import os
import random
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.harness.sessions import (
    corpus_stamp,
    expand_with_repeats,
    load,
    observed_repeat_rate,
)

logger = logging.getLogger(__name__)

R91_FAMILIES = {"passage_paste": 12, "locate": 8, "thematic": 10, "structural": 5, "refusal": 5}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    corpus = load()
    sessions = corpus["sessions"]

    # 1. Deterministic and re-readable.
    assert corpus_stamp(sessions) == corpus["version"], "stamp does not match content"
    assert load()["version"] == corpus["version"], "second read differs from the first"
    ids = [s["id"] for s in sessions]
    assert ids == sorted(ids), "ids are not in stable order"
    assert len(set(ids)) == len(ids), "duplicate session id"
    logger.info("ok: corpus is deterministic -- same stamp, ids and counts on re-read")

    # A tampered corpus must fail loudly rather than replay as if frozen.
    tampered = [dict(s) for s in sessions]
    tampered[0] = {**tampered[0], "family": "thematic"}
    assert corpus_stamp(tampered) != corpus["version"], "stamp is insensitive to content"
    logger.info("ok: a silently edited fixture changes the stamp")

    # 2. Composition matches R91's pre-registration.
    counts: dict[str, int] = {}
    for s in sessions:
        counts[s["family"]] = counts.get(s["family"], 0) + 1
    assert counts == R91_FAMILIES, f"composition {counts} != R91 {R91_FAMILIES}"
    literal = sum(1 for s in sessions if s["family"] in ("passage_paste", "locate"))
    assert literal / len(sessions) == 0.5, f"literal fraction {literal}/{len(sessions)} != 0.50"
    assert corpus["pre_registration"]["repeat_rate_points"] == [0.0, 0.30]
    logger.info("ok: composition matches R91 -- 40 sessions, 5 families, 0.50 literal-bearing")

    # Every retrieval-scored family carries a target; refusal carries none.
    for s in sessions:
        if s["family"] == "refusal":
            assert not s["targets"], f"{s['id']}: refusal must not be retrieval-scored"
        else:
            assert s["targets"], f"{s['id']}: missing target"
    logger.info("ok: targets present exactly where the family is retrieval-scored")

    # 3. The repeat-rate dial produces the rate it claims.
    zero = expand_with_repeats(sessions, 0.0, random.Random(0))
    assert observed_repeat_rate(zero) == 0.0, "rate 0.0 produced repeats"
    logger.info("ok: rate 0.0 is the cache floor -- no turn is a repeat")

    # 0.30 over 108 turns is a binomial draw, not an exact count: assert the
    # dial lands in a band, and assert the band is tight enough that a broken
    # dial (0.0 or 1.0) could not pass it.
    observed = [observed_repeat_rate(expand_with_repeats(sessions, 0.30, random.Random(seed)))
                for seed in range(20)]
    mean = sum(observed) / len(observed)
    assert 0.24 < mean < 0.36, f"dial set to 0.30 produced mean {mean:.3f}"
    assert all(0.15 < o < 0.45 for o in observed), f"a draw fell outside the band: {observed}"
    logger.info("ok: dial set to 0.30 observed mean %.3f over 20 seeds", mean)

    # The label must travel with the artifact, not live only in the register.
    assert "unproven and was not tested" in corpus["label"]
    assert "not realism" in corpus["label"]
    logger.info("ok: R91's label is carried in the corpus file itself")

    logger.info("ALL SESSION-CORPUS CHECKS PASSED")


if __name__ == "__main__":
    main()
