# test/test_gateway_ladder.py
"""P2 step 10 verify (schedule Batch B): the degradation ladder as policy --
src/gateway/ladder.py. Pure functions, no service, no network -- P4
exercises the ladder under real fault injection; this only checks the
decision function itself.

Plain-assert script, not pytest.
"""

import sys

sys.path.insert(0, ".")

from src.gateway.ladder import Readiness, Rung, next_rung, readiness_from_status  # noqa: E402


def test_everything_up_serves_the_primary_rung() -> None:
    r = Readiness(generation_available=True, retrieval_available=True, cache_available=True)
    assert next_rung(r, cache_hit=False) == Rung.PRIMARY


def test_generation_down_with_a_cache_hit_serves_cached() -> None:
    r = Readiness(generation_available=False, retrieval_available=True, cache_available=True)
    assert next_rung(r, cache_hit=True) == Rung.CACHED


def test_generation_down_with_no_cache_hit_falls_to_retrieval_only() -> None:
    """A cache MISS does not fall back to a cache rung just because caching
    is enabled -- there is no answer to serve. Retrieval-only if retrieval
    is still up."""
    r = Readiness(generation_available=False, retrieval_available=True, cache_available=True)
    assert next_rung(r, cache_hit=False) == Rung.RETRIEVAL_ONLY


def test_generation_and_retrieval_both_down_with_no_cache_hit_is_unavailable() -> None:
    r = Readiness(generation_available=False, retrieval_available=False, cache_available=True)
    assert next_rung(r, cache_hit=False) == Rung.UNAVAILABLE


def test_a_tripped_spend_breaker_falls_off_primary_even_when_generation_is_up() -> None:
    """degraded=True is step 4's spend-rate breaker having tripped -- the
    model is reachable, spending on it right now is not, so this must NOT
    read as Rung.PRIMARY."""
    r = Readiness(generation_available=True, retrieval_available=True, cache_available=True, degraded=True)
    assert next_rung(r, cache_hit=True) == Rung.CACHED


def test_cache_disabled_never_serves_the_cached_rung() -> None:
    r = Readiness(generation_available=False, retrieval_available=True, cache_available=False)
    assert next_rung(r, cache_hit=True) == Rung.RETRIEVAL_ONLY


def test_readiness_from_status_reads_llama_and_qdrant_independently() -> None:
    """retrieval_available must track Qdrant specifically, not the overall
    /ready 'healthy' flag -- otherwise 'generation is down' and 'retrieval is
    down' collapse into one bit and the ladder cannot tell them apart."""
    result = {
        "llama_server": "unreachable: connection refused",
        "qdrant": {"collection": "dostoevsky", "count": 4200},
    }
    readiness = readiness_from_status(result=result, cache_available=True)
    assert readiness.generation_available is False
    assert readiness.retrieval_available is True
    assert next_rung(readiness, cache_hit=False) == Rung.RETRIEVAL_ONLY


def test_readiness_from_status_treats_an_empty_collection_as_retrieval_down() -> None:
    result = {"llama_server": "ok", "qdrant": {"collection": "dostoevsky", "count": 0}}
    readiness = readiness_from_status(result=result, cache_available=False)
    assert readiness.retrieval_available is False
    assert readiness.generation_available is True
    assert next_rung(readiness, cache_hit=False) == Rung.PRIMARY  # generation is up, ladder need not descend


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
