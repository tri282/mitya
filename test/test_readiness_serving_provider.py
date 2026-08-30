# test/test_readiness_serving_provider.py
"""P4 Batch A0 item 1 verify (R89): `/ready`'s `generation_available` tracks
the SERVING provider, not `Config.LLAMA_SERVER_URL`.

Plain-assert script, not pytest. Offline: no server, no Qdrant, no network.
`_dependency_status()` is called directly against a stub state, and
`Config.LLAMA_SERVER_URL` is pointed at a port nothing listens on for every
hosted case -- if the probe were still unconditional, the hosted assertions
below would fail, which is the whole point of the file.

The regression it guards: before this batch the probe ran on every readiness
check regardless of who served, so a healthy hosted deployment reported
`status: degraded`, returned 503, and the SPA rendered "Mitya's study is
dimmed". Nothing in the repo would have caught that, which is why R89 was found
by reading source rather than by a failing test.

NOTE the one heavy import: `src.api.app` builds the FastAPI app at import time
and mounts `frontend/dist`, so this test must be run from the repo root. That
is the cost of testing the real bridge instead of a copy of it.
"""

import asyncio
import sys

sys.path.insert(0, ".")

from src.config import Config  # noqa: E402
from src.gateway.ladder import Rung, next_rung, readiness_from_status  # noqa: E402
from src.rag.generate import LOCAL_PROVIDER, credential_status, serving_provider_name  # noqa: E402
from src.api.app import _dependency_status  # noqa: E402
from src.gateway.local import LOCAL_MODEL_ID  # noqa: E402

# A port nothing listens on. Every hosted assertion runs with llama-server
# unreachable on purpose.
_DEAD_LLAMA = "http://127.0.0.1:1"


class _StubQdrant:
    """Enough of QdrantClient for `_dependency_status()`: a populated
    collection, so retrieval is up and only generation is in question."""

    class _Count:
        count = 4200

    def count(self, collection):
        return self._Count()


class _StubEntities:
    _entities: dict = {}


class _StubState:
    qdrant = _StubQdrant()
    entities = _StubEntities()
    summaries: dict = {}
    structure: dict = {}


def _status(serving_model: str, *, llama_url: str = _DEAD_LLAMA, **config_overrides) -> dict:
    """Run the real `_dependency_status()` under a chosen serving config."""
    saved = {"SERVING_MODEL": Config.SERVING_MODEL, "LLAMA_SERVER_URL": Config.LLAMA_SERVER_URL}
    saved.update({k: getattr(Config, k) for k in config_overrides})
    try:
        Config.SERVING_MODEL = serving_model
        Config.LLAMA_SERVER_URL = llama_url
        for key, value in config_overrides.items():
            setattr(Config, key, value)
        _, result = asyncio.run(_dependency_status(_StubState()))
        return result
    finally:
        for key, value in saved.items():
            setattr(Config, key, value)


def _a_hosted_model(provider: str) -> str:
    """A model id from the real quota table whose provider is `provider` --
    read, never invented, so this test cannot pass against a model the request
    path would resolve differently."""
    for model in Config.MODEL_LIMITS:
        if Config.MODEL_LIMITS[model].get("provider") == provider:
            return model
    raise AssertionError(f"no {provider} model in Config.MODEL_LIMITS")


def test_serving_provider_name_matches_what_provider_for_would_pick() -> None:
    """The readiness path and the request path must resolve the same vendor
    from the same model id -- two resolvers is the defect, not the fix."""
    assert serving_provider_name.__module__ == "src.rag.generate"
    saved = Config.SERVING_MODEL
    try:
        Config.SERVING_MODEL = LOCAL_MODEL_ID
        assert serving_provider_name() == LOCAL_PROVIDER
        Config.SERVING_MODEL = _a_hosted_model("google")
        assert serving_provider_name() == "google"
        Config.SERVING_MODEL = _a_hosted_model("groq")
        assert serving_provider_name() == "groq"
    finally:
        Config.SERVING_MODEL = saved


def test_a_healthy_hosted_deployment_is_not_degraded_by_a_dead_llama_server() -> None:
    """R89 itself. llama-server unreachable, a hosted arm serving with its key
    present: generation is available, and `/ready` therefore reports ready."""
    model = _a_hosted_model("google")
    result = _status(model, GOOGLE_API_KEY="test-key-not-used-for-a-call")
    assert result["generation"]["status"] == "ok", result["generation"]
    assert result["generation"]["provider"] == "google"
    assert result["generation"]["model"] == model
    readiness = readiness_from_status(result, cache_available=True)
    assert readiness.generation_available is True, (
        "generation_available still tracks llama-server -- R89 has regressed"
    )
    assert next_rung(readiness, cache_hit=False) == Rung.PRIMARY


def test_a_hosted_arm_with_no_credential_is_generation_down() -> None:
    """The hosted failure mode that is knowable without spending quota."""
    result = _status(_a_hosted_model("groq"), GROQ_API_KEY=None)
    assert result["generation"]["status"] != "ok"
    assert "GROQ_API_KEY" in result["generation"]["status"]
    readiness = readiness_from_status(result, cache_available=False)
    assert readiness.generation_available is False
    # Retrieval is up (the stub collection is populated), so the ladder must
    # descend to retrieval-only, not to 503.
    assert readiness.retrieval_available is True
    assert next_rung(readiness, cache_hit=False) == Rung.RETRIEVAL_ONLY


def test_the_local_arm_still_probes_llama_server() -> None:
    """The local path is not collapsed away: when llama-server IS what serves,
    its reachability is still the right question and an unreachable one is
    still generation-down."""
    result = _status(LOCAL_MODEL_ID)
    assert result["generation"]["provider"] == LOCAL_PROVIDER
    assert result["generation"]["status"].startswith("unreachable:"), result["generation"]
    assert readiness_from_status(result, cache_available=False).generation_available is False


def test_the_llama_server_field_survives_for_the_spa_but_decides_nothing() -> None:
    """`frontend/src/api/types.ts` declares `llama_server` and the degraded
    tooltip interpolates it. The SPA is out of Batch A0's scope, so the key
    stays -- as a subsystem line that no longer drives the verdict."""
    result = _status(_a_hosted_model("google"), GOOGLE_API_KEY="test-key-not-used-for-a-call")
    assert "llama_server" in result
    assert "not serving generation" in result["llama_server"]
    assert readiness_from_status(result, cache_available=True).generation_available is True


def test_credential_status_makes_no_network_call_and_names_the_variable() -> None:
    """A /health poll must not spend provider quota: Gemini Flash allows 5
    requests per minute (R85) and the SPA polls on an interval."""
    saved = Config.GOOGLE_API_KEY
    try:
        Config.GOOGLE_API_KEY = None
        assert credential_status("google") == "GOOGLE_API_KEY is not set"
        Config.GOOGLE_API_KEY = "k"
        assert credential_status("google") == "ok"
    finally:
        Config.GOOGLE_API_KEY = saved
    assert credential_status("nonesuch").startswith("no credential mapping")


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
