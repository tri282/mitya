#!/usr/bin/env python3
"""P7 Batch C -- CP-7a's runnable proof (schedule §7 Batch C): does D-1's
chosen objective (`src.p7_objective.price_config`) actually SEE the
memory/allocation axes `computed_cost_usd` reads as exactly 0.000 (G-P21)?

`[RUN -- needs approval]`. Opens local-mode Qdrant + fastembed + FlashRank
for one held-out-search-half pricing pass per config perturbed, closes them
on exit. No provider call, no promotion, no write to `config/serving/`
survives past the pointer (only `CURRENT_POINTER`-independent versioned
artifacts are written by `save()`, same as every other proposer call).

Asserts, not eyeballs (the task's own instruction): a number that does not
move on a memory-axis perturbation is exactly the failure P-21/G-P21 named,
so this fails loudly rather than printing a delta a reader might skim past.
"""

import logging
import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import config_artifact as CA  # noqa: E402
from src.p7_objective import gate_ceiling_usd, open_pricing_context, price_config  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    base_cfg = CA.load(CA.read_pointer())
    base_version = base_cfg.version()

    # memory_budget_tokens=512: P-20/P-21's own desk probe (scripts/
    # probe_cost_instruments.py) measured this fixture's full memory content
    # at 1031 tokens -- 1024 does not bind (still fits whole), 512 does.
    # allocation_policy="passages_only" drops memory entirely (the same
    # probe measured -1032 tokens for it) -- the second, unambiguously
    # binding memory/allocation axis the task asks for.
    perturbations = [
        ("memory_budget_tokens", 512),
        ("allocation_policy", "passages_only"),
    ]

    with open_pricing_context() as ctx:
        # Restrict to a small, fixed slice of the search-half ids so the
        # RUN is fast -- the objective's SENSITIVITY to an axis does not
        # need the full population to demonstrate; the full-population
        # measurement is the separate per-point wall-clock timing run.
        from src.held_out_split import build_split, search_view
        all_ids = sorted(search_view(build_split()).ids)
        sample_ids = frozenset(all_ids[:8])
        logger.info("sampling %d/%d search-half queries for this check", len(sample_ids), len(all_ids))

        t0 = time.monotonic()
        base_obj = price_config(ctx, base_version, query_ids=sample_ids)
        base_ceiling = gate_ceiling_usd(base_version)
        logger.info(
            "BASE %s: objective mean_usd=%.6f mean_prompt_tokens=%.1f | gate_ceiling_usd=%.6f",
            base_version, base_obj.mean_usd, base_obj.mean_prompt_tokens, base_ceiling,
        )

        for field, new_value in perturbations:
            old_value = getattr(base_cfg, field)
            cand_cfg = base_cfg.model_copy(update={field: new_value})
            cand_version = CA.save(cand_cfg)
            cand_obj = price_config(ctx, cand_version, query_ids=sample_ids)
            cand_ceiling = gate_ceiling_usd(cand_version)

            d_objective_tokens = cand_obj.mean_prompt_tokens - base_obj.mean_prompt_tokens
            d_objective_usd = cand_obj.mean_usd - base_obj.mean_usd
            d_ceiling_usd = cand_ceiling - base_ceiling

            print(f"\n=== {field}: {old_value} -> {new_value} ===")
            print(f"  D-1 objective (assembled-prompt estimate):")
            print(f"    mean_prompt_tokens: {base_obj.mean_prompt_tokens:.1f} -> "
                  f"{cand_obj.mean_prompt_tokens:.1f}  (delta {d_objective_tokens:+.1f})")
            print(f"    mean_usd:           {base_obj.mean_usd:.6f} -> "
                  f"{cand_obj.mean_usd:.6f}  (delta {d_objective_usd:+.6f})")
            print(f"  gate ceiling (computed_cost_usd, what the gate blocks on):")
            print(f"    usd:                {base_ceiling:.6f} -> {cand_ceiling:.6f}  "
                  f"(delta {d_ceiling_usd:+.6f})")

            # CP-7a rule 2, asserted: the objective must move on a
            # memory/allocation axis; the gate's ceiling must NOT (G-P21).
            assert abs(d_objective_usd) > 0 or abs(d_objective_tokens) > 0, (
                f"{field}: D-1's objective did NOT move -- this is exactly the "
                f"failure CP-7a rule 2 forbids"
            )
            assert d_ceiling_usd == 0.0, (
                f"{field}: the gate's ceiling moved ({d_ceiling_usd!r}) -- "
                f"G-P21's premise (computed_cost_usd never reads memory) was wrong"
            )
            print("  ASSERTED: objective moved, gate ceiling did not (G-P21 reproduced).")

        elapsed = time.monotonic() - t0
        logger.info("check complete in %.1fs (sample of %d queries x %d perturbations)",
                    elapsed, len(sample_ids), len(perturbations))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
