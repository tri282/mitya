#!/usr/bin/env python3
"""Judge test-retest variance -- mitya-v2 P0 step 9a, the instrument for R23.

[RUN -- needs approval]: calls the metered Gemini judge API. No generation, no
llama-server, no Qdrant -- it re-judges answers that are ALREADY on disk in an
`eval_quality.py` transcript, which is the whole point: the input is held fixed
so the only thing that can vary is the judge.

Why this exists. R23 measured v1's quality A/B and found persona_retention
useless as an instrument: the two arms correlate at -0.059, the scores are
bimodal (twelve 1s and thirteen 5s out of 36), and the MDE is 1.147 on a 1-5
scale. Groundedness, on the same rows, behaves properly (r = +0.625, MDE
0.551). Before authoring a single new row, step 9a asks WHERE that variance
lives, because the two candidates demand opposite fixes:

  * the JUDGE is unstable  -> fix the rubric (anchor the scale, or collapse
    persona to a binary a judge can apply repeatably). More rows change nothing.
  * GENERATION is unstable -> average k generations per row, and the unit of
    measurement becomes the cell mean rather than the single draw, which
    changes what "n" even means for this set.

This script measures the first. The second needs generation and is a separate
run (`eval_quality.py` with a repeated theme).

The temperature subtlety. `TeacherClient` judges at temperature 0.0 in
production, so re-judging identical input SHOULD be near-deterministic -- and
if it is, that is a real result but a narrow one: it says the judge does not
sample, not that the rubric is unambiguous. Two different answers of similar
quality could still be scored 1 and 5 by a deterministic judge applying a vague
rubric. So:

  --temperature 0.0  (default) -- "does the judge sample?" A cheap falsification.
                     Near-zero within-cell variance here means judge sampling
                     noise is NOT the explanation, and the search moves on.
  --temperature 1.0  -- "how wide is the judge's own distribution over this
                     rubric?" The honest measure of rubric ambiguity, since a
                     vague rubric shows up as a wide sampling distribution.

Run both. They answer different questions and the pair is what makes the
conclusion attributable.

The statistic that decides it is a variance decomposition, not an agreement
rate. For each axis:

    total variance = between-cell (signal) + within-cell (judge noise)
    noise_share    = within / total

`noise_share` near 0 means the judge is reading a real property of the answer.
Near 1 means the axis is measuring the judge. R23's persona-retention numbers
predict a high share; this is the check that confirms or refutes it.

Usage:
    python scripts/probe_judge_variance.py --dry-run
    python scripts/probe_judge_variance.py --k 3 --mode A
    python scripts/probe_judge_variance.py --k 3 --temperature 1.0 --limit 12
    python scripts/probe_judge_variance.py --self-check
"""

import argparse
import json
import logging
import os
import statistics as st
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config
from src.rag.judge import judge_answer
from src.rag.teacher import RateLimiter, TeacherClient, TeacherError

logger = logging.getLogger(__name__)

AXES = ("persona_retention", "groundedness", "fabricated_events")

_DEFAULT_TRANSCRIPT = os.path.join("plans", "mitya-rag", "05-impl", "quality_ab_results.json")


def load_cells(path: str, mode: str, limit: int | None) -> list[dict]:
    """Judged transcript rows for one mode, each carrying the exact inputs the
    judge saw. Rows without an answer (generation errors) are skipped -- there
    is nothing to re-judge."""
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)

    cells = [
        r for r in rows
        if r.get("mode") == mode and r.get("answer") and not r.get("generation_error")
    ]
    # `passages_block` is not stored in the v1 transcript -- only `sources` is.
    # Judging against a reconstructed context would change the input and defeat
    # the design, so a transcript without it can only be re-judged on the
    # answer text, and that is a different measurement. Flagged loudly.
    if cells and "passages_block" not in cells[0]:
        logger.warning(
            "transcript has no `passages_block` -- re-judging with an EMPTY context. "
            "Only persona_retention is valid from this run: it never depended on the "
            "passages. groundedness AND fabricated_events are both defined against "
            "RETRIEVED PASSAGES, so with none supplied the judge is being asked an "
            "unanswerable question and their variance measures that, not the rubric."
        )
    return cells[:limit] if limit else cells


def budget(n_cells: int, k: int, limits: dict) -> dict:
    """Planned call count against the daily cap, before anything is spent."""
    calls = n_cells * k
    return {
        "cells": n_cells,
        "k": k,
        "calls": calls,
        "rpd": limits["rpd"],
        "days": calls / limits["rpd"] if limits["rpd"] else float("inf"),
        "minutes": calls / limits["rpm"] if limits["rpm"] else float("inf"),
    }


def decompose(scores_by_cell: dict[str, list[float]]) -> dict:
    """Variance decomposition for one axis.

    within  -- mean of the per-cell variances (the judge disagreeing with
               itself on an unchanged input)
    between -- variance of the per-cell means (real differences between answers)

    `noise_share` = within / (within + between) is the headline: the fraction
    of what this axis measures that is the judge rather than the answer.
    Returns None where a statistic is undefined (k < 2, or fewer than 2 cells)
    rather than a misleading zero."""
    usable = {c: v for c, v in scores_by_cell.items() if len(v) >= 2}
    means = [st.mean(v) for v in scores_by_cell.values()]

    within = st.mean([st.variance(v) for v in usable.values()]) if usable else None
    between = st.variance(means) if len(means) >= 2 else None

    noise_share = None
    if within is not None and between is not None and (within + between) > 0:
        noise_share = within / (within + between)

    exact = sum(1 for v in scores_by_cell.values() if len(set(v)) == 1)
    return {
        "cells": len(scores_by_cell),
        "within_var": round(within, 4) if within is not None else None,
        "between_var": round(between, 4) if between is not None else None,
        "noise_share": round(noise_share, 4) if noise_share is not None else None,
        "exact_agreement": round(exact / len(scores_by_cell), 4) if scores_by_cell else None,
        "mean": round(st.mean(means), 4) if means else None,
    }


