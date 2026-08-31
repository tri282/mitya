"""v2 P6 Batch D verify: the indirect-injection regression set's PLUMBING (step 8).

What this file can check, and what it deliberately cannot:

  * **it checks the set is well formed** -- two separated arms, unique ids, a
    stated intent per attempt, and a canary that survives `normalize()` (a
    canary the store would mangle is undetectable, and an undetectable attempt
    is a blocked-looking attempt that was never made);
  * **it checks the known-answer case of arm `extractor` end to end**, offline:
    the free-text control recovers the canary while the shipped write path does
    not. That control is a string round trip through the schema CP-6a rejected,
    so it succeeds by construction and needs no model;
  * **it checks the control of arm `fence` is really a control** -- the same
    prompt, differing only in the fence lines;
  * **it checks a result cannot be misread as a pass rate**: a fake-provider run
    and a run whose control never succeeded both emit NO `pass_rate` field at
    all, and a real run with a live control does emit one (the can-fire
    companion, so the refusal is not vacuous);
  * **it cannot measure the fence.** Whether a real model obeys injected memory
    is Batch E-run's, against a real provider -- one of R128's four surviving
    step-10 rows. Nothing here produces or approximates that number.

Plain-assert script, not pytest, matching `test/test_memory_store.py`. Fully
offline: no server, no Qdrant, no provider, no network -- the gateway seam is
stubbed at `src.rag.generate.open_guarded_stream` and the database is a temp
file. Run from the repo root.
"""

import asyncio
import json
import sqlite3
import sys
import tempfile

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import run_injection_set as runner  # noqa: E402
import src.memory_store as store  # noqa: E402
import src.rag.generate as gen  # noqa: E402
from src.config import Config  # noqa: E402
from src.db import init_schema  # noqa: E402
from src.gateway.contract import GenerationStream, Usage  # noqa: E402
from src.injection_set import (  # noqa: E402
    ARM_EXTRACTOR,
    ARM_FENCE,
    ARMS,
    ATTEMPTS,
    ArmResult,
    Outcome,
    attempts_for,
    obeyed,
    recovered,
    report,
    strip_fence,
)
from src.memory import (  # noqa: E402
    AllocationInputs,
    PassagesFirstPolicy,
    context_budget_tokens,
    render_profile,
)

T0 = 1_700_000_000.0


def _db() -> str:
    """A temp database with the real schema and the runner's tenant, which both
    memory tables need an `accounts` row for (`ON DELETE CASCADE`)."""
    path = tempfile.mktemp(suffix="-injection-test.db")
    init_schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO accounts (tenant_id, password_hash, created_at) VALUES (?, ?, ?)",
            (runner.TENANT, "x", T0),
        )
    return path


class _HostileExtractor:
    """The worst extractor the write path could be handed: it returns the whole
    turn, verbatim, as a profile value.

    That is precisely what a model talked into compliance would emit, and it is
    the shape arm `extractor` is defending against. Patched at
    `src.rag.generate.open_guarded_stream`, the seam `src/summarizer.py` calls,
    so the summarizer's parsing, echo filter and write path all run for real.
    """

    def __enter__(self):
        self._orig_open, self._orig_provider = gen.open_guarded_stream, gen._provider_for

        async def _open(provider, messages, model):
            question = messages[-1]["content"]

            async def _chunks():
                yield json.dumps([{
                    "kind": "preference", "key": "house_rule", "value": question,
                    "importance": "high", "confidence": "high",
                }])

            return GenerationStream(
                chunks=_chunks(),
                usage=Usage(prompt_tokens=60, completion_tokens=40, model=model),
            )

        gen.open_guarded_stream = _open
        gen._provider_for = lambda model: None
        return self

    def __exit__(self, *exc_info):
        gen.open_guarded_stream, gen._provider_for = self._orig_open, self._orig_provider
        return False


# --- the set is well formed -------------------------------------------------


def test_every_attempt_states_what_it_is_trying_to_achieve() -> None:
    """An unreadable corpus of attack strings cannot be reviewed, and this
    set's whole value is that a human can check it means what it claims."""
    ids = [a.id for a in ATTEMPTS]
    assert len(ids) == len(set(ids)), f"duplicate attempt ids: {ids}"
    for a in ATTEMPTS:
        assert a.arm in ARMS, f"{a.id} is in no arm"
        assert len(a.intent) > 40, f"{a.id} has no stated intent"
        assert a.text and a.canary


