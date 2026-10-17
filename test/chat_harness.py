"""Shared driver for tests that run `chat()` directly over stubbed retrieval (v3 P0 B1).

Same posture as test_chat_semaphore.py: no server, no Qdrant, no network. The generation half
can be the real `stream_answer` over a `FakeProvider`, with the real guard, ledger and a temp
SQLite file, so a spend refusal or a forced truncation travels the whole path.
"""

import asyncio
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, ".")

import src.api.app as app_mod  # noqa: E402
import src.rag.generate as gen  # noqa: E402
from src import limits_config  # noqa: E402
from src.api.app import ChatRequest, chat  # noqa: E402
from src.config import Config  # noqa: E402
from src.db import init_schema  # noqa: E402
from src.gateway.guard import SpendRateBreaker  # noqa: E402
from src.gateway.ladder import Rung  # noqa: E402
from src.gateway.ledger import reset_spend_store  # noqa: E402
from src.limits import RateLimiter  # noqa: E402
from src.memory import SessionTurns  # noqa: E402

QUERY = "Was Raskolnikov guilty?"
POINTS = [types.SimpleNamespace(id="chunk-1")]
SOURCES = [{"book": "Crime and Punishment", "part": 3, "chapter": 2}]


class StubCache:
    enabled = True

    def __init__(self, cached=None):
        self.cached = cached

    def get(self, *a, **kw):
        return self.cached

    def set(self, *a, **kw):
        return None


class StubEntities:
    def resolve(self, query, active_book=None):
        return query, []


class OpenLimiter:
    """Stands in for `ScopedRateLimiter`: a window that never waits."""

    def limiter_for(self, model):
        return RateLimiter(rpm=10**6, tpm=10**9, rpd=10**9)


def make_state(cached=None):
    return types.SimpleNamespace(
        sem=asyncio.BoundedSemaphore(1), cache=StubCache(cached), entities=StubEntities(),
        qdrant=None, embedder=None, bm25=None, ranker=None, summaries={}, structure={},
        turns=SessionTurns(),
    )


async def _healthy_status(state):
    return True, {"generation": "ok", "retrieval": "ok"}


async def _no_write(*a, **kw):
    return 0


def use_temp_db() -> str:
    path = os.path.join(tempfile.mkdtemp(prefix="mitya-chat-db-"), "t.sqlite3")
    Config.DB_PATH = path
    init_schema(path)
    reset_spend_store()
    return path


def with_limits(**spend):
    """Replace the active limits, overriding spend fields. Returns a restore callable."""
    saved = limits_config._active
    base = limits_config.load_limits()
    limits_config._active = base.model_copy(update={"spend": base.spend.model_copy(update=spend)})
    reset_spend_store()

    def restore():
        limits_config._active = saved
        reset_spend_store()

    return restore


def patch(saved: dict, **overrides):
    defaults = {
        "route": lambda q, f, structure, summaries: ("DEFAULT", None),
        "_dependency_status": _healthy_status,
        "readiness_from_status": lambda result, cache_available: object(),
        "next_rung": lambda readiness, cache_hit: Rung.PRIMARY,
        "build_hard_filter": lambda filters: None,
        "retrieve": lambda *a, **kw: list(POINTS),
        "retrieve_structural_excerpts": lambda *a, **kw: list(POINTS),
        "rerank": lambda query, points, ranker, top_k=None: list(points),
        "build_context": lambda *a, **kw: ("passages", list(SOURCES)),
        "build_messages": lambda head, block, query, **kw: [],
        "active_persona_head": lambda: "",
        "build_sources_footer": lambda sources: "Sources:",
        "write_profile_from_turn": _no_write,
        "write_episode": _no_write,
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        saved[name] = getattr(app_mod, name)
        setattr(app_mod, name, value)


def parse_sse(frames) -> list[tuple[str, dict]]:
    events = []
    for frame in frames:
        lines = frame.strip().split("\n")
        name = lines[0].removeprefix("event: ")
        events.append((name, json.loads(lines[1].removeprefix("data: "))))
    return events


async def run_chat(state, *, query=QUERY, provider=None, breaker=None, **overrides):
    """Drive one request. With `provider`, the real `stream_answer` runs over it, through
    the real guard and ledger. Returns the parsed SSE events."""
    saved: dict = {}
    patch(saved, **overrides)
    gen_saved = (gen._provider_for, gen._limiter, gen._breaker)
    if provider is not None:
        gen._provider_for = lambda model: provider
        gen._limiter = OpenLimiter()
        gen._breaker = breaker or SpendRateBreaker(1e6, 0.8)
    try:
        response = await chat(ChatRequest(query=query), types.SimpleNamespace(
            app=types.SimpleNamespace(state=state)), tenant="tenant-a")
        return parse_sse([frame async for frame in response.body_iterator])
    finally:
        for name, value in saved.items():
            setattr(app_mod, name, value)
        gen._provider_for, gen._limiter, gen._breaker = gen_saved


def set_config(name: str, value):
    """Set a facade-resolved Config attribute; returns a restore callable."""
    setattr(Config, name, value)
    return lambda: delattr(Config, name)
