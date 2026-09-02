"""P7 Batch C -- the proposer proper, the budget enforcer, the cost-first
funnel (schedule-p7.md §7 Batch C, steps 3(ii) + 4; D-7's family/budget/funnel).

Three pieces, built on Batch A's seam (`src.optimizer`) and Batch C's own
objective (`src.p7_objective`, CP-7a/D-1):

  Family        `next_point(trace, base, rng)` -- "given the trace so far,
                emit the next candidate", D-7's own words. Evolutionary
                mutation (D-7a's decided default) plus grid/random and a
                minimal surrogate behind the SAME seam, for the family
                comparison D-7a still calls for.
  Budget        wall-clock and points, never USD (D-7c: the objective costs
                $0.00, so a USD cap caps nothing). Enforced BEFORE spend
                (`assert_affordable` refuses to start; `check_before_point`
                gates every gate run), never reconciled after.
  run_search    mutate -> price with the objective (free, deterministic,
                D-7d) -> only a candidate that actually got cheaper consumes
                a gate run. `price_fn`/`gate_runner`/`save_fn`/`clock` are all
                injected (limits.py's own idiom: `RateLimiter`'s clock/sleep
                are injectable for the same reason) -- CP-7e's fault test
                substitutes a fake gate call and a fake objective, at $0.00,
                with no Qdrant opened at all.

The proposer never promotes: no code path here can write `CURRENT_POINTER`
(`optimizer.assert_no_promotion_path`, asserted on the pointer's CONTENT,
never on an exit code -- Batch A's property, unchanged).
"""

from __future__ import annotations

import dataclasses
import logging
import random
import time
from collections.abc import Callable
from typing import Protocol

from src import config_artifact as CA
from src.config_artifact import ServingConfig
from src.optimizer import ALLOWED, propose_one

logger = logging.getLogger(__name__)


# --- the search space's mutation domain --------------------------------------
#
# D-5's allowlist (`src.optimizer.ALLOWED`) records WHY a field is
# proposable; it does not record what values are legal to try. A field
# admitted to `ALLOWED` without an entry here is unmutable by construction --
# `_check_domain_covers_allowed()` below makes that a loud build break rather
# than a silently-never-searched axis, D-5's own failure mode applied one
# layer down.

# (lo, hi) inclusive for an integer field; `None` for `allocation_policy`,
# whose domain is `src.memory.POLICIES` (read at mutation time so a third
# policy registered later needs no edit here).
FIELD_DOMAIN: dict[str, tuple[int, int] | None] = {
    "top_k_rerank": (2, 4),
    "top_k_fused": (5, 25),
    "prefetch_limit": (50, 200),
    "memory_budget_tokens": (1024, 3072),
    "allocation_policy": None,
}


def _check_domain_covers_allowed() -> None:
    """The domain and the allowlist must agree EXACTLY, in both directions.

    Covering-only was the original check and it is not enough: every proposer
    draws its field from `FIELD_DOMAIN`, so a name left here after leaving
    `ALLOWED` stays mutable -- the allowlist stops being the authority on what
    the search may touch, which is the one property D-5 exists to guarantee.
    That is not hypothetical: D-1 item 4 removed three write-path caps on
    2026-09-02 and this check, as written, would have kept proposing them.
    """
    missing = set(ALLOWED) - set(FIELD_DOMAIN)
    if missing:
        raise AssertionError(
            f"ALLOWED field(s) with no mutation domain -- unmutable by "
            f"construction, place one in FIELD_DOMAIN: {sorted(missing)}"
        )
    extra = set(FIELD_DOMAIN) - set(ALLOWED)
    if extra:
        raise AssertionError(
            f"FIELD_DOMAIN names field(s) the allowlist does not admit -- the "
            f"search would mutate them anyway: {sorted(extra)}"
        )


_check_domain_covers_allowed()


def _mutate_field(base: ServingConfig, field: str, rng: random.Random) -> object:
    """One new legal value for `field`, different from `base`'s current value
    where a different one exists."""
    domain = FIELD_DOMAIN[field]
    current = getattr(base, field)
    if domain is None:  # allocation_policy
        from src.memory import POLICIES
        choices = [p for p in POLICIES if p != current] or [current]
        return rng.choice(choices)
    lo, hi = domain
    choices = [v for v in range(lo, hi + 1) if v != current]
    return rng.choice(choices) if choices else current


