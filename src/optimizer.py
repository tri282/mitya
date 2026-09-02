"""P7 Batch A -- the optimizer's walking skeleton (charter D9, schedule-p7.md §7 Batch A).

Four pieces, and nothing past them (Batch B/C/D territory -- the split, the
proposer proper, the budget, the schedule, step 3b's fence guard -- is not
built here):

  step 1    the search space: a default-deny allowlist of `ServingConfig`
            field names, admitted one at a time by a human, each entry
            recording D-5's three properties as data (`ALLOWED`).
  step 3(i) a proposer that emits exactly one hand-specified candidate and
            has no promotion path (`propose_one`, `assert_no_promotion_path`).
  step 5    route the proposal through the unchanged P3 gate
            (`run_gate_for_candidate`) -- imported, never edited, never
            subclassed, and its `promote` argument is always false.
  --        the search trace: one row per point (`TraceRow`).

D-5's admission rule, restated because it is the whole security property of
this module: "a field added to the artifact later fails the partition test
until a human places it, and until placed it is OUT." `ALLOWED` and
`EXCLUDED` are that placement, taken from `phase-7-audit.md` §X.1 (as
amended -- `prefetch_limit` moved in) and §X.6a, with §X.2/§X.3/§X.4 and
D-6's three placements supplying every exclusion reason. Nothing here
invents a member the audit did not place.
"""

from __future__ import annotations

import ast
import dataclasses
import glob
import json
import logging
import os
import re

from src.config_artifact import CorpusManifest, CURRENT_POINTER, ServingConfig

logger = logging.getLogger(__name__)


# --- step 1: the search space (D-5) -----------------------------------------

@dataclasses.dataclass(frozen=True)
class SpaceEntry:
    """D-5's three-property record for one allowlisted `ServingConfig` field.

    Data, not a comment in the field's defining module -- each property is a
    short citation to the audit row or decision that justifies it, checkable
    against the source it cites rather than trusted on the strength of prose.
    """

    p1_proposable: str
    p2_behavior_not_belief: str
    p3_instrument: str


# audit §X.1 (as amended -- `prefetch_limit` moved in from §X.5's "4 -> 3"
# update) plus §X.6a's "runnable today" table.
#
# `persona_head` is NOT here, and its absence is the point. §X.1 lists it with
# "step 3b fence-regression guard -- PRECONDITION" in its guard column, and
# step 3b is Batch D. D-5's P3 requires an instrument "with measured power",
# named in the entry; an entry citing a guard that does not exist fails P3 by
# definition, whatever its other merits. It is excluded below with its
# re-entry condition stated, the same shape D-6 gave
# `top_k_structural_excerpts`. Re-admitting it is a human's placement
# decision after Batch D lands, never automatic.
ALLOWED: dict[str, SpaceEntry] = {
    "top_k_rerank": SpaceEntry(
        p1_proposable="ServingConfig field, range 2-4; churns config_version only (§X.1)",
        p2_behavior_not_belief=(
            "passages reranked into the prompt -- the largest single token lever (§X.1)"
        ),
        p3_instrument=(
            "gate quality_1a (recall@5 non-inferiority per family) + retrieval-damage "
            "tripwire (§X.1)"
        ),
    ),
    "top_k_fused": SpaceEntry(
        p1_proposable="ServingConfig field, range 5-25; churns config_version only (§X.1, §X.6a)",
        p2_behavior_not_belief=(
            "latency, ~90ms/unit; rerank is 84.2% of retrieval (R34) -- NOT a token lever, "
            "so it is cost-flat under D-1's objective (CP-7i, unresolved, not Batch A's)"
        ),
        p3_instrument=(
            "pool recall per family, NOT recall@5 -- invariant to this knob by "
            "construction (§X.1)"
        ),
    ),
    "prefetch_limit": SpaceEntry(
        p1_proposable=(
            "ServingConfig field, range 50-200; churns config_version only "
            "(§X.5's '4 -> 3' update moved it into §X.1)"
        ),
        p2_behavior_not_belief=(
            "free in local mode -- Qdrant scores all 3,284 points regardless (§X.6a)"
        ),
        p3_instrument="pool recall; promotes on non-inferiority since it is free (§X.1)",
    ),
    "memory_budget_tokens": SpaceEntry(
        p1_proposable="ServingConfig field, range 1024-3072; churns config_version only (§X.1)",
        p2_behavior_not_belief=(
            "ceiling on memory tokens injected into the prompt -- 44% of context tokens, "
            "1.328x/session (R132)"
        ),
        p3_instrument=(
            "assembled-prompt estimate (D-1) + passages-never-displaced allocator "
            "invariant (§X.1)"
        ),
    ),
    "memory_episodic_max_per_tenant": SpaceEntry(
        p1_proposable="ServingConfig field, range 5-20; churns config_version only (§X.1)",
        p2_behavior_not_belief="size of the accumulating episodic tier -> cost (§X.1)",
        p3_instrument=(
            "assembled-prompt estimate (D-1) + non-inferiority guard (§X.1). D-1 residual "
            "risk, stated not decided: this is a write-path cap and reads 0.000 in every "
            "read-path instrument (R135) -- only the read-side effect (fewer rows stored, "
            "fewer injected) is what P3 actually measures."
        ),
    ),
    "memory_profile_max_facts": SpaceEntry(
        p1_proposable="ServingConfig field, range 5-10; churns config_version only (§X.1)",
        p2_behavior_not_belief="flat per-tenant cost (§X.1)",
        p3_instrument=(
            "assembled-prompt estimate (D-1) + non-inferiority guard (§X.1). Same D-1 "
            "write-path-cap caveat as memory_episodic_max_per_tenant."
        ),
    ),
    "memory_profile_max_prefs": SpaceEntry(
        p1_proposable="ServingConfig field, range 10-20; churns config_version only (§X.1)",
        p2_behavior_not_belief="flat per-tenant cost (§X.1)",
        p3_instrument=(
            "assembled-prompt estimate (D-1) + non-inferiority guard (§X.1). Same D-1 "
            "write-path-cap caveat."
        ),
    ),
    "allocation_policy": SpaceEntry(
        p1_proposable=(
            "ServingConfig field, values from src.memory.POLICIES; churns config_version "
            "only (§X.1)"
        ),
        p2_behavior_not_belief="swaps the allocator -- cost, per R128 -- NEVER quality (§X.1)",
        p3_instrument=(
            "retrieval-damage tripwire; the field never carries a quality-improvement "
            "claim (§X.1)"
        ),
    ),
}

