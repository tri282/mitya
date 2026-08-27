# test/test_config_version.py
"""P1 step 5 verify: the versioned serving config (src/config_artifact.py).

Covers the three things the artifact's value rests on:
  * hashing is over PARSED values, so "3" and 3 are one version;
  * the hash is stable across serialize/parse round-trips (an unstable hash
    means version churn on every restart, which poisons the telemetry
    dimension silently -- so it gets a test, not an assumption);
  * secrets cannot enter, probed directly with a sentinel rather than trusted
    to the type signature (the step-3 trust boundary).

Plain-assert script, not pytest. Offline: no artifact store, no network, and
no real .env value is read -- the secrets checks write a sentinel into the
environment the way test/test_redaction.py writes one onto Config.
"""

import contextlib
import json
import os
import sys
import tempfile

sys.path.insert(0, ".")
from src import config_artifact  # noqa: E402
from src.config_artifact import ConfigError, ServingConfig, _canonical_json  # noqa: E402

SENTINEL = "SENTINEL-SECRET-do-not-leak-4f9a1c"


def _base() -> ServingConfig:
    return ServingConfig.from_env()


@contextlib.contextmanager
def _sentinel_key():
    """Put a fake API key in the environment. Never reads the real one."""
    previous = os.environ.get("GOOGLE_API_KEY")
    os.environ["GOOGLE_API_KEY"] = SENTINEL
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GOOGLE_API_KEY", None)
        else:
            os.environ["GOOGLE_API_KEY"] = previous


@contextlib.contextmanager
def _store(tmp: str):
    """Point the artifact store at a temp dir, and drop the process cache."""
    saved = (config_artifact.SERVING_DIR, config_artifact.CURRENT_POINTER)
    config_artifact.SERVING_DIR = os.path.join(tmp, "serving")
    config_artifact.CURRENT_POINTER = os.path.join(config_artifact.SERVING_DIR, "CURRENT")
    config_artifact.reset_active()
    try:
        yield
    finally:
        config_artifact.SERVING_DIR, config_artifact.CURRENT_POINTER = saved
        config_artifact.reset_active()


# --- hashing --------------------------------------------------------------

def test_input_shape_does_not_change_the_version() -> None:
    """str vs int, float literal, and dict ordering are all the same config."""
    base = _base().model_dump(mode="json")

    as_strings = dict(base, top_k_fused=str(base["top_k_fused"]), max_tokens=str(base["max_tokens"]))
    reordered = dict(reversed(list(base.items())))
    reordered["book_title_aliases"] = dict(reversed(list(base["book_title_aliases"].items())))
    as_float = dict(base, gen_top_p=0.9000)

    versions = {
        ServingConfig(**shape).version()
        for shape in (base, as_strings, reordered, as_float)
    }
    assert len(versions) == 1, f"input shape leaked into the version: {versions}"


def test_round_trip_is_byte_identical() -> None:
    cfg = _base()
    once = cfg.canonical_json()
    twice = ServingConfig(**json.loads(once)).canonical_json()
    assert once == twice
    assert ServingConfig(**json.loads(once)).version() == cfg.version()


def test_a_changed_knob_changes_the_version() -> None:
    cfg = _base()
    bumped = cfg.model_copy(update={"top_k_rerank": cfg.top_k_rerank + 1})
    assert bumped.version() != cfg.version()


def test_canonical_json_is_sorted_compact_ascii() -> None:
    """The serialization rule itself, so a well-meaning edit to _canonical_json
    that reintroduces whitespace or key order is caught here and not by a
    silent version churn in production."""
    out = _canonical_json({"b": 1, "a": "é"})
    assert out == '{"a":"\\u00e9","b":1}', out


# --- secrets (step 3, trust boundary) -------------------------------------

def test_a_secret_shaped_field_name_is_refused() -> None:
    class Leaky(ServingConfig):
        api_key: str = "x"

    try:
        Leaky(**_base().model_dump(mode="json"))
    except ConfigError as exc:
        assert "secret-shaped" in str(exc), exc
    else:
        raise AssertionError("a field named api_key was allowed into the artifact")


def test_a_secret_value_on_an_innocent_field_is_refused() -> None:
    """The field name is innocent; the value is the environment's API key.
    This is the leak a type signature cannot see."""
    with _sentinel_key():
        try:
            ServingConfig(**dict(_base().model_dump(mode="json"), prompt_composition=SENTINEL))
        except ConfigError as exc:
            assert "API keys" in str(exc), exc
        else:
            raise AssertionError("an API key value was allowed into the artifact")


