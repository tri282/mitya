# test/test_gateway_google.py
"""P2 Batch C step 3 verify: the Google adapter (src/gateway/google.py).

Plain-assert script, not pytest (matches test_gateway_contract.py's idiom).
Offline: httpx.MockTransport stands in for the wire -- no network, no real
key. Fixtures are recorded SSE bodies shaped exactly as R55's live probe
measured them (see p2-findings.md R55), not invented from the vendor docs
alone -- the whole point of that probe was that the docs undersell what the
wire actually does.

Two must-hit traps from the schedule:
  * the non-streaming-Google trap: a 200 with content-type: application/json
    (the "?alt=sse alone does not stream" failure mode) must raise, never
    silently succeed.
  * a refusal arriving as HTTP 200 (`interaction.completed` carrying a
    safety code) must raise ContentRefused, not report a successful empty
    answer.
"""

import asyncio
import sys

sys.path.insert(0, ".")

import httpx  # noqa: E402

from src.config import Config  # noqa: E402
from src.gateway import google as google_mod  # noqa: E402
from src.gateway.google import (  # noqa: E402
    GoogleProvider,
    GoogleStreamingContractViolation,
    GoogleUsage,
    _fill_usage_from_completed,
    _to_google_payload,
)
from src.gateway.ladder import ContentRefused  # noqa: E402
from src.gateway.retry import TransportError  # noqa: E402

_TEST_MODEL = "models/gemini-3.5-flash-lite"


class _rate_override:
    """Context manager: shadow `Config.MODEL_RATES` with a plain class
    attribute for the duration of one test, then remove it so `Config`'s
    facade (src/config.py's `_ConfigMeta`) resolves back through the
    currently-promoted config artifact afterwards.

    Exists because `MODEL_RATES` is a versioned field (v2 P1 step 5/7): the
    real row this step adds to `src/config_artifact.py`'s
    `_DEFAULT_MODEL_RATES` only becomes live once someone promotes a new
    config version (`save(ServingConfig.from_env())` + a `CURRENT` pointer
    write) -- a mutating, out-of-scope write for this test file to make on
    its own. Patching the class attribute directly, the same technique
    `test_redaction.py`'s `_sentinel_config` uses for the API-key attributes,
    keeps this test offline and independent of whatever version happens to
    be promoted when it runs."""

    def __enter__(self):
        self._had_own_attr = "MODEL_RATES" in Config.__dict__
        self._saved = Config.__dict__.get("MODEL_RATES")
        Config.MODEL_RATES = {_TEST_MODEL: {"prompt": 0.30, "completion": 2.50, "metered": False}}
        return self

    def __exit__(self, *exc):
        if self._had_own_attr:
            Config.MODEL_RATES = self._saved
        else:
            delattr(Config, "MODEL_RATES")

_MESSAGES = [
    {"role": "system", "content": "You are Fyodor Dostoevsky."},
    {"role": "user", "content": "who are you?"},
]

_STREAMING_BODY = (
    "event: interaction.created\n"
    'data: {"id": "abc"}\n'
    "\n"
    "event: interaction.status_update\n"
    'data: {"status": "in_progress"}\n'
    "\n"
    "event: step.start\n"
    'data: {"index": 0, "step": {"type": "thought"}}\n'
    "\n"
    "event: step.delta\n"
    'data: {"index": 0, "delta": {"type": "text", "text": "Marvel not "}}\n'
    "\n"
    "event: step.delta\n"
    'data: {"index": 0, "delta": {"type": "text", "text": "at the world\'s evil."}}\n'
    "\n"
    "event: step.stop\n"
    'data: {"index": 0}\n'
    "\n"
    "event: interaction.completed\n"
    'data: {"status": "completed", "usage": {"prompt_tokens": 64, "completion_tokens": 13}}\n'
    "\n"
    "event: done\n"
    "data: [DONE]\n"
    "\n"
)

_TRUNCATED_BY_THINKING_BODY = (
    "event: interaction.created\n"
    'data: {"id": "abc"}\n'
    "\n"
    "event: step.start\n"
    'data: {"index": 0, "step": {"type": "thought"}}\n'
    "\n"
    "event: step.delta\n"
    'data: {"index": 0, "delta": {"type": "text", "text": "Suffering"}}\n'
    "\n"
    "event: interaction.completed\n"
    'data: {"status": "truncated", "usage": {"prompt_tokens": 20, '
    '"total_output_tokens": 8, "total_thought_tokens": 288}}\n'
    "\n"
    "event: done\n"
    "data: [DONE]\n"
    "\n"
)

