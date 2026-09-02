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

# Same synthetic memory fixture P-20/P-21's desk probe uses (scripts/
# probe_cost_instruments.py's own defaults) -- reused so the objective's
# memory content matches the instrument that already established the
# write-path-cap caveat (D-1 condition 4), rather than a second, uncalibrated
# fixture drifting from it.
FIXTURE_FACTS, FIXTURE_PREFS, FIXTURE_EPISODES, FIXTURE_TURNS = 10, 20, 20, 4


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

    def close(self) -> None:
        self.qdrant.close()


@contextlib.contextmanager
def open_pricing_context():
    """Open once, price many candidates, close once.

    Embed/rerank models are deployment-fixed (`embed_model`/`rerank_model`
    are excluded from the search space, §X.2) so loading them from the
    CURRENTLY active config before any candidate is priced is correct for
    every candidate in the run. Closes the Qdrant client on exit -- local-mode
    Qdrant's exclusive lock on `data/vector_db/` must not outlive this
    context (`docs/RUNBOOK-P6.md:16`, CLAUDE.md's teardown rule).
    """
    logger.info("p7_objective: opening pricing context (embedder, bm25, ranker, qdrant)")
    embedder = TextEmbedding(model_name=Config.EMBED_MODEL)
    bm25 = SparseTextEmbedding(model_name="Qdrant/bm25")
    ranker = build_ranker()
    entities = EntityMap()
    with open(Config.STRUCTURE_PATH, encoding="utf-8") as f:
        structure = json.load(f)
    with open(Config.SUMMARIES_PATH, encoding="utf-8") as f:
        summaries = json.load(f)
    qdrant = QdrantClient(path=Config.VECTOR_DB_DIR)
    ctx = _PricingContext(qdrant, embedder, bm25, ranker, entities, structure, summaries)
    try:
        yield ctx
    finally:
        ctx.close()
        logger.info("p7_objective: pricing context closed (Qdrant lock released)")


def _assemble_messages(ctx: _PricingContext, cfg, query_row: dict) -> list[dict]:
    """route -> retrieve -> rerank -> build_context -> allocate -> build_messages,
    the same path `src/api/app.py`'s `/chat` serves (CP-7a's requirement:
    price the REAL assembled prompt, not the gate's reconstruction)."""
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
        points = rerank(query, points, ctx.ranker)  # app.py reranks unconditionally on DEFAULT

    passages_block, _sources = build_context(route_name, points, ctx.summaries, scope)

    facts_prefs, episodes = _fixture_memory(FIXTURE_FACTS, FIXTURE_PREFS, FIXTURE_EPISODES)
    plan = active_policy().allocate(AllocationInputs(
        tenant="p7-objective", session_id="p7-objective-1", query=query,
        budget_tokens=context_budget_tokens(), passages_block=passages_block,
        recent_turns=_turns(FIXTURE_TURNS), profile_facts=facts_prefs, episodes=episodes,
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
