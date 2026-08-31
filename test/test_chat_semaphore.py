# test/test_chat_semaphore.py
"""P4 Batch D verify (D7): the /chat retrieval lock is released exactly once,
on every path, and before generation starts.

Plain-assert script, not pytest. Offline: no server, no Qdrant, no network, no
GPU -- `chat()` is called directly with a stub `request.app.state` and with
every pipeline collaborator monkeypatched onto the `src.api.app` module, the
same posture `test_readiness_serving_provider.py` uses to exercise the real
bridge instead of a copy of it.

What it guards. D7 moved `state.sem` from three stream-`finally` blocks (held
across retrieval + generation + the whole SSE stream) to an `async with` scoped
to route -> cache -> ladder -> retrieval -> build_context. Both failure modes of
that move are silent-until-fatal:

  * a path that exits the region WITHOUT releasing deadlocks the server
    permanently on the next request -- one lost slot, never returned;
  * a path that releases TWICE raises capacity above 1, which a plain
    `asyncio.Semaphore` accepts silently. `BoundedSemaphore` is what turns that
    into a loud ValueError, and the last test here asserts that property is real
    rather than assumed.

The sharp assertion is not just "back to 1 at the end": it is that the value is
already 1 when `chat()` RETURNS, before the SSE body is drained. That is the
whole point of D7 -- generation and the stream run outside the lock.

NOTE the one heavy import: `src.api.app` builds the FastAPI app at import time
and mounts `frontend/dist`, so this test must be run from the repo root.
"""

import asyncio
import inspect
import sys
import types

sys.path.insert(0, ".")

from src.api.app import ChatRequest, chat  # noqa: E402
import src.api.app as app_mod  # noqa: E402
from src.gateway.ladder import Rung  # noqa: E402
from src.gateway.retry import TransportError  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _StubCache:
    """`state.cache`: `enabled` is read by the ladder, `get` decides the path."""

    enabled = True

    def __init__(self, cached=None):
        self.cached = cached

    def get(self, *a, **kw):
        return self.cached

    def set(self, *a, **kw):
        return None


class _StubEntities:
    def resolve(self, query, active_book=None):
        return query, []


def _state(cached=None):
    return types.SimpleNamespace(
        sem=asyncio.BoundedSemaphore(1),
        cache=_StubCache(cached),
        entities=_StubEntities(),
        qdrant=None, embedder=None, bm25=None, ranker=None,
        summaries={}, structure={},
    )


def _request(state):
    """`chat()` reads exactly one thing off the Request: `app.state`."""
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


_POINTS = [types.SimpleNamespace(id="chunk-1")]
_SOURCES = [{"book": "Crime and Punishment", "part": 3, "chapter": 2}]


async def _tokens():
    for text in ("Rodion ", "hesitated."):
        yield text


