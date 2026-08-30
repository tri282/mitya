"""Ladder rung forcer (mitya-v2 P4 step 8, schedule Batch C).

Depends on Batch A0: before it landed, `next_rung()` had no call site and
`/chat` never reported which rung served (R83, R89). After it, the served
rung is `meta.rung` on a stream and the `X-Mitya-Rung` header on every error
exit (R94) -- `HarnessClient`'s `ChatResult.rung` reads both.

**The lever this module is built on, and the one it is NOT.** G-P7 is open:
`Readiness.degraded` is assigned `True` nowhere, so the spend-rate breaker
never reaches the ladder. R94 measured that after Batch A0 the ONLY lever
into `CACHED`/`RETRIEVAL_ONLY` is `generation_available=False`, and that its
new source (`src.rag.generate.credential_status()`) is config-only credential
PRESENCE, not a live outage probe. This module forces rungs on that lever,
per the task's own instruction -- it does not wire the breaker into the
ladder and does not add a `degraded` setter.

**What this package can and cannot do about it.** This harness speaks only
the public API (step 1) -- it has no way to unset a server's environment
variable or restart its process. `PRIMARY` and `CACHED` are forceable purely
over HTTP; `RETRIEVAL_ONLY`, `503`, and `CHEAPER` are not, and
`FORCE_PROCEDURE` says why for each rather than silently pretending they are
reachable the same way.
"""

from __future__ import annotations

import logging

from src.harness.client import ChatResult, HarnessClient

logger = logging.getLogger(__name__)

# The `Readiness` field(s) (src/gateway/ladder.py's frozen dataclass:
# generation_available, retrieval_available, cache_available, degraded) that
# `next_rung()` actually branches on to reach each rung -- named IDENTICALLY
# to the dataclass fields on purpose, so this is checkable against the real
# type rather than a second, parallel health vocabulary invented in the
# harness (schedule-p4.md Batch C Verify: "the rung forcer's conditions map
# onto Readiness's fields"). This package cannot import
# `src.gateway.ladder` itself (step 1's rule) -- the corresponding test does,
# from outside `src/harness/`, and checks these names against the dataclass.
RUNG_CONDITIONS: dict[str, tuple[str, ...]] = {
    "primary_model": ("generation_available",),
    "cached_answer": ("generation_available", "cache_available"),
    # UNREACHABLE BY CONSTRUCTION (D3, R69): `Readiness` folds primary and
    # cheaper into one `generation_available` flag; routing was closed as
    # not-adopted, so there is no second flag for a second rung to disagree
    # on. Empty tuple, not omitted -- an omitted key would look like an
    # oversight rather than a stated structural fact.
    "cheaper_model": (),
    "retrieval_only": ("generation_available", "retrieval_available"),
    "503": ("generation_available", "retrieval_available"),
}

FORCE_PROCEDURE: dict[str, str] = {
    "primary_model": "Default deployment state, forceable over HTTP: force_primary() below.",
    "cached_answer": (
        "Forceable over HTTP: force_cached() below issues the same "
        "(query, filters) twice; the second call is served from AnswerCache."
    ),
    "cheaper_model": (
        "UNREACHABLE BY CONSTRUCTION (D3, R69) -- there is no second "
        "generation arm to force. Never attempted; no force_cheaper()."
    ),
    "retrieval_only": (
        "NOT forceable over HTTP (step 1's public-API-only rule). R94: the "
        "only lever is generation_available=False, and Batch A0 made that "
        "config-only credential PRESENCE, not a live outage probe -- so the "
        "deployment-level action is to unset the credential env var the "
        "serving provider needs (GOOGLE_API_KEY / GROQ_API_KEY, whichever "
        "src.rag.generate.serving_provider_name() resolves to for the "
        "running Config.SERVING_MODEL) and restart uvicorn. "
        "[RUN -- needs approval; not executed by this module.] "
        "Confirm afterward with observe_rung()."
    ),
    "503": (
        "Same credential-removal action as retrieval_only, PLUS retrieval "
        "down too (the Qdrant collection emptied or unreachable). Not "
        "forceable by this harness alone; confirm with observe_rung()."
    ),
}


async def force_primary(client: HarnessClient, query: str) -> ChatResult:
    """PRIMARY needs nothing forced: it is the default deployment state
    whenever generation is configured and credentialed (R94)."""
    return await client.chat(query)


async def force_cached(
    client: HarnessClient, query: str, filters: dict | None = None,
) -> tuple[ChatResult, ChatResult]:
    """CACHED: the same (query, filters) served twice. The caller checks the
    second result's `cache_hit is True` and `rung == "cached_answer"` --
    this function only drives the two calls, it does not assert on them."""
    first = await client.chat(query, filters=filters)
    second = await client.chat(query, filters=filters)
    return first, second


async def observe_rung(client: HarnessClient, query: str) -> ChatResult:
    """What rung `/chat` actually serves for `query`, right now. The
    confirmation step for a deployment-level rung change (`retrieval_only`,
    `503`) that this harness cannot itself perform -- see `FORCE_PROCEDURE`.
    """
    return await client.chat(query)
