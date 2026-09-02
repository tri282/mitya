#!/usr/bin/env python3
"""P7 Batch C -- measure the real per-point wall-clock cost of D-1's objective
plus a gate run, on THIS checkout. `[RUN -- needs approval]`.

CP-7d's own rule: "no cap from a cost that was never measured." Batch A
measured ~38.9s for a gate run alone; this adds the objective's own
retrieval pass and reports the true total, so `Budget(...).assert_affordable()`
is called with a real number rather than an assumption.
"""

import logging
import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import config_artifact as CA  # noqa: E402
from src.held_out_split import build_split, search_view  # noqa: E402
from src.optimizer import propose_one, run_gate_for_candidate  # noqa: E402
from src.p7_objective import open_pricing_context, price_config  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    base_version = CA.read_pointer()
    ids = search_view(build_split()).ids
    logger.info("search-half population: %d queries", len(ids))

    t0 = time.monotonic()
    with open_pricing_context() as ctx:
        t_models_loaded = time.monotonic()
        logger.info("model load (embedder+bm25+ranker+qdrant open): %.1fs", t_models_loaded - t0)

        t_price0 = time.monotonic()
        result = price_config(ctx, base_version, query_ids=ids)
        t_price1 = time.monotonic()
    price_elapsed = t_price1 - t_price0
    logger.info("objective pricing, FULL search half (%d queries): %.1fs (%.2fs/query)",
                result.n_queries, price_elapsed, price_elapsed / result.n_queries)

    production = CA.active_serving_config()
    candidate = propose_one(production, {"top_k_rerank": 2})
    cand_version = CA.save(candidate)
    t_gate0 = time.monotonic()
    report = run_gate_for_candidate(cand_version)
    t_gate1 = time.monotonic()
    gate_elapsed = t_gate1 - t_gate0
    logger.info("gate run: %.1fs (disposition=%s)", gate_elapsed, report["disposition"])

    print(f"\nmodel load (one-time, amortized over a whole search run): {t_models_loaded - t0:.1f}s")
    print(f"objective pricing, full search half ({result.n_queries} queries): "
          f"{price_elapsed:.1f}s ({price_elapsed / result.n_queries:.2f}s/query)")
    print(f"gate run (one candidate): {gate_elapsed:.1f}s")
    print(f"TOTAL per-point cost (objective + gate, amortized model load excluded): "
          f"{price_elapsed + gate_elapsed:.1f}s")
    print(f"TOTAL including one-time model load (first point in a fresh process): "
          f"{(t_models_loaded - t0) + price_elapsed + gate_elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