_REFUSAL_BODY = (
    "event: interaction.created\n"
    'data: {"id": "abc"}\n'
    "\n"
    "event: interaction.completed\n"
    'data: {"status": "safety", "usage": {"prompt_tokens": 12, "completion_tokens": 0}}\n'
    "\n"
    "event: done\n"
    "data: [DONE]\n"
    "\n"
)


class _mock_client:
    """Context manager: patches google.py's `httpx.AsyncClient` so every
    client it constructs is wired to `transport` -- restores the real class
    on exit. Patches the shared `httpx` module object (google.py does `import
    httpx`, not `from httpx import AsyncClient`), so this must be scoped
    tightly and always restored, which the `with` block guarantees even on
    assertion failure."""

    def __init__(self, transport: httpx.MockTransport) -> None:
        self._transport = transport

    def __enter__(self):
        self._original = httpx.AsyncClient
        transport = self._transport

        def _client(*args, **kwargs):
            kwargs["transport"] = transport
            return self._original(*args, **kwargs)

        google_mod.httpx.AsyncClient = _client
        return self

    def __exit__(self, *exc):
        google_mod.httpx.AsyncClient = self._original


def _handler_for(body: str, *, content_type: str = "text/event-stream", status: int = 200):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": content_type}, content=body.encode())

    return handler


async def _drain(stream) -> tuple[list[str], object]:
    chunks = []
    async for c in stream.chunks:
        chunks.append(c)
    return chunks, stream.usage


def test_streaming_reply_yields_text_and_fills_usage() -> None:
    provider = GoogleProvider(model=_TEST_MODEL)
    transport = httpx.MockTransport(_handler_for(_STREAMING_BODY))
    with _rate_override(), _mock_client(transport):
        stream = asyncio.run(provider.generate(_MESSAGES, "default", "default", None))
        chunks, usage = asyncio.run(_drain(stream))
    text = "".join(chunks)
    assert text == "Marvel not at the world's evil.", text
    # The step.start "thought" step contributed no text -- reasoning is never
    # yielded as answer text (must-hit #4's Google side).
    assert usage.prompt_tokens == 64
    assert usage.completion_tokens == 13
    assert usage.cost.priced is True
    assert usage.cost.metered is False  # free-tier list-rate estimate (R48), never spend


def test_thought_tokens_are_summed_into_completion_tokens_and_finish_is_recorded() -> None:
    """R56 (corrects R55): Google reports `total_thought_tokens` SEPARATELY
    from `total_output_tokens` -- normalized here into `completion_tokens`
    (the sum) so a cross-arm reader sees the same quantity Groq's raw
    `completion_tokens` already is. `finish_reason` must carry "truncated"
    so a near-empty answer is distinguishable from one that simply had
    nothing more to say (R56 point 3)."""
    provider = GoogleProvider(model=_TEST_MODEL)
    transport = httpx.MockTransport(_handler_for(_TRUNCATED_BY_THINKING_BODY))
    with _rate_override(), _mock_client(transport):
        stream = asyncio.run(provider.generate(_MESSAGES, "default", "default", None))
        chunks, usage = asyncio.run(_drain(stream))
    assert "".join(chunks) == "Suffering"
    assert usage.reasoning_tokens == 288
    assert usage.completion_tokens == 8 + 288, usage.completion_tokens
    assert usage.finish_reason == "truncated"


def test_non_streaming_200_raises_instead_of_silently_succeeding() -> None:
    """THE non-streaming-Google trap (R55): '?alt=sse' without 'stream': true
    in the body returns a 200 with content-type: application/json and one
    blocking JSON line. An adapter that does not check content-type would
    pass this off as a (very fast, zero-chunk) success. It must raise."""
    provider = GoogleProvider(model="models/gemini-3.5-flash-lite")
    non_streaming_body = '{"id": "abc", "status": "completed", "usage": {"prompt_tokens": 64, "completion_tokens": 13}}\n'
    transport = httpx.MockTransport(
        _handler_for(non_streaming_body, content_type="application/json")
    )
    with _mock_client(transport):
        stream = asyncio.run(provider.generate(_MESSAGES, "default", "default", None))
        try:
            asyncio.run(_drain(stream))
        except GoogleStreamingContractViolation:
            pass
        else:
            raise AssertionError(
                "a non-streaming 200 (application/json) was silently accepted as a stream"
            )


