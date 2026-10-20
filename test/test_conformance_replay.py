# test/test_conformance_replay.py
"""v3 P0 B2 verify (schedule-p0 III.6 items 1, 6; CP2 decision rule): every recorded fixture of a
surface replays through the generic adapter plus its profile to the expected normalized output
(text, finish reason, usage, error class). Jev replays through `decide()`.

Tier 1: no network (in-memory transport), no key. The script prints a pass count per
surface / model / profile for CP2. Run from the repo root after `python scripts/scrub_fixtures.py`.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "test")

import provider_replay as pr  # noqa: E402

from src.gateway.contract import normalize_finish_reason  # noqa: E402
from src.gateway.jev import JevDecider, noul_probability  # noqa: E402
from src.gateway.openai_compat import ErrorClass, ProviderError, classify  # noqa: E402
from src.gateway.retry import TransportError  # noqa: E402
from src.limits import estimate_tokens  # noqa: E402
from src.pricing import UNPRICED  # noqa: E402

JEV_MODEL = "typesafe/jev-1.14"
MODELS = {"openrouter": "openai/gpt-oss-120b", "baseten": "openai/gpt-oss-120b", "purdue": "gpt-oss:120b"}

# Expected normalized output per recorded stream fixture. `reasoning_chars` is the observed length of
# the streamed reasoning text; `reasoning_tokens` is what the surface reports.
STREAMS = {
    ("openrouter", "stream_ok"): dict(
        text="ok", finish="stop", prompt=74, completion=41, billed=0.00003527, upstream="Parasail",
        reasoning_tokens=30, reasoning_chars=126,
    ),
    ("openrouter", "stream_truncated"): dict(
        text="", finish="length", prompt=74, completion=5, billed=0.00000495, upstream="Crusoe",
        reasoning_tokens=2, reasoning_chars=8,
    ),
    ("baseten", "stream_ok"): dict(
        text="ok", finish="stop", prompt=85, completion=32, billed=None, upstream=None,
        reasoning_tokens=0, reasoning_chars=92,
    ),
    ("baseten", "stream_truncated"): dict(
        text="", finish="length", prompt=85, completion=5, billed=None, upstream=None,
        reasoning_tokens=0, reasoning_chars=13,
    ),
    ("purdue", "stream_ok"): dict(
        text="ok", finish="stop", prompt=None, completion=None, billed=None, upstream=None,
        reasoning_tokens=None, reasoning_chars=228,
    ),
    ("purdue", "stream_truncated"): dict(
        text="", finish="length", prompt=None, completion=None, billed=None, upstream=None,
        reasoning_tokens=None, reasoning_chars=8,
    ),
}
# Bad-key shapes (F23, F26, F28): (HTTP status, error shape, error class).
AUTH = {"openrouter": (401, "object"), "baseten": (403, "string"), "purdue": (401, "detail")}


def _replay_stream(surface: str, name: str) -> pr.Replay:
    fx = pr.fixture(surface, name)
    request = fx["request"]
    seen: list = []
    prov = pr.provider(surface, request["model"], pr.transport(fx, seen))
    prov._max_tokens = request["max_tokens"]
    replay = pr.run(prov)
    assert len(seen) == 1
    sent = json.loads(seen[0].content)
    assert sent["model"] == request["model"] and sent["messages"] == request["messages"], "payload drifted"
    assert sent["stream"] is True and sent["max_tokens"] == request["max_tokens"]
    # The profile reproduces the recorded stream_options choice per surface.
    assert ("stream_options" in sent) == ("stream_options" in request), surface
    assert seen[0].headers["authorization"] == f"Bearer {pr.TEST_KEY}"
    assert seen[0].headers["user-agent"].startswith("mitya-gateway/")
    assert str(seen[0].url) == pr.profile(surface).base_url + "/chat/completions"
    return replay


def _check_stream(surface: str, name: str) -> None:
    want = STREAMS[(surface, name)]
    got = _replay_stream(surface, name)
    assert got.error is None, got.error
    assert got.text == want["text"], got.text
    assert normalize_finish_reason(got.usage.finish_reason, got.usage.native_finish_reason).value == want["finish"]
    assert (got.usage.prompt_tokens, got.usage.completion_tokens) == (want["prompt"], want["completion"])
    assert got.usage.billed_usd == want["billed"], got.usage.billed_usd
    assert got.usage.upstream == want["upstream"]
    assert got.usage.reasoning_tokens == want["reasoning_tokens"]
    assert got.usage.reasoning_chars == want["reasoning_chars"], got.usage.reasoning_chars
    # No rate row bound: list cost is UNPRICED, never 0 (N-EV10).
    assert got.usage.cost == UNPRICED
    fx = pr.fixture(surface, name)
    limits = {k: v for k, v in got.usage.ratelimit.items()}
    assert limits == {k: v for k, v in fx["headers"].items() if k.startswith("x-ratelimit-")}


def _check_generic_stream(surface: str, name: str) -> None:
    """Structural expectations for a surface with no hand-written known answers (Groq, once
    recorded): the stream replays clean, ends as its case says, and the profile's usage mode holds."""
    got = _replay_stream(surface, name)
    assert got.error is None, got.error
    want_finish = {"stream_ok": "stop", "stream_truncated": "length"}[name]
    assert normalize_finish_reason(got.usage.finish_reason, got.usage.native_finish_reason).value == want_finish
    if name == "stream_ok":
        assert got.text.strip(), "an ok stream with no answer text"
    if pr.profile(surface).usage != "absent":
        assert isinstance(got.usage.prompt_tokens, int) and isinstance(got.usage.completion_tokens, int), got.usage


