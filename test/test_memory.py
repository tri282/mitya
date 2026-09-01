# test/test_memory.py
"""v2 P6 Batch A verify: the context plan, the recent-turns tier, the allocator seam.

Covers `phase-6-memory.md` steps 1, 2(a) and 3, and the half of the Verify
section that is checkable without a run:

  * "a context plan is produced per request with blocks, tokens, and
    provenance" -- the plan's shape and its span attributes (its visibility in
    P1's spine is asserted from the request path, in `test_cache_isolation.py`);
  * "swapping the allocator policy is a config change and nothing else --
    verified by shipping a second trivial policy and switching to it";
  * the C-1 invariant's Batch A half: recent turns are process memory and
    **nothing here can persist them** (`cp-6a-input.md` C-1 / O-A(i));
  * the budget: 3072 tokens after G-P18, and where that number comes from.

The bypass predicate (step 7(a) / G-P12(b)) is NOT here -- it is asserted on
the real `/chat` path in `test/test_cache_isolation.py`, because a bypass
asserted against a helper proves the helper.

Plain-assert script, not pytest. Fully offline: no server, no Qdrant, no
provider, no database. Run from the repo root.

**Requires a promoted serving config carrying `allocation_policy` and
`memory_budget_tokens`** -- both are new versioned fields this batch adds, so
`scripts/bootstrap_config.py --promote` must have run. A `ConfigError` from
this file's imports means that has not happened; it is the intended direction
of failure, not a defect here.
"""

import inspect
import sys

sys.path.insert(0, ".")

import src.memory as memory_mod  # noqa: E402
from src.config import Config  # noqa: E402
from src.memory import (  # noqa: E402
    MAX_RECENT_TURNS,
    MAX_TRACKED_SESSIONS,
    POLICIES,
    TIER_PASSAGES,
    TIER_RECENT,
    AllocationInputs,
    PassagesFirstPolicy,
    PassagesOnlyPolicy,
    SessionTurns,
    Turn,
    active_policy,
    context_budget_tokens,
)

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
_PASSAGES = "Raskolnikov paced the room. " * 8
_QUERY = "why does that matter later on?"


def _turns(n: int) -> list[Turn]:
    return [Turn(index=i + 1, question=f"question {i}", answer=f"answer {i}") for i in range(n)]


def _inputs(**over) -> AllocationInputs:
    base = dict(
        tenant=TENANT_A,
        session_id="s-1",
        query=_QUERY,
        budget_tokens=context_budget_tokens(),
        passages_block=_PASSAGES,
        recent_turns=_turns(2),
    )
    base.update(over)
    return AllocationInputs(**base)


# --- step 3: the seam --------------------------------------------------------


def test_two_policies_produce_different_plans_from_identical_inputs() -> None:
    """The seam's whole point, and the only way to test it: the SAME input
    object through two policies must not produce the same plan.

    If it did, the second policy would be decoration and the "swap the policy"
    verification would be measuring nothing -- which is why the phase file asks
    for a second *trivial* policy rather than a second plausible one.
    """
    inputs = _inputs()
    honest = PassagesFirstPolicy().allocate(inputs)
    starved = PassagesOnlyPolicy().allocate(inputs)

    assert honest != starved
    assert honest.has_memory and not starved.has_memory
    assert honest.text_for(TIER_PASSAGES) == starved.text_for(TIER_PASSAGES), (
        "the two policies must differ ONLY in what memory earned -- if the "
        "passages differ too, a policy comparison confounds allocation with "
        "retrieval and no delta between them is attributable"
    )