# audit §X.2 (out, including D-6's three placements), §X.3 (records, not
# knobs), §X.4 (calibration, not a search axis) -- every remaining
# ServingConfig field, named with its reason. `partition_gaps()` below is
# what keeps this list honest as the artifact grows: a field that lands in
# neither dict fails the build, loudly, by name.
EXCLUDED: dict[str, str] = {
    "persona_head": (
        "OUT until Batch D. §X.1 lists it IN with step 3b's fence-regression guard as a "
        "PRECONDITION, not a caveat (phase-7-optimizer.md step 3b; A4). That guard does not "
        "exist yet, so no instrument holds the guard this axis must not break and D-5's P3 "
        "fails by definition. The judge covers persona at MDE 0.497 and covers the injection "
        "surface not at all (R133). RE-ENTRY: re-apply D-5's P1-P3 by hand once step 3b lands. "
        "D-2 is not a workaround for this -- it puts the cost-flat persona axis OUTSIDE the "
        "loop, it does not license admitting the field on an unbuilt guard"
    ),
    "embed_model": (
        "asserted equal to the corpus manifest's dense_model, not searched (§X.2, §II.3)"
    ),
    "rerank_model": (
        "[UNINSTRUMENTED] -- the gated metric is computed above the rerank line "
        "(§X.2, §IX.2)"
    ),
    "rerank_enabled": (
        "[UNINSTRUMENTED] -- the gated metric is computed above the rerank line "
        "(§X.2, §IX.2)"
    ),
    "max_tokens": (
        "G-E undecided and G-P8 open (no truncation detector); the gate's cost_layer also "
        "ERRORs on any max_tokens delta by construction (src/rag/gate.py:298-303) -- a "
        "candidate moving it cannot be scored (§X.2, Q13)"
    ),
    "ctx_size": (
        "deployment-shaped -- local arm pinned at 3072 by CLAUDE.md, hosted at 6144 (§X.2)"
    ),
    "serving_model": (
        "settled on cost by CP-beta; the expensive tier is unpriceable at rpd 20 (§X.2)"
    ),
    "summary_intent": (
        "STRUCTURAL n=12; a bad entry silently reintroduces the word-order defect "
        "(§X.2, §IV.2)"
    ),
    "gen_top_p": "never varied, never measured; no reason to believe it separates (§X.2)",
    "gen_temperature": (
        "not a search axis -- an instrument calibration to run first (§X.4), measured on "
        "within-cell variance, not the mean; CP-7b's question, not Batch A's"
    ),
    "memory_retention_days": (
        "[estimate] by construction -- no corpus timestamp exists to cross a window "
        "(§X.2, R122)"
    ),
    "prompt_composition": (
        "deferred v1 A/B decision; judge MDE 0.497 cannot arbitrate it -- G-P22, open, "
        "recommends deleting composition B rather than searching it (§X.2)"
    ),
    "extractor_prompt": (
        "[SECURITY] write path with no instrument; reconsider only after step 3b exists (§X.2)"
    ),
    "episodic_prompt": (
        "[SECURITY] write path with no instrument; reconsider only after step 3b exists (§X.2)"
    ),
    "top_k_structural_excerpts": (
        "Placed OUT by D-6 -- fails P3 on denominator: STRUCTURAL carries 4 of 50 quality "
        "rows and 10 of 121 retrieval rows. Named re-entry, not permanent: re-apply "
        "P1-P3 once phase-7-eval-structural.md lands its rows (§X.5)"
    ),
    "summarizer_model": (
        "Placed OUT by D-6 -- fails P2: write-side cost is amortized across a tenant's "
        "sessions, not paid per request (a selection, not a search axis, CP-beta "
        "precedent), and its current value is load-bearing for quota-window separation "
        "(quota_scope() is (provider, model); two ids dissolved R121's rpd squeeze) (§X.5)"
    ),
    "memory_value_max_chars": (
        "Placed OUT by D-6 -- fails P2: [COST][SECURITY], the only bound defeating "
        "paragraph-scale recovery (src/memory_store.py:93-104). Owner ruling: a hand "
        "change may lower it, never raise it (§X.5)"
    ),
    "book_title_aliases": (
        "record, not a knob (§X.3) -- an unmapped title becomes a hard filter matching "
        "zero points; curated data, [UNINSTRUMENTED], non-exhaustive by admission"
    ),
    "model_limits": (
        "record, not a knob (§X.3) -- describes the provider's quota; editing it changes "
        "what the limiter BELIEVES the quota is, not the quota itself"
    ),
    "unknown_model_limits": (
        "record, not a knob (§X.3) -- strict fallback tier, deliberately pessimistic"
    ),
    "no_tpm_limit": "record, not a knob (§X.3) -- sentinel 1e9 standing in for 'no limit'",
    "model_rates": (
        "record, not a knob (§X.3) and the sharpest case -- the multiplier in the cost "
        "objective itself; an optimizer permitted to edit it drives its objective to zero "
        "without touching a token"
    ),
}

