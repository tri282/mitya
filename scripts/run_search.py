#!/usr/bin/env python3
"""P7 Batch C, step 6 -- the on-demand entry point (D-7b: "automated on
demand", never scheduled; G-P23 dissolves under this shape rather than being
engineered around).

`[RUN -- needs approval]`. Opens the vector DB (through the objective's own
pricing pass) and calls the unchanged P3 gate for every candidate the
cost-first funnel admits (D-7d). A human invokes this by hand:

    .venv/bin/python scripts/run_search.py --max-wall-clock-s 300 --max-points 5

No scheduler, no cron, no daemon -- a plain CLI a human runs, once, when they
want a search. `src.search.run_search()` holds every decision this script
makes; this file only wires it to real machinery (`src.p7_objective`'s
pricing context, `src.optimizer.run_gate_for_candidate`) and prints the trace.

`--per-point-wall-clock-s` MUST be a number the caller measured, not a guess
(CP-7d: "no cap from a cost that was never measured") -- there is no default,
by design.
"""

import argparse
import dataclasses
import datetime
import json
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config_artifact import active_config_version, active_serving_config, read_pointer  # noqa: E402
from src.optimizer import run_gate_for_candidate  # noqa: E402
from src.p7_objective import load_pricing_models, open_pricing_context, price_config  # noqa: E402
from src.search import Budget, EvolutionaryFamily, GridRandomFamily, SurrogateFamily, run_search  # noqa: E402

logger = logging.getLogger(__name__)

TRACE_DIR = os.path.join("test", "search")

_FAMILIES = {
    "evolutionary": EvolutionaryFamily,
    "grid_random": GridRandomFamily,
    "surrogate": SurrogateFamily,
}


def main(max_wall_clock_s: float, max_points: int, per_point_wall_clock_s: float,
         family_name: str, seed: int) -> int:
    pointer_before = read_pointer()

    base_config = active_serving_config()
    base_version = active_config_version()
    budget = Budget(max_wall_clock_s=max_wall_clock_s, max_points=max_points)
    family = _FAMILIES[family_name]()

    # Defect 1 fix: a pricing context and the gate's own Qdrant client cannot
    # both be open at once -- local-mode Qdrant permits exactly one client per
    # store (`data/vector_db/`). `price_fn` therefore opens and closes its
    # OWN pricing context for every candidate, never spanning a gate run, so
    # `run_gate_for_candidate` (which opens its own client inside `gate_runner`)
    # never contends with it. `models` (embedder/bm25/ranker, ~1.2s to load)
    # and `retrieval_cache` are loaded/created ONCE here and passed into every
    # open -- only the QdrantClient itself, not the models or the cache, is
    # re-acquired per candidate.
    models = load_pricing_models()
    retrieval_cache: dict = {}

    def price_fn(version: str):
        with open_pricing_context(models=models, retrieval_cache=retrieval_cache) as ctx:
            return price_config(ctx, version)

    # D-8(c): the trace is written AS THE RUN GOES, never only at the end.
    # Step 7's own words are that the search trace IS the evidence, and at
    # 38-140s per point an interrupted run would otherwise lose every
    # completed point. One JSON object per line, flushed and fsync'd per row,
    # so a kill -9 between points still leaves every finished point on disk.
    os.makedirs(TRACE_DIR, exist_ok=True)
    trace_path = os.path.join(
        TRACE_DIR, f"{datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ')}"
                   f"-{family_name}-seed{seed}.jsonl")
    logger.info("run_search: checkpointing every point to %s", trace_path)

    def on_row(row) -> None:
        with open(trace_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(dataclasses.asdict(row), default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        logger.info("  checkpoint: %s=%s->%s objective_usd=%s disposition=%s",
                    row.mutated_field, row.mutated_from, row.mutated_to,
                    row.objective_usd, row.disposition)

    rows = run_search(
        budget=budget,
        price_fn=price_fn,
        gate_runner=run_gate_for_candidate,
        base_config=base_config,
        base_version=base_version,
        per_point_wall_clock_s=per_point_wall_clock_s,
        family=family,
        seed=seed,
        on_row=on_row,
    )

    pointer_after = read_pointer()
    if pointer_before != pointer_after:
        raise AssertionError(
            f"CURRENT_POINTER moved from {pointer_before!r} to {pointer_after!r} -- "
            f"the search must never promote"
        )

    gate_runs = sum(1 for r in rows if r.passed_cost_first_funnel)
    logger.info(
        "run_search: %d mutation(s) tried, %d reached the gate (cost-first funnel, D-7d), "
        "CURRENT_POINTER unchanged (%s)", len(rows), gate_runs, pointer_after,
    )
    for row in rows:
        print(f"  {row.mutated_field}={row.mutated_from}->{row.mutated_to}: {row.divergence_line()}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-wall-clock-s", type=float, required=True)
    ap.add_argument("--max-points", type=int, required=True)
    ap.add_argument("--per-point-wall-clock-s", type=float, required=True,
                     help="MEASURED cost of one gate run in this checkout -- never a guess (CP-7d)")
    ap.add_argument("--family", choices=sorted(_FAMILIES), default="evolutionary")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(main(args.max_wall_clock_s, args.max_points, args.per_point_wall_clock_s,
                          args.family, args.seed))
