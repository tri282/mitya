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
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config_artifact import active_config_version, active_serving_config, read_pointer  # noqa: E402
from src.optimizer import run_gate_for_candidate  # noqa: E402
from src.p7_objective import open_pricing_context, price_config  # noqa: E402
from src.search import Budget, EvolutionaryFamily, GridRandomFamily, SurrogateFamily, run_search  # noqa: E402

logger = logging.getLogger(__name__)

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

    with open_pricing_context() as ctx:
        rows = run_search(
            budget=budget,
            price_fn=lambda version: price_config(ctx, version),
            gate_runner=run_gate_for_candidate,
            base_config=base_config,
            base_version=base_version,
            per_point_wall_clock_s=per_point_wall_clock_s,
            family=family,
            seed=seed,
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