# Instrument machinery scanned for D-5's instrument-integrity rule. A fixed
# list, named here rather than discovered, which is the staleness risk this
# module accepts explicitly (see `forbidden_instrument_fields`'s docstring):
# a new module that reads a config field into pricing, limits, or gate logic
# is invisible to the scan until it is added below.
_INSTRUMENT_SOURCES = (
    os.path.join("src", "pricing.py"),
    os.path.join("src", "limits.py"),
    os.path.join("src", "metrics.py"),
    os.path.join("src", "config.py"),
    os.path.join("src", "rag", "gate.py"),
    os.path.join("scripts", "gate.py"),
)

def forbidden_instrument_fields() -> frozenset[str]:
    """D-5's zero-trust set, derived from source rather than hand-maintained.

    Parses `_INSTRUMENT_SOURCES` -- the modules that actually price, limit,
    and gate a request -- and walks the AST for `Config.<NAME>` / `cls.<NAME>`
    attribute reads (`cls` catches `Config`'s own classmethods, e.g.
    `limits_for()` reading `cls.UNKNOWN_MODEL_LIMITS`). AST, not a text
    search, so a docstring or comment merely MENTIONING a field name (several
    do, e.g. `Config.TOP_K_FUSED` in src/config.py's own facade docstring)
    cannot make an ordinary proposable field look forbidden. Hits are
    lowercased and intersected against `ServingConfig`'s own field names. An
    optimizer may never author an input to its own objective, guard, or
    budget; this is the mechanical check, not a list somebody has to
    remember to update by hand when a new pricing table is added elsewhere.

    Staleness risk, named rather than hidden: `_INSTRUMENT_SOURCES` is a
    FIXED file list as of 2026-09-01. A future instrument module that reads a
    `ServingConfig` field through some other spelling (not `Config.<NAME>` or
    `cls.<NAME>`, e.g. a local alias or `getattr`) is invisible to this scan.
    """
    read: set[str] = set()
    for path in _INSTRUMENT_SOURCES:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in ("Config", "cls")):
                read.add(node.attr)
    fields = set(ServingConfig.model_fields)
    return frozenset(name.lower() for name in read if name.lower() in fields)


