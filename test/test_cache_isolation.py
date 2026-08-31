# test/test_cache_isolation.py
"""v2 P5 step 4 verify: the cross-tenant answer-cache leak test.

**Read this before reading the assertions, because they are not what the phase
file leads you to expect.** `04-plan/phase-5-auth-tenancy.md` step 4 says
"re-key the cache with tenant". Charter **A12(2)** (2026-08-24) supersedes it
and `02-decide/decisions-p5.md` retracts C9 to match (R103):

    "The answer cache is deliberately NOT tenant-keyed while the corpus is
    shared... with no per-tenant documents there is nothing to leak. Key is
    (query, filters, config_version, route). Tenant enters the key at CP6 and
    not before -- memory is per-tenant data in the prompt, and a shared key
    would serve one tenant's memory to another. That single conditional is
    CP5/CP6's real isolation test."

So P5's cross-tenant cache-leak test is **vacuous by construction**, and the
charter instructs that it be recorded as such rather than reported as a pass.
This file is that record. It asserts three things:

  1. the key is tenant-free and two tenants therefore SHARE an entry -- the
     "leak", demonstrated deliberately and under concurrency, because a shared
     corpus makes it a cost saving rather than a disclosure (tests 1-2);
  2. nothing per-tenant is in what gets shared -- the cached value's shape is
     closed and none of its fields is tenant-derived (test 3). This is what
     makes claim 1 safe rather than merely true;
  3. **the CP6 isolation test** (test 4), AMENDED at v2 P6 Batch A -- see
     below. Memory now reaches the prompt, so the conditional has fired.

**The amendment, and why the assertion changed** (`02-decide/decisions-p6.md`,
G-P12(b), 2026-08-31). The tripwire used to assert that `AnswerCache._key`
gains a `tenant` parameter once memory can reach the prompt. That arm is
retired: R126 `[measured]` put the hit rate of any correct memory-keyed cache
at **0.000**, and the only fingerprint that recovered anything (0.157)
recovered it *by colliding differing memory states* -- i.e. by reintroducing
the leak the key existed to close. So step 7's "extend the cache key with
memory state" is retired as written-in-error, and A12(2)'s disjunct is
satisfied through its OTHER arm instead: **nothing memory-bearing is cached at
all.** Tenant never enters the key; the cached population is memory-free and
therefore tenant-invariant, and C9 is CLOSED.

Test 4 is therefore now: **a memory-bearing request neither reads nor writes
the cache, and a memory-free one still does.** Both halves, on the real
`/chat` path, in one session -- because "no cache write happened" is exactly
what a dead code path also produces, and P5 spent R112/R115/C9 learning that
those two look identical from outside. Asserting only the absence is how a
vacuous pass gets reported as an isolation pass.

Tests 1-3 are the non-vacuity of the population that REMAINS cached: they
prove the sharing among memory-free requests is real, so the bypass is
guarding something.

Plain-assert script, not pytest. Offline: `_key()` is a pure hash over its
arguments and the store paths run against a real diskcache in a temp dir --
no server, no Qdrant, no network.
"""

import asyncio
import inspect
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

import src.rag.cache as cache_mod  # noqa: E402
from src.config import Config  # noqa: E402
from src.rag.cache import AnswerCache  # noqa: E402

_CFG = "451327aab528"
_CORPUS = "abc123abc123"
_QUERY = "why does raskolnikov confess?"

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _fresh_cache() -> tuple[AnswerCache, str]:
    """A real diskcache in a temp dir -- the store path matters here, not just
    the hash: a leak would be observed through get()/set(), not through _key()."""
    tmp = tempfile.mkdtemp(prefix="mitya-cache-isolation-")
    old_dir, old_enabled = Config.CACHE_DIR, Config.CACHE_ENABLED
    Config.CACHE_DIR, Config.CACHE_ENABLED = tmp, True
    try:
        return AnswerCache(), tmp
    finally:
        Config.CACHE_DIR, Config.CACHE_ENABLED = old_dir, old_enabled


