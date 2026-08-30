# scripts/eval_quality.py
"""Answer-quality eval harness + prompt-composition A/B runner (design VII.2,
ruling R19) -- Phase 7.

[RUN -- needs approval]: this script calls the live llama-server (generation,
`Config.LLAMA_SERVER_URL`) and, unless `--skip-judge` is given, the metered
Gemini judge API. Never invoked by the agent -- see
plans/mitya-rag/05-impl/phase-7.md. Requires the same artifacts as
`src/api/app.py`'s lifespan (Qdrant collection populated, entities/summaries/
structure on disk) and a running llama-server (P5's
`scripts/start_server.sh`).

For every themed query in `test/quality_eval.json` and every prompt
composition in `--modes` (default both "A" and "B", ruling R19's A/B), this:

  1. Builds real retrieval context exactly as `src/api/app.py`'s `/chat`
     handler does -- `router.route()` -> `EntityMap.resolve()` ->
     `retrieve()`/`retrieve_structural_excerpts()` -> `rerank()` ->
     `context.build_context()` -- so the eval judges the actual serving
     pipeline's output, not a hand-rolled shortcut. No hard filter (these are
     open questions, not book-scoped).
  2. Streams the answer from llama-server via `generate.build_messages(mode=)`
     + `generate.stream_answer()`, `--draws` times. The k draws share ONE
     retrieval: retrieval is deterministic since the `EntityMap._scan()` fix
     (R18/A8b), so the spread across draws isolates generator variance, which
     is where 93% of persona-retention noise lives (R25).
  3. Unless `--skip-judge`: judges the (query, answer, retrieved passages)
     triple with `src.rag.judge.judge_answer()` -- reuses `src/rag/teacher.py`'s
     metered `TeacherClient` (rate limiting + retry discipline per
     `docs/RUNBOOK.md`), never a fresh API integration.
  4. Writes every `(id, theme, query, mode, draw, answer, sources, judge_scores)` row to
     a JSON transcript (`--out`) for human review, AND a compact
     `<out>_summary.json` beside it. The summary's unit is the CELL -- one
     (row id, mode) pair, averaged over its draws -- not the individual draw
     (R25). Per-mode cell means (persona_retention, groundedness), fabrication
     rate, failed-cell counts, error counts, a per-theme breakdown, and
     the 5 lowest-scoring cells per mode. The summary is the at-a-glance A/B
     decision surface (also logged as a table); the transcript is for drilling
     into specific answers.

This script PRODUCES transcripts. It does not pick an A/B winner or judge
persona retention itself -- that is the human call the phase file reserves
(plans/mitya-rag/05-impl/phase-7.md "delete the losing A/B path" TODO).

Grounding is judged against the retrieved passage TEXT (`passages_block`,
the programmatic Sources content), never against inline citations in the
answer -- the persona is instructed to never mention passages/sources at all
(R19), so an inline-citation check would fail by design on every response.

The transcript is flushed after every query, so an interrupted run keeps its
completed rows. Combined with `--themes-only` + `--append`, a long run can be
done in theme-sized chunks that survive a Ctrl-C and accumulate onto one
transcript (re-running a cell overwrites in place, keyed by id/mode/draw).

Usage:
    python scripts/eval_quality.py
    python scripts/eval_quality.py --modes A                       # composition A only
    python scripts/eval_quality.py --themes test/quality_eval.json --limit 3
    python scripts/eval_quality.py --skip-judge                    # generate transcripts, judge by hand later
    python scripts/eval_quality.py --themes-only Ambiguity --append   # run one theme, merge onto existing transcript
    python scripts/eval_quality.py --draws 1                       # single draw, smoke test only -- NOT comparable to a k=3 run
"""

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
import time
from datetime import datetime, timezone

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient

from src.config import Config
from src.pricing import cost_usd
from src.gateway.guard import BudgetExceeded, Degraded, RequestTooLarge
from src.gateway.ladder import ContentRefused
from src.gateway.retry import TransportError
from src.limits import QuotaExhausted
from src.rag.gate import paired_bootstrap
from src.rag.metrics import assistant_tell_rate, source_leak_rate
from src.config_artifact import active_corpus_version, active_serving_config
from src.rag.context import build_context
from src.rag.entities import EntityMap
from src.rag.generate import (
    active_persona_head,
    build_messages,
    stream_answer,
)
from src.rag.judge import judge_answer
from src.rag.retrieve import build_ranker, rerank, retrieve, retrieve_structural_excerpts
from src.rag.router import route
from src.rag.teacher import RateLimiter, TeacherClient, TeacherError