@dataclasses.dataclass(frozen=True)
class Point:
    """One mutation a family proposes: `field` set to `value`, on top of
    whatever config the caller applies it to."""

    field: str
    value: object


class Family(Protocol):
    """The seam every search family implements identically (D-7's own
    framing): given the trace so far, emit the next candidate. Q1's O6
    comparison -- grid/random, surrogate, evolutionary -- is a comparison of
    implementations of this one method, never a build-time fork."""

    name: str

    def next_point(self, trace: list["SearchRow"], base: ServingConfig,
                    rng: random.Random) -> Point: ...


class EvolutionaryFamily:
    """D-7a's decided default: mutate ONE allowlisted field at a time from
    the best-known-good config so far (the last improving, non-blocked
    candidate), keep improvements, iterate. One readable change per step --
    the shape the search trace was designed to record."""

    name = "evolutionary"

    def next_point(self, trace: list["SearchRow"], base: ServingConfig,
                    rng: random.Random) -> Point:
        field = rng.choice(sorted(ALLOWED))
        return Point(field, _mutate_field(base, field, rng))


class GridRandomFamily:
    """O6(a): a field and a value drawn uniformly at random from its domain,
    ignoring the trace entirely -- the loop's own "keep improvements" state
    (`production`, shared across every family so the comparison holds the
    rest of the harness fixed) is what accumulates; this family only picks
    WHERE to mutate next. Q1's comparison arm; never P7's default (D-7a)."""

    name = "grid_random"

    def next_point(self, trace: list["SearchRow"], base: ServingConfig,
                    rng: random.Random) -> Point:
        field = rng.choice(sorted(ALLOWED))
        return Point(field, _mutate_field(base, field, rng))


class SurrogateFamily:
    """O6(b): the sample-efficient choice when points are expensive -- D-7a's
    own reasoning for why it is NOT the default here (points are $0.00 and
    ~30s, which removes the surrogate's only advantage). Minimal by design:
    ranks fields by the mean objective delta observed so far and mutates the
    field with the best (most negative) mean, falling back to a random field
    with no history yet."""

    name = "surrogate"

    def next_point(self, trace: list["SearchRow"], base: ServingConfig,
                    rng: random.Random) -> Point:
        deltas: dict[str, list[float]] = {}
        for row in trace:
            if row.gate_ceiling_usd is not None:  # only points that reached the gate
                deltas.setdefault(row.mutated_field, []).append(row.objective_usd)
        if not deltas:
            field = rng.choice(sorted(ALLOWED))
        else:
            field = min(deltas, key=lambda f: sum(deltas[f]) / len(deltas[f]))
        return Point(field, _mutate_field(base, field, rng))


# --- the search trace ---------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class SearchRow:
    """One point in Batch C's search trace. Unlike Batch A's `TraceRow`
    (which had no per-query retrieval plumbing), `objective_usd` here is D-1's
    REAL chosen number -- CP-7a's assembled-prompt estimate -- and
    `gate_ceiling_usd` is the number the gate actually blocks on. Both are
    always named (D-1 condition 1: never silent about which number produced
    which value), and `divergence_line()` prints the two side by side on
    every row, including a candidate the cost-first funnel screened out
    before any gate run (`gate_ceiling_usd is None` -- no gate run happened,
    stated as fact rather than a missing field)."""

    candidate_version: str
    production_version: str
    mutated_field: str
    mutated_from: object
    mutated_to: object
    objective_usd: float
    objective_prompt_tokens: float
    passed_cost_first_funnel: bool
    objective_source: str = "assembled_prompt_estimate (generate._estimated_cost_usd, D-1)"
    gate_ceiling_usd: float | None = None
    gate_blocks_on: str = "computed_cost_usd (reconstructed ceiling, tolerance 0, src/rag/gate.py:311)"
    disposition: str | None = None
    family_verdicts: dict[str, str] = dataclasses.field(default_factory=dict)
    wall_clock_s: float = 0.0
    usd_spent: float = 0.0  # faucet 1 shut (D-1): no provider call anywhere in this loop

    @property
    def ranked_first_but_blocked(self) -> bool:
        """D-1's named collision: cheaper on the objective P7 ranks on, but
        the gate's zero-tolerance ceiling still blocks it."""
        return self.passed_cost_first_funnel and self.disposition == "blocks"

    def divergence_line(self) -> str:
        """Printed on every row (D-1 condition 1) -- never silent."""
        if not self.passed_cost_first_funnel:
            return (
                f"objective_usd={self.objective_usd:.6f} did not improve on the running "
                f"best -- screened out by the cost-first funnel (D-7d), no gate run"
            )
        blocked = self.disposition == "blocks"
        line = (
            f"objective_usd={self.objective_usd:.6f} ({self.objective_source}) vs "
            f"gate_ceiling_usd={self.gate_ceiling_usd:.6f} ({self.gate_blocks_on}) -- "
            f"disposition={self.disposition}"
        )
        if blocked:
            line += " -- RANKS BETTER ON THE OBJECTIVE BUT BLOCKED BY THE GATE"
        return line


