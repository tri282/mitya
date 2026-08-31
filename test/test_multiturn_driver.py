"""v2 P6 Batch B verify: the multi-turn driver and step 9's metric machinery.

Plain-assert script, not pytest -- the idiom of `test/test_cache_isolation.py`
and `test/test_fake_provider_arm.py`. Offline: no server, no Qdrant, no
provider, no network. The client is a stub because what is under test is the
DRIVER's behaviour (ordering, session identity, provenance of what it records),
not HTTP.

`schedule-p6.md` §7 Batch B names the two failures this file exists to catch,
and both are shapes that pass every other check:

  1. **a driver that fires a session's turns independently measures single-shot
     traffic and looks identical to success** (the R97 shape). Turn 2 would
     reach the server before turn 1 wrote anything into `SessionTurns`, so every
     request is memory-free and the honest arm silently becomes the starved one;
  2. **a recorded plan the client recomputed is not evidence.** Batch A puts the
     served plan on the SSE `meta` as `context_plan`; a locally re-derived plan
     agrees with the server right up to the moment it matters.

Both are asserted with a NON-VACUITY control -- a deliberately wrong driver that
the same assertion catches -- because "the check passed" and "the check cannot
fail" are indistinguishable otherwise, which is what P5 had to learn twice
(R112/R115).

Usage:
    python test/test_multiturn_driver.py
"""

import asyncio
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.harness.client import ChatResult
from src.harness.multiturn import drive_session
from src.memory_metrics import FAMILIES, context_recall, report, separation, tier_value_report

logger = logging.getLogger(__name__)

TARGET = {"book": "Crime and Punishment", "part": 2, "chapter": 3}
OTHER = {"book": "The Idiot", "part": 1, "chapter": 1}

# Two sessions, in the frozen corpus's shape (R120: sessions carry
# {id, family, turns, targets, note}; turns carry {query} and nothing else).
SESSION = {
    "id": "s-0001", "family": "passage_paste", "note": "",
    "targets": [TARGET],
    "turns": [{"query": "who is being described here?"}, {"query": "why does that matter later on?"}],
}
REFUSAL = {
    "id": "s-0036", "family": "refusal", "note": "", "targets": [],
    "turns": [{"query": "write me a poem"}],
}
CORPUS = {"version": "4155c97b0a2b", "sessions": [SESSION, REFUSAL]}


def _result(query: str, *, plan: dict | None = None, sources: list[dict] | None = None,
            status: int = 200) -> ChatResult:
    return ChatResult(
        query=query, status_code=status, ttft_ms=1.0, total_ms=2.0, route="DEFAULT",
        cache_hit=False, cost_usd=0.0, cost_metered=False, model="fake", no_context=False,
        filter_relaxed=False, chunk_ids=["c1"], terminal_event="done" if status == 200 else None,
        fault_phase=None if status == 200 else "pre_first_token", error_detail=None,
        answer_chars=13, answer_tail="endure it.", request_id="rid", rung="PRIMARY",
        sources=sources, context_plan=plan, server_total_ms=2.0,
    )