def test_the_key_takes_no_tenant_at_all() -> None:
    """A12(2), recorded structurally: there is no tenant parameter to pass, so
    a caller cannot key on tenant even by accident."""
    params = set(inspect.signature(AnswerCache._key).parameters) - {"self"}
    assert params == {"query", "filters", "route", "config_version", "corpus_version"}, params


def test_two_tenants_share_one_entry_under_concurrency() -> None:
    """The "leak attempt", run concurrently as decisions-p5.md requires, and it
    SUCCEEDS -- which is the intended behaviour, not a finding.

    Tenant A stores an answer; tenant B, running concurrently, reads the same
    answer. Under a shared corpus with no per-tenant content in the prompt,
    that is one cost saving, not one disclosure. This assertion is what makes
    the record honest: P5 does not get to report an isolation pass here.
    """
    cache, tmp = _fresh_cache()
    try:
        async def writer() -> None:
            cache.set(
                _QUERY, None, "DEFAULT", _CFG, _CORPUS,
                "He confesses because...", [{"id": 1}], "footer", "local-q4km",
            )

        async def reader() -> dict | None:
            return cache.get(_QUERY, None, "DEFAULT", _CFG, _CORPUS)

        async def both() -> dict | None:
            await writer()
            results = await asyncio.gather(reader(), reader())
            assert results[0] == results[1]
            return results[0]

        seen_by_b = asyncio.run(both())
        assert seen_by_b is not None, (
            "the entry tenant A stored was not visible to tenant B -- if this "
            "fails, the key gained a tenant dimension and A12(2) was reversed "
            "without this file being updated"
        )
        assert seen_by_b["answer"] == "He confesses because..."
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_nothing_in_a_cached_value_is_per_tenant() -> None:
    """Why the sharing above is safe, asserted rather than reasoned.

    `get()` already enforces the value shape exactly (it treats any other key
    set as a miss). Pinning the shape HERE as well is deliberate duplication:
    this is the assertion that fails if P6 adds a memory-derived field to the
    stored value while leaving the key tenant-free, which is the same defect
    the tripwire below catches from the prompt side.
    """
    cache, tmp = _fresh_cache()
    try:
        cache.set(
            _QUERY, None, "DEFAULT", _CFG, _CORPUS,
            "answer", [{"id": 1}], "footer", "local-q4km",
        )
        stored = cache.get(_QUERY, None, "DEFAULT", _CFG, _CORPUS)
        assert set(stored) == {"answer", "sources", "footer", "model"}, set(stored)
        # Each field's provenance, in one line each: the generated text, the
        # retrieved corpus chunks, a rendering of those chunks, and the arm id.
        # None of the four can carry per-tenant content while the prompt has
        # none -- which is exactly the condition the tripwire below watches.
        assert isinstance(stored["model"], str)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _memory_can_reach_the_prompt() -> bool:
    """Has CP6 landed? Two independent signals, so a partial P6 still trips it.

    (a) a `src.memory` module exists and imports; or
    (b) `build_messages()` grew a parameter naming memory/history/turns --
        the prompt composer is the one funnel every per-tenant block must pass
        through to reach a provider.

    Deliberately NOT a version flag or a plan-file read: this has to key off
    the code, because the failure mode it guards is somebody adding memory and
    not remembering the cache.

    **Both signals are TRUE as of P6 Batch A**, which is why the assertion
    below is no longer conditional. The predicate is kept rather than deleted:
    it is what makes the test below refuse to silently become vacuous again if
    the memory module is ever removed.
    """
    from src.rag.generate import build_messages

    try:
        import src.memory  # noqa: F401

        return True
    except ImportError:
        pass
    memoryish = {"memory", "memory_block", "history", "turns", "session_memory"}
    return bool(set(inspect.signature(build_messages).parameters) & memoryish)


