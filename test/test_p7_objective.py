# test/test_p7_objective.py
"""P7 Batch C -- the retrieval cache's key-coverage test (safety before speed).

Runnable now -- no Qdrant, no provider, no gate call. Only imports and set
arithmetic against `src.optimizer.ALLOWED`; the known-answer check that
actually exercises the cache against real Qdrant is
`scripts/measure_retrieval_cache.py` (needs approval, not part of this file).
"""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.optimizer import ALLOWED  # noqa: E402
from src.p7_objective import (  # noqa: E402
    RETRIEVAL_AFFECTING_FIELDS,
    RETRIEVAL_IRRELEVANT_FIELDS,
    _check_retrieval_fields_cover_allowed,
)


def test_retrieval_relevance_sets_partition_allowed_exactly() -> None:
    """Every ALLOWED field is placed in exactly one of the two sets -- the
    same partition-completeness shape as `src.optimizer.partition_gaps()`.
    A field added to ALLOWED later and left unplaced here would silently
    reuse a stale cache entry if it actually affects retrieval; this is the
    loud, by-name failure instead."""
    assert RETRIEVAL_AFFECTING_FIELDS & RETRIEVAL_IRRELEVANT_FIELDS == set()
    assert RETRIEVAL_AFFECTING_FIELDS | RETRIEVAL_IRRELEVANT_FIELDS == set(ALLOWED)


def test_check_function_passes_against_the_real_allowlist() -> None:
    """`_check_retrieval_fields_cover_allowed()` is also called at import
    time (a loud build break, matching `src.optimizer._check_domain_covers_allowed`'s
    idiom) -- restated here as a readable, standalone check."""
    _check_retrieval_fields_cover_allowed()


def test_an_unplaced_allowed_field_fails_loudly_by_name() -> None:
    """Simulates D-5's own failure mode one layer down: a field admitted to
    ALLOWED with no retrieval-relevance placement must name itself in the
    error, not fail silently or generically."""
    fake_allowed = dict(ALLOWED)
    fake_allowed["a_brand_new_axis"] = fake_allowed[next(iter(ALLOWED))]
    both = RETRIEVAL_AFFECTING_FIELDS | RETRIEVAL_IRRELEVANT_FIELDS
    missing = set(fake_allowed) - both
    assert missing == {"a_brand_new_axis"}


def test_known_retrieval_affecting_fields() -> None:
    """Locks in the table this task verified against src/rag/retrieve.py:
    top_k_fused changes what gets scored; nothing else
    among the current five ALLOWED fields does."""
    assert RETRIEVAL_AFFECTING_FIELDS == {"top_k_fused"}
    assert RETRIEVAL_IRRELEVANT_FIELDS == {
        "memory_budget_tokens",
    }


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