def test_the_secret_probe_is_not_vacuous() -> None:
    """Non-vacuity: the same construction with the sentinel absent from the
    environment must SUCCEED, or the test above would pass for the wrong
    reason (see R39/R42 -- tests passing because their subject was never
    exercised)."""
    previous = os.environ.pop("GOOGLE_API_KEY", None)
    try:
        cfg = ServingConfig(**dict(_base().model_dump(mode="json"), prompt_composition=SENTINEL))
        assert cfg.prompt_composition == SENTINEL
    finally:
        if previous is not None:
            os.environ["GOOGLE_API_KEY"] = previous


# --- store, fail-closed (step 4) ------------------------------------------

def test_missing_pointer_refuses_to_start() -> None:
    with tempfile.TemporaryDirectory() as tmp, _store(tmp):
        try:
            config_artifact.read_pointer()
        except ConfigError as exc:
            assert "no serving config pointer" in str(exc), exc
        else:
            raise AssertionError("a missing pointer did not refuse to start")


def test_pointer_naming_an_absent_version_refuses_to_start() -> None:
    with tempfile.TemporaryDirectory() as tmp, _store(tmp):
        os.makedirs(config_artifact.SERVING_DIR, exist_ok=True)
        with open(config_artifact.CURRENT_POINTER, "w", encoding="utf-8") as f:
            f.write("deadbeefcafe\n")
        try:
            config_artifact.load(config_artifact.read_pointer())
        except ConfigError as exc:
            assert "not found" in str(exc), exc
        else:
            raise AssertionError("an absent version did not refuse to start")


def test_an_invalid_artifact_refuses_to_start() -> None:
    with tempfile.TemporaryDirectory() as tmp, _store(tmp):
        os.makedirs(config_artifact.SERVING_DIR, exist_ok=True)
        broken = dict(_base().model_dump(mode="json"))
        broken.pop("top_k_fused")
        with open(config_artifact.artifact_path("0123456789ab"), "w", encoding="utf-8") as f:
            json.dump(broken, f)
        try:
            config_artifact.load("0123456789ab")
        except ConfigError as exc:
            assert "failed validation" in str(exc), exc
        else:
            raise AssertionError("an invalid artifact did not refuse to start")


def test_an_edited_artifact_refuses_to_start() -> None:
    """Content-addressing is only worth something if it is checked: a file
    whose contents no longer hash to its name names a config nobody ran."""
    with tempfile.TemporaryDirectory() as tmp, _store(tmp):
        version = config_artifact.save(_base())
        with open(config_artifact.artifact_path(version), encoding="utf-8") as f:
            data = json.load(f)
        data["top_k_rerank"] += 1
        with open(config_artifact.artifact_path(version), "w", encoding="utf-8") as f:
            json.dump(data, f)
        try:
            config_artifact.load(version)
        except ConfigError as exc:
            assert "edited in place" in str(exc), exc
        else:
            raise AssertionError("a hand-edited artifact loaded anyway")


def test_save_then_load_round_trips_through_the_pointer() -> None:
    with tempfile.TemporaryDirectory() as tmp, _store(tmp):
        cfg = _base()
        version = config_artifact.save(cfg)
        with open(config_artifact.CURRENT_POINTER, "w", encoding="utf-8") as f:
            f.write(version + "\n")
        assert config_artifact.active_version() == version
        assert config_artifact.active_serving_config() == cfg
        assert config_artifact.active_config_version() == version


def test_the_env_override_wins_over_the_pointer() -> None:
    """What P3's subprocess-per-arm and P7's search both rely on."""
    with tempfile.TemporaryDirectory() as tmp, _store(tmp):
        base = _base()
        other = base.model_copy(update={"top_k_rerank": base.top_k_rerank + 1})
        config_artifact.save(base)
        other_version = config_artifact.save(other)
        with open(config_artifact.CURRENT_POINTER, "w", encoding="utf-8") as f:
            f.write(base.version() + "\n")

        os.environ[config_artifact.VERSION_ENV] = other_version
        try:
            config_artifact.reset_active()
            assert config_artifact.active_config_version() == other_version
            assert config_artifact.active_serving_config().top_k_rerank == other.top_k_rerank
        finally:
            os.environ.pop(config_artifact.VERSION_ENV, None)


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