# --- the budget enforcer (D-7c) -----------------------------------------------

class BudgetExceeded(RuntimeError):
    """Raised BEFORE a point is spent, never after (D-7c: the invariant is
    'enforced before the call, never reconciled after')."""


@dataclasses.dataclass
class Budget:
    """Wall-clock and points, never USD (D-7c: this objective's USD is
    exactly 0.00, so a USD cap caps nothing)."""

    max_wall_clock_s: float
    max_points: int

    def assert_affordable(self, per_point_wall_clock_s: float) -> None:
        """The standing known-answer case (schedule §5, CP-7e): a cap below
        the MEASURED cost of one point refuses to start outright, rather than
        running one point and stopping. Called once, before the loop opens."""
        if per_point_wall_clock_s <= 0:
            raise ValueError(
                f"per_point_wall_clock_s must be a real measurement, got "
                f"{per_point_wall_clock_s} (CP-7d: 'no cap from a cost that was never measured')"
            )
        if self.max_points < 1:
            raise BudgetExceeded(
                f"point cap {self.max_points} cannot afford a single point -- refusing to start"
            )
        if self.max_wall_clock_s < per_point_wall_clock_s:
            raise BudgetExceeded(
                f"wall-clock cap {self.max_wall_clock_s}s cannot afford one point "
                f"(measured at {per_point_wall_clock_s:.1f}s) -- refusing to start"
            )

    def check_before_point(self, points_spent: int, wall_clock_spent_s: float,
                            per_point_wall_clock_s: float) -> None:
        """The enforcement point: called immediately BEFORE a gate run would
        spend, never after. A point whose spend WOULD exceed the cap is
        refused before it starts."""
        if points_spent >= self.max_points:
            raise BudgetExceeded(f"point cap reached: {points_spent}/{self.max_points}")
        if wall_clock_spent_s + per_point_wall_clock_s > self.max_wall_clock_s:
            raise BudgetExceeded(
                f"next point (~{per_point_wall_clock_s:.1f}s) would exceed the wall-clock "
                f"cap ({wall_clock_spent_s:.1f}s spent of {self.max_wall_clock_s}s)"
            )


# --- the search loop: mutate -> price -> (funnel) -> gate (D-7d) -------------

