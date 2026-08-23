# test/test_eval_cells.py
"""Phase 0 step 9b verify: cell aggregation in scripts/eval_quality.py (R25, R28).

Plain-assert script, not pytest (pytest is not installed in .venv -- see
plans/mitya-rag/05-impl/phase-4.md). Offline, no GPU, no Qdrant, no API: feeds
`_cell_stats`/`_summarize` a hand-built transcript and checks the two rules that
the k=3 run depends on being right, since a wrong mean here is invisible in the
output and permanent once step 13 freezes.

    1. A cell's score is the mean over its draws, and modes are compared on cell
       means -- draws are never pooled flat, or a cell that kept 3 draws would
       outvote one that kept 2.
    2. A cell below `min_draws` judged draws is counted as failed and kept out of
       the means, never dropped silently.

Usage:
    python test/test_eval_cells.py
"""

import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.eval_quality import _cell_stats, _summarize

logger = logging.getLogger(__name__)


def _row(cell_id: str, draw: int, persona=None, ground=None, fabricated=0, gen_error=None):
    """One transcript row. `persona=None` means the draw was never judged --
    a generation or judge failure."""
    judge = None if persona is None else {
        "persona_retention": persona,
        "groundedness": ground,
        "fabricated_events": fabricated,
        "rationale": "fixture",
    }
    return {
        "id": cell_id, "theme": "Ambiguity", "query": f"q for {cell_id}",
        "mode": "A", "draw": draw, "answer": "answer text",
        "generation_error": gen_error, "judge": judge, "judge_error": None,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Rule 1: cell score is the mean over draws.
    rows = [_row("q-0001", 0, 5, 4), _row("q-0001", 1, 2, 4), _row("q-0001", 2, 2, 1)]
    cells, failed = _cell_stats(rows, min_draws=2)
    assert failed == 0, failed
    assert len(cells) == 1, cells
    assert cells[0]["persona_retention"] == 3.0, cells[0]
    assert cells[0]["groundedness"] == 3.0, cells[0]
    assert cells[0]["n_judged"] == 3, cells[0]
    # The weakest draw is the one surfaced for review (2 + 1, not 2 + 4).
    assert cells[0]["worst_draw"]["draw"] == 2, cells[0]["worst_draw"]
    logger.info("ok: cell score is the mean over its draws")

    # Rule 2: a cell that kept too few judged draws is failed, not averaged.
    rows = [
        _row("q-0001", 0, 5, 5), _row("q-0001", 1, 5, 5), _row("q-0001", 2, 5, 5),
        _row("q-0002", 0, 1, 1), _row("q-0002", 1, None, gen_error="empty response"),
        _row("q-0002", 2, None, gen_error="empty response"),
    ]
    cells, failed = _cell_stats(rows, min_draws=2)
    assert failed == 1, failed
    assert [c["id"] for c in cells] == ["q-0001"], cells
    logger.info("ok: sub-min_draws cell is counted failed and excluded from the means")

    # ... and at k=1 the single draw IS the cell, so nothing is failed.
    cells, failed = _cell_stats([_row("q-0003", 0, 3, 3)], min_draws=1)
    assert (failed, len(cells)) == (0, 1), (failed, cells)
    logger.info("ok: min_draws=1 keeps the single-draw cell")

    # Rule 1, mode level: cells are weighted equally regardless of draw count.
    # Flat pooling of the 5 judged draws below would give persona 4.2; the cell
    # means are 5.0 and 1.0, so the mode mean must be 3.0.
    rows = [
        _row("q-0001", 0, 5, 5), _row("q-0001", 1, 5, 5), _row("q-0001", 2, 5, 5),
        _row("q-0002", 0, 1, 1), _row("q-0002", 1, 1, 1),
        _row("q-0002", 2, None, gen_error="empty response"),
    ]
    summary = _summarize(rows, ["A"], min_draws=2)
    m = summary["per_mode"]["A"]
    assert m["mean_persona_retention"] == 3.0, m
    assert (m["n_cells"], m["n_cells_scored"], m["n_cells_failed"]) == (2, 2, 0), m
    assert m["generation_errors"] == 1, m
    logger.info("ok: mode mean weights cells equally, not draws")

    # Fabrication is boolean per draw (R27), so a cell that fabricated in 1 of 3
    # draws contributes 1/3 -- not the event count, which stays diagnostic.
    rows = [_row("q-0001", 0, 3, 3, fabricated=7), _row("q-0001", 1, 3, 3), _row("q-0001", 2, 3, 3)]
    summary = _summarize(rows, ["A"], min_draws=2)
    m = summary["per_mode"]["A"]
    assert m["fabrication_rate"] == 0.333, m
    assert m["mean_fabricated_events_UNRELIABLE"] == 2.333, m
    logger.info("ok: fabrication rate is boolean-per-draw; the count stays separate")

    logger.info("ALL CELL-AGGREGATION CHECKS PASSED")


if __name__ == "__main__":
    main()
