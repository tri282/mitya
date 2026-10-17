# test/test_derive_config.py
"""scripts/derive_config.py: a derived version differs from its base only by the overrides, and
an override the artifact would reject is refused before anything is written.

Plain-assert script. Offline, no network, writes nothing. Run from the repo root.
"""

import sys

sys.path.insert(0, ".")

from scripts.derive_config import derive, parse_override  # noqa: E402
from src.config_artifact import load, read_pointer  # noqa: E402

CANARY = "MITYA-CANARY-7Q4X"


def test_parse_override_json_and_raw() -> None:
    assert parse_override("top_k_rerank=2") == ("top_k_rerank", 2)
    assert parse_override(f"guard_canary={CANARY}") == ("guard_canary", CANARY)
    for bad in ("guard_canary", "=x"):
        try:
            parse_override(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} accepted")


def test_canary_changes_version_and_nothing_else() -> None:
    base = load(read_pointer())
    derived = derive(base, {"guard_canary": CANARY})
    assert derived.version() != base.version()
    assert derived.model_dump() == {**base.model_dump(), "guard_canary": CANARY}
    # No override: same content, same version.
    assert derive(base, {}).version() == base.version()


def test_unknown_field_refused() -> None:
    try:
        derive(load(read_pointer()), {"no_such_field": 1})
    except ValueError:
        return
    raise AssertionError("unknown field accepted")


def test_invalid_value_refused() -> None:
    try:
        derive(load(read_pointer()), {"top_k_rerank": "three"})
    except Exception:  # pydantic ValidationError; ServingConfig may wrap it
        return
    raise AssertionError("invalid value accepted")


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
