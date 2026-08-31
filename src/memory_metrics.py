"""Step 9: the multi-turn success metric (v2 P6 Batch B).

**This module implements family (c) and nothing else, and that is a finding,
not a shortcut.** `phase-6-memory.md`'s O4 row names three candidate families;
**R120** measured which of them the frozen corpus can actually feed:

  * **(a) task success on scripted multi-turn goals** -- no gold. All 108 turns
    of `test/session_corpus.json` v`4155c97b0a2b` carry the key set `{query}`
    and nothing else; there is no goal, no expected action, no success
    predicate anywhere in the file.
  * **(b) answer quality at turn N given information introduced at turn 1** --
    no per-turn gold either, and the only instrument that could stand in for it
    is the judge, which A7 pins to **Tier 2 (reports, never gates)** and which
    R63 records as unable to arbitrate quality on this project. It is metered,
    so it cannot run on Batch B's $0 fake arm at all.
  * **(c) retrieval-style recall over what should have been in context** --
    gold exists: session-level `targets`, one `{book, part, chapter}` each.

So (c) is computed here. (a) and (b) are **declared** in `FAMILIES` with the
exact gold each would need, because CP-6b chooses the family and a stub that
looks fillable would misrepresent the cost of choosing (a) or (b): both need a
**corpus re-freeze** under A9 (`src/harness/sessions.py` refuses a file whose
recomputed hash disagrees with its recorded version), not a new field.

**What (c) can and cannot see (R120, three qualifiers, all of them binding).**

  1. **Denominator 35/40 sessions, 95/108 turns.** All five `refusal` sessions
     carry `targets: []` -- correct by construction, R91 records that family as
     not retrieval-scored -- so `refusal` is not in this metric's denominator at
     all. It is reported with `n=0`, never as 0.0.
  2. **The gold is constant across a session's turns.** `targets` sits on the
     session. So this metric can ask "was the target chapter in the context
     served at turn N" per turn, but it cannot discriminate turn-to-turn
     allocation: the right answer is the same at every turn.
  3. **Per-family denominators are 5-12 sessions** (R91/R119). Every figure is
     reported per family with its denominator and is never pooled.

**CP-6b's binding rule: a metric computed only from the allocator's own output
is not a metric.** (c) is admissible because its gold is external to the run and
because the quantity compared against it -- the `sources` the server reports as
having gone into context -- is retrieval's output, not the allocator's. Any
claim that a memory tier "earned its tokens", scored from the context plan
alone, would be the allocator scoring itself; `tier_value_report()` therefore
returns **unmeasured** for every tier, with the reason, and there is deliberately
no code path here that can turn it into a pass.

**A prediction, written before the run** (R91's pre-registration idiom, so the
result cannot be reinterpreted afterwards): (c) is expected to be **arm-
invariant**. `PassagesFirstPolicy` never truncates passages to make room for
memory and `PassagesOnlyPolicy` drops only the memory tiers, so the passages
block -- and therefore `sources` -- is identical under both arms; retrieval is
issued on `body.query` alone and reads no transcript. If that holds, (c) does
not separate the starved arm from the honest one and **CP-6b's first rule
rejects it** -- which is the finding, immediately, and is exactly why the
starved arm is maximally extreme. A non-zero separation here means either
retrieval nondeterminism or an error path, and `separation()` says so rather
than reporting it as sensitivity.
"""

import logging
from dataclasses import dataclass

# Re-exported so the R91 label travels with every published figure derived from
# this corpus, without a second copy of the text.
from src.harness.sessions import LABEL as CORPUS_LABEL

logger = logging.getLogger(__name__)

_TARGET_KEYS = ("book", "part", "chapter")


@dataclass(frozen=True)
class Family:
    """One candidate metric family from `phase-6-memory.md`'s O4 row.

    `computable` is not a TODO. It is R120's measured answer for THIS corpus
    version, and `gold_required` is the precise input CP-6b would have to
    authorize to change it.
    """

    key: str
    name: str
    computable: bool
    gold_required: str


FAMILIES: dict[str, Family] = {
    "a": Family(
        key="a",
        name="task success on scripted multi-turn goals",
        computable=False,
        gold_required=(
            "Per-turn authored gold, 108 rows: for each turn, the goal it advances and a "
            "machine-checkable success predicate (an expected entity, chapter, or refusal). "
            "The corpus has none -- every turn is {query}. A9 makes this a NEW VERSION STAMP "
            "of test/session_corpus.json (a re-freeze with its own pre-registration, CP-4a "
            "shape), not an added field: src/harness/sessions.py refuses a modified file. "
            "Cost rider (R120): authored gold on a corpus already declared unvalidated (R91/O5) "
            "compounds the O5/O10 label rather than fixing it."
        ),
    ),
    "b": Family(
        key="b",
        name="answer quality at turn N given information introduced at turn 1",
        computable=False,
        gold_required=(
            "Either (i) per-session authored gold stating what a turn-N answer must retain from "
            "turn 1 -- 68 follow-up turns, again a corpus re-freeze under A9 -- or (ii) a judge. "
            "The judge exists (scripts/eval_quality.py, Config.JUDGE_MODEL) but is METERED, so it "
            "cannot run on Batch B's $0 fake arm; A7 pins it to Tier 2 (reports, never gates) and "
            "R63 records that it cannot arbitrate quality here. A further blocker specific to this "
            "batch: the fake provider replays one fixed fixture, so on the fake arm every arm's "
            "answer text is identical by construction and there is nothing for a judge to score."
        ),
    ),
    "c": Family(
        key="c",
        name="retrieval-style recall over what should have been in context",
        computable=True,
        gold_required=(
            "Already present: session-level `targets`, one {book, part, chapter} per session, on "
            "35 of 40 sessions (95 of 108 turns). Constant within a session, so per-turn scoring "
            "is possible but turn-to-turn discrimination is not (R120 qualifier 2)."
        ),
    ),
}