logger = logging.getLogger(__name__)

_BM25_MODEL = "Qdrant/bm25"  # matches src/api/app.py, scripts/eval_retrieval.py, scripts/ingest_corpus.py (P2 debt)


def _load_json(path: str) -> dict | list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _row_key(row: dict) -> tuple[str, str, int]:
    """Identity of a transcript ROW -- one (row id, mode, draw) triple. Used to
    merge a resumed/appended run onto an existing transcript: a re-run of the
    same draw overwrites, it does not duplicate.

    Keyed on the immutable `id` (A9) rather than the query text, so a row whose
    wording is revised before it is frozen does not strand its prior transcript
    cell as an orphan. Falls back to the query for pre-step-9 transcripts,
    written before the quality set had ids, and to draw 0 for pre-step-9b
    transcripts, written before the set was drawn more than once."""
    return (row.get("id") or row["query"], row["mode"], row.get("draw", 0))


def _write_transcript(merged: dict[tuple, dict], out_path: str) -> None:
    """Flush the current transcript to disk. Called after every query (not only
    at the end) so an interrupted run keeps the work already completed."""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(list(merged.values()), f, indent=2, ensure_ascii=False)


def _mean(xs: list[float]) -> float | None:
    """Mean of a list, or None if empty (a mode/theme with no judged rows)."""
    return round(statistics.mean(xs), 3) if xs else None


def _cell_stats(rows: list[dict], min_draws: int) -> tuple[list[dict], int]:
    """Collapse per-draw rows into per-cell means for one mode.

    The CELL -- one (row id, mode) pair averaged over its draws -- is the unit of
    measurement, not the draw (R25): judge test-retest on fixed inputs gives
    within-variance 0.089 while re-generating the same cells gives 1.333, so a
    single draw measures that draw's luck, not the configuration. The cell mean's
    standard error falls as sqrt(within-variance/k) -- on the frozen 50-row set
    within-variance is 2.367, i.e. 0.888 at k = 3 (R29 supersedes R25's 1.155
    constant, which came from one theme).

    A cell needs `min_draws` judged draws to enter the means. Below that it is
    counted as failed and EXCLUDED: at R28's residual ~8% generation-failure rate
    a cell can lose draws, and averaging a one-draw survivor beside a three-draw
    cell mixes two different noise levels into one number. Failed cells are
    reported rather than silently dropped -- excluding failures without saying so
    flatters the configuration by deleting exactly the queries it handles worst
    (R28), and the failures are per-query and reproducible, not random infra
    noise.
    """
    by_cell: dict[str, list[dict]] = {}
    for r in rows:
        by_cell.setdefault(r.get("id") or r["query"], []).append(r)

    cells: list[dict] = []
    failed = 0
    for cell_id, draws in by_cell.items():
        judged = [d for d in draws if d.get("judge")]
        if len(judged) < min_draws:
            failed += 1
            continue

        personas = [d["judge"]["persona_retention"] for d in judged]
        grounds = [d["judge"]["groundedness"] for d in judged]
        # HEADLINE fabrication is the SHARE of a cell's draws that invented
        # something. The boolean is the only form of this axis the judge
        # reproduces (0.800 vs 0.500 for the exact count, R27); the count below
        # is diagnostic evidence only.
        fabricated_bools = [bool(d["judge"].get("fabricated_events")) for d in judged]
        fabricated_counts = [d["judge"].get("fabricated_events") or 0 for d in judged]

        # The weakest draw of the cell -- what a reviewer should read first when
        # the cell mean is low, and the only place a specific answer survives
        # into the summary now that the cell is the unit.
        worst = min(judged, key=lambda d: d["judge"]["persona_retention"] + d["judge"]["groundedness"])
        answer = worst["answer"]

        cells.append({
            "id": cell_id,
            "theme": draws[0]["theme"],
            "query": draws[0]["query"],
            "n_draws": len(draws),
            "n_judged": len(judged),
            "persona_retention": round(statistics.mean(personas), 3),
            "groundedness": round(statistics.mean(grounds), 3),
            "fabrication": round(statistics.mean(fabricated_bools), 3),
            "fabricated_events_UNRELIABLE": round(statistics.mean(fabricated_counts), 3),
            # Per-draw spread, kept so a reviewer can see whether a cell mean
            # rests on agreement or on averaging a 1 and a 5.
            # Tier 1b MECHANICAL rates (regex, no judge). Computed here because
            # the answers are already in hand: closing G-C costs nothing but the
            # arithmetic, and these are the only 1b metrics A7 does not touch.
            "source_leak": round(source_leak_rate([d["answer"] for d in judged]), 3),
            "assistant_tell": round(assistant_tell_rate([d["answer"] for d in judged]), 3),
            "draw_persona_retention": personas,
            "draw_groundedness": grounds,
            "worst_draw": {
                "draw": worst.get("draw", 0),
                "persona_retention": worst["judge"]["persona_retention"],
                "groundedness": worst["judge"]["groundedness"],
                "fabricated_events": worst["judge"].get("fabricated_events"),
                "rationale": worst["judge"].get("rationale"),
                "answer_excerpt": (answer[:300] + "...") if len(answer) > 300 else answer,
            },
        })
    return cells, failed