def _patch(monkeypatched, **overrides):
    """Swap module-level collaborators on src.api.app, recording originals."""
    defaults = {
        # route/retrieval half -- inside the lock
        "route": lambda q, f, structure, summaries: ("DEFAULT", None),
        "_dependency_status": _healthy_status,
        "readiness_from_status": lambda result, cache_available: object(),
        "next_rung": lambda readiness, cache_hit: Rung.PRIMARY,
        "build_hard_filter": lambda filters: None,
        "retrieve": lambda *a, **kw: list(_POINTS),
        "retrieve_structural_excerpts": lambda *a, **kw: list(_POINTS),
        "rerank": lambda query, points, ranker: list(points),
        "build_context": lambda *a, **kw: ("passages", list(_SOURCES)),
        # generation half -- outside the lock
        "build_messages": lambda head, block, query: [],
        "active_persona_head": lambda: "",
        "build_sources_footer": lambda sources: "Sources:",
        "stream_answer": lambda messages, usage: _tokens(),
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatched[name] = getattr(app_mod, name)
        setattr(app_mod, name, value)


async def _healthy_status(state):
    return True, {"generation": "ok", "retrieval": "ok"}


async def _run(state, **overrides):
    """Drive one full request. Returns (value_at_return, value_after_drain)."""
    monkeypatched: dict = {}
    _patch(monkeypatched, **overrides)
    try:
        response = await chat(ChatRequest(query="Was Raskolnikov guilty?"), _request(state))
        at_return = state.sem._value
        async for _ in response.body_iterator:
            pass
        return at_return, state.sem._value
    finally:
        for name, value in monkeypatched.items():
            setattr(app_mod, name, value)


async def _run_expecting(exc_type, state, **overrides):
    """Drive one request that must raise. Returns the semaphore value after."""
    monkeypatched: dict = {}
    _patch(monkeypatched, **overrides)
    try:
        try:
            await chat(ChatRequest(query="Was Raskolnikov guilty?"), _request(state))
        except exc_type:
            return state.sem._value
        raise AssertionError(f"expected {exc_type.__name__}, nothing was raised")
    finally:
        for name, value in monkeypatched.items():
            setattr(app_mod, name, value)


# --- the four properties -----------------------------------------------------

def test_construction_site_is_bounded() -> None:
    """The live semaphore -- the one lifespan() builds -- is the bounded kind.

    Asserted against source rather than by running lifespan(), which would open
    the real Qdrant client and take the data/vector_db/ lock. Every test below
    builds its own BoundedSemaphore; this is what ties them to the real one.
    """
    src = inspect.getsource(app_mod.lifespan)
    assert "asyncio.BoundedSemaphore(1)" in src, src
    assert "asyncio.Semaphore(1)" not in src, src


def test_double_release_raises() -> None:
    """BoundedSemaphore's whole reason for being here: over-release is LOUD.

    A plain asyncio.Semaphore accepts this silently and runs at capacity 2 --
    the exact corruption D7's several release sites could have introduced.
    """
    sem = asyncio.BoundedSemaphore(1)
    try:
        sem.release()
    except ValueError:
        return
    raise AssertionError("BoundedSemaphore(1) accepted a release above its bound")


def test_cache_hit_releases_before_the_stream() -> None:
    state = _state(cached={
        "answer": "He was.", "sources": _SOURCES, "footer": "Sources:", "model": "stub",
    })
    at_return, after_drain = asyncio.run(_run(state))
    assert at_return == 1, f"cache hit still held the lock at return: {at_return}"
    assert after_drain == 1, f"cache-hit stream released again: {after_drain}"


def test_structural_route_releases_before_generation() -> None:
    """STRUCTURAL skips the BM25 leg and the rerank stage -- a distinct exit."""
    state = _state()
    at_return, after_drain = asyncio.run(_run(
        state,
        route=lambda q, f, structure, summaries: (
            "STRUCTURAL", {"book": "Crime and Punishment", "part": 3, "chapter": 2},
        ),
    ))
    assert at_return == 1, f"STRUCTURAL still held the lock at return: {at_return}"
    assert after_drain == 1, f"STRUCTURAL stream released again: {after_drain}"


def test_default_route_releases_before_generation() -> None:
    state = _state()
    at_return, after_drain = asyncio.run(_run(state))
    assert at_return == 1, f"DEFAULT still held the lock at return: {at_return}"
    assert after_drain == 1, f"DEFAULT stream released again: {after_drain}"


def test_retrieval_only_rung_releases() -> None:
    """The third stream. Generation is down; the passages still stream, and
    they stream outside the lock like everything else."""
    state = _state()
    at_return, after_drain = asyncio.run(_run(
        state, next_rung=lambda readiness, cache_hit: Rung.RETRIEVAL_ONLY,
    ))
    assert at_return == 1, f"RETRIEVAL_ONLY still held the lock at return: {at_return}"
    assert after_drain == 1, f"RETRIEVAL_ONLY stream released again: {after_drain}"


def test_exception_inside_the_locked_region_releases() -> None:
    """A raise during retrieval must not carry the lock out with it."""
    def _boom(*a, **kw):
        raise RuntimeError("qdrant exploded")

    state = _state()
    value = asyncio.run(_run_expecting(RuntimeError, state, retrieve=_boom))
    assert value == 1, f"a retrieval exception leaked the lock: {value}"


def test_bad_filter_inside_the_locked_region_releases() -> None:
    """The ValueError -> 400 exit, which goes through `_abort()` INSIDE the
    region. `_abort()` must record without releasing; the context manager
    releases."""
    def _bad(*a, **kw):
        raise ValueError("unmapped book title")

    state = _state()
    value = asyncio.run(_run_expecting(HTTPException, state, build_hard_filter=_bad))
    assert value == 1, f"the 400 exit leaked or doubled the lock: {value}"


def test_unavailable_rung_releases() -> None:
    """The ladder's bottom rung raises 503 from inside the region."""
    state = _state()
    value = asyncio.run(_run_expecting(
        HTTPException, state, next_rung=lambda readiness, cache_hit: Rung.UNAVAILABLE,
    ))
    assert value == 1, f"the UNAVAILABLE 503 leaked or doubled the lock: {value}"


def test_generation_failure_after_release_does_not_release_again() -> None:
    """The other half of D7's hazard. This path calls `_abort()` too, but it
    runs AFTER the lock is gone -- a release there would raise ValueError out of
    a BoundedSemaphore instead of the 502 the caller must see.
    """
    def _dead(messages, usage):
        async def _gen():
            raise TransportError("provider unreachable")
            yield  # marks this an async generator
        return _gen()

    state = _state()
    value = asyncio.run(_run_expecting(HTTPException, state, stream_answer=_dead))
    assert value == 1, f"a post-release generation failure disturbed the lock: {value}"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
