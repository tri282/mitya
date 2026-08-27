# test/test_cache_key.py
"""P1 5c verify: `route` is part of the cache key (src/rag/cache.py).

Plain-assert script, not pytest. Offline: no diskcache backend touched --
_key() is a pure hash over its arguments, so this drives it directly.
"""

import sys

sys.path.insert(0, ".")

from src.rag.cache import AnswerCache  # noqa: E402


def test_same_query_different_route_gets_different_keys() -> None:
    cache = AnswerCache()
    default_key = cache._key("what happens in part 1?", None, "DEFAULT")
    structural_key = cache._key("what happens in part 1?", None, "STRUCTURAL")
    assert default_key != structural_key, "route must be part of the key"


def test_same_query_same_route_gets_the_same_key() -> None:
    cache = AnswerCache()
    key_a = cache._key("what happens in part 1?", {"book": "Demons"}, "DEFAULT")
    key_b = cache._key("what happens in part 1?", {"book": "Demons"}, "DEFAULT")
    assert key_a == key_b


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
