# test/test_sse_terminal.py
"""v3 P0 B1 verify: a truncated answer, a canary leak and a budget refusal are each visible in
`meta` with a terminal `done` event, over every SSE branch of `chat()`.

Plain-assert script. Offline: FakeProvider only, temp SQLite, no network. Run from the repo root.
"""

import ast
import asyncio
import inspect
import sys
import textwrap

sys.path.insert(0, ".")
sys.path.insert(0, "test")

from fastapi import HTTPException  # noqa: E402

import chat_harness as h  # noqa: E402
import src.api.app as app_mod  # noqa: E402
from src.gateway.fake import DEFAULT_FIXTURE, FakeProvider, Fixture  # noqa: E402
from src.gateway.ladder import Rung  # noqa: E402
from src.gateway.ledger import BudgetExceeded, SpendLayer  # noqa: E402
from src.guard import CANARY_SENTINEL  # noqa: E402

h.use_temp_db()

_META_KEYS = {"finish_reason", "complete", "guard", "refusal", "ledger"}


def _fixture(**kw) -> Fixture:
    base = dict(
        chunks=("Marvel ", "not."), prompt_tokens=10, completion_tokens=2,
        prompt_rate=1.0, completion_rate=4.0,
    )
    base.update(kw)
    return Fixture(**base)


def _by_name(events):
    return {name: payload for name, payload in events}


def _assert_terminal(events, *, complete: bool) -> dict:
    names = [name for name, _ in events]
    assert names[-1] == "done", names
    assert names.count("done") == 1, names
    meta = _by_name(events)["meta"]
    assert _META_KEYS <= set(meta), sorted(_META_KEYS - set(meta))
    done = events[-1][1]
    assert set(done) == {"complete", "finish_reason", "refusal"}, done
    assert done["complete"] is complete, done
    return meta


def test_live_branch_forced_truncation_is_visible() -> None:
    provider = FakeProvider(fixture=_fixture(finish_reason="length"))
    events = asyncio.run(h.run_chat(h.make_state(), provider=provider))
    meta = _assert_terminal(events, complete=False)
    assert meta["finish_reason"] == "length" and meta["complete"] is False
    assert events[-1][1]["finish_reason"] == "length"
    assert len(provider.calls) == 1


def test_live_branch_clean_stop_is_complete() -> None:
    events = asyncio.run(h.run_chat(h.make_state(), provider=FakeProvider(fixture=_fixture())))
    meta = _assert_terminal(events, complete=True)
    assert meta["finish_reason"] == "stop"
    assert meta["guard"]["tripped"] is False
    assert meta["ledger"]["calls"] == 1 and meta["ledger"]["list_usd"] is not None
    # No provider reported a charge: billed stays unknown, never 0.
    assert meta["ledger"]["billed_usd"] is None


def test_live_branch_planted_canary_leak_is_visible() -> None:
    restore = h.set_config("GUARD_CANARY", CANARY_SENTINEL)
    try:
        provider = FakeProvider(fixture=_fixture(chunks=("The code is ", CANARY_SENTINEL, ".")))
        events = asyncio.run(h.run_chat(h.make_state(), provider=provider))
    finally:
        restore()
    meta = _assert_terminal(events, complete=True)
    assert meta["guard"] == {"tripped": True, "layer": "canary", "label": "leak"}, meta["guard"]
    # The sentinel was planted in the system turn the provider received.
    assert CANARY_SENTINEL in provider.calls[0]["messages"][0]["content"]
    # Verdict carries no matched text.
    assert CANARY_SENTINEL not in str(meta["guard"])


def test_live_branch_prestream_refusal_is_429_with_retry_after() -> None:
    restore = h.with_limits(tenant_day_usd=1e-9)
    provider = FakeProvider(fixture=_fixture())
    try:
        asyncio.run(h.run_chat(h.make_state(), provider=provider))
    except HTTPException as exc:
        assert exc.status_code == 429, exc.status_code
        assert exc.headers["X-Mitya-Refusal"] == "tenant_day"
        assert 1 <= int(exc.headers["Retry-After"]) <= 86400
    else:
        raise AssertionError("tenant-day refusal did not surface")
    finally:
        restore()
    assert provider.calls == [], "a refused call must not send"


def test_live_branch_prestream_refusal_without_a_window_has_no_retry_after() -> None:
    restore = h.with_limits(per_call_usd=1e-9)
    provider = FakeProvider(fixture=_fixture())
    try:
        asyncio.run(h.run_chat(h.make_state(), provider=provider))
    except HTTPException as exc:
        assert exc.status_code == 429
        assert exc.headers["X-Mitya-Refusal"] == "per_call"
        assert "Retry-After" not in exc.headers
        assert "exceeds its budget" in exc.detail
    else:
        raise AssertionError("per-call refusal did not surface")
    finally:
        restore()
    assert provider.calls == []


def test_live_branch_mid_request_refusal_ends_with_terminal_event() -> None:
    def _stream(messages, usage):
        async def _gen():
            yield "Marvel "
            raise BudgetExceeded(SpendLayer.GLOBAL_DAY, 3600)
        return _gen()

    events = asyncio.run(h.run_chat(h.make_state(), stream_answer=_stream))
    names = [name for name, _ in events]
    assert names[0] == "token", "text already streamed stays"
    meta = _assert_terminal(events, complete=False)
    assert meta["complete"] is False
    assert meta["refusal"] == {"layer": "global_day", "retry_after_s": 3600}
    assert events[-1][1]["refusal"] == meta["refusal"]
    assert "error" not in names, "a refusal is a clean decline, not an error event"


def test_cached_branch_has_a_terminal_event_and_a_verdict() -> None:
    restore = h.set_config("GUARD_CANARY", CANARY_SENTINEL)
    try:
        cached = {
            "answer": f"It is {CANARY_SENTINEL}.", "sources": h.SOURCES, "footer": "Sources:",
            "model": "stub", "finish_reason": "stop",
        }
        events = asyncio.run(h.run_chat(h.make_state(cached=cached)))
    finally:
        restore()
    meta = _assert_terminal(events, complete=True)
    assert meta["cache_hit"] is True
    assert meta["finish_reason"] == "stop"
    assert meta["guard"]["layer"] == "canary"
    assert meta["refusal"] is None


def test_retrieval_only_branch_has_a_terminal_event() -> None:
    events = asyncio.run(h.run_chat(
        h.make_state(), next_rung=lambda readiness, cache_hit: Rung.RETRIEVAL_ONLY,
    ))
    meta = _assert_terminal(events, complete=True)
    assert meta["finish_reason"] == "none" and meta["guard"]["tripped"] is False


def test_every_streaming_branch_in_chat_yields_done() -> None:
    """Enumerate the branches from source: each StreamingResponse in chat() wraps a generator
    that yields a `done` built by `_done`. A new branch without one fails here."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(app_mod.chat)))
    fn = tree.body[0]
    generators = {n.name: n for n in ast.walk(fn) if isinstance(n, ast.AsyncFunctionDef)}
    wrapped = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "StreamingResponse":
            wrapped.append(node.args[0].func.id)
    assert len(wrapped) >= 3, wrapped
    for name in wrapped:
        calls = [
            c for c in ast.walk(generators[name])
            if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_done"
        ]
        assert calls, f"SSE branch {name} never yields a terminal done"


def test_default_fixture_is_untouched_by_the_finish_field() -> None:
    assert DEFAULT_FIXTURE.finish_reason == "stop"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