def _provenance() -> dict:
    """What is needed to reproduce this run, recorded IN the artifact.

    `config_version` is the load-bearing one: it pins every versioned serving
    knob including, from Batch C step 7, the persona prompt itself. The git SHA
    covers the unversioned remainder (retrieval code, this script). `dirty`
    matters more than it looks -- a measurement taken on an uncommitted tree is
    not reproducible from its SHA, and silently recording the SHA anyway is how
    that becomes invisible.
    """
    import subprocess

    def _git(*args: str) -> str | None:
        try:
            return subprocess.run(
                ["git", *args], capture_output=True, text=True, timeout=10, check=True
            ).stdout.strip()
        except Exception as exc:  # not a measurement failure -- record the gap
            logger.warning("provenance: git %s unavailable: %s", " ".join(args), exc)
            return None

    status = _git("status", "--porcelain")
    return {
        "config_version": active_serving_config().version(),
        "corpus_version": active_corpus_version(),
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": None if status is None else bool(status),
        "measured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "serving_model": Config.SERVING_MODEL,
        "prompt_composition": Config.PROMPT_COMPOSITION,
        "top_k_rerank": Config.TOP_K_RERANK,
    }



# The axes a paired comparison is computed for. Judge axes are OFFLINE ONLY --
# A7 bars a judge from CI, and this is not CI. The two mechanical rates carry no
# judge dependency at all and are the 1b metrics G-C asked for an MDE on.
_PAIRED_AXES = ("persona_retention", "groundedness", "fabrication",
                "source_leak", "assistant_tell")


def _paired_power(cells_a: list[dict], cells_b: list[dict], axis: str) -> dict | None:
    """Paired delta, MDE and the PAIRING DIAGNOSTIC for one axis, across modes.

    Every judge MDE this project has published (persona 1.147, groundedness
    0.551 -- docs/RAG.md VII) was computed by hand, outside any script, so no run
    reproduced it. This puts it in the artifact.

    The correlation is not decoration. R23 measured that persona retention does
    NOT pair (r ~ 0, sd_diff larger than either arm's own sd), which means query
    difficulty is not a shared property on that axis and **n is the wrong lever
    for it** -- growing the set cannot fix persona. Groundedness pairs properly
    (r = +0.852), so rows do buy power there. Printing r on every run stops that
    being rediscovered.

    Reuses `src.rag.gate.paired_bootstrap` rather than growing a second
    bootstrap: one implementation, one seed, one MDE convention (2.8 x SE).
    """
    by_id_a = {c["id"]: c for c in cells_a}
    by_id_b = {c["id"]: c for c in cells_b}
    shared = sorted(set(by_id_a) & set(by_id_b))
    if len(shared) < 3:
        return None

    a = {i: by_id_a[i][axis] for i in shared}
    b = {i: by_id_b[i][axis] for i in shared}
    result = paired_bootstrap(a, b)

    xs, ys = [a[i] for i in shared], [b[i] for i in shared]
    try:
        r = round(statistics.correlation(xs, ys), 3)
    except statistics.StatisticsError:
        r = None   # one arm is constant across cells; correlation undefined
    result["axis"] = axis
    result["pair_correlation"] = r
    result["pairs_usefully"] = None if r is None else r > 0.3
    result["judge_dependent"] = axis in ("persona_retention", "groundedness", "fabrication")
    return result