def probe(client: TeacherClient, cells: list[dict], k: int) -> dict[str, dict[str, list[float]]]:
    """Re-judge every cell k times. Returns {axis: {cell_id: [k scores]}}."""
    collected: dict[str, dict[str, list[float]]] = {axis: {} for axis in AXES}

    for i, cell in enumerate(cells, start=1):
        cell_id = cell.get("id") or cell["query"]
        for rep in range(k):
            try:
                result = judge_answer(
                    client, cell["query"], cell["answer"], cell.get("passages_block", ""),
                )
            except (TeacherError, ValueError) as exc:
                # One bad draw must not void the cell -- record and continue,
                # the decomposition tolerates ragged k.
                logger.error("cell %s rep %d failed: %s", cell_id, rep + 1, exc)
                continue
            for axis in AXES:
                collected[axis].setdefault(cell_id, []).append(result[axis])
        logger.info("[%d/%d] %s", i, len(cells), cell_id)
    return collected


def report(collected: dict, temperature: float) -> dict:
    out = {"temperature": temperature, "axes": {}}
    logger.info("=== judge test-retest, temperature=%.2f ===", temperature)
    logger.info("%-20s%9s%12s%13s%14s%10s", "axis", "cells", "within_var", "between_var", "noise_share", "exact")
    for axis in AXES:
        stats = decompose(collected[axis])
        out["axes"][axis] = stats
        logger.info(
            "%-20s%9s%12s%13s%14s%10s",
            axis, stats["cells"], stats.get("within_var"), stats.get("between_var"),
            stats.get("noise_share"), stats.get("exact_agreement"),
        )
    logger.info("noise_share near 1.0 = the axis measures the judge, not the answer (R23).")
    return out


def _self_check() -> None:
    """Smallest runnable check: the decomposition must separate a clean axis
    from a pure-noise one, and must not invent numbers where k < 2."""
    # Clean: every cell perfectly repeatable, cells differ from each other.
    clean = {"a": [5, 5, 5], "b": [1, 1, 1], "c": [3, 3, 3]}
    s = decompose(clean)
    assert s["within_var"] == 0.0, s
    assert s["noise_share"] == 0.0, s
    assert s["exact_agreement"] == 1.0, s

    # Pure noise: cells identical in expectation, judge swinging the full range.
    # Cell means all equal, so between-cell variance is exactly 0 and every
    # bit of spread is the judge.
    noisy = {"a": [1, 5, 1, 5], "b": [5, 1, 5, 1], "c": [1, 5, 1, 5]}
    s = decompose(noisy)
    assert s["between_var"] == 0.0, s
    assert s["noise_share"] == 1.0, s
    assert s["exact_agreement"] == 0.0, s

    # k=1 -- within-cell variance is undefined, and must be None, not 0.0:
    # reporting 0.0 would read as "perfectly repeatable" on evidence that
    # cannot support it.
    s = decompose({"a": [3], "b": [4]})
    assert s["within_var"] is None and s["noise_share"] is None, s

    # A single cell cannot yield a between-cell variance either.
    s = decompose({"a": [3, 3]})
    assert s["between_var"] is None, s

    b = budget(36, 3, {"rpm": 5, "rpd": 20})
    assert b["calls"] == 108 and b["days"] == 5.4, b

    logger.info("scripts/probe_judge_variance.py self-check passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Judge test-retest variance (mitya-v2 P0 step 9a).")
    ap.add_argument("--transcript", default=_DEFAULT_TRANSCRIPT)
    ap.add_argument("--mode", default="A", help="prompt composition to re-judge (transcript field)")
    ap.add_argument("--k", type=int, default=3, help="repeat judgements per cell")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--limit", type=int, default=None, help="only the first N cells")
    ap.add_argument("--out", default=os.path.join("plans", "mitya-v2", "05-impl", "judge_variance.json"))
    ap.add_argument("--dry-run", action="store_true", help="print the call budget and exit, spending nothing")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.self_check:
        _self_check()
        sys.exit(0)

    cells = load_cells(args.transcript, args.mode, args.limit)
    limits = Config.judge_limits()
    plan = budget(len(cells), args.k, limits)
    logger.info(
        "model=%s  %d cells x k=%d = %d calls  |  %d rpd -> %.1f day(s), %d rpm -> %.0f min",
        Config.JUDGE_MODEL, plan["cells"], plan["k"], plan["calls"],
        plan["rpd"], plan["days"], limits["rpm"], plan["minutes"],
    )
    if plan["calls"] > plan["rpd"]:
        logger.warning(
            "planned calls (%d) exceed the daily cap (%d) -- the run WILL stall partway. "
            "Lower --k/--limit, or switch JUDGE_MODEL to a flash-lite tier (rpd 500).",
            plan["calls"], plan["rpd"],
        )
    if args.dry_run:
        logger.info("--dry-run: nothing spent")
        sys.exit(0)

    client = TeacherClient(
        model=Config.JUDGE_MODEL,
        limiter=RateLimiter(rpm=limits["rpm"], tpm=limits["tpm"], rpd=limits["rpd"]),
        temperature=args.temperature,
    )
    collected = probe(client, cells, args.k)
    summary = report(collected, args.temperature)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "raw": collected, "plan": plan}, f, indent=2)
    logger.info("wrote %s", args.out)
