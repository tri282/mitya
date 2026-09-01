#!/usr/bin/env python3
"""P-24 / P-25 / P-26 -- what P7's three faucets would cost, before any open.

[DESK -- $0.00, no provider call]. Arithmetic over `Config.limits_for()` and
`Config.MODEL_RATES`, read from the ACTIVE artifact rather than from a
schedule's copy of them. Nothing here calls a provider, and nothing here
approves a run.

**Every budget consults all four limits (rpm, tpm, rpd, tpd) and takes the
minimum.** `CLAUDE.md`'s batch rule; R68 is why. `tpd` is declared in the quota
table and read by no line of `src/` (R121), so where it binds it binds by
discipline alone -- that is reported per row rather than assumed away.
"""

import argparse
import logging
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config
from src.config_artifact import active_serving_config
from src.rag.gate import _MDE_Z

logger = logging.getLogger("probe_p7_budgets")

# R79's measured decomposition (`p3-findings.md:541`), and today's re-measure.
R79_FULL_ARM_S = 14.1
R79_RERANK_ARM_S = 104.7
MODEL_LOAD_S = 1.25

# R132's measured P6-shaped session, from which one draw's token size is
# derived. `[inferred]` -- R132 published aggregates, not a per-draw count.
R132_PASSAGE_TOKENS = 162_267
R132_MEMORY_TOKENS = 127_609
R132_TURNS = 108
R132_SERVING_USD = 0.213968


def ceilings(model: str, tokens_per_draw: int) -> dict:
    """Draws/minute and draws/day under each of the four limits, min taken."""
    lim = Config.limits_for(model)
    tpm = lim.get("tpm")
    tpd = lim.get("tpd")
    per_min = {
        "rpm": lim["rpm"],
        "tpm": (tpm / tokens_per_draw) if tpm and tpm < Config.NO_TPM_LIMIT else math.inf,
    }
    per_day = {
        "rpd": lim["rpd"],
        "tpd": (tpd / tokens_per_draw) if tpd else math.inf,
    }
    bind_min = min(per_min, key=lambda k: per_min[k])
    bind_day = min(per_day, key=lambda k: per_day[k])
    return {
        "model": model, "limits": lim,
        "per_min": per_min, "binds_pace": bind_min, "pace": per_min[bind_min],
        "per_day": per_day, "binds_volume": bind_day, "volume": per_day[bind_day],
        "tpd_declared": tpd is not None,
    }


def known_answer_check() -> None:
    """Reproduce R121's published binding-limit block before any new number.

    R121 measured, on `models/gemini-3.5-flash-lite` at 2766 tokens/draw:
    rpm binds pace at 15 (tpm would allow 250000/2766 = 90.4); rpd binds volume
    at 500; tpd does not exist on the row. And on Groq `openai/gpt-oss-120b`:
    tpm binds pace at 8000/2766 = 2.89 and tpd binds volume at 200000/2766 = 72.
    If this table has moved, every figure below is against a different quota.
    """
    g = ceilings("models/gemini-3.5-flash-lite", 2766)
    assert g["binds_pace"] == "rpm" and g["pace"] == 15, g
    assert abs(g["per_min"]["tpm"] - 90.4) < 0.1, g
    assert g["binds_volume"] == "rpd" and g["volume"] == 500, g
    assert not g["tpd_declared"], g

    q = ceilings("openai/gpt-oss-120b", 2766)
    assert q["binds_pace"] == "tpm" and abs(q["pace"] - 2.89) < 0.01, q
    assert q["binds_volume"] == "tpd" and abs(q["volume"] - 72.3) < 0.1, q

    assert _MDE_Z == 2.8
    logger.info("known-answer check PASSED (R121's two binding-limit blocks reproduced)")