def test_refusal_raises_content_refused_not_success_not_transport_error() -> None:
    """Must-hit #3: HTTP 200 carrying a refusal is a third outcome -- neither
    a successful empty answer nor a TransportError."""
    provider = GoogleProvider(model="models/gemini-3.5-flash-lite")
    transport = httpx.MockTransport(_handler_for(_REFUSAL_BODY))
    with _mock_client(transport):
        stream = asyncio.run(provider.generate(_MESSAGES, "default", "default", None))
        try:
            asyncio.run(_drain(stream))
        except ContentRefused:
            pass
        except TransportError:
            raise AssertionError("a refusal must not be classified as a transport failure")
        else:
            raise AssertionError("a refusal was not raised at all -- looks like a success")


def test_http_error_status_becomes_transport_error() -> None:
    """A real 429/5xx (distinct from the 200-carrying-a-refusal case above)
    is exactly what retry.py's bounded retry exists to absorb."""
    provider = GoogleProvider(model="models/gemini-3.5-flash-lite")
    transport = httpx.MockTransport(_handler_for("", status=429, content_type="application/json"))
    with _mock_client(transport):
        stream = asyncio.run(provider.generate(_MESSAGES, "default", "default", None))
        try:
            asyncio.run(_drain(stream))
        except TransportError:
            pass
        else:
            raise AssertionError("a 429 was not classified as TransportError")


def test_request_payload_matches_the_live_interactions_schema() -> None:
    """Pins the four request/response schema facts that a live 400 (or worse,
    a silent empty cost row) taught us on 2026-08-28. Every one of them was
    wrong when written from `generateContent`'s documented shape, and the
    existing fixture tests all passed anyway -- they exercised SSE parsing and
    never looked at what got sent. This is the check that would have caught it.
    """
    payload = _to_google_payload(
        [{"role": "system", "content": "persona"},
         {"role": "user", "content": "question"}],
        "models/gemini-3.5-flash-lite",
        max_tokens=200, temperature=0.7, top_p=0.9, safety_settings=None,
    )
    # 1. The prompt goes in `input`. `contents` is the classic API's field and
    #    this endpoint answers "Unknown parameter 'contents'."
    assert payload["input"] == "question", payload
    assert "contents" not in payload, "`contents` is generateContent's field, not interactions'"
    # 2. `system_instruction` is a plain string, not {"parts": [...]}.
    assert payload["system_instruction"] == "persona", payload
    # 3. snake_case, not camelCase -- "Did you mean 'generation_config'?"
    assert "generation_config" in payload and "generationConfig" not in payload, payload
    assert payload["generation_config"]["max_output_tokens"] == 200, payload
    # 4. R55: ?alt=sse alone does not stream; this field is what turns it on.
    assert payload["stream"] is True, payload

    # Multi-turn is refused loudly rather than silently dropping history --
    # the interactions API needs a step_list format that is not implemented.
    try:
        _to_google_payload(
            [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
             {"role": "user", "content": "c"}],
            "models/gemini-3.5-flash-lite",
            max_tokens=None, temperature=None, top_p=None, safety_settings=None,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("an assistant turn was silently dropped instead of raising")


def test_usage_is_read_from_the_nested_interaction_object() -> None:
    """The quiet one. Usage sits at payload["interaction"]["usage"], not at the
    root, and the input key is `total_input_tokens`. Reading the root returned
    {} -- so a successful streamed answer reported every token count as None
    and priced UNPRICED, with nothing raised and nothing logged."""
    usage = GoogleUsage(model="models/gemini-3.5-flash-lite")
    _fill_usage_from_completed(usage, {
        "interaction": {
            "status": "completed",
            "usage": {"total_tokens": 44, "total_input_tokens": 12,
                      "total_output_tokens": 32, "total_thought_tokens": 0,
                      "total_cached_tokens": 0},
        },
    })
    assert usage.prompt_tokens == 12, usage.prompt_tokens
    assert usage.completion_tokens == 32, usage.completion_tokens

    # R56: thought tokens are reported separately by Google and must be summed
    # into completion_tokens, so the field means the same thing as Groq's.
    thinking = GoogleUsage(model="models/gemini-3.5-flash")
    _fill_usage_from_completed(thinking, {
        "interaction": {"status": "completed",
                        "usage": {"total_input_tokens": 13,
                                  "total_output_tokens": 8,
                                  "total_thought_tokens": 288}},
    })
    assert thinking.completion_tokens == 296, thinking.completion_tokens
    assert thinking.reasoning_tokens == 288, thinking.reasoning_tokens


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