class _RecordingCache:
    """`state.cache` for the two drives below. Records every get and set, so
    the assertion is about what the request path DID, not about what a helper
    returned."""

    enabled = True

    def __init__(self) -> None:
        self.store: dict[tuple, dict] = {}
        self.gets: list[tuple] = []
        self.sets: list[tuple] = []

    def get(self, query, filters, route, config_version, corpus_version):
        key = (query, route, config_version, corpus_version)
        self.gets.append(key)
        return self.store.get(key)

    def set(self, query, filters, route, config_version, corpus_version,
            answer, sources, footer, model):
        key = (query, route, config_version, corpus_version)
        self.sets.append(key)
        self.store[key] = {
            "answer": answer, "sources": sources, "footer": footer, "model": model,
        }


async def _drive_two_turns(session_id: str | None) -> "_RecordingCache":
    """Drive TWO `/chat` turns through the real handler and return the cache.

    Same posture as `test/test_chat_semaphore.py`: `chat()` is called directly
    with a stub `app.state` and the pipeline collaborators monkeypatched onto
    `src.api.app`, so the code under test is the actual request path and not a
    re-implementation of it. `state.turns` is a REAL `SessionTurns` -- it is
    pure process memory, and stubbing the very store whose contents decide the
    bypass would be assuming the answer.

    Turn 2 is memory-bearing precisely because turn 1 wrote a turn into that
    store, which is what makes the difference between the two turns causal
    rather than staged.
    """
    import types

    import src.api.app as app_mod
    from src.api.app import ChatRequest, chat
    from src.db import init_schema
    from src.gateway.ladder import Rung
    from src.memory import SessionTurns

    # P6 Batch C: /chat now reads the profile store, so this fixture needs the
    # memory tables. It drives `chat()` directly rather than through the app, so
    # `lifespan()` -- the only caller of `init_schema()` in the request path --
    # never runs. Point DB_PATH at a temp file and initialise it here: the
    # alternative, leaning on whatever `data/mitya.sqlite3` happens to contain,
    # makes the test pass on a developer box and fail on a fresh clone.
    Config.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="mitya-cache-db-"), "t.sqlite3")
    init_schema()

    points = [types.SimpleNamespace(id="chunk-1")]

    async def tokens():
        for text in ("Rodion ", "hesitated."):
            yield text

    async def healthy(state):
        return True, {"generation": "ok", "retrieval": "ok"}

    patches = {
        "route": lambda q, f, structure, summaries: ("DEFAULT", None),
        "_dependency_status": healthy,
        "readiness_from_status": lambda result, cache_available: object(),
        "next_rung": lambda readiness, cache_hit: Rung.PRIMARY,
        "build_hard_filter": lambda filters: None,
        "retrieve": lambda *a, **kw: list(points),
        "retrieve_structural_excerpts": lambda *a, **kw: list(points),
        "rerank": lambda query, pts, ranker: list(pts),
        "build_context": lambda *a, **kw: ("passages", [{"book": "b", "part": 1, "chapter": 1}]),
        "build_messages": lambda head, block, query, **kw: [],
        "active_persona_head": lambda: "",
        "build_sources_footer": lambda sources: "Sources:",
        "stream_answer": lambda messages, usage: tokens(),
    }
    cache = _RecordingCache()
    state = types.SimpleNamespace(
        sem=asyncio.BoundedSemaphore(1),
        cache=cache,
        entities=types.SimpleNamespace(resolve=lambda q, active_book=None: (q, [])),
        qdrant=None, embedder=None, bm25=None, ranker=None,
        summaries={}, structure={},
        turns=SessionTurns(),
    )
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=state))

    saved = {name: getattr(app_mod, name) for name in patches}
    for name, value in patches.items():
        setattr(app_mod, name, value)
    try:
        for _ in range(2):
            body = ChatRequest(query=_QUERY, session_id=session_id)
            response = await chat(body, request, tenant=TENANT_A)
            async for _chunk in response.body_iterator:
                pass
    finally:
        for name, value in saved.items():
            setattr(app_mod, name, value)
    return cache


