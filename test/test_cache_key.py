# test/test_cache_key.py
"""P1 5c/step 9 verify: `route`, `config_version`, and `corpus_version` are
part of the cache key (src/rag/cache.py).

Plain-assert script, not pytest. Offline: no diskcache backend touched --
_key() is a pure hash over its arguments, so this drives it directly.
"""

import sys

sys.path.insert(0, ".")

from src.rag.cache import AnswerCache  # noqa: E402

_CFG_V1 = "451327aab528"
_CFG_V2 = "000000000000"
_CORPUS_V1 = "abc123abc123"
_CORPUS_V2 = "def456def456"


def test_same_query_different_route_gets_different_keys() -> None:
    cache = AnswerCache()
    default_key = cache._key("what happens in part 1?", None, "DEFAULT", _CFG_V1, _CORPUS_V1)
    structural_key = cache._key("what happens in part 1?", None, "STRUCTURAL", _CFG_V1, _CORPUS_V1)
    assert default_key != structural_key, "route must be part of the key"


def test_same_query_same_route_same_config_same_corpus_gets_the_same_key() -> None:
    cache = AnswerCache()
    key_a = cache._key("what happens in part 1?", {"book": "Demons"}, "DEFAULT", _CFG_V1, _CORPUS_V1)
    key_b = cache._key("what happens in part 1?", {"book": "Demons"}, "DEFAULT", _CFG_V1, _CORPUS_V1)
    assert key_a == key_b


def test_same_query_same_route_different_config_gets_different_keys() -> None:
    """P1 step 9: a changed serving config can change what a correct answer
    is (D9's versioned/deployment split), so a stale answer from a prior
    config must not be served as a hit under a new one. Corpus version held
    fixed so this isolates the config dimension."""
    cache = AnswerCache()
    key_v1 = cache._key("what happens in part 1?", None, "DEFAULT", _CFG_V1, _CORPUS_V1)
    key_v2 = cache._key("what happens in part 1?", None, "DEFAULT", _CFG_V2, _CORPUS_V1)
    assert key_v1 != key_v2, "config_version must be part of the key"


def test_same_query_same_route_different_corpus_gets_different_keys() -> None:
    """Coordinator-approved extension: a re-ingest replaces the index that
    grounded every cached answer, so it must not survive as a hit either.
    Config version held fixed so this isolates the corpus dimension."""
    cache = AnswerCache()
    key_v1 = cache._key("what happens in part 1?", None, "DEFAULT", _CFG_V1, _CORPUS_V1)
    key_v2 = cache._key("what happens in part 1?", None, "DEFAULT", _CFG_V1, _CORPUS_V2)
    assert key_v1 != key_v2, "corpus_version must be part of the key"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
