# test/test_cache_arm.py
"""P2 step 9 verify (schedule Batch B, R51/CP-delta): the cache carries the
producing arm -- src/rag/cache.py's `model` field on the stored value.

Two things settled by R51 and checked here:
  * `AnswerCache.set()` requires a producing model, and `get()` returns it on
    a hit -- fixes attribution under per-request routing, where
    config_version alone cannot say which arm answered.
  * a legacy entry stored under the OLD shape (no `model`) is a miss, not a
    migrated hit -- the self-healing property `get()` already had (R6/C10)
    must survive this shape change unchanged.

Plain-assert script, not pytest. Uses a real (temp-dir) diskcache backend,
same as test_cache_key.py's sibling module -- `_key()` alone is covered
there; this file is about `get`/`set`'s stored VALUE shape.
"""

import os
import sys
import tempfile

sys.path.insert(0, ".")

import diskcache  # noqa: E402

from src.rag.cache import AnswerCache  # noqa: E402

_CFG = "451327aab528"
_CORPUS = "abc123abc123"


def _cache_in(tmp: str) -> AnswerCache:
    """A real AnswerCache pointed at a temp dir -- __init__ reads
    Config.CACHE_DIR, so the backing diskcache.Cache is swapped in directly
    rather than threading a new constructor argument through for a test."""
    cache = AnswerCache()
    cache._cache = diskcache.Cache(tmp)
    return cache


def test_set_then_get_round_trips_the_producing_model() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cache = _cache_in(tmp)
        cache.set(
            "who is Raskolnikov?", None, "DEFAULT", _CFG, _CORPUS,
            "a poor student.", [{"chunk_id": "r-0001"}], "footer text",
            "local-q4km", finish_reason="stop",
        )
        hit = cache.get("who is Raskolnikov?", None, "DEFAULT", _CFG, _CORPUS)
        assert hit is not None
        assert hit["model"] == "local-q4km"
        assert hit.keys() == {"answer", "sources", "footer", "model", "finish_reason"}
        assert hit["finish_reason"] == "stop"


def test_a_legacy_entry_without_model_is_a_miss_not_a_migration() -> None:
    """R6/C10's self-healing property, preserved across the shape change: a
    value stored under the pre-step-9 shape ({answer, sources, footer}, no
    `model`) has no way to acquire a producing arm after the fact, so it must
    be treated as a miss and regenerated -- never silently served with a
    fabricated or missing `model`."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = _cache_in(tmp)
        key = cache._key("what happens in part 1?", None, "DEFAULT", _CFG, _CORPUS)
        cache._cache.set(key, {"answer": "old shape", "sources": [], "footer": ""})
        assert cache.get("what happens in part 1?", None, "DEFAULT", _CFG, _CORPUS) is None


def test_an_entry_without_a_finish_reason_is_a_miss_not_a_complete_hit() -> None:
    """v3 P0 B2 (P0-F6): an entry stored before `finish_reason` existed could be a truncated
    answer; it regenerates once rather than replaying as complete."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = _cache_in(tmp)
        key = cache._key("what happens in part 1?", None, "DEFAULT", _CFG, _CORPUS)
        cache._cache.set(key, {"answer": "a", "sources": [], "footer": "", "model": "local-q4km"})
        assert cache.get("what happens in part 1?", None, "DEFAULT", _CFG, _CORPUS) is None


def test_the_finish_reason_round_trips() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cache = _cache_in(tmp)
        cache.set("q", None, "DEFAULT", _CFG, _CORPUS, "cut off", [], "", "local-q4km", finish_reason="length")
        assert cache.get("q", None, "DEFAULT", _CFG, _CORPUS)["finish_reason"] == "length"


def test_disabled_cache_is_a_no_op_for_get_and_set() -> None:
    cache = AnswerCache.__new__(AnswerCache)
    cache._cache = None
    cache.set("q", None, "DEFAULT", _CFG, _CORPUS, "answer", [], "", "local-q4km", finish_reason="stop")
    assert cache.get("q", None, "DEFAULT", _CFG, _CORPUS) is None


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