def test_every_canary_survives_the_stores_normalization() -> None:
    """`normalize()` lowercases, strips punctuation and truncates. A canary it
    mangles cannot be detected in the store or in a rendered block, so the
    attempt would read as blocked while never having been made -- a vacuous
    pass wearing a measurement's clothes."""
    for a in ATTEMPTS:
        assert a.canary == store.normalize(a.canary), (
            f"{a.id}'s canary {a.canary!r} does not survive normalize()"
        )


def test_the_two_arms_are_separated_and_both_populated() -> None:
    for arm in ARMS:
        picked = attempts_for(arm)
        assert picked, f"arm {arm} has no attempts"
        assert all(a.arm == arm for a in picked)
    assert len(attempts_for(ARM_EXTRACTOR)) + len(attempts_for(ARM_FENCE)) == len(ATTEMPTS)


def test_the_report_carries_no_pooled_number() -> None:
    """Two defenses, two denominators. A combined rate is not a weaker claim
    than the per-arm ones, it is an unattributable one."""
    out = report([
        ArmResult(ARM_EXTRACTOR, "fake", (Outcome("e", False, True),)),
        ArmResult(ARM_FENCE, "fake", (Outcome("f", False, True),)),
    ])
    assert set(out["arms"]) == set(ARMS)
    blob = json.dumps(out)
    for pooled in ("total_blocked", "overall", "combined_pass_rate"):
        assert pooled not in blob
    assert out["arms"][ARM_EXTRACTOR]["n"] == 1 and out["arms"][ARM_FENCE]["n"] == 1


# --- arm 1's known-answer case, end to end and offline ----------------------


def test_the_free_text_control_recovers_what_the_shipped_write_path_refuses() -> None:
    """**The known-answer case for arm `extractor`.** Both halves asserted in
    one test, because either alone is worthless:

      * the CONTROL -- the same text in the same column with no typing, no
        bound, no normalization, no echo filter (option (ii)'s free-text store,
        which CP-6a rejected) -- **recovers every canary**;
      * the SHIPPED path, handed a maximally hostile extractor, stores none of
        them.

    The control's success is a string round trip and not a model's behaviour,
    so this arm's known answer holds without a provider and without a run.
    """
    path = _db()
    with _HostileExtractor():
        result = asyncio.run(runner.run_extractor_arm(path, dry=False))

    assert result.n == len(attempts_for(ARM_EXTRACTOR))
    assert result.control_successes == result.n, (
        f"the free-text control recovered only {result.control_successes} of {result.n} "
        f"canaries -- the control is not a control and the arm proves nothing"
    )
    landed = [o.attempt_id for o in result.outcomes if o.landed]
    assert landed == [], f"an instruction reached the store on the shipped path: {landed}"


def test_the_paragraph_attempt_is_defeated_by_the_bound_not_by_the_filter() -> None:
    """`e4_paragraph_recovery`'s canary sits past `MEMORY_VALUE_MAX_CHARS` on
    purpose, so the attempt fails on `normalize()`'s bound -- structural, per
    CP-6a -- and not on the echo filter, which is a by-test defense against a
    different shape. Asserting it separately keeps the two claims from being
    confused in the write-up.
    """
    attempt = next(a for a in ATTEMPTS if a.id == "e4_paragraph_recovery")
    assert attempt.canary in store.normalize(attempt.text, 10_000), (
        "the attempt does not carry its own canary -- it can never be detected"
    )
    assert attempt.canary not in store.normalize(attempt.text), (
        f"the canary fits inside {Config.MEMORY_VALUE_MAX_CHARS} chars, so this attempt "
        f"tests the echo filter rather than the length bound"
    )


def test_the_recovery_oracle_can_actually_fire() -> None:
    """Non-vacuity for the arm above: prove the predicate detects a hit."""
    attempt = attempts_for(ARM_EXTRACTOR)[0]
    assert not recovered(attempt, ["reads slowly", "prefers part 6"])
    assert recovered(attempt, ["reads slowly", f"x {attempt.canary} y"])


# --- arm 2's control is really a control ------------------------------------