def n_required(p_disc: float, margin: float) -> float:
    """The gate's own MDE form solved for n: MDE = 2.8*sqrt(p_disc/n) <= margin."""
    return _MDE_Z ** 2 * p_disc / margin ** 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    known_answer_check()

    cfg = active_serving_config()
    serving, judge = cfg.serving_model, Config.JUDGE_MODEL
    rate = cfg.model_rates[serving]

    # --- the P6-shaped draw, derived from R132 -----------------------------
    ctx_per_turn = (R132_PASSAGE_TOKENS + R132_MEMORY_TOKENS) / R132_TURNS
    persona_t = len(cfg.persona_head) // 4
    prompt_t = round(ctx_per_turn + persona_t + 15)          # +15 = a question
    realised_usd = R132_SERVING_USD / R132_TURNS
    completion_t = round((realised_usd - prompt_t * rate["prompt"] / 1e6)
                         / (rate["completion"] / 1e6))
    reserved_t = prompt_t + cfg.max_tokens                   # what the limiter reserves
    ceiling_usd = (prompt_t * rate["prompt"] + cfg.max_tokens * rate["completion"]) / 1e6

    print("=" * 78)
    print("THE DRAW (P6-shaped, derived from R132's measured aggregates)")
    print("=" * 78)
    print(f"  prompt tokens        {prompt_t:>8}  [inferred] context {ctx_per_turn:.0f} "
          f"+ persona {persona_t} + question 15")
    print(f"  completion, realised {completion_t:>8}  [inferred] from R132's serving USD")
    print(f"  completion, ceiling  {cfg.max_tokens:>8}  MAX_TOKENS -- what the limiter reserves")
    print(f"  limiter reservation  {reserved_t:>8}  prompt + max_output (src/rag/teacher.py:209)")
    print(f"  realised USD/draw    {realised_usd:>8.6f}   ceiling USD/draw {ceiling_usd:.6f}")
    print(f"  R96's P4 floor       {2766:>8}  tokens -- VOID for a P6 prompt (R121)")
    print()

    print("=" * 78)
    print("CURRENCY ONE -- WALL CLOCK. $0.00, no provider, no quota.")
    print("=" * 78)
    non_promoting = 2 * R79_FULL_ARM_S + MODEL_LOAD_S
    promoting = non_promoting + 2 * R79_RERANK_ARM_S
    print(f"  non-promoting gate run  {non_promoting:6.1f} s  (2 x full arm + model load)")
    print(f"  promoting gate run      {promoting:6.1f} s  (+ 2 x rerank arm, R79 241.6 s measured)")
    print(f"  {'points':>7} {'non-promoting':>16} {'all promoting':>16}")
    for pts in (50, 100, 300):
        print(f"  {pts:>7} {pts * non_promoting / 3600:>13.2f} h {pts * promoting / 3600:>13.2f} h")
    print("  DISCREPANCY, flagged not resolved: R79's headline 241.6 s is a PROMOTING run;")
    print("  P3's promote-only rerank decision (scripts/gate.py:464-466) makes the routine")
    print("  run ~30 s. 8x. Batch A measures the real one; both are inputs here.")
    print()

    print("=" * 78)
    print("FAUCET 1 -- a realised-cost objective (one generation pass per point)")
    print("=" * 78)
    c = ceilings(serving, reserved_t)
    print(f"  {serving}: {c['limits']}")
    print(f"    pace   {c['pace']:.1f} draws/min  ({c['binds_pace']} BINDS; "
          f"tpm allows {c['per_min']['tpm']:.1f})")
    print(f"    volume {c['volume']:.0f} draws/day  ({c['binds_volume']} BINDS; "
          f"tpd {'declared' if c['tpd_declared'] else 'ABSENT on this row'})")
    print(f"  {'rows/point':>11} {'draws/point':>12} {'points/day':>11} {'USD/point (realised)':>21}")
    for rows in (1, 4, 10, 38, 50):
        print(f"  {rows:>11} {rows:>12} {c['volume'] / rows:>11.1f} "
              f"{rows * realised_usd:>21.4f}")
    print("  The rows/point column is CP-7a's to choose; every row is priced, none is picked.")
    print()

    print("=" * 78)
    print("FAUCET 2 -- the gen_temperature calibration (P-26)")
    print("=" * 78)
    cells, k, arms = 50, 3, 2
    gen = cells * k * arms
    jud = gen
    cg = ceilings(serving, reserved_t)
    cj = ceilings(judge, 2000 + Config.TEACHER_MAX_OUTPUT_TOKENS)
    print(f"  generation: {gen} draws on {serving}")
    print(f"    {gen / cg['volume']:.0%} of one day's {cg['binds_volume']}={cg['volume']:.0f}; "
          f"floor {gen / cg['pace']:.0f} min at {c['binds_pace']}={cg['pace']:.0f}/min")
    print(f"  judging:    {jud} draws on {judge}")
    print(f"    {jud / cj['volume']:.0%} of one day's {cj['binds_volume']}={cj['volume']:.0f}; "
          f"floor {jud / cj['pace']:.0f} min at {cj['binds_pace']}={cj['pace']:.0f}/min")
    same = judge == serving
    print(f"  quota scopes: quota_scope() is (provider, model) -> "
          f"{'SAME window' if same else 'SEPARATE windows'}")
    print(f"    If Google in fact pools per-account, the two collapse into one 500-rpd")
    print(f"    window and {gen + jud} > 500: the calibration does NOT fit in one day.")
    print(f"    R121: per-model separation is [measured] for Groq, NEVER for Google.")
    print(f"  USD shadow [estimate]: generation {gen * 0.001942:.4f} "
          f"(R79's 1b proxy $0.001942/answer, $0.2913 per 50-row k=3 set) "
          f"+ judge of the same order")
    print()

    print("=" * 78)
    print("FAUCET 3 -- the fence guard, THREE arms (P-25, schedule 8b(a))")
    print("=" * 78)
    print(f"  {'p_disc':>7} {'margin':>7} {'n valid':>8} {'K launched':>11} "
          f"{'draws/cand':>11} {'cands/day':>10}")
    control_land = 7 / 8   # R133: extractor 4/4 + fence 3/4. SINGLE DIGIT -- R119.
    for p_disc in (0.10, 0.20, 0.30):
        for margin in (0.10, 0.15, 0.20):
            n = n_required(p_disc, margin)
            K = math.ceil(n / control_land)
            # 3 arms x K attempts x 2 draws (attempt + its unfenced control),
            # x2 again because F7 re-runs production live beside the candidate.
            draws = 3 * K * 2 * 2
            print(f"  {p_disc:>7.2f} {margin:>7.2f} {n:>8.1f} {K:>11} {draws:>11} "
                  f"{500 / draws:>10.3f}")
    print("  control_land = 7/8 is R133's own count and is SINGLE-DIGIT: under R119 it is")
    print("  not a number. It is carried as a divisor because omitting it would understate K.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
