"""The gateway's contract (v2 P2 step 2, schedule Batch A).

Shape, from the session brief: `generate(messages, tier, tenant, budget) ->
stream of chunks + usage(tokens, cost, model, cache_hit)`. End-state: every
caller gets cost and model attribution back, and P1's spine
(`src/observability.py`'s `set_cost()`) records it with no second lookup --
one `GenerationStream.usage` read after the stream is drained, not a
per-provider call back into the adapter.

`Usage.cost` reuses `src.pricing.Cost` rather than a bare float, for the same
reason `pricing.py` states: a cost is either a real number or `None` (unknown),
never a zero standing in for "we don't know", and `metered` has to travel with
the number or a caller re-deriving it drifts from the value that was actually
billed.

`tier` and `budget` are typed as loosely as the phase allows on purpose: O1
(tier count/membership) and the budget guard's enforcement are both later
Decide-during-implementation items (phase-2-gateway.md, schedule CP-beta and
Batch B respectively). Fixing a tier enum or a budget dataclass here would be
choosing an answer for a question this batch is explicitly not allowed to
close.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from src.pricing import UNPRICED, Cost


class FinishReason(StrEnum):
    """Closed set. Values outside the recorded vocabulary land in OTHER and are counted,
    never dropped (P-1 F4, F24, findings VIII.1)."""

    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    OTHER = "other"
    NONE = "none"


# Recorded raw values: Google status (R53, R56), OpenAI-dialect finish_reason on
# Groq, OpenRouter, Baseten and Purdue (F4, F23, F26, F28).
_FINISH_MAP: dict[str, FinishReason] = {
    "stop": FinishReason.STOP,
    "completed": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "truncated": FinishReason.LENGTH,
    "error": FinishReason.ERROR,
    # Idempotent over the enum's own values, so a normalized value re-normalizes to itself.
    "content_filter": FinishReason.CONTENT_FILTER,
    "other": FinishReason.OTHER,
    "none": FinishReason.NONE,
    **{code: FinishReason.CONTENT_FILTER for code in (
        "safety", "recitation", "language", "prohibited_content",
        "spii", "blocklist", "image_safety", "content_blocked",
    )},
}

# A truncated or failed finish: the answer text is not the whole answer.
INCOMPLETE_FINISHES = frozenset({FinishReason.LENGTH, FinishReason.ERROR, FinishReason.CONTENT_FILTER})


def normalize_finish_reason(raw: str | None, native: str | None = None) -> FinishReason:
    """Pure. `native` is OpenRouter's `native_finish_reason`, consulted only when `raw`
    is absent or unmapped."""
    for candidate in (raw, native):
        if candidate:
            hit = _FINISH_MAP.get(candidate.strip().lower())
            if hit is not None:
                return hit
    return FinishReason.OTHER if (raw or native) else FinishReason.NONE


@dataclass
class Usage:
    """What one `generate()` call cost, and what produced it.

    Starts as the unfilled placeholder (`UNPRICED`, no model, no cache hit)
    and is mutated in place by the provider as the stream is drained --
    mirrors `src/rag/generate.py`'s `stream_answer(usage=...)` out-param
    pattern, generalized to carry model and cost attribution across more than
    one provider instead of a bare token-count dict. A caller must not read
    these fields as final before the associated `GenerationStream.chunks` has
    been fully consumed.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost: Cost = field(default_factory=lambda: UNPRICED)
    model: str | None = None
    # Provider-side caching (step 9 / CP-epsilon), not the AnswerCache's own
    # hit/miss (that stays src/rag/cache.py's concern, upstream of the
    # gateway entirely). False here means "this provider did not serve this
    # answer from its own cache" -- not "no cache was consulted at all".
    cache_hit: bool = False
    # Raw provider strings; `normalize_finish_reason` is the only reader.
    finish_reason: str | None = None
    native_finish_reason: str | None = None
    # Observed, recorded per run and never hashed (config identity split, schedule-p0 V).
    upstream: str | None = None
    # Provider-reported charge (e.g. OpenRouter usage.cost); None when the provider reports none.
    # Kept apart from `cost`, which is list price computed from the rate table (N-EV10).
    billed_usd: float | None = None


@dataclass
class GenerationStream:
    """The gateway's return value: token text as it streams, plus the `Usage`
    it fills in along the way.

    Bundled rather than returned as a `(iterator, usage)` tuple so a caller
    that only wants to log the eventual cost still holds one object, not two
    things it must remember to keep together.
    """

    chunks: AsyncIterator[str]
    usage: Usage


@runtime_checkable
class Provider(Protocol):
    """One provider adapter. Structural (a `Protocol`), not an ABC: a real
    adapter at Batch C, and `FakeProvider` here, both satisfy this by shape
    alone -- no shared base class to keep in sync, and `isinstance()` still
    works (`runtime_checkable`) for the tests that check a candidate really
    implements the contract.

    Routing input (`tier`) always comes from the server, never from the
    request body (charter IV) -- enforcing that is the caller's job, not this
    contract's; the contract only states that `tier` is a parameter a caller
    controls, and a request body is not a caller.
    """

    async def generate(
        self,
        messages: list[dict],
        tier: str,
        tenant: str,
        budget: float | None,
    ) -> GenerationStream:
        """Start a generation. `budget` is the remaining USD ceiling for this
        call, or `None` when unbounded (the local frozen baseline, and any
        caller before the budget guard exists) -- accepting it now, unused,
        so Batch B's guard is an enforcement point added in front of this
        call, not a signature change to every implementation of it.
        """
        ...