def _summarize(results: list[dict], modes: list[str], min_draws: int) -> dict:
    """Reduce the raw per-(id, mode, draw) transcript to a compact, decision-ready
    summary: per-mode CELL means (persona_retention, groundedness), fabrication
    rate, failed-cell and error counts, a per-theme mode breakdown, and the
    lowest-scoring cells per mode. This is the artifact meant for review -- the
    full transcript stays in `--out` for drilling into specific answers.

    Every published mean is a mean OVER CELLS, each cell already a mean over its
    own draws (R25). Draws are never pooled flat: a cell that kept 3 draws would
    otherwise outvote one that kept 2.

    Rows that failed generation or judging are counted separately, never folded
    into the means as zeros (which would silently punish a mode for an infra
    failure)."""
    summary: dict = {"min_draws_per_cell": min_draws, "per_mode": {}, "per_theme": {}, "worst_cells": {}}

    for mode in modes:
        rows = [r for r in results if r["mode"] == mode]
        cells, failed = _cell_stats(rows, min_draws)

        summary["per_mode"][mode] = {
            "n_draws": len(rows),
            "n_cells": len(cells) + failed,
            "n_cells_scored": len(cells),
            # Cells that could not keep `min_draws` judged draws. Read this
            # BEFORE the means: it is the generation-failure rate in cell units.
            "n_cells_failed": failed,
            "mean_persona_retention": _mean([c["persona_retention"] for c in cells]),
            "mean_groundedness": _mean([c["groundedness"] for c in cells]),
            # HEADLINE. Mean over cells of each cell's share of fabricating
            # draws. Boolean, because that is the form the judge reproduces
            # (0.800 vs 0.500 on the exact count, P0 step 9a / R27).
            "fabrication_rate": _mean([c["fabrication"] for c in cells]),
            # DIAGNOSTIC ONLY -- do not publish, do not gate, do not compare
            # across runs. The count is kept because it is the raw evidence and
            # a stronger judge may one day make it usable, but at 0.500
            # exact-agreement it cannot carry a claim. R27 also measured that
            # ASKING for it more precisely (enumerate-then-count) made it worse,
            # not better: 0.200 agreement and 3x the variance.
            "mean_fabricated_events_UNRELIABLE": _mean([c["fabricated_events_UNRELIABLE"] for c in cells]),
            "generation_errors": sum(1 for r in rows if r.get("generation_error")),
            "judge_errors": sum(1 for r in rows if r.get("judge_error")),
        }

        # Lowest combined (persona + groundedness) cells -- where a mode is
        # weakest, so a human can read those specific answers first.
        ranked = sorted(cells, key=lambda c: c["persona_retention"] + c["groundedness"])
        summary["worst_cells"][mode] = ranked[:5]

    # Paired power across the two modes, when there are two. OFFLINE ONLY: the
    # judge axes here are exactly what A7 forbids in CI, and that is the point --
    # this is the evaluation phase, where the noisiest instruments most need an
    # interval printed beside their means.
    if len(modes) == 2:
        cells_by_mode = {m: _cell_stats([r for r in results if r["mode"] == m], min_draws)[0]
                         for m in modes}
        power = [_paired_power(cells_by_mode[modes[0]], cells_by_mode[modes[1]], axis)
                 for axis in _PAIRED_AXES]
        summary["paired_power"] = {
            "modes": list(modes),
            "caveats": [
                "Observed-effect MDE: computed from the run it judges, not fixed in advance.",
                "A judge-axis MDE cannot go below the judge's own test-retest variance "
                "(R25/R27) -- the floor is the instrument, not the sample size.",
                "pairs_usefully=false means n is the WRONG LEVER for that axis (R23).",
            ],
            "axes": [p for p in power if p],
        }

    themes = sorted({r["theme"] for r in results})
    for theme in themes:
        summary["per_theme"][theme] = {}
        for mode in modes:
            rows = [r for r in results if r["theme"] == theme and r["mode"] == mode]
            cells, failed = _cell_stats(rows, min_draws)
            summary["per_theme"][theme][mode] = {
                "n_cells_scored": len(cells),
                "n_cells_failed": failed,
                "mean_persona_retention": _mean([c["persona_retention"] for c in cells]),
                "mean_groundedness": _mean([c["groundedness"] for c in cells]),
            }
    return summary