def _target_of(session: dict) -> dict | None:
    """The session's one gold chapter, or None for the `refusal` family.

    Refuses a multi-target session rather than silently scoring the first: R120
    measured every non-empty `targets` as a one-element array, and a corpus that
    grew a second target would change what recall MEANS (any-of vs all-of)
    without changing this function.
    """
    targets = session.get("targets") or []
    if not targets:
        return None
    if len(targets) > 1:
        raise ValueError(
            f"session {session.get('id')} has {len(targets)} targets; R120 measured exactly one "
            f"per session and this metric's definition assumes it. Decide any-of vs all-of before "
            f"scoring a multi-target corpus."
        )
    return {k: targets[0].get(k) for k in _TARGET_KEYS}


def _served(record: dict) -> list[dict] | None:
    """The chapters the SERVER reported as having gone into context.

    `None` (no `sources` event at all) is NOT an empty set: it is a turn that
    cannot be scored. Collapsing the two would score a failed request as a miss,
    which manufactures exactly the metric drop a starved arm is supposed to
    have to earn.
    """
    if record.get("sources") is None:
        return None
    return [{k: s.get(k) for k in _TARGET_KEYS} for s in record["sources"]]


def score_turn(record: dict, target: dict | None) -> tuple[bool | None, str]:
    """(hit, reason). `None` means UNMEASURED, and the reason says why."""
    if target is None:
        return None, "no_gold"  # the refusal family: out of the denominator entirely
    if record.get("status_code") != 200:
        return None, f"status_{record.get('status_code')}"
    if record.get("fault_phase"):
        return None, f"fault_{record['fault_phase']}"
    served = _served(record)
    if served is None:
        return None, "no_sources_event"
    return target in served, "scored"


def context_recall(records: list[dict], corpus: dict) -> dict:
    """Family (c), per family, with its denominator. Never pooled (R91/R119).

    Returns per-family `{turns_scored, hits, recall, unmeasured}` plus a
    `sessions` block, and an `overall` that is a sum of the scored turns and is
    labelled as such -- it is present for arithmetic, and no decision may be
    taken on it (R119: the per-family counts are 5-12 and that is the unit the
    decision rule speaks about).
    """
    by_id = {s["id"]: s for s in corpus["sessions"]}
    families: dict[str, dict] = {}
    per_session: dict[str, dict] = {}

    for record in records:
        session = by_id.get(record["session"])
        if session is None:
            raise ValueError(
                f"record names session {record['session']!r}, absent from corpus "
                f"{corpus['version']} -- records and corpus disagree, so nothing here is scorable"
            )
        family = session.get("family", "unknown")
        fam = families.setdefault(
            family, {"turns_scored": 0, "hits": 0, "sessions": set(), "unmeasured": {}}
        )
        sess = per_session.setdefault(
            session["id"], {"family": family, "turns_scored": 0, "hits": 0}
        )
        hit, reason = score_turn(record, _target_of(session))
        if hit is None:
            fam["unmeasured"][reason] = fam["unmeasured"].get(reason, 0) + 1
            continue
        fam["sessions"].add(session["id"])
        fam["turns_scored"] += 1
        sess["turns_scored"] += 1
        fam["hits"] += int(hit)
        sess["hits"] += int(hit)

    out: dict = {"per_family": {}, "per_session": per_session}
    for family, fam in sorted(families.items()):
        n = fam["turns_scored"]
        out["per_family"][family] = {
            "sessions_scored": len(fam["sessions"]),
            "turns_scored": n,
            "hits": fam["hits"],
            # None, not 0.0, when nothing was scorable -- `refusal` has no gold
            # and a 0.0 there would read as "retrieval missed", which is a claim
            # about a measurement that did not happen.
            "recall": (fam["hits"] / n) if n else None,
            "unmeasured": fam["unmeasured"],
        }
    scored = sum(f["turns_scored"] for f in out["per_family"].values())
    hits = sum(f["hits"] for f in out["per_family"].values())
    out["overall"] = {
        "turns_scored": scored,
        "hits": hits,
        "recall": (hits / scored) if scored else None,
        "note": "arithmetic only -- decisions are taken per family (R91/R119), never on this row",
    }
    return out