def test_swapping_the_policy_is_a_config_change_and_nothing_else() -> None:
    """`Config.ALLOCATION_POLICY` selects it; no call site names a class.

    The second assertion is the one that matters: `src/api/app.py` must not
    mention any policy by name, or "swapping is a config change" is false and
    P7's search axis needs a code edit per candidate.
    """
    saved = Config.__dict__.get("ALLOCATION_POLICY")
    try:
        Config.ALLOCATION_POLICY = "passages_only"
        assert active_policy().name == "passages_only"
        Config.ALLOCATION_POLICY = "passages_first"
        assert active_policy().name == "passages_first"
        Config.ALLOCATION_POLICY = "no-such-policy"
        try:
            active_policy()
            raise AssertionError("an unknown policy name must raise, not fall back to a default")
        except ValueError as exc:
            assert "no-such-policy" in str(exc)
    finally:
        if saved is None:
            del Config.ALLOCATION_POLICY
        else:
            Config.ALLOCATION_POLICY = saved

    # The request path must not NAME a policy -- not import one, not construct
    # one, not branch on one. Checked against the class names rather than the
    # config values, so a comment may still explain which policy does what
    # (comments do not select anything; an import does).
    app_source = open("src/api/app.py", encoding="utf-8").read()
    for policy_cls in POLICIES.values():
        assert policy_cls.__name__ not in app_source, (
            f"src/api/app.py names {policy_cls.__name__} -- selection must go "
            f"through Config.ALLOCATION_POLICY and `active_policy()` only, or "
            f"P7 needs a code edit per candidate and the seam is not a seam"
        )


def test_every_policy_takes_the_same_input_and_returns_a_plan() -> None:
    """The interface is stable across policies: one `AllocationInputs`, one
    `ContextPlan`. A policy with its own extra parameter is not swappable."""
    inputs = _inputs()
    for name, policy_cls in POLICIES.items():
        params = list(inspect.signature(policy_cls.allocate).parameters)
        assert params == ["self", "inputs"], (name, params)
        plan = policy_cls().allocate(inputs)
        assert plan.policy == name


# --- step 1: the plan, its tokens, its provenance ----------------------------


def test_every_block_carries_tokens_and_provenance() -> None:
    """Provenance per block is step 1's actual deliverable -- a plan that
    reports totals cannot say WHICH block caused a bad answer."""
    plan = PassagesFirstPolicy().allocate(_inputs())
    assert {b.tier for b in plan.blocks} == {TIER_PASSAGES, TIER_RECENT}
    for block in plan.blocks:
        assert block.tokens > 0, block.tier
        assert block.provenance.strip(), block.tier
    assert plan.tokens == sum(b.tokens for b in plan.blocks)


def test_the_memory_block_provenance_names_the_turns_it_carries() -> None:
    """G-P13's residual risk, in the shape it asked for: a stale memory block
    must be visible per request, so provenance carries the turns held and the
    turns injected, not just the tier name."""
    plan = PassagesFirstPolicy().allocate(_inputs(recent_turns=_turns(2)))
    provenance = [b.provenance for b in plan.blocks if b.tier == TIER_RECENT][0]
    assert "s-1" in provenance
    assert "never persisted" in provenance


def test_the_plan_renders_span_attributes_per_block() -> None:
    """What `/chat` stamps on the `context.allocate` span. Per-block keys, so a
    trace answers "which block spent the budget" without a second query."""
    attrs = PassagesFirstPolicy().allocate(_inputs()).attributes()
    assert attrs["plan.policy"] == "passages_first"
    assert attrs["plan.has_memory"] == "true"
    assert attrs[f"plan.block.{TIER_RECENT}.tokens"] > 0
    assert attrs[f"plan.block.{TIER_PASSAGES}.provenance"]
    # OTel attribute values must be primitives -- a dict or a list here is
    # dropped by the exporter, silently, which is the failure mode P1 exists
    # to make impossible.
    assert all(isinstance(v, (str, int)) for v in attrs.values()), attrs


# --- the budget: G-P18's 3072 tokens, raised from C-2's 306 -------------------


def test_the_budget_is_ctx_size_minus_max_tokens() -> None:
    """2048 on the shipped config, derived rather than hardcoded so a CTX_SIZE
    experiment moves it instead of leaving a stale literal behind."""
    assert context_budget_tokens() == Config.CTX_SIZE - Config.MAX_TOKENS


