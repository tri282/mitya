# test/test_retrieve.py
"""Phase 4 verify: build_hard_filter() part/chapter type coercion (D2 fix).

Plain-assert script, not pytest (pytest is not installed in .venv -- see
plans/mitya-rag/05-impl/phase-4.md). Offline: only exercises
build_hard_filter()'s own logic, no Qdrant client construction, no network.

Usage:
    python test/test_retrieve.py
"""

import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from qdrant_client import models

from src.rag.retrieve import build_hard_filter

logger = logging.getLogger(__name__)


def _condition_value(hard_filter: models.Filter, key: str):
    for cond in hard_filter.must:
        if cond.key == key:
            return cond.match.value
    raise AssertionError(f"no condition for key={key!r} in {hard_filter.must!r}")


def main() -> None:
    failures = 0

    def check(description: str, fn) -> None:
        nonlocal failures
        try:
            fn()
            logger.info("[PASS] %s", description)
        except AssertionError as exc:
            failures += 1
            logger.error("[FAIL] %s: %s", description, exc)

    def no_filters_none():
        assert build_hard_filter(None) is None

    def empty_dict_none():
        assert build_hard_filter({}) is None

    check("no filters -> None", no_filters_none)
    check("empty dict -> None", empty_dict_none)

    def d2_int_filter_passthrough():
        hf = build_hard_filter({"book": "The Idiot", "part": 1, "chapter": 3})
        part_val = _condition_value(hf, "part")
        chapter_val = _condition_value(hf, "chapter")
        assert part_val == 1 and isinstance(part_val, int), f"part={part_val!r}"
        assert chapter_val == 3 and isinstance(chapter_val, int), f"chapter={chapter_val!r}"

    check("int part/chapter pass through unchanged", d2_int_filter_passthrough)

    def d2_string_filter_coerced():
        # D2 regression: P6 will hand over {"part": "1", "chapter": "3"}
        # (strings are the default for JSON query params / form UIs).
        # Pre-fix, MatchValue(value="1") went straight to Qdrant against an
        # integer payload field and matched zero points -- silent empty
        # context, no exception. Post-fix, these must coerce to real ints.
        hf = build_hard_filter({"book": "The Idiot", "part": "1", "chapter": "3"})
        part_val = _condition_value(hf, "part")
        chapter_val = _condition_value(hf, "chapter")
        assert part_val == 1 and isinstance(part_val, int), f"part={part_val!r} (type={type(part_val)})"
        assert chapter_val == 3 and isinstance(chapter_val, int), f"chapter={chapter_val!r} (type={type(chapter_val)})"

    check("D2: string part/chapter coerced to int", d2_string_filter_coerced)

    def d2_non_integral_raises():
        try:
            build_hard_filter({"part": "not-a-number"})
        except ValueError:
            return
        raise AssertionError("expected ValueError for non-integral part filter")

    check("D2: non-integral part raises ValueError", d2_non_integral_raises)

    def d2_bool_rejected():
        # bool is an int subclass in Python; must not silently pass as 1/0.
        try:
            build_hard_filter({"chapter": True})
        except ValueError:
            return
        raise AssertionError("expected ValueError for bool chapter filter")

    check("D2: bool chapter rejected, not coerced to 1", d2_bool_rejected)

    logger.info("=" * 60)
    logger.info("%d passed, %d failed", 6 - failures, failures)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
