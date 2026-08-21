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
     + `generate.stream_answer()`.
  3. Unless `--skip-judge`: judges the (query, answer, retrieved passages)
     triple with `src.rag.judge.judge_answer()` -- reuses `src/rag/teacher.py`'s
     metered `TeacherClient` (rate limiting + retry discipline per
     `docs/RUNBOOK.md`), never a fresh API integration.
  4. Writes every `(id, theme, query, mode, answer, sources, judge_scores)` row to
     a JSON transcript (`--out`) for human review, AND a compact
     `<out>_summary.json` beside it -- per-mode means (persona_retention,
     groundedness), fabrication rate AND mean fabricated-event count, error
     counts, a per-theme breakdown, and
     the 5 lowest-scoring rows per mode. The summary is the at-a-glance A/B
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
transcript (re-running a cell overwrites in place, keyed by theme/query/mode).

Usage:
    python scripts/eval_quality.py
    python scripts/eval_quality.py --modes A                       # composition A only
    python scripts/eval_quality.py --themes test/quality_eval.json --limit 3
    python scripts/eval_quality.py --skip-judge                    # generate transcripts, judge by hand later
    python scripts/eval_quality.py --themes-only Ambiguity --append   # run one theme, merge onto existing transcript
"""

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient

from src.config import Config
from src.rag.context import build_context
from src.rag.entities import EntityMap
from src.rag.generate import (
    PERSONA_HEAD,
    LlamaServerTimeout,
    LlamaServerUnavailable,
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


def _row_key(row: dict) -> tuple[str, str]:
    """Identity of a transcript cell -- one (row id, mode) pair. Used to merge a
    resumed/appended run onto an existing transcript: a re-run of the same cell
    overwrites, it does not duplicate.

    Keyed on the immutable `id` (A9) rather than the query text, so a row whose
    wording is revised before it is frozen does not strand its prior transcript
    cell as an orphan. Falls back to the query for pre-step-9 transcripts,
    written before the quality set had ids."""
    return (row.get("id") or row["query"], row["mode"])


def _write_transcript(merged: dict[tuple, dict], out_path: str) -> None:
    """Flush the current transcript to disk. Called after every query (not only
    at the end) so an interrupted run keeps the work already completed."""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(list(merged.values()), f, indent=2, ensure_ascii=False)


def _mean(xs: list[float]) -> float | None:
    """Mean of a list, or None if empty (a mode/theme with no judged rows)."""
    return round(statistics.mean(xs), 3) if xs else None


def _summarize(results: list[dict], modes: list[str]) -> dict:
    """Reduce the raw per-(query,mode) transcript to a compact, decision-ready
    A/B summary: per-mode means (persona_retention, groundedness),
    fabrication rate, error counts, a per-theme mode breakdown, and the
    lowest-scoring rows per mode. This is the artifact meant for review --
    the full transcript stays in `--out` for drilling into specific answers.

    Judged rows are those with a non-null `judge` block; rows that failed
    generation or judging are counted separately, never folded into the means
    as zeros (which would silently punish a mode for an infra failure)."""
    summary: dict = {"per_mode": {}, "per_theme": {}, "worst_rows": {}}

    for mode in modes:
        rows = [r for r in results if r["mode"] == mode]
        judged = [r for r in rows if r.get("judge")]
        personas = [r["judge"]["persona_retention"] for r in judged]
        grounds = [r["judge"]["groundedness"] for r in judged]
        fabricated_counts = [r["judge"].get("fabricated_events") or 0 for r in judged]
        fabricated = [c for c in fabricated_counts if c]

        summary["per_mode"][mode] = {
            "n": len(rows),
            "n_judged": len(judged),
            "mean_persona_retention": _mean(personas),
            "mean_groundedness": _mean(grounds),
            "fabrication_rate": round(len(fabricated) / len(judged), 3) if judged else None,
            # Mean over ALL judged rows including the zeros -- the rate says how
            # often the model invents, this says how much (P0 step 9).
            "mean_fabricated_events": _mean(fabricated_counts),
            "generation_errors": sum(1 for r in rows if r.get("generation_error")),
            "judge_errors": sum(1 for r in rows if r.get("judge_error")),
        }

        # Lowest combined (persona + groundedness) judged rows -- where a mode
        # is weakest, so a human can read those specific answers first.
        ranked = sorted(
            judged,
            key=lambda r: r["judge"]["persona_retention"] + r["judge"]["groundedness"],
        )
        summary["worst_rows"][mode] = [
            {
                "id": r.get("id"),
                "theme": r["theme"],
                "query": r["query"],
                "persona_retention": r["judge"]["persona_retention"],
                "groundedness": r["judge"]["groundedness"],
                "fabricated_events": r["judge"].get("fabricated_events"),
                "rationale": r["judge"].get("rationale"),
                "answer_excerpt": (r["answer"][:300] + "...") if len(r["answer"]) > 300 else r["answer"],
            }
            for r in ranked[:5]
        ]

    themes = sorted({r["theme"] for r in results})
    for theme in themes:
        summary["per_theme"][theme] = {}
        for mode in modes:
            judged = [r for r in results if r["theme"] == theme and r["mode"] == mode and r.get("judge")]
            summary["per_theme"][theme][mode] = {
                "n_judged": len(judged),
                "mean_persona_retention": _mean([r["judge"]["persona_retention"] for r in judged]),
                "mean_groundedness": _mean([r["judge"]["groundedness"] for r in judged]),
            }
    return summary


def _print_summary(summary: dict, modes: list[str]) -> None:
    """Log a compact per-mode A/B table -- the at-a-glance decision surface.
    Persona retention is weighted in the R19 call (see phase-7.md); this only
    prints the numbers, it does not declare a winner."""
    logger.info("=== A/B summary (persona/groundedness are 1-5 means; higher better, fabrication lower better) ===")
    header = f"{'mode':<6}{'n_judged':>10}{'persona':>10}{'ground':>10}{'fab_rate':>10}{'fab_mean':>10}{'gen_err':>9}{'judge_err':>11}"
    logger.info(header)
    for mode in modes:
        m = summary["per_mode"][mode]
        logger.info(
            "%-6s%10s%10s%10s%10s%10s%9s%11s",
            mode, m["n_judged"], m["mean_persona_retention"], m["mean_groundedness"],
            m["fabrication_rate"], m["mean_fabricated_events"], m["generation_errors"], m["judge_errors"],
        )


async def _collect_answer(messages: list[dict]) -> tuple[str, str | None]:
    """Stream one answer to completion. Returns (answer_text, error) --
    error is None on success, else a short description (llama-server
    unreachable/timed out) so the caller can record a failed row instead of
    crashing the whole run over one query."""
    parts: list[str] = []
    try:
        async for token in stream_answer(messages):
            parts.append(token)
    except LlamaServerUnavailable as exc:
        return "", f"llama-server unreachable: {exc}"
    except LlamaServerTimeout as exc:
        return "", f"llama-server timed out: {exc}"

    # An empty stream is a generation FAILURE, not an answer worth zero. Before
    # this check (P0 step 9a) it returned ("", None), so the row looked healthy,
    # went to the judge, and came back scored 1/1 with the rationale "the
    # response is empty" -- and those 1s entered v1's published means as if
    # they measured quality. 7 of 36 mode-A rows and 3 of 36 mode-B rows were
    # this. Excluding them flips the persona delta's sign.
    answer = "".join(parts)
    if not answer.strip():
        return "", "llama-server returned an empty response (no tokens streamed)"
    return answer, None


async def run(
    themes_path: str,
    out_path: str,
    modes: list[str],
    limit: int | None,
    skip_judge: bool,
    themes_only: list[str] | None,
    append: bool,
) -> None:
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
    logger.info("loaded %d themed queries from %s", len(queries), themes_path)

    # Merge onto an existing transcript when --append: a re-run of a cell
    # overwrites in place (keyed by theme/query/mode), so a chunked, resumed
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
        logger.info(
            "query=%r theme=%s route=%s entities=%s sources=%s",
            query, theme, route_name, resolved_entities, sources,
        )

        for mode in modes:
            messages = build_messages(PERSONA_HEAD, passages_block, query, mode=mode)
            answer, error = await _collect_answer(messages)

            judge_result: dict | None = None
            judge_error: str | None = None
            if judge_client is not None and error is None:
                try:
                    judge_result = judge_answer(judge_client, query, answer, passages_block)
                except (TeacherError, ValueError) as exc:
                    judge_error = str(exc)
                    logger.error("judge failed query=%r mode=%s: %s", query, mode, exc)

            merged[(row.get("id") or query, mode)] = {
                "id": row.get("id"),
                "theme": theme,
                "query": query,
                "mode": mode,
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
                "answer": answer,
                "generation_error": error,
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
    summary = _summarize(results, summary_modes)
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
                    help="merge onto the existing --out transcript (keyed by theme/query/mode) instead of overwriting")
    ap.add_argument("--skip-judge", action="store_true", help="write transcripts without calling the judge API")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run(args.themes, args.out, args.modes, args.limit, args.skip_judge, args.themes_only, args.append))
