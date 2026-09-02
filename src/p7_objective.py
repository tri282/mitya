"""P7 Batch C -- CP-7a's objective: the assembled-prompt estimate (D-1).

D-1 chose `generate._estimated_cost_usd`'s quantity -- real assembled prompt,
completion held at the ceiling -- as P7's RANKING objective. Batch A could not
compute it: it had no per-query retrieval plumbing, so its trace row reported
the gate's reconstructed ceiling (`computed_cost_usd`) under `objective_source`
instead. This module is that missing plumbing.

`price_config()` reruns the SAME path `src/api/app.py`'s `/chat` serves for
every eval query -- route -> retrieve -> rerank -> build_context -> allocate
-> build_messages -- then prices the result with `_estimated_cost_usd()`. That
is what lets the objective SEE the five memory/allocation axes
`computed_cost_usd` reads as exactly 0.000 (G-P21, D-1 condition 4): a real
`memory_block` sits in the assembled messages, `computed_cost_usd` never
builds one.

Eval queries carry no real tenant memory state, so a fixed SYNTHETIC memory
fixture stands in for it -- the same fixture shape `scripts/probe_cost_instruments.py`
(P-20/P-21) already established and calibrated, reused rather than reinvented
(`_fixture_memory`/`_turns`, imported below).

Two numbers this module deliberately keeps apart, per D-1 condition 1:
`price_config()`'s `mean_usd`/`mean_prompt_tokens` (P7 RANKS on this) and
`gate_ceiling_usd()` (the gate BLOCKS on this, unchanged, imported read-only
from `src.rag.gate`). Nothing in `src/rag/gate.py` or `scripts/gate.py` is
imported for write, subclassed, or edited.

Held-out discipline (Batch B, A9): callers pass `query_ids` explicitly, or
this module defaults to `held_out_split.search_view(build_split()).ids` --
the search-side entry point Batch B built precisely so a search-side caller
structurally cannot obtain the held-out half. Never pass `held_out_ids` here.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient

from scripts.probe_cost_instruments import _fixture_memory, _turns  # noqa: E402 (namespace pkg, see optimizer.py)
from src import config_artifact as CA
from src.config import Config
from src.held_out_split import build_split, search_view
from src.limits import estimate_tokens
from src.memory import AllocationInputs, active_policy, context_budget_tokens
from src.rag.context import build_context
from src.rag.entities import EntityMap
from src.rag.gate import computed_cost_usd  # read-only: the gate's own ceiling, D-1 condition 1
from src.rag.generate import _estimated_cost_usd, build_messages
from src.rag.retrieve import (
    build_hard_filter,
    build_ranker,
    rerank,
    retrieve,
    retrieve_structural_excerpts,
)
from src.rag.router import route

logger = logging.getLogger(__name__)

RETRIEVAL_EVAL_PATH = os.path.join("test", "retrieval_eval.json")

# The retrieval cache's key fields -- the exact subset of `src.optimizer.ALLOWED`
# whose value can change route/retrieve/rerank's SCORED output, verified against
# src/rag/retrieve.py: `prefetch_limit` sets each leg's Qdrant prefetch depth
# (:121,147), `top_k_fused` sets the RRF fusion's output size (:159). The other
# three ALLOWED fields never reach retrieval: `top_k_rerank` only changes the
# FINAL SLICE of an already-scored list (:188, `rerank()`'s `top_k` param), and
# `memory_budget_tokens`/`allocation_policy` are read only by the allocator
# (src/memory.py), downstream of build_context. Named constants, not a comment,
# so `_check_retrieval_fields_cover_allowed` below is a loud, by-name build
# break -- same shape as `src.optimizer._check_domain_covers_allowed` -- if a
# field is ever added to ALLOWED without a retrieval-relevance placement here.
RETRIEVAL_AFFECTING_FIELDS: frozenset[str] = frozenset({"prefetch_limit", "top_k_fused"})
RETRIEVAL_IRRELEVANT_FIELDS: frozenset[str] = frozenset({
    "top_k_rerank", "memory_budget_tokens", "allocation_policy",
})


def _check_retrieval_fields_cover_allowed() -> None:
    """The retrieval cache's key fields and `src.optimizer.ALLOWED` must
    partition exactly, in both directions. A field left unplaced here would
    be invisible to the cache key: if it actually affects retrieval, the
    cache would silently serve STALE passages for candidates that vary only
    that field -- the exact failure mode the known-answer check
    (scripts/measure_retrieval_cache.py) exists to catch, one layer earlier."""
    from src.optimizer import ALLOWED  # local: avoids a p7_objective <-> optimizer import cycle

    overlap = RETRIEVAL_AFFECTING_FIELDS & RETRIEVAL_IRRELEVANT_FIELDS
    if overlap:
        raise AssertionError(f"field(s) placed in BOTH retrieval-relevance sets: {sorted(overlap)}")
    both = RETRIEVAL_AFFECTING_FIELDS | RETRIEVAL_IRRELEVANT_FIELDS
    missing = set(ALLOWED) - both
    if missing:
        raise AssertionError(
            f"ALLOWED field(s) with no retrieval-relevance placement -- classify each as "
            f"RETRIEVAL_AFFECTING_FIELDS or RETRIEVAL_IRRELEVANT_FIELDS "
            f"(src/p7_objective.py): {sorted(missing)}"
        )
    extra = both - set(ALLOWED)
    if extra:
        raise AssertionError(
            f"retrieval-relevance set names field(s) ALLOWED does not admit: {sorted(extra)}"
        )


_check_retrieval_fields_cover_allowed()

# Same synthetic memory fixture P-20/P-21's desk probe uses (scripts/
# probe_cost_instruments.py's own defaults) -- reused so the objective's
# memory content matches the instrument that already established the
# write-path-cap caveat (D-1 condition 4), rather than a second, uncalibrated
# fixture drifting from it.
FIXTURE_FACTS, FIXTURE_PREFS, FIXTURE_EPISODES, FIXTURE_TURNS = 10, 20, 20, 4

# Recent-turn SIZE, calibrated 2026-09-02, and it is not cosmetic.
#
# `probe_cost_instruments._turns()` builds a turn whose answer is
# "She waits, as the poor always wait." x4 -- 144 chars, ~36 tokens. That is
# the same class of artifact R131 caught in P6: "Batch B's 68 memory blocks
# existed only because FakeProvider replays a 67-character fixture."
# R131 [measured], against a LIVE provider: a single prior turn renders
# **738 and 708 tokens**. A ~36-token turn under-states a real one by ~20x.
#
# The consequence was measured here before this constant existed: with the
# short turns the whole fixture rendered 1031 tokens, BELOW `memory_budget_
# tokens`'s entire searchable domain (1024-3072, src/search.py FIELD_DOMAIN).
# So the axis holding A13's largest prize moved the objective by exactly
# 0.000 at 3072, 2048 AND 1024, and only moved at 512 -- a value the search
# can never propose. Batch C's "the objective sees the memory axes" proof used
# 3072 -> 512 and was therefore made with an out-of-domain value.
#
# 723 is the midpoint of R131's two measurements. At 4 turns that is ~2,892
# tokens of recent turns alone, which puts the fixture above the domain
# ceiling and makes the budget bind across the whole searchable range.
# Calibrated to a measurement, never to whatever makes the knob move.
FIXTURE_TURN_TOKENS = 723  # R131 [measured]: 738 and 708 for one live turn


def _calibrated_turns(n: int) -> list:
    """`probe_cost_instruments._turns()`'s shape at R131's measured SIZE.

    The probe's own `_turns()` is deliberately left alone: R134/R135 are
    recorded against it, and editing it would silently restate findings that
    were published under the short fixture.
    """
    from scripts.probe_cost_instruments import Turn

    answer = "She waits, as the poor always wait. " * (FIXTURE_TURN_TOKENS * 4 // 36)
    return [Turn(index=i, question="And Sonya?", answer=answer) for i in range(n)]


@dataclasses.dataclass
class _PricingContext:
    """Retrieval machinery, loaded once and reused across every candidate
    priced in one process -- amortising the fastembed/FlashRank model loads
    that dominate `open_pricing_context()`'s own cost. Safe across candidates
    because nothing here reads a cached serving config as a hidden
    dependency; `price_config()` sets `config_artifact._active` explicitly
    before every call and restores it after."""

    qdrant: QdrantClient
    embedder: object
    bm25: object
    ranker: object
    entities: EntityMap
    structure: dict
    summaries: dict
    retrieval_cache: dict = dataclasses.field(default_factory=dict)

    def close(self) -> None:
        self.qdrant.close()


@dataclasses.dataclass
class _PricingModels:
    """The deployment-fixed models (`embed_model`/`rerank_model` are excluded
    from the search space, §X.2) plus the static structure/summaries reads --
    none of it is per-candidate or Qdrant-lock-holding, so it is safe to load
    ONCE per process and reuse across many `open_pricing_context()` calls
    (Defect 1 fix, P7 search restructuring, 2026-09-02): only the `QdrantClient`
    below holds local-mode Qdrant's exclusive lock, and that is the one piece
    that must not outlive a single price/gate cycle."""

    embedder: object
    bm25: object
    ranker: object
    entities: EntityMap
    structure: dict
    summaries: dict


def load_pricing_models() -> _PricingModels:
    """The ~1.2s-of-model-loads part of what `open_pricing_context()` used to
    do on every open. Callers that price many candidates across several
    Qdrant open/close cycles in one process (`src.search.run_search` via
    `scripts/run_search.py`) load this once and pass it to every
    `open_pricing_context(models=...)` call."""
    logger.info("p7_objective: loading pricing models (embedder, bm25, ranker)")
    embedder = TextEmbedding(model_name=Config.EMBED_MODEL)
    bm25 = SparseTextEmbedding(model_name="Qdrant/bm25")
    ranker = build_ranker()
    entities = EntityMap()
    with open(Config.STRUCTURE_PATH, encoding="utf-8") as f:
        structure = json.load(f)
    with open(Config.SUMMARIES_PATH, encoding="utf-8") as f:
        summaries = json.load(f)
    return _PricingModels(embedder, bm25, ranker, entities, structure, summaries)


@contextlib.contextmanager
def open_pricing_context(models: _PricingModels | None = None,
                          retrieval_cache: dict | None = None):
    """Open a Qdrant client (and, if `models` is None, load the models too),
    price candidates, close the Qdrant client on exit.

    `models=None` (every caller before Defect 1) loads its own models for
    this one context, matching the original one-open-one-close behaviour --
    `scripts/measure_retrieval_cache.py`'s and `scripts/measure_per_point_cost.py`'s
    single-shot use is unchanged.

    A caller that opens and closes this context MANY times in one process
    (Defect 1: pricing must release the Qdrant lock before a gate run, so
    `scripts/run_search.py` now opens/closes once per candidate) passes its
    own `models` (loaded once via `load_pricing_models()`) and its own
    `retrieval_cache` dict, so neither the ~1.2s model loads nor the
    retrieval cache's hits are repaid on every open -- only the QdrantClient
    itself (a local mmap open, not a model load) is re-acquired per call.
    Closes the Qdrant client on exit either way -- local-mode Qdrant's
    exclusive lock on `data/vector_db/` must not outlive this context
    (`docs/RUNBOOK-P6.md:16`, CLAUDE.md's teardown rule).
    """
    logger.info("p7_objective: opening pricing context (qdrant%s)",
                "" if models is not None else ", embedder, bm25, ranker")
    if models is None:
        models = load_pricing_models()
    cache = retrieval_cache if retrieval_cache is not None else {}
    qdrant = QdrantClient(path=Config.VECTOR_DB_DIR)
    ctx = _PricingContext(qdrant, models.embedder, models.bm25, models.ranker,
                           models.entities, models.structure, models.summaries, cache)
    try:
        yield ctx
    finally:
        ctx.close()
        logger.info("p7_objective: pricing context closed (Qdrant lock released)")


def _retrieval_cache_key(cfg, query_id: str) -> tuple:
    """`(query_id, retrieval-affecting field values)` -- exactly
    `RETRIEVAL_AFFECTING_FIELDS`, sorted for a stable tuple. Two candidates
    that differ only in `RETRIEVAL_IRRELEVANT_FIELDS` collide on this key by
    construction and correctly share the cached retrieval pass."""
    return (query_id, tuple(sorted((f, getattr(cfg, f)) for f in RETRIEVAL_AFFECTING_FIELDS)))


def _cached_retrieval(ctx: _PricingContext, cfg, query_row: dict) -> tuple[str, list, dict | None]:
    """route -> retrieve -> rerank (UNSLICED), cached in `ctx.retrieval_cache`
    for the life of one search run. Returns the FULL FlashRank-scored list,
    not yet cut to `cfg.top_k_rerank` -- that slice is the one thing
    `top_k_rerank` changes (src/rag/retrieve.py:188) and it happens on every
    call, outside the cache, so `top_k_rerank` still prices correctly
    per-candidate even though it never appears in the cache key."""
    key = _retrieval_cache_key(cfg, query_row["id"])
    cached = ctx.retrieval_cache.get(key)
    if cached is not None:
        return cached

    filters = query_row.get("filters")
    query = query_row["query"]
    active_book = filters.get("book") if filters else None

    route_name, scope = route(query, filters, ctx.structure, ctx.summaries)
    expanded_query, _resolved = ctx.entities.resolve(query, active_book=active_book)

    if route_name == "STRUCTURAL" and scope is not None:
        points = retrieve_structural_excerpts(
            ctx.qdrant, ctx.embedder, query, scope["book"], scope["part"], scope["chapter"],
        )
    else:
        hard_filter = build_hard_filter(filters) if filters else None
        points = retrieve(ctx.qdrant, ctx.embedder, ctx.bm25, expanded_query, hard_filter)
        if not points and hard_filter is not None:
            # Matches app.py's fallback (design §IV.3): a hard filter that
            # matches nothing retries once unfiltered.
            points = retrieve(ctx.qdrant, ctx.embedder, ctx.bm25, expanded_query, None)
        # top_k=len(points): cache the WHOLE scored list, not app.py's final
        # slice -- see docstring.
        points = rerank(query, points, ctx.ranker, top_k=len(points))

    result = (route_name, points, scope)
    ctx.retrieval_cache[key] = result
    return result


def _assemble_messages(ctx: _PricingContext, cfg, query_row: dict) -> list[dict]:
    """route -> retrieve -> rerank -> build_context -> allocate -> build_messages,
    the same path `src/api/app.py`'s `/chat` serves (CP-7a's requirement:
    price the REAL assembled prompt, not the gate's reconstruction).

    Retrieval (route/retrieve/rerank-scoring) runs through `_cached_retrieval`,
    keyed on `RETRIEVAL_AFFECTING_FIELDS` only -- everything after the
    `top_k_rerank` slice below re-runs on every call, uncached, so it always
    reflects the CURRENT candidate's `top_k_rerank`/`memory_budget_tokens`/
    `allocation_policy`."""
    query = query_row["query"]
    route_name, points, scope = _cached_retrieval(ctx, cfg, query_row)
    if route_name != "STRUCTURAL":
        points = points[:cfg.top_k_rerank]

    passages_block, _sources = build_context(route_name, points, ctx.summaries, scope)

    facts_prefs, episodes = _fixture_memory(FIXTURE_FACTS, FIXTURE_PREFS, FIXTURE_EPISODES)
    plan = active_policy().allocate(AllocationInputs(
        tenant="p7-objective", session_id="p7-objective-1", query=query,
        budget_tokens=context_budget_tokens(), passages_block=passages_block,
        recent_turns=_calibrated_turns(FIXTURE_TURNS), profile_facts=facts_prefs, episodes=episodes,
    ))
    return build_messages(cfg.persona_head, passages_block, query, memory_block=plan.memory_block())


@dataclasses.dataclass(frozen=True)
class ObjectiveResult:
    """D-1's chosen number, aggregated over one config version's held-out
    search-half queries. `mean_usd`/`mean_prompt_tokens` is what `src/search.py`
    ranks candidates on -- never `total_usd`, which is only a sanity readout."""

    config_version: str
    n_queries: int
    mean_prompt_tokens: float
    mean_usd: float
    total_usd: float
    per_query: tuple[dict, ...]


def price_config(
    ctx: _PricingContext,
    version: str,
    query_ids: frozenset[str] | None = None,
    eval_path: str = RETRIEVAL_EVAL_PATH,
) -> ObjectiveResult:
    """D-1's objective for one config version, over `query_ids` (default: the
    WHOLE held-out split's SEARCH half -- `held_out_split.search_view()`,
    never the held-out half -- see module docstring)."""
    cfg = CA.load(version)
    if query_ids is None:
        query_ids = search_view(build_split()).ids

    with open(eval_path, encoding="utf-8") as f:
        rows = json.load(f)
    rows = [r for r in rows if r["id"] in query_ids]
    if not rows:
        raise ValueError(f"no eval rows matched query_ids for config {version!r}")

    # `config_artifact._active` is the process-lifetime cache every call above
    # reads through the `Config` facade (probe_cost_instruments.py's own
    # technique, reused): swap it in-process, never write config/serving/, and
    # restore it in `finally` so pricing several candidates in one process
    # never leaks one candidate's config into the next.
    prior_active, prior_version = CA._active, CA._active_version
    CA._active, CA._active_version = cfg, version
    try:
        per_query = []
        for row in rows:
            messages = _assemble_messages(ctx, cfg, row)
            prompt_tokens = sum(estimate_tokens(m.get("content", "") or "") for m in messages)
            usd = _estimated_cost_usd(messages, cfg.serving_model)
            per_query.append({"id": row["id"], "prompt_tokens": prompt_tokens, "usd": usd})
    finally:
        CA._active, CA._active_version = prior_active, prior_version

    n = len(per_query)
    mean_tokens = sum(r["prompt_tokens"] for r in per_query) / n
    mean_usd = sum(r["usd"] for r in per_query) / n
    total_usd = sum(r["usd"] for r in per_query)
    return ObjectiveResult(version, n, mean_tokens, mean_usd, total_usd, tuple(per_query))


def gate_ceiling_usd(version: str) -> float:
    """P-20 option (a) -- the number the gate blocks on, for the divergence
    check (D-1 condition 1). Read-only import of `src.rag.gate.computed_cost_usd`;
    nothing in `src/rag/gate.py` is edited, subclassed, or written to."""
    cfg = CA.load(version)
    manifest = CA.active_corpus_manifest()
    return computed_cost_usd(cfg, manifest)["usd"]