def partition_gaps(fields: set[str] | None = None) -> tuple[frozenset[str], frozenset[str]]:
    """(unplaced, in_both) against `ServingConfig.model_fields` -- empty/empty
    is the only passing state (D-5's partition-completeness test). A field
    added to the artifact later shows up here as unplaced until a human adds
    it to `ALLOWED` or `EXCLUDED`; until then it is OUT by construction,
    because it is not in `ALLOWED`."""
    fields = fields if fields is not None else set(ServingConfig.model_fields)
    allowed, excluded = set(ALLOWED), set(EXCLUDED)
    unplaced = frozenset(fields - allowed - excluded)
    in_both = frozenset(allowed & excluded)
    return unplaced, in_both


# --- step 3(i): the proposer --------------------------------------------------

# One hand-specified candidate (§X.1's stated range for top_k_rerank is 2-4;
# production defaults to 3). The walking skeleton's job is to prove the seam
# -- allowlist -> candidate -> gate -> trace -- not to pick a good candidate;
# Batch C's proposer proper picks candidates by CP-7d's chosen family.
HAND_SPECIFIED_OVERRIDE: dict[str, object] = {"top_k_rerank": 2}


def propose_one(base: ServingConfig, overrides: dict[str, object] | None = None) -> ServingConfig:
    """Exactly one candidate. Ranks; never promotes -- there is no code path
    here that can write `CURRENT_POINTER` (that is `promote_to()`, in
    `scripts/gate.py`, reachable only by a human passing `--promote` by hand).

    `overrides` must name only allowlisted fields -- P1's proposability check,
    enforced here rather than trusted to the caller, since an override of an
    excluded field is exactly the failure mode D-5 exists to make a build
    break rather than a silent edit.
    """
    overrides = overrides or {}
    not_allowed = set(overrides) - set(ALLOWED)
    if not_allowed:
        raise ValueError(
            f"not in the search space (not allowlisted): {sorted(not_allowed)}"
        )
    return base.model_copy(update=overrides)


def assert_no_promotion_path(pointer_before: str, pointer_after: str) -> None:
    """The batch's security property (D9), asserted on `CURRENT_POINTER`'s
    CONTENT, never on an exit code -- scripts/gate.py's own comment is the
    rule: 'an exit code says what a script decided, not what it did' (§5)."""
    if pointer_before != pointer_after:
        raise AssertionError(
            f"CURRENT_POINTER moved from {pointer_before!r} to {pointer_after!r} -- "
            f"the optimizer must never promote"
        )


# --- step 5: route through the unchanged P3 gate ------------------------------

def run_gate_for_candidate(candidate_version: str) -> dict:
    """`[RUN -- needs approval]`. Calls the unchanged P3 gate in-process --
    the same import `test/test_gate_promote.py` already uses (`scripts` is a
    namespace package; no `__init__.py` is added or needed) -- rather than a
    second subprocess-invocation mechanism gate.py's own `run_arm()` already
    owns for isolating its eval arms. Zero edits to `scripts/gate.py` or
    `src/rag/gate.py`; this only calls `main()`, the function every human
    invocation of the CLI already goes through.

    `promote` is hard-coded `False` here and NOWHERE ELSE in this module
    calls `p3_gate.main` -- `test/test_optimizer.py` greps this file's source
    to assert that literally, so the property survives a careless edit as a
    loud test failure rather than a silent one.
    """
    from scripts import gate as p3_gate  # local: importing this module opens no Qdrant client

    pointer_before = _read_pointer_text()
    exit_code = p3_gate.main(candidate_version, False)
    pointer_after = _read_pointer_text()
    assert_no_promotion_path(pointer_before, pointer_after)

    report = _latest_report_for(candidate_version)
    logger.info("gate exit=%s candidate=%s disposition=%s",
                exit_code, candidate_version, report.get("disposition"))
    return report