def test_the_memory_budget_is_g_p18s_raised_ceiling() -> None:
    """3072, raised from 306 at CP-6d (G-P18), and the arithmetic that bounds it.

    306 was C-2's headroom (2048 of prompt budget minus R96's measured
    1742-token draw) and G-P16 is what retired it: the allocator admits WHOLE
    turns, a real answer runs toward `MAX_TOKENS`, so at 306 the recent tier
    contributed zero blocks on essentially every turn against a live provider --
    the phase would have published a utilization row for a tier that cannot fire.

    **Asserted against the ACTIVE artifact, not the source default**, for the
    reason this test always gave: the number that binds at serving time is the
    promoted one. Two consequences, both deliberate. A run before the promotion
    fails here, which is the correct direction of failure -- CP-6d's raise is
    promoted first and the suite is run after. And a LOCAL llama-server run
    promotes an artifact carrying `ctx_size=3072`, where this assertion is
    expected to fail and the second one below is the invariant that still holds.
    """
    assert Config.MEMORY_BUDGET_TOKENS == 3072
    # The real constraint, which survives either promotion: R96's measured
    # passages+persona draw plus the memory ceiling plus the completion ceiling
    # must fit inside the context window. 1742 + 3072 + 1024 = 5838 <= 6144.
    assert 1742 + Config.MEMORY_BUDGET_TOKENS + Config.MAX_TOKENS <= Config.CTX_SIZE


def test_memory_never_exceeds_the_memory_budget() -> None:
    """A long transcript is capped by the memory budget, not by the whole
    prompt budget -- otherwise memory would eat the passages' room."""
    fat = [Turn(index=i + 1, question="q " * 200, answer="a " * 200) for i in range(4)]
    plan = PassagesFirstPolicy().allocate(_inputs(recent_turns=fat))
    memory_tokens = sum(b.tokens for b in plan.blocks if b.tier == TIER_RECENT)
    assert memory_tokens <= Config.MEMORY_BUDGET_TOKENS * 1.2, memory_tokens


def test_passages_win_when_the_budget_is_tight() -> None:
    """Hardware fit outranks goal fit (`CLAUDE.md`): if retrieval alone fills
    the budget, memory gets zero and the plan SAYS SO in provenance -- it does
    not quietly overspend the context window."""
    plan = PassagesFirstPolicy().allocate(_inputs(budget_tokens=10))
    assert not plan.has_memory
    passage = [b for b in plan.blocks if b.tier == TIER_PASSAGES][0]
    assert "over budget" in passage.provenance
    assert passage.text == _PASSAGES, "passages must never be truncated mid-text"


def test_turns_are_admitted_whole_and_newest_first() -> None:
    """A half turn is a transcript that lies about what was said. With room for
    one turn, the block contains exactly one turn, and it is the LAST one --
    the anaphoric follow-up ("why does that matter later on?") points at the
    most recent turn, not the first."""
    saved = Config.__dict__.get("MEMORY_BUDGET_TOKENS")
    try:
        Config.MEMORY_BUDGET_TOKENS = 20
        turns = [
            Turn(index=1, question="q one " * 4, answer="a one " * 4),
            Turn(index=2, question="q two " * 4, answer="a two " * 4),
        ]
        plan = PassagesFirstPolicy().allocate(_inputs(recent_turns=turns))
        text = plan.memory_block()
        assert "a two" in text and "a one" not in text, text
    finally:
        if saved is None:
            del Config.MEMORY_BUDGET_TOKENS
        else:
            Config.MEMORY_BUDGET_TOKENS = saved


def test_the_injected_transcript_is_fenced_as_untrusted_data() -> None:
    """Step 5 owns the real fence and step 8 is the only thing that can put a
    number behind it. This asserts only that the framing EXISTS -- injecting a
    user's own words back into their prompt with no framing at all, for three
    batches, is shipping the vector while its defense waits."""
    text = PassagesFirstPolicy().allocate(_inputs()).memory_block()
    assert "never instructions" in text
    assert text.rstrip().endswith("[End of transcript.]")