def tier_value_report(records: list[dict]) -> dict:
    """Per-tier: how often a tier earned tokens, and the value it is NOT scored.

    Everything here comes from `meta.context_plan`, i.e. from the allocator's
    own output, so under CP-6b it can describe ALLOCATION and can never
    describe VALUE. `value` is the string `"unmeasured"` in every row, with the
    reason, and there is no argument that makes it anything else -- the module
    has no external signal to score a tier against, and inventing one from the
    plan is the failure the rule names.
    """
    tiers: dict[str, dict] = {}
    planless = 0
    for record in records:
        plan = record.get("context_plan")
        if not plan:
            planless += 1
            continue
        blocks = [b for b in str(plan.get("plan.blocks", "")).split(",") if b and b != "-"]
        for tier in blocks:
            row = tiers.setdefault(tier, {"turns_present": 0, "tokens": 0})
            row["turns_present"] += 1
            row["tokens"] += int(plan.get(f"plan.block.{tier}.tokens", 0) or 0)
    for tier, row in tiers.items():
        row["value"] = "unmeasured"
        row["value_reason"] = (
            "scored from the context plan alone, which is the allocator's own output; CP-6b "
            "refuses that as a metric. An external signal for this tier's value does not exist "
            "in the frozen corpus -- see FAMILIES['a'] and ['b'] for the gold it would take."
        )
    return {
        "tiers": tiers,
        "turns_without_a_served_plan": planless,
        "note": (
            "allocation, not value. A cache-hit or retrieval-only turn composes no prompt and "
            "emits no context_plan; those are counted, not treated as an empty allocation."
        ),
    }


def report(records: list[dict], corpus: dict) -> dict:
    """One arm's full metric report: every family, computed or declared."""
    families = {}
    for key, family in FAMILIES.items():
        row = {"name": family.name, "computable": family.computable,
               "gold_required": family.gold_required}
        if family.computable:
            row["result"] = context_recall(records, corpus)
        else:
            row["result"] = None
            row["status"] = "not_computable_on_this_corpus_version"
        families[key] = row
    return {
        "corpus_version": corpus["version"],
        "corpus_label": CORPUS_LABEL,
        "turns": len(records),
        "families": families,
        "tier_value": tier_value_report(records),
    }


def separation(honest: dict, starved: dict) -> dict:
    """CP-6b's actual question: does the metric drop on a starved allocation?

    Per family, honest recall minus starved recall, plus the verdict the
    checkpoint's decision rule licenses. Two rules are applied mechanically, so
    a reader cannot round a null result up:

      * **no separation -> the metric is rejected**, whatever else it
        recommends. That is the plan's rule and this function does not soften
        it (`phase-6-memory.md`'s O4 row; `schedule-p6.md` CP-6b rule 1).
      * **separation on single-digit counts is not a separation** (R119). A
        family whose scored-turn count is below `_R119_FLOOR` is flagged
        `too_small_to_read` regardless of the delta's size.
    """
    _R119_FLOOR = 10
    rows: dict[str, dict] = {}
    h_fams = honest["families"]["c"]["result"]["per_family"]
    s_fams = starved["families"]["c"]["result"]["per_family"]
    for family in sorted(set(h_fams) | set(s_fams)):
        h, s = h_fams.get(family, {}), s_fams.get(family, {})
        hr, sr = h.get("recall"), s.get("recall")
        n = min(h.get("turns_scored", 0), s.get("turns_scored", 0))
        delta = None if (hr is None or sr is None) else hr - sr
        if n == 0:
            verdict = "unmeasured_no_gold"
        elif n < _R119_FLOOR:
            verdict = "too_small_to_read"
        elif delta is None or delta == 0:
            verdict = "no_separation"
        elif delta > 0:
            verdict = "separates"
        else:
            verdict = "inverted"  # starved scored HIGHER: read it, do not report it as a pass
        rows[family] = {
            "honest_recall": hr, "starved_recall": sr, "delta": delta,
            "turns_scored": n, "verdict": verdict,
        }
    separating = [f for f, r in rows.items() if r["verdict"] == "separates"]
    return {
        "family": "c",
        "per_family": rows,
        "families_separating": separating,
        # The pre-registered expectation, restated in the artifact so the
        # result is read against what was predicted, not against a hope.
        "pre_registered_expectation": (
            "arm-invariant: neither policy truncates the passages block and retrieval reads no "
            "transcript, so `sources` is expected identical under both arms and delta 0.000 "
            "everywhere. Under CP-6b rule 1 that REJECTS family (c) as the allocation metric."
        ),
        "verdict": (
            "REJECTED under CP-6b rule 1 -- a metric that does not separate the starved arm from "
            "the honest arm is rejected, whatever else it recommends"
            if not separating else
            f"separates on {separating}; read each family's turns_scored against R119 before "
            f"quoting a delta"
        ),
        "corpus_label": CORPUS_LABEL,
    }
