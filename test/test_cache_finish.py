# test/test_cache_finish.py
"""v3 P0 B2 verify (P0-F6 (1)): the answer cache stores the finish reason and a hit replays it, so a
truncated answer never replays as complete.

Plain-assert script. Offline: FakeProvider, a real AnswerCache in a temp dir, temp SQLite. Run from
the repo root.
"""

import asyncio
import sys
import tempfile

sys.path.insert(0, ".")
sys.path.insert(0, "test")

import chat_harness as h  # noqa: E402

from src.config import Config  # noqa: E402
from src.gateway.fake import FakeProvider, Fixture  # noqa: E402
from src.rag.cache import AnswerCache  # noqa: E402

h.use_temp_db()


def _fixture(finish: str) -> Fixture:
    return Fixture(
        chunks=("Marvel ", "not."), prompt_tokens=10, completion_tokens=2,
        prompt_rate=1.0, completion_rate=4.0, finish_reason=finish,
    )


def _real_cache() -> AnswerCache:
    saved = Config.CACHE_DIR, Config.CACHE_ENABLED
    Config.CACHE_DIR, Config.CACHE_ENABLED = tempfile.mkdtemp(prefix="mitya-cache-finish-"), True
    try:
        return AnswerCache()
    finally:
        Config.CACHE_DIR, Config.CACHE_ENABLED = saved


def _serve_twice(finish: str) -> tuple[dict, dict, FakeProvider]:
    cache = _real_cache()
    first_provider = FakeProvider(fixture=_fixture(finish))
    first_state = h.make_state()
    first_state.cache = cache
    first = dict(asyncio.run(h.run_chat(first_state, provider=first_provider)))
    second_state = h.make_state()
    second_state.cache = cache
    second_events = asyncio.run(h.run_chat(second_state))
    assert first["meta"]["cache_hit"] is False and second_events[-1][0] == "done"
    return first, dict(second_events), first_provider


def test_a_truncated_answer_replays_as_truncated() -> None:
    first, second, provider = _serve_twice("length")
    assert first["meta"]["finish_reason"] == "length" and first["meta"]["complete"] is False
    assert len(provider.calls) == 1, "the replay must come from the cache"
    assert second["meta"]["cache_hit"] is True
    assert second["meta"]["finish_reason"] == "length", second["meta"]["finish_reason"]
    assert second["meta"]["complete"] is False
    assert second["done"]["complete"] is False and second["done"]["finish_reason"] == "length"
    assert second["token"]["text"] == "Marvel not."


def test_a_complete_answer_replays_as_complete() -> None:
    _, second, _ = _serve_twice("stop")
    assert second["meta"]["cache_hit"] is True
    assert second["meta"]["finish_reason"] == "stop" and second["meta"]["complete"] is True
    assert second["done"] == {"complete": True, "finish_reason": "stop", "refusal": None}


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