def test_a_memory_bearing_request_bypasses_the_cache_entirely() -> None:
    """**THE isolation test**, amended (G-P12(b)). Both arms, one session.

    Turn 1 carries no memory (the store is empty), so it must behave exactly as
    it did before P6: one `get` and one `set`. Turn 2 carries turn 1's
    transcript, so it must do NEITHER -- no read, no write.

    The positive half is not decoration. A test that asserted only "turn 2 did
    not write" would pass just as green if the whole cache had been disabled,
    if the stub never reached the request path, or if `chat()` raised before
    the cache at all -- which is the vacuous-by-construction result P5 had to
    report (R112/C9) and the shape R115 caught twice. Two turns differing in
    exactly one property, with opposite outcomes, is what makes the pass mean
    the bypass FIRED.
    """
    assert _memory_can_reach_the_prompt(), (
        "memory can no longer reach the prompt -- src.memory is gone and "
        "build_messages has no memory parameter. This test's premise is P6; "
        "if P6 was reverted, revert this file to its P5 tripwire form rather "
        "than leaving an assertion that cannot fail."
    )
    cache = asyncio.run(_drive_two_turns(session_id="s-iso-1"))

    assert len(cache.gets) == 1, (
        f"expected exactly one cache GET across two turns, saw {len(cache.gets)}. "
        f"Turn 2 is memory-bearing and must not read the cache: a hit there would "
        f"serve an answer produced without this session's transcript."
    )
    assert len(cache.sets) == 1, (
        f"expected exactly one cache SET across two turns, saw {len(cache.sets)}. "
        f"The WRITE half is the half that isolates -- a memory-bearing answer "
        f"stored under a memory-free key is served to the next tenant who asks "
        f"the same question."
    )
    assert cache.store, "turn 1 stored nothing -- the memory-FREE arm is dead, so a green bypass here would be vacuous"


def test_a_sessionless_request_is_never_memory_bearing() -> None:
    """The negative control for the test above, and the population the cache
    still serves: with no `session_id` there is no transcript, so BOTH turns
    are memory-free and both go through the cache -- get, get, and one set (the
    second is a hit)."""
    cache = asyncio.run(_drive_two_turns(session_id=None))
    assert len(cache.gets) == 2, cache.gets
    assert len(cache.sets) == 1, (
        f"expected one SET and then a HIT, saw {len(cache.sets)} sets -- if this "
        f"is 2, turn 2 missed, and the memory-free population is not actually "
        f"sharing entries (tests 1-2's premise)."
    )


def test_the_tripwire_can_actually_fire() -> None:
    """Non-vacuity for the tests above -- the same discipline test_metrics.py
    applies to its tenant-label predicate.

    Three properties, each of which would silently defuse the amended test:
    the memory-parameter predicate can see a memory parameter; the key is still
    tenant-free (this file's premise under G-P12(b), and now the SETTLED shape
    rather than a temporary one); and the recording cache actually records --
    checked against a real `AnswerCache` round trip, not asserted about itself.
    """
    def _fake_build_messages(persona_head, passages_block, question, memory=None):
        pass

    memoryish = {"memory", "memory_block", "history", "turns", "session_memory"}
    assert set(inspect.signature(_fake_build_messages).parameters) & memoryish, (
        "the memory-parameter predicate cannot see a memory parameter"
    )
    assert "tenant" not in set(inspect.signature(cache_mod.AnswerCache._key).parameters), (
        "the key gained a tenant -- G-P12(b) decided the opposite (bypass, not "
        "a key component) on R126's measured 0.000 hit rate. If the key really "
        "must carry tenant now, that reverses a decision, not a test."
    )
    recorder = _RecordingCache()
    recorder.set(_QUERY, None, "DEFAULT", _CFG, _CORPUS, "a", [], "f", "local-q4km")
    assert recorder.sets and recorder.get(_QUERY, None, "DEFAULT", _CFG, _CORPUS) is not None, (
        "the recording cache does not record -- every assertion above would "
        "then read zero writes no matter what the request path did"
    )


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