# --- step 2(a): the recent-turns store ---------------------------------------


def test_turns_are_scoped_to_tenant_and_session() -> None:
    """`session_id` is client-supplied and unauthenticated. Tenant B presenting
    tenant A's session id must see nothing -- otherwise the id itself is a
    credential, which it has never been."""
    store = SessionTurns()
    store.append(TENANT_A, "s-1", "q", "a")
    assert len(store.recent(TENANT_A, "s-1")) == 1
    assert store.recent(TENANT_B, "s-1") == []
    assert store.recent(TENANT_A, "s-2") == []


def test_a_sessionless_request_stores_and_reads_nothing() -> None:
    """No `session_id` means memory-free, which is what keeps turn-1 and every
    sessionless caller in the cacheable population under G-P12(b)."""
    store = SessionTurns()
    store.append(TENANT_A, None, "q", "a")
    assert store.recent(TENANT_A, None) == []


def test_an_empty_answer_is_not_a_turn() -> None:
    """A zero-token completion (R33's path) produced no reply, so recording one
    would put an empty assistant turn into every later prompt."""
    store = SessionTurns()
    store.append(TENANT_A, "s-1", "q", "")
    assert store.recent(TENANT_A, "s-1") == []


def test_the_store_is_bounded_in_both_dimensions() -> None:
    """Turns per session and sessions per process. Unbounded process memory is
    the one way an in-process store can hurt a box this size."""
    store = SessionTurns()
    for i in range(MAX_RECENT_TURNS + 3):
        store.append(TENANT_A, "s-1", f"q{i}", f"a{i}")
    kept = store.recent(TENANT_A, "s-1")
    assert len(kept) == MAX_RECENT_TURNS
    assert kept[-1].answer == f"a{MAX_RECENT_TURNS + 2}", "the OLDEST turns must be the ones dropped"

    store = SessionTurns(max_sessions=3)
    for i in range(5):
        store.append(TENANT_A, f"s-{i}", "q", "a")
    assert len(store._sessions) == 3
    assert store.recent(TENANT_A, "s-0") == [], "the least recently used session must be evicted"
    assert MAX_TRACKED_SESSIONS > 0


def test_purge_removes_one_tenant_and_leaves_the_other() -> None:
    """The in-process half of step 6. A purge that leaves live transcripts in
    RAM has not purged; the durable half is Batch C's, once CP-6a says what the
    durable rows are."""
    store = SessionTurns()
    store.append(TENANT_A, "s-1", "q", "a")
    store.append(TENANT_A, "s-2", "q", "a")
    store.append(TENANT_B, "s-3", "q", "a")
    assert store.purge_tenant(TENANT_A) == 2
    assert store.recent(TENANT_A, "s-1") == []
    assert len(store.recent(TENANT_B, "s-3")) == 1


def test_nothing_in_this_module_can_persist_a_turn() -> None:
    """C-1, asserted structurally rather than promised in a docstring.

    The charter §IV invariant is that no verbatim user text is persisted, and
    `phase-6-memory.md:52`'s "verbatim within the session only" is true only
    while the store has no durable backing. So: this module imports no
    database, opens no file, and contains no SQL. If Batch C adds a table, it
    adds it for the STRUCTURED tiers, and this assertion is what forces that
    conversation to happen at CP-6a instead of silently.
    """
    source = inspect.getsource(memory_mod)
    for forbidden in ("src.db", "sqlite3", "INSERT INTO", "open(", "pickle", "json.dump"):
        assert forbidden not in source, (
            f"src/memory.py mentions {forbidden!r} -- recent turns are verbatim "
            f"user text and must not reach durable storage (charter IV, C-1). "
            f"If CP-6a chose option (ii), amend this test WITH the decision."
        )


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
