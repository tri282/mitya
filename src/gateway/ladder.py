"""The degradation ladder, written as policy (v2 P2 step 10, charter V).

Ordered, fixed: primary model -> cheaper model -> cached answer ->
retrieval-only (passages, no generation) -> 503. Written HERE as a pure
decision function; P4 EXERCISES it rung-by-rung under real fault injection
(charter V, phase-2-gateway.md step 10). Nothing here calls llama-server, a
provider, or Qdrant -- `next_rung()` is a pure function over a `Readiness`
snapshot, unit-testable with no service running.

`Readiness` is what `GET /ready` reports (`src/api/app.py`) -- health and
readiness are split there for the same reason charter V requires it: an
orchestrator's liveness probe ("should this process restart?") and this
ladder's readiness read ("can it serve the primary rung right now?") are two
different questions over the same dependency state, not one question asked
twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Rung(str, Enum):
    PRIMARY = "primary_model"
    CHEAPER = "cheaper_model"
    CACHED = "cached_answer"
    RETRIEVAL_ONLY = "retrieval_only"
    UNAVAILABLE = "503"


@dataclass(frozen=True)
class Readiness:
    """One dependency snapshot.

    `generation_available` folds "the primary model is reachable" together
    with "so is any cheaper model" until routing (Batch D, O1/CP-beta) gives
    the ladder more than one generation arm to distinguish -- there is
    exactly one generation backend today (`local-q4km`), so a two-flag
    PRIMARY/CHEAPER split has nothing to disagree on yet and would be a
    field this batch cannot honestly fill in. `degraded` is step 4's
    spend-rate breaker having tripped (`src/gateway/guard.py`'s `Degraded`) --
    a distinct reason from `generation_available=False`: the model is UP,
    spending on it right now is not.
    """

    generation_available: bool
    retrieval_available: bool
    cache_available: bool
    degraded: bool = False


def readiness_from_status(result: dict, cache_available: bool) -> Readiness:
    """Bridge `GET /ready`'s dependency-check body (`src/api/app.py`'s
    `_dependency_status()`) into what this module consumes, so the ladder
    reads readiness through the same fields `/ready` already reports rather
    than a second, parallel health check invented for the ladder alone.

    `retrieval_available` is read off the `qdrant` sub-result specifically
    (not the overall `healthy` flag) because retrieval-only (this ladder's
    third rung) needs Qdrant, not llama-server -- collapsing both into one
    boolean would make "generation is down" indistinguishable from
    "retrieval is down", which is exactly the distinction this rung exists
    to act on.
    """
    llama_ok = result.get("llama_server") == "ok"
    qdrant_result = result.get("qdrant")
    qdrant_ok = isinstance(qdrant_result, dict) and qdrant_result.get("count", 0) > 0
    return Readiness(
        generation_available=llama_ok,
        retrieval_available=qdrant_ok,
        cache_available=cache_available,
    )


def next_rung(readiness: Readiness, cache_hit: bool) -> Rung:
    """Which rung serves this request, given what is currently up.

    Order is fixed by charter V and does not change with the inputs: try the
    primary model; if generation itself is down or the spend-rate breaker
    has tripped, fall to a cached answer if this exact query has one;
    failing that, retrieval-only if retrieval is up; failing that, 503.

    "Cheaper model" (`Rung.CHEAPER`) becomes a reachable rung once Batch D
    gives the ladder a second generation arm to fall back to -- until then
    it is unreachable by construction (`readiness.generation_available`
    covers both), which is stated rather than silently absent.
    """
    if readiness.generation_available and not readiness.degraded:
        return Rung.PRIMARY
    if readiness.cache_available and cache_hit:
        return Rung.CACHED
    if readiness.retrieval_available:
        return Rung.RETRIEVAL_ONLY
    return Rung.UNAVAILABLE