def _read_pointer_text() -> str:
    with open(CURRENT_POINTER, encoding="utf-8") as f:
        return f.read()


_REPORT_NAME_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]+\.json$")


def _latest_report_for(candidate_version: str) -> dict:
    from scripts import gate as p3_gate

    # `baseline-<version>.json` (gate.py:227) sits in the same directory and
    # matches a bare `*-<version>.json` glob -- and on a ZERO-CHANGE candidate
    # it is the file that wins, because the candidate hashes to production's
    # own version. So the known-answer case is exactly the case where the
    # loose glob picks a baseline instead of a report, and it fails with a
    # KeyError on a key the report does have. Match gate.py's report stamp
    # (`%Y%m%dT%H%M%SZ-<version>.json`, gate.py:255-256) instead of anything
    # ending in the version.
    pattern = os.path.join(p3_gate.GATE_DIR, f"*-{candidate_version}.json")
    reports = [p for p in glob.glob(pattern)
               if _REPORT_NAME_RE.match(os.path.basename(p))]
    if not reports:
        raise RuntimeError(f"no gate report found for candidate {candidate_version!r}")
    with open(max(reports, key=os.path.getmtime), encoding="utf-8") as f:
        return json.load(f)


# --- the search trace ----------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class TraceRow:
    """One row per point (step 5's fourth item).

    `objective_source` names which of P-20's three cost numbers produced
    `objective_usd`. D-1 chose the ASSEMBLED-PROMPT ESTIMATE
    (`generate._estimated_cost_usd`) as P7's ranking objective -- but that
    number needs a real assembled prompt (retrieved passages + memory block)
    per query, and Batch A's walking skeleton has no per-query retrieval
    plumbing (that is step 2's held-out split and step 7's search loop,
    Batch B/E territory). So this trace row reports the ONE number the
    unmodified gate produces at $0 with no retrieval-result plumbing of its
    own: `computed_cost_usd`, P-20 option (a), the reconstructed ceiling the
    gate already blocks on at tolerance zero. `objective_source` says so
    rather than mislabeling it as D-1's chosen number. `gate_blocks_on` is
    always the ceiling (D-1 condition 1's divergence sentence) -- for Batch A
    specifically the two happen to be the same field, and the row states that
    rather than leaving it implicit.
    """

    candidate_version: str
    production_version: str
    objective_usd: float | None
    objective_source: str
    gate_ceiling_usd: float | None
    gate_blocks_on: str
    disposition: str
    family_verdicts: dict[str, str]
    wall_clock_s: float
    usd_spent: float


def trace_row_from_report(report: dict, wall_clock_s: float) -> TraceRow:
    """Build one trace row from a gate report (`run_gate_for_candidate`'s
    return value). Pure -- no I/O, so it is testable against a hand-built
    report fixture without a gate run."""
    cost = report["layers"]["cost"][0]
    family_verdicts = {
        row["family"]: row["verdict"]
        for row in report["layers"]["quality_1a"]
        if row.get("metric") == "recall_at_5"
    }
    return TraceRow(
        candidate_version=report["arms"]["candidate"]["config_version"],
        production_version=report["arms"]["production"]["config_version"],
        objective_usd=cost.get("cand"),
        objective_source=(
            "gate_ceiling (computed_cost_usd, P-20 option a) -- Batch A has no per-query "
            "assembled prompt to price D-1's chosen objective; see TraceRow's docstring"
        ),
        gate_ceiling_usd=cost.get("cand"),
        gate_blocks_on="computed_cost_usd (reconstructed ceiling, tolerance 0, src/rag/gate.py:311)",
        disposition=report["disposition"],
        family_verdicts=family_verdicts,
        wall_clock_s=wall_clock_s,
        usd_spent=0.0,  # faucet 1 shut (D-1): no provider call anywhere in Batch A
    )


# --- corpus_version assertion -------------------------------------------------

def churns_only_config_version() -> bool:
    """No allowlisted field is also a `CorpusManifest` field -- a proposal in
    this space can never (by construction) touch the corpus identity."""
    return not (set(ALLOWED) & set(CorpusManifest.model_fields))
