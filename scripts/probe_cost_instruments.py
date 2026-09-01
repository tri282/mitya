#!/usr/bin/env python3
"""P-20/P-21 -- the three cost numbers, and what each axis moves.

[DESK -- $0.00, no provider call]. Reads the ACTIVE serving artifact, prices one
fixed synthetic request three ways, and perturbs each of the audit's §X.1
in-space axes to record the delta each number reports.

The three numbers, deliberately kept apart:

  1. `computed_cost_usd`      -- src/rag/gate.py:251. Reconstructed prompt.
  2. `_estimated_cost_usd`    -- src/rag/generate.py:281. Real assembled prompt.
  3. `mitya_answer_cost_usd`  -- src/metrics.py:300. Realised provider usage.

(3) cannot be produced here by construction: it is recorded from
`usage["prompt_tokens"]`/`["completion_tokens"]`, which only a provider response
carries. The probe reports that as UNCOMPUTABLE-OFFLINE rather than substituting
one of the other two, which is the whole point of the item.

Nothing is promoted: the perturbed config is installed into
`config_artifact._active` in-process only, and `config/serving/` is never
written.
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config_artifact as CA
from src.config import Config
from src.limits import estimate_tokens
from src.memory import (
    context_budget_tokens,
    AllocationInputs, Episode, ProfileFact, Turn, active_policy,
)
from src.pricing import cost_usd
from src.rag.gate import computed_cost_usd, _rough_tokens
from src.rag.generate import _estimated_cost_usd, build_messages

logger = logging.getLogger("probe_cost_instruments")

QUESTION = "Why does Raskolnikov confess?"
# One deterministic passage body of exactly `chunk_tokens * 4` characters, so
# the assembled prompt and the reconstructed prompt are comparable by
# construction rather than by luck.
PASSAGE_CHAR = "x"


def _fixture_memory(n_facts: int, n_prefs: int, n_eps: int):
    facts = [
        ProfileFact(kind="fact", key=f"fact_{i}", value="reader is studying the 1866 serialisation",
                    importance="medium", confidence="medium", permanent=False, count=1,
                    first_seen=0.0, last_confirmed=0.0, writer_model="probe")
        for i in range(n_facts)
    ]
    prefs = [
        ProfileFact(kind="preference", key=f"pref_{i}", value="prefers short answers with citations",
                    importance="medium", confidence="medium", permanent=False, count=1,
                    first_seen=0.0, last_confirmed=0.0, writer_model="probe")
        for i in range(n_prefs)
    ]
    eps = [
        Episode(session_id=f"s{i}", ended_at=float(i), turn_count=4,
                topics=("guilt", "confession"), chapters=(), writer_model="probe")
        for i in range(n_eps)
    ]
    return facts + prefs, eps


def _turns(n: int) -> list[Turn]:
    return [Turn(index=i, question="And Sonya?", answer="She waits, as the poor always wait." * 4)
            for i in range(n)]


def assemble(cfg, manifest, n_facts: int, n_prefs: int, n_eps: int, n_turns: int) -> dict:
    """Both computable numbers for one config, plus the token counts behind them."""
    CA._active = cfg  # in-process only; nothing is written to config/serving/

    passages = cfg.top_k_rerank if cfg.rerank_enabled else cfg.top_k_fused
    passages_block = "\n\n".join(PASSAGE_CHAR * (manifest.chunk_tokens * 4) for _ in range(passages))

    rows, eps = _fixture_memory(n_facts, n_prefs, n_eps)
    plan = active_policy().allocate(AllocationInputs(
        tenant="probe", session_id="probe-1", query=QUESTION,
        budget_tokens=context_budget_tokens(), passages_block=passages_block,
        recent_turns=_turns(n_turns), profile_facts=rows, episodes=eps,
    ))
    messages = build_messages(cfg.persona_head, passages_block, QUESTION,
                              memory_block=plan.memory_block())

    computed = computed_cost_usd(cfg, manifest)
    estimated = _estimated_cost_usd(messages, cfg.serving_model)
    est_prompt_tokens = sum(estimate_tokens(m.get("content", "") or "") for m in messages)
    return {
        "computed_usd": computed["usd"],
        "computed_prompt_tokens": computed["prompt_tokens"],
        "computed_completion_tokens": computed["completion_tokens"],
        "estimated_usd": estimated,
        "estimated_prompt_tokens": est_prompt_tokens,
        "estimated_completion_tokens": cfg.max_tokens,
        "memory_tokens": plan.tokens - estimate_tokens(passages_block) if plan.has_memory else 0,
        "memory_block_tokens": estimate_tokens(plan.memory_block()),
        "realised_usd": None,  # UNCOMPUTABLE-OFFLINE -- provider usage only
    }


def known_answer_check(cfg, manifest) -> None:
    """Both computable numbers reproduced by independent arithmetic.

    An instrument not checked against a known-answer case is not evidence. Each
    formula is re-derived here from the config alone; if either function changes
    shape this assertion fails rather than the matrix quietly reporting a
    different quantity.
    """
    rate = cfg.model_rates[cfg.serving_model]

    passages = cfg.top_k_rerank if cfg.rerank_enabled else cfg.top_k_fused
    hand_prompt = passages * manifest.chunk_tokens + len(cfg.persona_head) // 4
    hand_usd = (hand_prompt * rate["prompt"] + cfg.max_tokens * rate["completion"]) / 1_000_000
    got = computed_cost_usd(cfg, manifest)
    assert got["prompt_tokens"] == hand_prompt, (got["prompt_tokens"], hand_prompt)
    assert abs(got["usd"] - hand_usd) < 1e-12, (got["usd"], hand_usd)
    assert _rough_tokens(cfg.persona_head) == len(cfg.persona_head) // 4

    msgs = [{"role": "system", "content": "a" * 4000}, {"role": "user", "content": "b" * 400}]
    CA._active = cfg
    hand_est = ((1000 + 100) * rate["prompt"] + cfg.max_tokens * rate["completion"]) / 1_000_000
    assert abs(_estimated_cost_usd(msgs, cfg.serving_model) - hand_est) < 1e-12

    # And the third number's own arithmetic, on a KNOWN usage pair -- this is
    # the only part of the realised number reachable offline: the pricing, not
    # the counts.
    c = cost_usd(cfg.serving_model, 1000, 100)
    assert abs(c.usd - (1000 * rate["prompt"] + 100 * rate["completion"]) / 1_000_000) < 1e-12
    assert c.metered is rate["metered"]
    # The third number's RECORDING branch, which is not arithmetic at all.
    # `src/api/app.py:1173-1181` routes on `cost.metered`: an unmetered answer
    # goes to a counter, never to the histogram. Every row on the shipped price
    # card is `metered: false` (R48), so the branch is decidable offline even
    # though the number is not.
    from src.pricing import total_usd
    total = total_usd([cost_usd(cfg.serving_model, 2751, 462)])
    assert total.usd is not None and total.metered is False, total
    assert all(r["metered"] is False for r in cfg.model_rates.values())
    logger.info("known-answer check PASSED (3 formulas re-derived independently); "
                "mitya_answer_cost_usd branch on this config = record_unmetered_answer "
                "(cost.metered=False on every model_rates row) -- the histogram gets NOTHING")


# (field, baseline, perturbed) -- the audit §X.1 in-space axes that are
# existing ServingConfig fields. `persona_head` is perturbed by doubling it,
# which is the cheapest unambiguous token change.
AXES = [
    ("top_k_rerank", 3, 2),
    ("top_k_fused", 25, 5),
    ("prefetch_limit", 50, 200),
    ("memory_budget_tokens", 3072, 1024),
    ("memory_profile_max_facts", 10, 5),
    ("memory_profile_max_prefs", 20, 10),
    ("memory_episodic_max_per_tenant", 20, 5),
    ("allocation_policy", "passages_first", None),  # filled from POLICIES at runtime
    ("persona_head", None, None),                   # doubled at runtime
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--facts", type=int, default=10)
    ap.add_argument("--prefs", type=int, default=20)
    ap.add_argument("--episodes", type=int, default=20)
    ap.add_argument("--turns", type=int, default=4)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    base_cfg = CA.load(CA.read_pointer())
    manifest = CA.active_corpus_manifest()
    logger.info("config=%s corpus=%s chunk_tokens=%d serving_model=%s",
                base_cfg.version(), manifest.version(), manifest.chunk_tokens,
                base_cfg.serving_model)

    known_answer_check(base_cfg, manifest)

    kw = dict(n_facts=args.facts, n_prefs=args.prefs, n_eps=args.episodes, n_turns=args.turns)
    base = assemble(base_cfg, manifest, **kw)

    rows = []
    for field, _lo, hi in AXES:
        if field == "persona_head":
            new = base_cfg.persona_head * 2
        elif field == "allocation_policy":
            from src.memory import POLICIES
            others = [p for p in POLICIES if p != base_cfg.allocation_policy]
            if not others:
                rows.append({"axis": field, "note": "only one policy registered -- unperturbable",
                             "policies": list(POLICIES)})
                continue
            new = others[0]
        else:
            new = hi
        cand = base_cfg.model_copy(update={field: new})
        got = assemble(cand, manifest, **kw)
        rows.append({
            "axis": field,
            "from": getattr(base_cfg, field) if field != "persona_head" else f"{len(base_cfg.persona_head)} chars",
            "to": new if field != "persona_head" else f"{len(new)} chars",
            "d_computed_usd": round(got["computed_usd"] - base["computed_usd"], 9),
            "d_computed_prompt_tokens": got["computed_prompt_tokens"] - base["computed_prompt_tokens"],
            "d_estimated_usd": round(got["estimated_usd"] - base["estimated_usd"], 9),
            "d_estimated_prompt_tokens": got["estimated_prompt_tokens"] - base["estimated_prompt_tokens"],
            "d_realised_usd": "UNCOMPUTABLE-OFFLINE",
        })

    # `memory_budget_tokens` is a CEILING, so a single perturbation reports
    # 0.000 whenever the fixture's memory already fits under both values. Sweep
    # it so the binding point is visible rather than inferred.
    sweep = []
    for b in (3072, 2048, 1536, 1024, 768, 512, 256, 128):
        got = assemble(base_cfg.model_copy(update={"memory_budget_tokens": b}), manifest, **kw)
        sweep.append({"memory_budget_tokens": b,
                      "memory_block_tokens": got["memory_block_tokens"],
                      "estimated_prompt_tokens": got["estimated_prompt_tokens"],
                      "estimated_usd": round(got["estimated_usd"], 9),
                      "computed_usd": got["computed_usd"]})

    CA._active = base_cfg
    out = {"memory_budget_sweep": sweep, "baseline": base, "config_version": base_cfg.version(),
           "corpus_version": manifest.version(), "axes": rows}
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
