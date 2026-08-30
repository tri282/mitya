# scripts/freeze_sessions.py
"""Freeze reviewed session candidates into `test/session_corpus.json`
(mitya-v2 P4 step 3, R91).

Assigns immutable `s-NNNN` ids (A9: ids are assigned only on acceptance),
stamps the corpus with a content hash, and records R91's pre-registration and
label alongside the sessions so a reader of the artifact alone can see what it
does and does not support.

Usage:
    python scripts/freeze_sessions.py --candidates test/drafts/sessions-X.json
"""

import argparse
import json
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.harness.sessions import CORPUS_PATH, LABEL, corpus_stamp

logger = logging.getLogger(__name__)
FAMILY_SIZES = {"passage_paste": 12, "locate": 8, "thematic": 10, "structural": 5, "refusal": 5}


def main() -> None:
    ap = argparse.ArgumentParser(description="Freeze reviewed sessions (R91).")
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--out", default=CORPUS_PATH)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    with open(args.candidates, encoding="utf-8") as f:
        candidates = json.load(f)

    # The pre-registration is enforced here, not trusted: a corpus whose
    # composition drifted from R91 silently is the accreted-distribution
    # failure (residual risk 4) in a new costume.
    counts: dict[str, int] = {}
    for s in candidates:
        counts[s["family"]] = counts.get(s["family"], 0) + 1
    if counts != FAMILY_SIZES:
        raise SystemExit(f"composition {counts} != R91's pre-registration {FAMILY_SIZES}")

    sessions = [{"id": f"s-{i:04d}", **s} for i, s in enumerate(candidates, start=1)]
    literal = sum(1 for s in sessions if s["family"] in ("passage_paste", "locate"))
    corpus = {
        "version": corpus_stamp(sessions),
        "finding": "R91",
        "label": LABEL,
        "pre_registration": {
            "families": FAMILY_SIZES,
            "literal_bearing_fraction": round(literal / len(sessions), 4),
            "repeat_rate_points": [0.0, 0.30],
            "validation": "none by choice (CP-4a option iv) -- see R91",
        },
        "sessions": sessions,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(corpus, f, indent=2, ensure_ascii=False)
    logger.info("froze %d sessions (%d turns) to %s, version %s",
                len(sessions), sum(len(s["turns"]) for s in sessions), args.out, corpus["version"])


if __name__ == "__main__":
    main()
