#!/usr/bin/env python3
"""P7 Batch C -- known-answer check for the retrieval cache
(src/p7_objective.py's `_cached_retrieval`). `[RUN -- needs approval]`.

Three properties, each asserted with EXACT equality/inequality, not
approximate -- a cache that changes a number is a silent correctness bug
that looks exactly like a speedup:

  1. cold vs warm, SAME config -- per-query `prompt_tokens`/`usd` identical.
  2. a candidate that changes a RETRIEVAL-AFFECTING field (`top_k_fused`) --
     the cache must MISS (new entries in `ctx.retrieval_cache`), never reuse
     stale passages.
  3. a candidate that changes a RETRIEVAL-IRRELEVANT field (`top_k_rerank`) --
     the cache must HIT (zero new entries) AND the objective must still
     DIFFER from the base config, proving the speedup did not flatten the
     signal Batch C exists to expose.

Needs real Qdrant (local-mode, in-process) -- not part of the zero-spend
suite (test/test_p7_objective.py), same split Batch A/C already draw
between plain-assert unit tests and `[RUN]` scripts.
"""

import logging
import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import config_artifact as CA  # noqa: E402
from src.held_out_split import build_split, search_view  # noqa: E402
from src.optimizer import propose_one  # noqa: E402
from src.p7_objective import open_pricing_context, price_config  # noqa: E402

logger = logging.getLogger(__name__)

N_QUERIES = 8  # small subset -- runnable in a couple minutes of real Qdrant + FlashRank


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    pointer_before = CA.read_pointer()
    base_version = pointer_before
    base_cfg = CA.load(base_version)
    ids = sorted(search_view(build_split()).ids)[:N_QUERIES]
    logger.info("known-answer check on %d queries: %s", len(ids), ids)

    # A candidate on the RETRIEVAL-AFFECTING axis (top_k_fused): must MISS.
    new_top_k_fused = 10 if base_cfg.top_k_fused != 10 else 15
    affecting_cfg = propose_one(base_cfg, {"top_k_fused": new_top_k_fused})
    affecting_version = CA.save(affecting_cfg)

    # A candidate on a RETRIEVAL-IRRELEVANT axis: must HIT. `top_k_rerank`, not
    # `memory_budget_tokens` -- checked empirically first: the fixture memory
    # content (FIXTURE_FACTS/PREFS/EPISODES/TURNS, p7_objective.py) renders to
    # ~960 tokens total, under the WHOLE (1024, 3072) legal domain for
    # memory_budget_tokens, so no legal candidate value ever truncates it --
    # any memory_budget_tokens candidate would be a false negative for
    # "objective still differs" here, not a cache bug, just a fixture too small
    # to exercise that particular axis. `top_k_rerank` has no such gap: it
    # changes the passage-count slice (src/rag/retrieve.py:188) on every call,
    # so it is guaranteed to move `prompt_tokens` while never touching the
    # cache key -- the sharper test of this cache's actual design (the reason
    # `rerank()` grew a `top_k` parameter at all).
    new_top_k_rerank = 2 if base_cfg.top_k_rerank != 2 else 4
    irrelevant_cfg = propose_one(base_cfg, {"top_k_rerank": new_top_k_rerank})
    irrelevant_version = CA.save(irrelevant_cfg)

    with open_pricing_context() as ctx:
        # --- 1. cold vs warm, same config -----------------------------------
        t0 = time.monotonic()
        cold = price_config(ctx, base_version, query_ids=frozenset(ids))
        t1 = time.monotonic()
        cold_s = t1 - t0
        cache_after_cold = len(ctx.retrieval_cache)

        t2 = time.monotonic()
        warm = price_config(ctx, base_version, query_ids=frozenset(ids))
        t3 = time.monotonic()
        warm_s = t3 - t2
        cache_after_warm = len(ctx.retrieval_cache)

        assert cold.per_query == warm.per_query, (
            f"cold != warm for the SAME config -- cache changed a number:\n"
            f"cold={cold.per_query}\nwarm={warm.per_query}"
        )
        assert cold.mean_usd == warm.mean_usd
        assert cold.mean_prompt_tokens == warm.mean_prompt_tokens
        assert cache_after_warm == cache_after_cold, (
            f"warm pass grew the cache ({cache_after_cold} -> {cache_after_warm}) -- "
            f"it recomputed instead of hitting"
        )

        # --- 2. retrieval-affecting axis: must MISS -------------------------
        affecting = price_config(ctx, affecting_version, query_ids=frozenset(ids))
        cache_after_affecting = len(ctx.retrieval_cache)
        assert cache_after_affecting > cache_after_warm, (
            f"top_k_fused candidate did not add cache entries "
            f"({cache_after_warm} -> {cache_after_affecting}) -- the cache is reusing "
            f"passages from a DIFFERENT top_k_fused, i.e. stale"
        )

        # --- 3. retrieval-irrelevant axis: must HIT, objective still differs -
        irrelevant = price_config(ctx, irrelevant_version, query_ids=frozenset(ids))
        cache_after_irrelevant = len(ctx.retrieval_cache)
        assert cache_after_irrelevant == cache_after_affecting, (
            f"top_k_rerank candidate grew the cache "
            f"({cache_after_affecting} -> {cache_after_irrelevant}) -- it should have hit "
            f"the base config's cached retrieval entirely"
        )
        assert irrelevant.mean_prompt_tokens != cold.mean_prompt_tokens, (
            "top_k_rerank candidate priced IDENTICALLY to the base config -- "
            "the cache flattened the very signal Batch C measures"
        )

    pointer_after = CA.read_pointer()
    assert pointer_after == pointer_before, (
        f"CURRENT_POINTER moved ({pointer_before!r} -> {pointer_after!r}) -- nothing here "
        f"may promote"
    )

    print(f"\nqueries priced per pass: {len(ids)}")
    print(f"base config version: {base_version}")
    print(f"cold pass:  {cold_s:.2f}s ({cold_s / len(ids):.2f}s/query), "
          f"mean_usd={cold.mean_usd:.6f}, mean_prompt_tokens={cold.mean_prompt_tokens:.1f}")
    print(f"warm pass:  {warm_s:.2f}s ({warm_s / len(ids):.2f}s/query), "
          f"mean_usd={warm.mean_usd:.6f}, mean_prompt_tokens={warm.mean_prompt_tokens:.1f}")
    print(f"speedup (cold/warm wall-clock): {cold_s / warm_s:.1f}x")
    print(f"cold == warm per-query (exact equality): PASS")
    print(f"cache entries: after cold={cache_after_cold}, after warm={cache_after_warm} "
          f"(+0 -- hit), after top_k_fused candidate={cache_after_affecting} "
          f"(+{cache_after_affecting - cache_after_warm} -- miss, as expected)")
    print(f"top_k_fused candidate ({base_cfg.top_k_fused} -> {new_top_k_fused}): "
          f"mean_usd={affecting.mean_usd:.6f}, mean_prompt_tokens={affecting.mean_prompt_tokens:.1f}")
    print(f"top_k_rerank candidate ({base_cfg.top_k_rerank} -> {new_top_k_rerank}): "
          f"mean_usd={irrelevant.mean_usd:.6f}, mean_prompt_tokens={irrelevant.mean_prompt_tokens:.1f} "
          f"(cache HIT, objective still differs from base: PASS)")
    print(f"CURRENT_POINTER unchanged: {pointer_before} -> {pointer_after}: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