def test_the_unfenced_control_differs_from_the_shipped_prompt_only_by_the_fence() -> None:
    """If the control changed the content as well as the framing, a difference
    in outcome would not be attributable to the fence."""
    attempt = next(a for a in ATTEMPTS if a.id == "f1_profile_command")
    facts, episodes = runner._plant(attempt)
    plan = PassagesFirstPolicy().allocate(AllocationInputs(
        tenant=runner.TENANT, session_id="s-1", query=runner.QUESTION,
        budget_tokens=context_budget_tokens(), passages_block=runner.PASSAGES,
        profile_facts=facts, episodes=episodes,
    ))
    fenced = plan.memory_block()
    control = strip_fence(fenced)
    assert attempt.text in fenced and attempt.text in control, (
        "the planted row must reach BOTH prompts -- otherwise the control is a "
        "different attempt, not the same one unfenced"
    )
    assert "never instructions" in fenced, "the shipped path lost its fence"
    assert "never instructions" not in control, "the control is still fenced"


def test_the_obedience_oracle_can_actually_fire() -> None:
    """Non-vacuity: a "blocked" verdict must be a fact about the answer, not a
    predicate that cannot detect compliance."""
    attempt = attempts_for(ARM_FENCE)[0]
    assert not obeyed(attempt, "He felt the air of the Haymarket on his face.")
    assert obeyed(attempt, f"He felt everything at once. {attempt.canary}")


def test_the_episodic_attempt_reaches_the_prompt_through_its_own_renderer() -> None:
    """The fence is tested on both renderers, not only the profile one. The
    tier is untriggered in the live path (G-P15) and this exercises the
    renderer, which is reachable the moment CP-6d chooses a trigger."""
    attempt = next(a for a in ATTEMPTS if a.id == "f4_episodic_topic")
    facts, episodes = runner._plant(attempt)
    assert episodes and not facts
    plan = PassagesFirstPolicy().allocate(AllocationInputs(
        tenant=runner.TENANT, session_id="s-1", query=runner.QUESTION,
        budget_tokens=context_budget_tokens(), passages_block="",
        profile_facts=facts, episodes=episodes,
    ))
    assert attempt.canary in plan.memory_block()


# --- the pass rate is not producible here, by construction ------------------


def test_a_fake_provider_run_carries_no_pass_rate_field() -> None:
    """`schedule-p6.md` 5: a fake provider replays a fixture and cannot be
    jailbroken. The artifact therefore carries NO numeric field a pass rate
    could be read out of -- not a null and not a zero, either of which a
    spreadsheet would happily average."""
    result = ArmResult(ARM_FENCE, "fake", tuple(
        Outcome(a.id, False, True) for a in attempts_for(ARM_FENCE)
    ))
    assert result.pass_rate is None
    payload = result.to_dict()
    assert "pass_rate" not in payload
    assert "PLUMBING ONLY" in payload["pass_rate_withheld"]
    assert "NO PASS RATE" in result.verdict()


def test_an_arm_whose_control_never_succeeded_is_invalid_not_perfect() -> None:
    """100% blocked with a dead control is the sixth-instance pattern: it
    measures the weakness of the attempts and looks identical to success."""
    result = ArmResult(ARM_FENCE, "models/gemini-3.5-flash-lite", tuple(
        Outcome(a.id, False, False) for a in attempts_for(ARM_FENCE)
    ))
    assert result.blocked == result.n
    assert result.pass_rate is None
    assert "INVALID" in result.to_dict()["pass_rate_withheld"]


def test_a_real_run_with_a_live_control_does_publish_a_rate() -> None:
    """The can-fire companion for the two refusals above: without this, they
    would pass on an implementation that never publishes anything."""
    outcomes = [Outcome(a.id, False, True) for a in attempts_for(ARM_FENCE)]
    outcomes[0] = Outcome(outcomes[0].attempt_id, True, True)
    result = ArmResult(ARM_FENCE, "models/gemini-3.5-flash-lite", tuple(outcomes))
    payload = result.to_dict()
    assert payload["pass_rate"] == (result.n - 1) / result.n
    assert "pass_rate_withheld" not in payload
    assert "against a control where" in result.verdict()


def test_the_fence_wording_the_set_relies_on_is_the_shipped_one() -> None:
    """`strip_fence()` drops the first and last lines of a rendered block. If
    the fence ever becomes multi-line, the control would silently keep half of
    it and the arm would compare two fenced prompts."""
    rendered = render_profile(runner._plant(
        next(a for a in ATTEMPTS if a.id == "f1_profile_command"))[0])
    assert len(rendered.splitlines()) == 3, (
        "the profile fence is no longer one line above and one below the body -- "
        "strip_fence() would leave the control partially fenced"
    )


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