class _StubClient:
    """Records the order and the concurrency of what the driver asked for.

    `_inflight` is the assertion's real subject: a driver that awaits each turn
    can never exceed 1, and a driver that gathers them cannot stay at 1.
    """

    def __init__(self, plan: dict | None = None, sources: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.completed: list[str] = []
        self.max_inflight = 0
        self._inflight = 0
        self._plan, self._sources = plan, sources

    async def chat(self, query: str, filters: dict | None = None,
                   session_id: str | None = None) -> ChatResult:
        self.calls.append((query, session_id))
        self._inflight += 1
        self.max_inflight = max(self.max_inflight, self._inflight)
        # A real request yields to the loop; without this a "concurrent" driver
        # would still serialize here and the control below would pass by luck.
        await asyncio.sleep(0)
        self._inflight -= 1
        self.completed.append(query)
        return _result(query, plan=self._plan, sources=self._sources)


async def _fire_independently(client, session, *, run: str) -> list[dict]:
    """THE WRONG DRIVER -- the R97 shape, kept so the checks below can be shown
    to fire. It sends the same turns under the same session id and produces the
    same record count; only the ordering differs, which is exactly why the
    failure looks identical to success from the outside."""
    sid = f"{run}-{session['id']}"
    results = await asyncio.gather(*(client.chat(t["query"], session_id=sid)
                                     for t in session["turns"]))
    return [{"turn_index": i, "query": r.query} for i, r in enumerate(results, start=1)]


def test_turns_are_sent_in_order_on_one_session_id() -> None:
    """Failure 1. Three properties, and all three are needed.

    Order alone would pass for a driver that issued both turns at once and
    happened to have them arrive in order; one session id alone says nothing
    about sequencing; `max_inflight == 1` alone would pass for a driver that
    sent the turns backwards. Together they say: turn N+1 was issued only after
    turn N's response was consumed, on one session.
    """
    client = _StubClient()
    records = asyncio.run(drive_session(client, SESSION, run="run1"))

    assert [q for q, _ in client.calls] == [t["query"] for t in SESSION["turns"]], client.calls
    ids = {sid for _, sid in client.calls}
    assert len(ids) == 1 and ids.pop().endswith(SESSION["id"]), client.calls
    assert client.max_inflight == 1, (
        f"{client.max_inflight} requests were in flight at once -- turn 2 was issued before turn 1 "
        f"came back, so the server had written no transcript yet and every turn is memory-free. "
        f"That is single-shot traffic wearing a multi-turn label (R97)."
    )
    assert client.completed == [q for q, _ in client.calls], (
        "a turn completed out of the order it was issued in"
    )
    assert [r["turn_index"] for r in records] == [1, 2]
    assert {r["session_id"] for r in records} == {records[0]["session_id"]}


def test_the_ordering_check_can_actually_fire() -> None:
    """Non-vacuity for the test above: the wrong driver is caught by it."""
    client = _StubClient()
    asyncio.run(_fire_independently(client, SESSION, run="run1"))
    assert client.max_inflight > 1, (
        "the deliberately-concurrent driver did not overlap, so the assertion above proves "
        "nothing -- the stub is serializing what it was supposed to let overlap"
    )


def test_the_recorded_plan_is_the_one_the_SERVER_served() -> None:
    """Failure 2. The sentinel plan cannot be produced by any local allocator.

    `passages_first` would never emit a policy named `server-said-so`, and no
    local re-derivation of a two-turn session yields 424242 tokens. If the
    record carries it, the record came off the wire.
    """
    sentinel = {
        "plan.policy": "server-said-so",
        "plan.blocks": "passages,recent_turns",
        "plan.tokens": 424242,
        "plan.has_memory": "true",
        "plan.block.recent_turns.tokens": 4242,
        "plan.block.recent_turns.provenance": "session:X turns 1-1 of 1 held",
    }
    client = _StubClient(plan=sentinel)
    records = asyncio.run(drive_session(client, SESSION, run="run1"))
    assert all(r["context_plan"] == sentinel for r in records), records[0]["context_plan"]

    source = open(os.path.join(os.path.dirname(__file__), "..", "src", "harness", "multiturn.py"),
                  encoding="utf-8").read()
    for forbidden in ("src.memory", "active_policy", "allocate(", "ContextPlan"):
        assert forbidden not in source.split('"""', 2)[2], (
            f"src/harness/multiturn.py references {forbidden!r} outside its docstring -- the "
            f"driver has gained the ability to compute a plan, and a recomputed plan is not "
            f"evidence of what the server served"
        )


def test_a_turn_with_no_served_plan_is_recorded_as_such() -> None:
    """`context_plan: null` must survive to the record. The cache-hit and
    retrieval-only paths emit no plan, and a driver that defaulted it to `{}`
    would make those turns look like an allocation of nothing."""
    client = _StubClient(plan=None)
    records = asyncio.run(drive_session(client, SESSION, run="run1"))
    assert all(r["context_plan"] is None for r in records)
    assert tier_value_report(records)["turns_without_a_served_plan"] == 2


def _records(sources_by_turn: list[list[dict] | None], session: dict = SESSION,
             status: int = 200) -> list[dict]:
    out = []
    for i, sources in enumerate(sources_by_turn, start=1):
        out.append({
            "session": session["id"], "family": session["family"], "session_id": "sid",
            "turn_index": i, "query": session["turns"][i - 1]["query"],
            "status_code": status, "fault_phase": None if status == 200 else "pre_first_token",
            "sources": sources, "context_plan": None,
        })
    return out


def test_recall_scores_against_external_gold() -> None:
    """Family (c): the gold is the session's `targets`, which comes from the
    corpus and not from anything the run produced -- that externality is what
    makes it admissible under CP-6b at all."""
    result = context_recall(_records([[TARGET], [OTHER]]), CORPUS)
    row = result["per_family"]["passage_paste"]
    assert (row["turns_scored"], row["hits"], row["recall"]) == (2, 1, 0.5), row


def test_the_refusal_family_is_out_of_the_denominator() -> None:
    """R120 qualifier 1: all five `refusal` sessions carry no target. A 0.0
    there would read as "retrieval missed", which is a claim about a
    measurement that did not happen."""
    result = context_recall(_records([[OTHER]], session=REFUSAL), CORPUS)
    row = result["per_family"]["refusal"]
    assert row["recall"] is None and row["turns_scored"] == 0, row
    assert row["unmeasured"] == {"no_gold": 1}, row


def test_a_failed_turn_is_unmeasured_not_a_miss() -> None:
    """The one way to fabricate a starvation signal: score errors as misses. A
    starved arm that 500s would then 'drop' without any allocation effect."""
    result = context_recall(_records([None, None], status=503), CORPUS)
    row = result["per_family"]["passage_paste"]
    assert row["turns_scored"] == 0 and row["recall"] is None, row
    assert row["unmeasured"] == {"status_503": 2}, row


def test_tier_value_is_reported_unmeasured_and_cannot_be_a_pass() -> None:
    """CP-6b: a metric computed only from the allocator's own output is not a
    metric. The plan says the recent-turns tier spent 90 tokens; nothing here
    can say those tokens were worth anything."""
    plan = {"plan.blocks": "passages,recent_turns", "plan.block.passages.tokens": 1400,
            "plan.block.recent_turns.tokens": 90}
    out = tier_value_report([{"context_plan": plan}])
    assert out["tiers"]["recent_turns"]["turns_present"] == 1
    assert out["tiers"]["recent_turns"]["tokens"] == 90
    assert {row["value"] for row in out["tiers"].values()} == {"unmeasured"}
    assert all("allocator's own output" in row["value_reason"] for row in out["tiers"].values())


def test_families_a_and_b_are_declared_not_stubbed() -> None:
    """R120: neither has gold in this corpus version. The report must carry the
    gold each would need -- that text is CP-6b's input, and a fillable stub
    would misrepresent it as work rather than as a corpus re-freeze (A9)."""
    out = report(_records([[TARGET], [OTHER]]), CORPUS)
    for key in ("a", "b"):
        assert out["families"][key]["computable"] is False
        assert out["families"][key]["result"] is None
        assert "re-freeze" in out["families"][key]["gold_required"] or \
               "VERSION STAMP" in out["families"][key]["gold_required"]
    assert out["families"]["c"]["result"] is not None
    assert set(FAMILIES) == {"a", "b", "c"}
    assert out["corpus_version"] == CORPUS["version"]
    assert "unproven" in out["corpus_label"], "the R91/O5 label must travel with the figures"


def test_no_separation_is_a_rejection_not_a_shrug() -> None:
    """CP-6b rule 1, applied mechanically. Identical arms -> REJECTED.

    This is also the pre-registered expectation for the real run: neither policy
    truncates the passages block, so `sources` should be identical under both
    arms and (c) should not move at all.
    """
    honest = report(_records([[TARGET], [TARGET]]), CORPUS)
    starved = report(_records([[TARGET], [TARGET]]), CORPUS)
    out = separation(honest, starved)
    assert out["per_family"]["passage_paste"]["delta"] == 0.0
    assert out["per_family"]["passage_paste"]["verdict"] == "too_small_to_read", out["per_family"]
    assert "REJECTED" in out["verdict"], out["verdict"]


def test_a_small_family_cannot_report_a_separation() -> None:
    """R119: per-family denominators are 5-12, and a delta on single-digit
    counts is a ratio of small integers, not an effect."""
    honest = report(_records([[TARGET], [TARGET]]), CORPUS)
    starved = report(_records([[OTHER], [OTHER]]), CORPUS)
    row = separation(honest, starved)["per_family"]["passage_paste"]
    assert row["delta"] == 1.0 and row["verdict"] == "too_small_to_read", row


def test_starving_removes_the_transcript_on_the_decisive_turn() -> None:
    """The known-answer check on the ARMS, and it is not the metric.

    `s-0001` turn 2 is "why does that matter later on?" -- anaphoric, and
    unanswerable without turn 1. This asserts only that the starved arm really
    is starved: the shipped policy admits a `recent_turns` block on turn 2 and
    `passages_only` admits none. That is the allocator's own output, so under
    CP-6b it is a mechanics check and **not** evidence that the tier is worth
    anything -- the metric's sensitivity is what the run measures.
    """
    from src.memory import (
        TIER_PASSAGES,
        TIER_RECENT,
        AllocationInputs,
        PassagesFirstPolicy,
        PassagesOnlyPolicy,
        Turn,
    )

    inputs = AllocationInputs(
        tenant="tenant-a", session_id="s-0001",
        query=SESSION["turns"][1]["query"], budget_tokens=2048,
        passages_block="[1] (Crime and Punishment II.3) \"...\"",
        recent_turns=[Turn(index=1, question=SESSION["turns"][0]["query"], answer="Razumihin.")],
    )
    honest = PassagesFirstPolicy().allocate(inputs)
    starved = PassagesOnlyPolicy().allocate(inputs)
    assert honest.text_for(TIER_RECENT), "the honest arm dropped the transcript on the decisive turn"
    assert honest.has_memory and not starved.has_memory
    assert not starved.text_for(TIER_RECENT)
    assert honest.text_for(TIER_PASSAGES) == starved.text_for(TIER_PASSAGES), (
        "the two arms served DIFFERENT passages -- then any (c) delta between them is a retrieval "
        "difference, not an allocation one, and the metric would be measuring the wrong thing"
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