def run_search(
    *,
    budget: Budget,
    price_fn: Callable[[str], object],
    gate_runner: Callable[[str], dict],
    base_config: ServingConfig,
    base_version: str,
    per_point_wall_clock_s: float,
    family: Family | None = None,
    seed: int = 0,
    save_fn: Callable[[ServingConfig], str] = CA.save,
    clock: Callable[[], float] = time.monotonic,
) -> list[SearchRow]:
    """Batch C's search loop.

    `price_fn(version) -> object with .mean_usd/.mean_prompt_tokens`
    (`src.p7_objective.price_config`, partially applied over an open pricing
    context, in real use) and `gate_runner(version) -> gate report dict`
    (`src.optimizer.run_gate_for_candidate` in real use) are both injected --
    CP-7e's fault test substitutes fakes for both and never opens Qdrant.

    Never promotes: `save_fn` (`config_artifact.save`) only ever writes a
    versioned config artifact, never `CURRENT_POINTER`; nothing here calls
    `scripts.gate.main(..., promote=True)`.
    """
    family = family or EvolutionaryFamily()
    rng = random.Random(seed)
    budget.assert_affordable(per_point_wall_clock_s)  # refuse to start, before anything runs

    production = base_config
    baseline = price_fn(base_version)
    best_usd = baseline.mean_usd

    rows: list[SearchRow] = []
    t0 = clock()
    points_spent = 0
    last_candidate_version: str | None = None
    stall = 0

    while True:
        elapsed_total = clock() - t0
        if elapsed_total >= budget.max_wall_clock_s:
            logger.info("run_search: wall-clock cap reached (%.1fs)", elapsed_total)
            break
        if points_spent >= budget.max_points:
            logger.info("run_search: point cap reached (%d)", points_spent)
            break

        point = family.next_point(rows, production, rng)
        candidate = propose_one(production, {point.field: point.value})
        cand_version = save_fn(candidate)

        # Injected failure mode (CP-7e): a family that keeps re-emitting the
        # same candidate. Caught before it consumes a gate run OR spins the
        # wall-clock check above forever on a family that never changes state.
        if cand_version == last_candidate_version:
            stall += 1
            if stall >= 2:
                rows.append(SearchRow(
                    cand_version, base_version, point.field,
                    getattr(production, point.field), point.value,
                    objective_usd=float("nan"), objective_prompt_tokens=float("nan"),
                    passed_cost_first_funnel=False, disposition="duplicate_candidate_stall",
                ))
                logger.warning("run_search: duplicate-candidate stall -- stopping")
                break
        else:
            stall = 0
        last_candidate_version = cand_version

        # Cost-first funnel (D-7d): price for free, before any gate run.
        priced = price_fn(cand_version)
        passed_funnel = priced.mean_usd < best_usd
        if not passed_funnel:
            rows.append(SearchRow(
                cand_version, base_version, point.field,
                getattr(production, point.field), point.value,
                objective_usd=priced.mean_usd, objective_prompt_tokens=priced.mean_prompt_tokens,
                passed_cost_first_funnel=False,
            ))
            continue  # D-7d: no gate run consumed -- not a budgeted point

        # Enforcement point: BEFORE the call, never reconciled after (D-7c).
        try:
            budget.check_before_point(points_spent, clock() - t0, per_point_wall_clock_s)
        except BudgetExceeded as exc:
            logger.info("run_search: budget stop before point %d -- %s", points_spent + 1, exc)
            break

        t_point = clock()
        try:
            report = gate_runner(cand_version)
        except Exception as exc:  # a forced gate ERROR (CP-7e) -- fail closed, stop the loop
            elapsed = clock() - t_point
            points_spent += 1
            rows.append(SearchRow(
                cand_version, base_version, point.field,
                getattr(production, point.field), point.value,
                objective_usd=priced.mean_usd, objective_prompt_tokens=priced.mean_prompt_tokens,
                passed_cost_first_funnel=True, disposition="error",
                wall_clock_s=elapsed,
            ))
            logger.error("run_search: gate raised %r -- stopping (fail closed, A7)", exc)
            break
        elapsed = clock() - t_point
        points_spent += 1

        cost_layer = report["layers"]["cost"][0]
        disposition = report["disposition"]
        family_verdicts = {
            r["family"]: r["verdict"] for r in report["layers"]["quality_1a"]
            if r.get("metric") == "recall_at_5"
        }
        row = SearchRow(
            cand_version, base_version, point.field,
            getattr(production, point.field), point.value,
            objective_usd=priced.mean_usd, objective_prompt_tokens=priced.mean_prompt_tokens,
            passed_cost_first_funnel=True,
            gate_ceiling_usd=cost_layer.get("cand"), disposition=disposition,
            family_verdicts=family_verdicts, wall_clock_s=elapsed,
        )
        rows.append(row)
        logger.info("run_search: point %d %s", points_spent, row.divergence_line())

        if disposition != "blocks":
            best_usd = priced.mean_usd
            production = candidate  # evolutionary: keep the improvement

    return rows