def _print_summary(summary: dict, modes: list[str]) -> None:
    """Log a compact per-mode A/B table -- the at-a-glance decision surface.
    Persona retention is weighted in the R19 call (see phase-7.md); this only
    prints the numbers, it does not declare a winner."""
    logger.info(
        "=== A/B summary: means over CELLS (>= %d judged draws each); persona/groundedness 1-5, "
        "higher better, fabrication lower better ===",
        summary["min_draws_per_cell"],
    )
    # fab_rate only. The mean count is deliberately absent from this table:
    # it is the decision surface, and an unreliable number on it gets quoted
    # (R27). It stays in the JSON summary under an _UNRELIABLE key.
    header = (f"{'mode':<6}{'cells':>8}{'failed':>8}{'persona':>10}{'ground':>10}"
              f"{'fab_rate':>10}{'gen_err':>9}{'judge_err':>11}")
    logger.info(header)
    for mode in modes:
        m = summary["per_mode"][mode]
        logger.info(
            "%-6s%8s%8s%10s%10s%10s%9s%11s",
            mode, m["n_cells_scored"], m["n_cells_failed"],
            m["mean_persona_retention"], m["mean_groundedness"],
            m["fabrication_rate"], m["generation_errors"], m["judge_errors"],
        )


async def _collect_answer(messages: list[dict]) -> tuple[str, str | None, dict]:
    """Stream one answer to completion. Returns (answer_text, error) --
    error is None on success, else a short description (llama-server
    unreachable/timed out) so the caller can record a failed row instead of
    crashing the whole run over one query."""
    parts: list[str] = []
    # 11a records the cost/latency half, not just the judge half (schedule:
    # "record per query tokens, USD, model, cache_hit, TTFT, total"). Without
    # these the run measures quality alone and the phase's headline -- cost per
    # answer per arm -- has no data behind it.
    usage: dict = {}
    t_start = time.monotonic()
    t_first: float | None = None
    # Two clocks, deliberately. `t_attempt` is reset by the caller-visible
    # first token only, so `ttft_s` measures the SUCCESSFUL attempt -- provider
    # latency. `t_start` never moves, so `total_s` measures what the caller
    # actually waited, retries and rate-limit backoff included.
    #
    # Conflating them produced a 316-second "TTFT" on Groq, which was five
    # minutes of honouring `retry-after` and roughly 1.5s of provider. Both
    # numbers are legitimate; reporting one as the other is not.
    # Filled by stream_answer() -- only the generator knows when its final
    # attempt began, because the retry loop lives inside it.

    def _telemetry(error: str | None) -> dict:
        total = time.monotonic() - t_start
        cost = cost_usd(usage.get("model") or Config.SERVING_MODEL,
                        usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return {
            "model": usage.get("model") or Config.SERVING_MODEL,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "cost_usd": cost.usd,
            "cost_metered": cost.metered,
            # Provider latency: first token measured from the attempt that
            # actually produced it.
            "ttft_s": None if t_first is None else round(t_first - t_start - usage.get("retry_wait_s", 0.0), 4),
            # Wall-clock the caller waited, including every retry and backoff.
            "total_s": round(total, 4),
            "retry_wait_s": usage.get("retry_wait_s"),
            # Split by class (step 8(b)): 11b reports the retry cost multiplier
            # per class, never blended -- a transport retry and a duplicated
            # generation have different causes and different remedies.
            "transport_retries": usage.get("transport_retries"),
            "quality_retries": usage.get("quality_retries"),
            "attempts": usage.get("attempts"),
            # R33: ttft is None when no token ever arrived. Recording the
            # give-up time as a TTFT would report the least trustworthy
            # requests as the fastest.
            "zero_token_completion": t_first is None,
            "error": error,
        }

    try:
        async for token in stream_answer(messages, usage):
            if t_first is None:
                t_first = time.monotonic()
            parts.append(token)
    # Gateway vocabulary (v2 P2 Batch C step 6). This used to catch
    # LlamaServerUnavailable/LlamaServerTimeout, which the gateway path no
    # longer raises -- so this function's whole reason for existing ("record a
    # failed row instead of crashing the whole run over one query") silently
    # stopped working, and 11a's first attempt died at cell 19 of 150 on one
    # transient Google overload. Per-row isolation only isolates the
    # exceptions it names.
    except TransportError as exc:
        return "", f"provider transport error: {exc}", _telemetry(f"transport: {exc}")
    except ContentRefused as exc:
        return "", f"provider refused to generate: {exc}", _telemetry(f"refused: {exc}")
    except QuotaExhausted as exc:
        # Not isolatable the way the others are: quota does not recover within
        # a run, so every subsequent row would fail identically. Re-raised so
        # the run stops with completed work already flushed to disk, rather
        # than burning through 130 rows writing the same error into each.
        logger.error("quota exhausted -- stopping the run; completed rows are on disk")
        raise
    except (BudgetExceeded, Degraded, RequestTooLarge) as exc:
        return "", f"gateway refused the request: {type(exc).__name__}: {exc}", _telemetry(type(exc).__name__)

    # An empty stream is a generation FAILURE, not an answer worth zero. Before
    # this check (P0 step 9a) it returned ("", None), so the row looked healthy,
    # went to the judge, and came back scored 1/1 with the rationale "the
    # response is empty" -- and those 1s entered v1's published means as if
    # they measured quality. 7 of 36 mode-A rows and 3 of 36 mode-B rows were
    # this. Excluding them flips the persona delta's sign.
    answer = "".join(parts)
    if not answer.strip():
        return "", "empty response (no tokens streamed)", _telemetry("empty")
    return answer, None, _telemetry(None)


def _truncation_report(results: list[dict]) -> dict:
    """How many answers ran into the completion ceiling.

    Exists because 11a's first two arms were judged on answers that stopped
    mid-sentence and nobody noticed until the telemetry was read afterwards.
    `Config.MAX_TOKENS` is an API parameter, NOT an instruction -- the model is
    never told about it, so it writes as though unconstrained and is cut off.
    Meanwhile the persona prompt asks for a "deep literary, existential, and
    philosophical tone", i.e. the opposite of brevity.

    A quality score computed over truncated answers measures the token budget
    as much as the model, so this is surfaced in the summary and logged at
    WARNING rather than left for someone to infer from a token histogram.
    """
    ceiling = Config.MAX_TOKENS
    at_ceiling = sum(
        1 for r in results
        if (r.get("telemetry") or {}).get("completion_tokens") == ceiling
    )
    # Cheap textual proxy for rows with no telemetry: an answer that does not
    # end on terminal punctuation was almost certainly cut off.
    terminal = '.!?"\u201d)'
    unterminated = sum(
        1 for r in results
        if (r.get("answer") or "").rstrip() and (r.get("answer") or "").rstrip()[-1] not in terminal
    )
    n = len(results) or 1
    if at_ceiling or unterminated > n * 0.2:
        logger.warning(
            "TRUNCATION: %d/%d draws hit the %d-token completion ceiling and %d/%d "
            "do not end on terminal punctuation. Config.MAX_TOKENS is an API cutoff, "
            "not a prompt instruction -- quality scores over truncated answers "
            "measure the token budget as much as the model.",
            at_ceiling, len(results), ceiling, unterminated, len(results),
        )
    return {
        "max_tokens": ceiling,
        "draws_at_completion_ceiling": at_ceiling,
        "draws_not_ending_on_terminal_punctuation": unterminated,
        "n_draws": len(results),
    }


async def run(
    themes_path: str,
    out_path: str,
    modes: list[str],
    limit: int | None,
    skip_judge: bool,
    themes_only: list[str] | None,
    append: bool,
    draws: int,
    no_rag: bool = False,
) -> None:
    # A cell needs two judged draws to mean anything; at k=1 there is nothing to
    # average and the single draw is the cell. Not a flag -- there is no reason
    # to want a different rule, and a knob here would let a run quietly redefine
    # what "failed cell" means between the baseline and a candidate.
    min_draws = min(2, draws)
    logger.info("loading dense embedder: %s", Config.EMBED_MODEL)
    embedder = TextEmbedding(model_name=Config.EMBED_MODEL)
    logger.info("loading sparse embedder: %s", _BM25_MODEL)
    bm25 = SparseTextEmbedding(model_name=_BM25_MODEL)
    logger.info("loading FlashRank ranker: %s", Config.RERANK_MODEL)
    ranker = build_ranker()
    entity_map = EntityMap()
    structure = _load_json(Config.STRUCTURE_PATH)
    summaries = _load_json(Config.SUMMARIES_PATH)
    qdrant = QdrantClient(path=Config.VECTOR_DB_DIR)

    judge_client: TeacherClient | None = None
    if not skip_judge:
        # The judge gets a limiter built from JUDGE_MODEL's own quota row --
        # not the teacher's. The two models can differ by 25x on rpd.
        jl = Config.judge_limits()
        logger.info("loading judge client: %s (%d rpm / %d rpd)", Config.JUDGE_MODEL, jl["rpm"], jl["rpd"])
        judge_client = TeacherClient(
            model=Config.JUDGE_MODEL,
            limiter=RateLimiter(rpm=jl["rpm"], tpm=jl["tpm"], rpd=jl["rpd"]),
        )

    queries: list[dict] = _load_json(themes_path)
    if themes_only:
        wanted = set(themes_only)
        queries = [q for q in queries if q["theme"] in wanted]
    if limit is not None:
        queries = queries[:limit]
    logger.info(
        "loaded %d themed queries from %s -- %d draws x %d modes = %d generations",
        len(queries), themes_path, draws, len(modes), len(queries) * draws * len(modes),
    )

    # Merge onto an existing transcript when --append: a re-run of a cell
    # overwrites in place (keyed by id/mode/draw), so a chunked, resumed
    # run accumulates instead of clobbering earlier themes.
    merged: dict[tuple, dict] = {}
    if append and os.path.exists(out_path):
        for prior in _load_json(out_path):
            merged[_row_key(prior)] = prior
        logger.info("append: loaded %d existing rows from %s", len(merged), out_path)

    for row in queries:
        query = row["query"]
        theme = row["theme"]

        route_name, scope = route(query, None, structure, summaries)
        expanded_query, resolved_entities = entity_map.resolve(query, active_book=None)

        if route_name == "STRUCTURAL" and scope is not None:
            points = retrieve_structural_excerpts(
                qdrant, embedder, expanded_query, scope["book"], scope["part"], scope["chapter"],
            )
        else:
            points = retrieve(qdrant, embedder, bm25, expanded_query, hard_filter=None)
            points = rerank(query, points, ranker)

        passages_block, sources = build_context(route_name, points, summaries, scope)

        # P3 step 7 -- the no-RAG positive control (charter 9(b), F4). Retrieval
        # above still RUNS, so route, entities and sources are recorded exactly
        # as the grounded arm records them; only the passages the model actually
        # sees are removed. That is the point: the two arms differ in one thing,
        # the grounding, and everything upstream is held identical.
        #
        # The failure mode this guards is specific and silent: if the flag were
        # not threaded past this seam the arms would be identical, the run would
        # report "no difference", and that reads exactly like outcome C (a broken
        # gate). Hence the assertion rather than a log line.
        if no_rag:
            passages_block = ""
            assert not passages_block, "no-RAG arm still carries passage text"
        logger.info(
            "query=%r theme=%s route=%s entities=%s sources=%s",
            query, theme, route_name, resolved_entities, sources,
        )

        # The draws share the retrieval above. Retrieval is deterministic since
        # the `EntityMap._scan()` ordering fix (R18/A8b) -- verified cross-process
        # -- so re-running it per draw would cost time and change nothing, and
        # holding it fixed is what makes the spread across draws a measurement of
        # the GENERATOR, which carries 93% of persona-retention variance (R25).
        for mode in modes:
            for draw in range(draws):
                messages = build_messages(active_persona_head(), passages_block, query, mode=mode)
                answer, error, telemetry = await _collect_answer(messages)
                if error is not None:
                    logger.warning("draw failed query=%r mode=%s draw=%d: %s", query, mode, draw, error)

                judge_result: dict | None = None
                judge_error: str | None = None
                if judge_client is not None and error is None:
                    try:
                        judge_result = judge_answer(judge_client, query, answer, passages_block)
                    except (TeacherError, ValueError) as exc:
                        judge_error = str(exc)
                        logger.error("judge failed query=%r mode=%s draw=%d: %s", query, mode, draw, exc)

                merged[(row.get("id") or query, mode, draw)] = {
                    "id": row.get("id"),
                    "theme": theme,
                    "query": query,
                    "mode": mode,
                    "draw": draw,
                    "route": route_name,
                    "resolved_entities": resolved_entities,
                    "sources": sources,
                    # The context the judge actually saw. Stored so a transcript can
                    # be RE-judged later without regenerating -- v1 kept only
                    # `sources` (chapter refs), which made the step-9a probe judge
                    # against an empty context and cost it two of its three axes
                    # (R25). Groundedness and fabricated_events are both defined
                    # against these passages; without them they cannot be measured.
                    "passages_block": passages_block,
                    # Stamped because the two arms are otherwise INDISTINGUISHABLE
                    # in a transcript: retrieval runs in both, so route, entities
                    # and sources are populated identically, and only the filename
                    # would say which arm a row came from. A checkpoint whose whole
                    # output is a comparison cannot rest on a filename.
                    "no_rag": no_rag,
                    "answer": answer,
                    "generation_error": error,
                    "telemetry": telemetry,
                    "judge": judge_result,
                    "judge_error": judge_error,
                }

        # Flush after every query so an interrupt keeps completed work.
        _write_transcript(merged, out_path)

    results = list(merged.values())
    logger.info("wrote %d transcript rows to %s", len(results), out_path)

    # Compact, decision-ready summary alongside the full transcript -- this is
    # the artifact for review; the transcript is for drilling into answers.
    # Summarize across every mode present in the merged transcript, not just the
    # --modes of this chunk, so a resumed run still reports both A and B.
    summary_modes = sorted({r["mode"] for r in results}) or modes
    summary = _summarize(results, summary_modes, min_draws)
    # Provenance block (v2 P2 Batch C, R54). P-3 found that the frozen
    # 2026-08-24 row's own artifacts recorded NO config_version, git SHA or
    # timestamp -- so it is reproducible from git history and from nothing the
    # artifact itself carries, and A3 forbids re-measuring it, which makes that
    # unrepairable after the fact. Every run from here stamps its own
    # provenance so 11a does not inherit the same defect.
    summary["provenance"] = _provenance()
    summary_path = out_path[:-len(".json")] + "_summary.json" if out_path.endswith(".json") else out_path + "_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    logger.info("wrote A/B summary to %s", summary_path)
    if not skip_judge:
        _print_summary(summary, summary_modes)
    else:
        logger.info("--skip-judge: summary has error counts only (no judge scores)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Answer-quality eval + prompt-composition A/B runner (design VII.2).")
    ap.add_argument("--themes", default=os.path.join("test", "quality_eval.json"))
    ap.add_argument("--out", default=os.path.join("plans", "mitya-rag", "05-impl", "quality_ab_results.json"))
    ap.add_argument("--modes", nargs="+", default=["A", "B"], choices=["A", "B"])
    ap.add_argument("--limit", type=int, default=None, help="only run the first N themed queries (smoke test)")
    ap.add_argument("--themes-only", nargs="+", default=None,
                    help="only run queries whose theme is in this list (e.g. Ambiguity), for chunked/resumed runs")
    ap.add_argument("--append", action="store_true",
                    help="merge onto the existing --out transcript (keyed by id/mode/draw) instead of overwriting")
    ap.add_argument("--draws", type=int, default=3,
                    help="generations per (row, mode) cell -- k. Default 3, the frozen value: k=3 "
                         "cuts per-cell noise 42%%, k=5 reaches only 55%% for 67%% more cost (R25). "
                         "Measured within-cell variance on the frozen 50-row set is 2.367, so the "
                         "cell mean's SE at k=3 is 0.888 -- not the 0.667 R25's single-theme 1.333 "
                         "predicted (R29). A baseline and a "
                         "candidate must use the SAME k or their means are not comparable. Use 1 for "
                         "smoke tests only.")
    ap.add_argument("--skip-judge", action="store_true", help="write transcripts without calling the judge API")
    ap.add_argument("--no-rag", action="store_true",
                    help="P3 step 7 positive control: run retrieval, then send the model NO "
                         "passages. Everything upstream (route, entities, sources) is recorded "
                         "identically, so the two arms differ only in grounding.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.draws < 1:
        ap.error("--draws must be >= 1")
    if args.no_rag:
        logger.warning("--no-rag: POSITIVE CONTROL arm. The model receives no passages. "
                       "This arm is expected to be ungrounded; that is what it measures.")
    asyncio.run(run(args.themes, args.out, args.modes, args.limit, args.skip_judge,
                    args.themes_only, args.append, args.draws, args.no_rag))
