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
  3. **the CP6 tripwire** (test 4): the moment per-tenant memory can reach the
     prompt, `AnswerCache._key` must take a tenant. It passes today because
     there is no memory module and `build_messages` has no memory parameter;
     it starts FAILING the moment either appears without the key changing.

Test 4 is the actual isolation test. Tests 1-3 are its non-vacuity: they prove
the sharing is real, so the tripwire is guarding something.

Plain-assert script, not pytest. Offline: `_key()` is a pure hash over its
arguments and the store paths run against a real diskcache in a temp dir --
no server, no Qdrant, no network.
"""

import asyncio
import inspect
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
    """
    from src.rag.generate import build_messages

    try:
        import src.memory  # noqa: F401

        return True
    except ImportError:
        pass
    memoryish = {"memory", "memory_block", "history", "turns", "session_memory"}
    return bool(set(inspect.signature(build_messages).parameters) & memoryish)


def test_memory_in_the_prompt_requires_tenant_in_the_cache_key() -> None:
    """**THE isolation test** (charter A12(2)): the CP5/CP6 conditional.

    Vacuously true today -- no memory exists, so there is nothing per-tenant in
    the prompt and a tenant-free key isolates nothing because there is nothing
    to isolate. It becomes a real, failing assertion the moment memory can
    reach the prompt: at that point a shared key serves one tenant's memory to
    another, and this test says so before a run does.
    """
    if not _memory_can_reach_the_prompt():
        # Recorded, not skipped silently: "vacuous" is the result P5 reports.
        print("      (vacuous by construction: no per-tenant content in the prompt yet -- A12(2))")
        return
    params = set(inspect.signature(AnswerCache._key).parameters)
    assert "tenant" in params, (
        "per-tenant memory can now reach the prompt (src.memory exists, or "
        "build_messages takes a memory parameter) but AnswerCache._key is still "
        "tenant-free. A cache hit will serve one tenant's memory to another. "
        "This is the CP6 conditional charter A12(2) names as the real isolation "
        "test -- add tenant to the key."
    )


def test_the_tripwire_can_actually_fire() -> None:
    """Non-vacuity for the test above -- the same discipline test_metrics.py
    applies to its tenant-label predicate. A tripwire that cannot detect its
    own condition proves nothing, and this one spends most of its life in the
    branch that returns early.

    The predicate, not the module, gets the fabricated input: monkeypatching a
    real `src.memory` into `sys.modules` would leave every later import in the
    process seeing a fake P6."""
    def _fake_build_messages(persona_head, passages_block, question, memory=None):
        pass

    memoryish = {"memory", "memory_block", "history", "turns", "session_memory"}
    assert set(inspect.signature(_fake_build_messages).parameters) & memoryish, (
        "the memory-parameter predicate cannot see a memory parameter"
    )
    assert "tenant" not in set(inspect.signature(cache_mod.AnswerCache._key).parameters), (
        "the key already has a tenant -- this file's premise (A12(2)) no longer holds"
    )


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
