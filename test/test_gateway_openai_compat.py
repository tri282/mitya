# test/test_gateway_openai_compat.py
"""v3 P0 B2 verify: the generic OpenAI-dialect adapter. Wire payload and route pin, error
classification that never trusts the HTTP status alone (200-with-error, JSON null, mid-stream
error, cut stream), no key or provider text in a raised message, key read at send time.

Plain-assert script. Offline: in-memory transport, no network. Run from the repo root after
`python scripts/scrub_fixtures.py`.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "test")

import provider_replay as pr  # noqa: E402

from src.config_artifact import ProviderRoute  # noqa: E402
from src.gateway.contract import Provider, normalize_finish_reason  # noqa: E402
from src.gateway.openai_compat import (  # noqa: E402
    UNDECODABLE,
    ErrorClass,
    OpenAICompatProvider,
    ProviderError,
    classify,
    route_payload,
)
from src.gateway.retry import TransportError  # noqa: E402

OR_MODEL = "openai/gpt-oss-120b"
PIN = {"order": ["deepinfra"], "allow_fallbacks": False, "zdr": True}
JSON_HDR = {"content-type": "application/json"}
SSE_HDR = {"content-type": "text/event-stream"}


def _sse(*chunks) -> str:
    return "".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks)


def _delta(text=None, finish=None, **extra) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": text} if text is not None else {}, "finish_reason": finish}], **extra}


def _run(surface, model, status, headers, body, seen=None):
    return pr.run(pr.provider(surface, model, pr.raw_transport(status, headers, body, seen)))


def test_provider_satisfies_the_protocol() -> None:
    assert isinstance(pr.provider("openrouter", OR_MODEL, pr.raw_transport(200, SSE_HDR, "")), Provider)


def test_openrouter_wire_request_carries_exactly_the_ruled_pin() -> None:
    seen: list = []
    _run("openrouter", OR_MODEL, 200, SSE_HDR, _sse(_delta("ok", "stop"), "[DONE]"), seen)
    sent = json.loads(seen[0].content)
    assert sent["provider"] == PIN, sent["provider"]
    assert sent["model"] == OR_MODEL and "stream_options" not in sent


def test_pin_is_data_in_the_seed_not_a_code_default() -> None:
    route = pr.route(OR_MODEL)
    assert route_payload(route) == PIN
    assert route_payload(ProviderRoute(order=("a",), allow_fallbacks=True, zdr=False, only=("b",))) == {
        "order": ["a"], "allow_fallbacks": True, "zdr": False, "only": ["b"],
    }
    with open("src/gateway/openai_compat.py", encoding="utf-8") as f:
        assert "deepinfra" not in f.read().lower()


def test_an_openrouter_binding_without_a_pin_cannot_be_built() -> None:
    for build in (
        lambda: OpenAICompatProvider("openrouter", OR_MODEL, pr.profile("openrouter"), route=None, rate_model=None),
        lambda: OpenAICompatProvider("baseten", OR_MODEL, pr.profile("baseten"), route=pr.route(OR_MODEL), rate_model=None),
    ):
        try:
            build()
        except ValueError:
            continue
        raise AssertionError("an inconsistent binding was accepted")


def test_other_surfaces_send_no_provider_object_and_their_own_usage_option() -> None:
    for surface, usage_opt in (("baseten", True), ("purdue", False), ("groq", True)):
        seen: list = []
        _run(surface, "m", 200, SSE_HDR, _sse(_delta("ok", "stop"), "[DONE]"), seen)
        sent = json.loads(seen[0].content)
        assert "provider" not in sent, surface
        assert ("stream_options" in sent) is usage_opt, surface


def test_status_is_not_trusted_alone() -> None:
    cases = [
        # (status, body, declared, class, form)
        (401, {"error": {"message": "x", "code": 401}}, "object", ErrorClass.AUTH, "object"),
        (403, {"error": "please check the api-key"}, "string", ErrorClass.AUTH, "string"),
        (401, {"detail": "expired"}, "detail", ErrorClass.AUTH, "detail"),
        (200, {"error": {"message": "x", "code": 429}}, "object", ErrorClass.RATE_LIMIT, "object"),
        (200, {"error": {"message": "x"}}, "object", ErrorClass.SERVER, "object"),
        (200, {"error": {"message": "x", "code": 402, "metadata": {"limit_source": "k"}}}, "object", ErrorClass.QUOTA, "object"),
        (402, {"error": {"message": "x", "metadata": {"limit_source": "k"}}}, "object", ErrorClass.QUOTA, "object"),
        (429, {"error": {"message": "x"}}, "object", ErrorClass.RATE_LIMIT, "object"),
        (503, {"error": {"message": "no provider"}}, "object", ErrorClass.SERVER, "object"),
        (404, {}, "object", ErrorClass.CLIENT, None),
        (500, UNDECODABLE, "object", ErrorClass.SERVER, None),
        (200, UNDECODABLE, "object", ErrorClass.SHAPE, None),
        (200, None, "detail", ErrorClass.EMPTY, None),
        # A surface answering in a shape it did not declare is still an error, and says which shape.
        (401, {"detail": "x"}, "object", ErrorClass.AUTH, "detail"),
    ]
    for status, body, declared, error_class, form in cases:
        fault = classify(status, body, declared)
        assert fault is not None, (status, body)
        assert (fault.error_class, fault.form) == (error_class, form), (status, body, fault)
    for body in ({"choices": []}, {"choices": [{"delta": {"content": "x"}}]}):
        assert classify(200, body, "object") is None, body


def test_json_null_and_empty_bodies_are_retryable_faults_at_status_200() -> None:
    for body in ("null", "", "  \n"):
        got = _run("purdue", "gpt-oss:120b", 200, JSON_HDR, body)
        assert isinstance(got.error, TransportError) and got.error.error_class == "empty", (body, got.error)
    got = _run("purdue", "gpt-oss:120b", 200, SSE_HDR, _sse("null"))
    assert isinstance(got.error, TransportError) and got.error.error_class == "empty"


def test_a_200_json_error_is_classified_by_its_own_code() -> None:
    body = json.dumps({"error": {"message": "upstream said no", "code": 429}})
    got = _run("openrouter", OR_MODEL, 200, JSON_HDR, body)
    assert isinstance(got.error, TransportError) and got.error.error_class == "rate_limit"
    body = json.dumps({"error": {"message": "bad key", "code": 401}})
    got = _run("openrouter", OR_MODEL, 200, JSON_HDR, body)
    assert isinstance(got.error, ProviderError) and got.error.error_class is ErrorClass.AUTH


def test_a_mid_stream_error_event_keeps_the_text_and_raises() -> None:
    body = _sse(
        _delta("partial "),
        {"error": {"message": "provider dropped", "code": 502}, "choices": [{"delta": {}, "finish_reason": "error"}]},
    )
    got = _run("openrouter", OR_MODEL, 200, SSE_HDR, body)
    assert got.text == "partial "
    assert isinstance(got.error, TransportError) and got.error.error_class == "server"
    assert normalize_finish_reason(got.usage.finish_reason).value == "error"


def test_a_stream_cut_without_a_finish_is_an_error() -> None:
    """Baseten can fail mid-stream with no status and no error object (F7)."""
    got = _run("baseten", OR_MODEL, 200, SSE_HDR, _sse(_delta("half")))
    assert got.text == "half"
    assert isinstance(got.error, TransportError) and got.error.error_class == "server"
    assert normalize_finish_reason(got.usage.finish_reason).value == "error"
    # A finish reason without [DONE] is accepted.
    ok = _run("baseten", OR_MODEL, 200, SSE_HDR, _sse(_delta("done", "stop")))
    assert ok.error is None and ok.text == "done"


def test_retry_after_and_class_travel_on_a_retryable_fault() -> None:
    hdr = {**JSON_HDR, "retry-after": "12"}
    got = _run("baseten", OR_MODEL, 429, hdr, json.dumps({"error": "slow down"}))
    assert isinstance(got.error, TransportError)
    assert got.error.retry_after == 12.0 and got.error.error_class == "rate_limit"
    got = _run("baseten", OR_MODEL, 500, {"content-type": "text/html"}, "<html>oops</html>")
    assert isinstance(got.error, TransportError) and got.error.error_class == "server"


def test_no_key_and_no_provider_text_in_a_raised_message() -> None:
    secret_text = "echoed-prompt-text-xyz"
    for status, body in ((401, {"error": {"message": secret_text, "code": 401}}), (429, {"error": {"message": secret_text}})):
        got = _run("openrouter", OR_MODEL, status, JSON_HDR, json.dumps(body))
        assert secret_text not in str(got.error) and pr.TEST_KEY not in str(got.error)


def test_a_missing_key_refuses_before_any_request() -> None:
    seen: list = []
    prov = pr.provider("openrouter", OR_MODEL, pr.raw_transport(200, SSE_HDR, "", seen))
    env = pr.profile("openrouter").key_env
    saved = os.environ.pop(env, None)

    async def go():
        stream = await prov.generate(pr.MESSAGES, "default", "tenant-a", None)
        return [c async for c in stream.chunks]

    try:
        asyncio.run(go())
    except ProviderError as exc:
        assert exc.error_class is ErrorClass.AUTH and env in str(exc)
    else:
        raise AssertionError("sent with no key")
    finally:
        if saved is not None:
            os.environ[env] = saved
    assert seen == []


def test_unknown_finish_values_are_kept_raw_for_the_counted_bucket() -> None:
    body = _sse(_delta("x", "mystery_stop"), "[DONE]")
    got = _run("purdue", "gpt-oss:120b", 200, SSE_HDR, body)
    assert got.usage.finish_reason == "mystery_stop"
    assert normalize_finish_reason(got.usage.finish_reason, got.usage.native_finish_reason).value == "other"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