def _check_auth(surface: str) -> None:
    status, shape = AUTH[surface]
    fx = pr.fixture(surface, "auth_error")
    assert fx["status"] == status
    # The recorded body uses exactly the error shape the profile declares.
    fault = classify(fx["status"], json.loads(fx["body"]), pr.profile(surface).error_shape)
    assert fault is not None and fault.form == shape == pr.profile(surface).error_shape
    got = pr.run(pr.provider(surface, fx["request"]["model"], pr.transport(fx)))
    assert isinstance(got.error, ProviderError), got.error
    assert got.error.error_class is ErrorClass.AUTH and got.error.status == status
    assert got.text == ""
    # The sentinel key (and any key) never reaches the message.
    assert pr.TEST_KEY not in str(got.error)


def _check_models(surface: str) -> None:
    fx = pr.fixture(surface, "models")
    assert fx["status"] == 200 and fx["model_listed"] is True and fx["n_models"] >= 1


def _check_jev() -> None:
    fx = pr.fixture("jev", "first_call")
    request, seen = fx["request"], []
    decider = JevDecider(
        JEV_MODEL, pr.profile("openrouter"), route=pr.route(JEV_MODEL), rate_model=None,
        transport=pr.transport(fx, seen),
    )
    result = asyncio.run(_decide(decider, request["state"], request["questions"]))
    assert result.answers == {"suspect": {"type": "noul", "noul": 0.98}}
    assert noul_probability(result.answers, "suspect") == 0.98
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (303, 21)
    assert result.usage.billed_usd == 0.000012726
    assert result.usage.cost == UNPRICED
    assert result.usage.resolved_model == "typesafe/jev-1.14-20261014" and result.usage.upstream == "TypeSafe"
    sent = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/systemone"
    assert sent["model"] == JEV_MODEL and sent["questions"] == request["questions"]
    assert sent["provider"] == {"order": ["typesafe"], "allow_fallbacks": False, "zdr": True}


async def _decide(decider, state, questions):
    saved = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = pr.TEST_KEY
    try:
        return await decider.decide(state, questions, "tenant-a", None)
    finally:
        if saved is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = saved


def conformance_matrix() -> dict[tuple[str, str, str], list[int]]:
    """(surface, model, profile) -> [passed, total] over every recorded fixture."""
    rows: dict[tuple[str, str, str], list[int]] = {}

    def record(surface: str, model: str, profile_name: str, check, *args) -> None:
        row = rows.setdefault((surface, model, profile_name), [0, 0])
        row[1] += 1
        try:
            check(*args)
            row[0] += 1
        except AssertionError as exc:
            print(f"  FAIL {surface}/{model}: {check.__name__}{args}: {exc}")

    surfaces = dict(MODELS)
    if (pr.FIXTURES / "groq").is_dir():
        surfaces["groq"] = "openai/gpt-oss-120b"
        AUTH.setdefault("groq", (401, "object"))
    for surface, model in surfaces.items():
        for name in ("stream_ok", "stream_truncated"):
            check = _check_stream if (surface, name) in STREAMS else _check_generic_stream
            record(surface, model, surface, check, surface, name)
        record(surface, model, surface, _check_auth, surface)
        record(surface, model, surface, _check_models, surface)
    record("jev", JEV_MODEL, "openrouter", _check_jev)
    return rows


