# scripts/run_harness_replay.py
"""Thin CLI driver for the traffic harness's replay mode (mitya-v2 P4 step 2,
`04-plan/phase-4-harness.md`). All logic lives in `src/harness/replay.py`;
this file only parses argv and calls it.

`[RUN -- needs approval]`: this drives real `/chat` requests against a live
API and, on a hosted arm, spends real provider USD. It requires `uvicorn`
already running (this script starts nothing) and must not run concurrently
with any offline eval/REPL against `data/vector_db/` (exclusive file lock,
`docs/RUNBOOK-P6.md:16`).

Usage:
    .venv/bin/python scripts/run_harness_replay.py --pairs test/retrieval_eval.json --json out.json
"""

import argparse
import logging

from src.harness.client import DEFAULT_BASE_URL
from src.harness.replay import run

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Replay an eval set's queries through the live API.")
    ap.add_argument("--pairs", default="test/retrieval_eval.json",
                     help="eval-set JSON to replay (read-only; not modified)")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--json", dest="out_path", default=None,
                     help="write per-row records + summary to this path")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(args.pairs, args.base_url, args.out_path)
