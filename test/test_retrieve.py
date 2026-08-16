# test/test_retrieve.py
"""Phase 4 verify: build_hard_filter() part/chapter type coercion (D2 fix),
plus retrieve()'s `legs` ablation switch (plans/mitya-ablation/plan.md).

Plain-assert script, not pytest (pytest is not installed in .venv -- see
plans/mitya-rag/05-impl/phase-4.md). Offline: exercises build_hard_filter()'s
own logic and retrieve()'s prefetch assembly against stubs -- no Qdrant client
construction, no embedding model load, no network.

Usage:
    python test/test_retrieve.py
"""

import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from qdrant_client import models

from src.rag.retrieve import build_hard_filter, retrieve

logger = logging.getLogger(__name__)


class _Arr(list):
    """Stands in for the numpy arrays fastembed returns -- retrieve() calls
    .tolist() on both the dense vector and the sparse indices/values."""

    def tolist(self):
        return list(self)


class _StubSparse:
    indices = _Arr([1, 2])
    values = _Arr([0.5, 0.5])


class _StubEmbedder:
    def embed(self, texts):
        return [_Arr([0.1, 0.2])]


class _StubBm25:
    def query_embed(self, text):
        return [_StubSparse()]


class _StubClient:
    """Captures the prefetch list retrieve() assembles."""

    def __init__(self):
        self.prefetch = None

    def query_points(self, collection, prefetch, query, limit, with_payload):
        self.prefetch = prefetch
        return type("Response", (), {"points": []})()


def _legs_used(legs) -> list[str]:
    client = _StubClient()
    retrieve(client, _StubEmbedder(), _StubBm25(), "query", None, legs=legs)
    return [p.using for p in client.prefetch]


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

    # --- retrieve() `legs` (ablation switch; default must stay hybrid) ---

    def legs_default_is_hybrid():
        # The load-bearing one: no production caller passes `legs`, so a
        # regression here silently halves retrieval in the live app.
        assert _legs_used(("dense", "bm25")) == ["dense", "bm25"]
        client = _StubClient()
        retrieve(client, _StubEmbedder(), _StubBm25(), "query", None)
        assert [p.using for p in client.prefetch] == ["dense", "bm25"], "default legs changed"

    def legs_single_arms():
        assert _legs_used(("dense",)) == ["dense"]
        assert _legs_used(("bm25",)) == ["bm25"]

    def legs_empty_raises():
        try:
            _legs_used(())
        except ValueError:
            return
        raise AssertionError("expected ValueError for empty legs")

    check("legs: default (and explicit) hybrid builds both prefetch legs", legs_default_is_hybrid)
    check("legs: dense-only / sparse-only build exactly one leg", legs_single_arms)
    check("legs: empty tuple raises ValueError", legs_empty_raises)

    logger.info("=" * 60)
    logger.info("%d passed, %d failed", 9 - failures, failures)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