def test_every_recorded_fixture_replays_to_the_expected_output() -> None:
    rows = conformance_matrix()
    for key, (passed, total) in sorted(rows.items()):
        assert passed == total, f"{key}: {passed}/{total}"
    # 4 per generic surface (ok, truncated, auth, models) and the Jev call.
    for (surface, _, _), (_, total) in rows.items():
        assert total == (1 if surface == "jev" else 4), (surface, total)
    assert {"openrouter", "baseten", "purdue", "jev"} <= {surface for surface, _, _ in rows}


def test_fixtures_on_disk_are_all_covered_by_a_case() -> None:
    covered = {(s, n) for s, n in STREAMS} | {(s, "auth_error") for s in AUTH} | {
        (s, "models") for s in MODELS
    } | {("jev", "first_call")}
    on_disk = {(p.parent.name, p.stem) for p in pr.FIXTURES.rglob("*.json")}
    # A recorded Groq set, once scrubbed in, adds a surface: it must get cases before CP2 reads this.
    # Tool-call fixtures are recorded only (OI13 is P6) and have no replay case yet.
    extra = {(s, n) for s, n in on_disk if s == "groq" or n == "tool_call"}
    assert on_disk - covered <= extra, on_disk - covered
    assert covered <= on_disk, covered - on_disk


def test_baseten_token_accounting_instrument() -> None:
    """F27: `completion_tokens` includes the streamed reasoning while `reasoning_tokens` reads 0.
    Instrument note: reasoning_tokens == 0 here is a claim about Baseten's reporting, not about the
    model; the streamed `reasoning_content` is the independent witness."""
    for name in ("stream_ok", "stream_truncated"):
        got = _replay_stream("baseten", name)
        assert got.usage.reasoning_tokens == 0
        assert got.usage.reasoning_chars > 0, "reasoning streamed"
        answer_tokens = estimate_tokens(got.text) if got.text else 0
        reasoning_estimate = got.usage.reasoning_chars // 4  # char/4, the repo's estimator
        assert got.usage.completion_tokens >= answer_tokens + reasoning_estimate, (
            got.usage.completion_tokens, answer_tokens, reasoning_estimate,
        )
        print(
            f"  baseten {name}: completion_tokens={got.usage.completion_tokens} "
            f"answer~{answer_tokens} reasoning_chars={got.usage.reasoning_chars} "
            f"(~{reasoning_estimate} tokens) reasoning_tokens={got.usage.reasoning_tokens}"
        )
    # Contrast: OpenRouter reports its reasoning tokens.
    assert _replay_stream("openrouter", "stream_ok").usage.reasoning_tokens == 30


def test_profiles_declare_what_the_fixtures_show() -> None:
    """usage mode: one usage object (last_chunk), one per chunk (cumulative), none (absent)."""
    counts = {}
    for surface in MODELS:
        body = pr.fixture(surface, "stream_ok")["body"]
        counts[surface] = sum(
            1 for line in body.splitlines()
            if line.startswith("data:") and '"usage"' in line and '"usage": null' not in line
        )
    assert counts["openrouter"] == 1 and pr.profile("openrouter").usage == "last_chunk"
    assert counts["baseten"] > 1 and pr.profile("baseten").usage == "cumulative"
    assert counts["purdue"] == 0 and pr.profile("purdue").usage == "absent"


def test_transport_faults_are_data_for_the_retry_loop() -> None:
    """A retryable fault replays as TransportError with its class; the recorded 429 shape is not on
    disk (P-1 recorded none), so this is the synthetic half."""
    fx = {"status": 429, "headers": {"content-type": "application/json", "retry-after": "7"},
          "body": '{"error": {"message": "slow down", "code": 429}}'}
    got = pr.run(pr.provider("openrouter", MODELS["openrouter"], pr.transport(fx)))
    assert isinstance(got.error, TransportError)
    assert got.error.error_class == "rate_limit" and got.error.retry_after == 7.0


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed\n\nconformance pass counts (recorded fixtures only):")
    for (surface, model, profile_name), (passed, total) in sorted(conformance_matrix().items()):
        print(f"  {surface:<11}{model:<28}profile={profile_name:<11}{passed}/{total}")
    if not (pr.FIXTURES / "groq").is_dir():
        print("  groq       (no recorded fixture; not generic until one replays, CP2 rule)")


if __name__ == "__main__":
    main()
