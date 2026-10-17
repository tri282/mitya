# test/test_config_identity.py
"""v3 P0 B1 verify (OI26): every answer-determining artifact is in the content hash.

Known-answer per artifact: change one, the version changes; change none, it is stable. The
identity slots added at B1 are omitted while empty, so no version written before them moves.

Plain-assert script. Offline, no network. Run from the repo root.
"""

import glob
import json
import os
import sys

sys.path.insert(0, ".")

from src import config_artifact  # noqa: E402
from src.config_artifact import ServingConfig  # noqa: E402

SLOTS = ("guard_canary", "jev_model", "provider_routes", "adapter_profiles", "provider_safety")
ROUTE = {"order": ["deepinfra"], "allow_fallbacks": False, "zdr": True, "only": []}

_PROBES = {
    "provider_routes": ROUTE,
    "book_title_aliases": "x",
    "provider_safety": [{"category": "c", "threshold": "t"}],
}


def _base_dump() -> dict:
    dump = ServingConfig.from_env().model_dump(mode="json")
    dump.update(guard_canary="", jev_model="", provider_routes={}, adapter_profiles={},
                provider_safety={})
    return dump


def _mutated(dump: dict, name: str):
    value = dump[name]
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 0.125
    if isinstance(value, str):
        return value + "x"
    if isinstance(value, list):
        return value + ["x"]
    if isinstance(value, dict):
        return {**value, "probe": _PROBES.get(name, {"a": 1})}
    raise AssertionError(f"no mutation for {name}: {type(value)}")


def test_a_changed_artifact_changes_the_version_and_an_unchanged_one_does_not() -> None:
    base = _base_dump()
    version = ServingConfig(**base).version()
    assert set(base) == set(ServingConfig.model_fields)
    for name in ServingConfig.model_fields:
        changed = ServingConfig(**{**base, name: _mutated(base, name)}).version()
        assert changed != version, f"changing {name} did not change the version"
        assert ServingConfig(**{**base, name: base[name]}).version() == version, name
    assert set(SLOTS) <= set(ServingConfig.model_fields)


def test_each_route_pin_field_is_hashed() -> None:
    base = _base_dump()
    pinned = ServingConfig(**{**base, "provider_routes": {"openai/gpt-oss-120b": ROUTE}})
    assert pinned.version() != ServingConfig(**base).version()
    for key, new in (("order", ["parasail"]), ("allow_fallbacks", True), ("zdr", False),
                     ("only", ["deepinfra"])):
        other = ServingConfig(**{**base, "provider_routes": {"openai/gpt-oss-120b": {**ROUTE, key: new}}})
        assert other.version() != pinned.version(), key


def test_observed_values_are_not_part_of_the_identity() -> None:
    for observed in ("upstream", "resolved_model", "native_finish_reason", "billed_usd"):
        assert observed not in ServingConfig.model_fields, observed


def test_empty_slots_are_omitted_so_existing_versions_hold() -> None:
    base = ServingConfig(**_base_dump())
    for slot in SLOTS:
        assert f'"{slot}"' not in base.canonical_json(), slot
    for slot in SLOTS:
        dump = _base_dump()
        dump[slot] = _mutated(dump, slot)
        assert f'"{slot}"' in ServingConfig(**dump).canonical_json(), slot


def test_every_loadable_stored_version_still_hashes_to_its_name() -> None:
    """`load()` re-hashes a file and refuses a mismatch. Run it over every stored version
    that carries the full pre-slot field set (older ones were already unloadable: later
    defaulted fields change their hash). None of those moved when the slots were added."""
    pre_slot = set(ServingConfig.model_fields) - set(SLOTS)
    loadable = []
    for path in sorted(glob.glob(os.path.join(config_artifact.SERVING_DIR, "*.json"))):
        with open(path, encoding="utf-8") as f:
            if set(json.load(f)) == pre_slot:
                loadable.append(os.path.basename(path)[: -len(".json")])
    assert config_artifact.read_pointer() in loadable or os.environ.get(config_artifact.VERSION_ENV)
    for version in loadable:
        config_artifact.load(version)
    config_artifact.load(config_artifact.read_pointer())


def test_a_slotted_config_round_trips_to_the_same_version() -> None:
    dump = _base_dump()
    dump.update(
        guard_canary="MITYA-CANARY-7Q4X", jev_model="typesafe/jev-1.14",
        provider_routes={"openai/gpt-oss-120b": ROUTE},
        adapter_profiles={"openrouter": {"reasoning": "reasoning"}},
        provider_safety={"google": [{"category": "c", "threshold": "t"}]},
    )
    cfg = ServingConfig(**dump)
    again = ServingConfig(**json.loads(cfg.canonical_json()))
    assert again.version() == cfg.version() and again.canonical_json() == cfg.canonical_json()


def test_provider_safety_reaches_the_google_adapter() -> None:
    import src.rag.generate as gen

    settings = [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"}]
    model = "models/gemini-3.5-flash-lite"
    try:
        for declared, expected in (({"google": settings}, settings), ({}, None)):
            cfg = ServingConfig(**{**_base_dump(), "provider_safety": declared})
            config_artifact._active, config_artifact._active_version = cfg, cfg.version()
            assert gen._resolve_adapter(model)._safety_settings == expected
    finally:
        config_artifact.reset_active()


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
