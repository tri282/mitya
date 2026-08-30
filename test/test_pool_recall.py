# test/test_pool_recall.py
"""Verify: pool-recall reporting in scripts/eval_retrieval.py.

Plain-assert script, not pytest (pytest is not installed in .venv). Offline,
no GPU, no Qdrant, no API: feeds `_report_pool_recall` a hand-built per-row
vector and checks the one arithmetic identity the diagnostic exists to state --
headroom counts rows that are in the fused pool but below k, and NOT rows that
are missing from the pool entirely. Getting that backwards would report
retrieval failures as rerank headroom, which is the exact confusion the
metric was added to remove.

Usage:
    python test/test_pool_recall.py
"""

import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.eval_retrieval import _report_pool_recall

logger = logging.getLogger(__name__)


def _row(row_id: str, rank, pool_rank, family="primary", pool_size=25):
    return {
        "id": row_id,
        "family": family,
        "rank": rank,
        "hit": rank is not None,
        "pool_rank": pool_rank,
        "pool_hit": pool_rank is not None,
        "pool_size": pool_size,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Four rows, one of each kind: hit at k, ordering failure (in pool, below
    # k), retrieval failure (absent from pool), and a second hit at k.
    rows = [
        _row("r-0001", 2, 2),        # hit at k
        _row("r-0002", None, 12),    # in pool, below k -- ordering
        _row("r-0003", None, None),  # absent from pool -- retrieval
        _row("r-0004", 1, 1),        # hit at k
    ]
    out = _report_pool_recall(rows, k=5)["primary"]
    assert out["rows"] == 4, out
    assert out["recall_at_k"] == 0.5, out
    assert out["pool_recall"] == 0.75, out
    # The identity: headroom is the ordering failure alone, never the pool miss.
    assert out["headroom"] == 0.25, out
    assert out["pool_size_max"] == 25, out
    logger.info("ok: headroom counts below-k rows only, not rows missing from the pool")

    # A row can never be a k-hit and a pool-miss: recall@k <= pool_recall, so
    # headroom is non-negative by construction. Assert it on an all-hit set.
    out = _report_pool_recall([_row("r-0005", 1, 1), _row("r-0006", 3, 3)], k=5)["primary"]
    assert out["headroom"] == 0.0, out
    logger.info("ok: no headroom when every row already hits inside k")

    # Families are scored on their own denominators, never pooled.
    mixed = [_row("r-0007", 1, 1), _row("r-0008", None, 9, family="quotation")]
    out = _report_pool_recall(mixed, k=5)
    assert out["primary"]["recall_at_k"] == 1.0, out
    assert out["quotation"]["recall_at_k"] == 0.0, out
    assert out["quotation"]["headroom"] == 1.0, out
    logger.info("ok: per-family denominators kept separate")

    logger.info("ALL POOL-RECALL CHECKS PASSED")


if __name__ == "__main__":
    main()
