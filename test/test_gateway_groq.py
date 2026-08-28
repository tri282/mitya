# test/test_gateway_groq.py
"""P2 Batch C step 3 verify: the Groq adapter (src/gateway/groq.py).

Plain-assert script, not pytest. Offline: httpx.MockTransport, no network, no
real key. The reasoning-only stream fixture below reproduces R55's measured
delta-key sequence for openai/gpt-oss-120b (content|role -> channel|reasoning
-> content -> empty/finish_reason=length -> [DONE]), not an invented shape.

The must-hit this file exists to prove: a Groq stream whose deltas are
reasoning-only yields ZERO content and still records reasoning_tokens.
"""

import asyncio
import sys

sys.path.insert(0, ".")

import httpx  # noqa: E402

from src.config import Config  # noqa: E402
from src.gateway import groq as groq_mod  # noqa: E402
from src.gateway.groq import GroqProvider  # noqa: E402
from src.gateway.retry import TransportError  # noqa: E402

_TEST_MODEL = "openai/gpt-oss-120b"
_MESSAGES = [{"role": "user", "content": "who are you?"}]


class _rate_override:
    """See test_gateway_google.py's identical helper for the full rationale:
    MODEL_RATES is a versioned field resolved through Config's facade, and
    this step's new row is not live until a config version is promoted --
    out of scope for an offline test to do itself."""

    def __enter__(self):
        self._had_own_attr = "MODEL_RATES" in Config.__dict__
        self._saved = Config.__dict__.get("MODEL_RATES")
        Config.MODEL_RATES = {_TEST_MODEL: {"prompt": 0.15, "completion": 0.60, "metered": False}}
        return self

    def __exit__(self, *exc):
        if self._had_own_attr:
            Config.MODEL_RATES = self._saved
        else:
            delattr(Config, "MODEL_RATES")


class _mock_client:
    """See test_gateway_google.py's identical helper."""

    def __init__(self, transport: httpx.MockTransport) -> None:
        self._transport = transport

    def __enter__(self):
        self._original = httpx.AsyncClient
        transport = self._transport

        def _client(*args, **kwargs):
            kwargs["transport"] = transport
            return self._original(*args, **kwargs)

        groq_mod.httpx.AsyncClient = _client
        return self

    def __exit__(self, *exc):
        groq_mod.httpx.AsyncClient = self._original


def _sse(*lines: str) -> str:
    return "".join(f"data: {line}\n\n" for line in lines) + "data: [DONE]\n\n"


def _handler_for(body: str, *, status: int = 200):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": "text/event-stream"}, content=body.encode())

    return handler


async def _drain(stream) -> tuple[list[str], object]:
    chunks = []
    async for c in stream.chunks:
        chunks.append(c)
    return chunks, stream.usage


# R55's measured delta-key sequence for one 60-token completion:
# content|role -> channel|reasoning -> content -> (empty, finish_reason=length)
_NORMAL_BODY = _sse(
    '{"choices": [{"delta": {"role": "assistant", "content": ""}}]}',
    '{"choices": [{"delta": {"content": "Marvel "}}]}',
    '{"choices": [{"delta": {"content": "not at the world\'s evil."}}]}',
    '{"choices": [{"delta": {}, "finish_reason": "stop"}]}',
    '{"choices": [], "usage": {"prompt_tokens": 79, "completion_tokens": 60, '
    '"total_tokens": 139, "completion_tokens_details": {"reasoning_tokens": 45}, '
    '"queue_time": 0.112, "prompt_time": 0.0031, "completion_time": 0.128}}',
)

# THE trap this file must catch: every delta carries reasoning/channel, never
# content. A naive "if token:" happens to skip these because delta.content is
# None -- this test proves it is skipped on purpose and that reasoning_tokens
# still gets recorded, not silently discarded.
_REASONING_ONLY_BODY = _sse(
    '{"choices": [{"delta": {"role": "assistant", "content": ""}}]}',
    '{"choices": [{"delta": {"channel": "analysis", "reasoning": "Let me think"}}]}',
    '{"choices": [{"delta": {"channel": "analysis", "reasoning": " about this question."}}]}',
    '{"choices": [{"delta": {}, "finish_reason": "length"}]}',
    '{"choices": [], "usage": {"prompt_tokens": 79, "completion_tokens": 60, '
    '"total_tokens": 139, "completion_tokens_details": {"reasoning_tokens": 45}, '
    '"queue_time": 0.112, "prompt_time": 0.0031, "completion_time": 0.128}}',
)


def test_normal_stream_yields_content_and_fills_usage() -> None:
    provider = GroqProvider(model=_TEST_MODEL)
    transport = httpx.MockTransport(_handler_for(_NORMAL_BODY))
    with _rate_override(), _mock_client(transport):
        stream = asyncio.run(provider.generate(_MESSAGES, "default", "default", None))
        chunks, usage = asyncio.run(_drain(stream))
    assert "".join(chunks) == "Marvel not at the world's evil."
    assert usage.prompt_tokens == 79
    assert usage.completion_tokens == 60
    assert usage.reasoning_tokens == 45
    assert usage.queue_time == 0.112
    assert usage.prompt_time == 0.0031
    assert usage.completion_time == 0.128
    assert usage.cost.priced is True
    assert usage.cost.metered is False
    assert usage.finish_reason == "stop"


def test_reasoning_only_deltas_yield_zero_content_but_record_reasoning_tokens() -> None:
    """THE must-hit: a Groq stream whose deltas are reasoning-only. Zero
    content yielded; reasoning_tokens still recorded from the trailing usage
    chunk (measured: 45 of 60 completion tokens were reasoning, R55)."""
    provider = GroqProvider(model=_TEST_MODEL)
    transport = httpx.MockTransport(_handler_for(_REASONING_ONLY_BODY))
    with _rate_override(), _mock_client(transport):
        stream = asyncio.run(provider.generate(_MESSAGES, "default", "default", None))
        chunks, usage = asyncio.run(_drain(stream))
    assert chunks == [], f"reasoning/channel deltas leaked into the answer stream: {chunks!r}"
    assert usage.completion_tokens == 60
    assert usage.reasoning_tokens == 45, "reasoning tokens must be recorded even with zero content"
    # R56: this fixture's finish_reason is "length" -- the new
    # truncated-by-thinking case, distinguishable from a genuinely empty
    # stream via this field, not by completion_tokens alone (60 != 0 here).
    assert usage.finish_reason == "length"


def test_http_error_status_becomes_transport_error() -> None:
    provider = GroqProvider(model=_TEST_MODEL)
    transport = httpx.MockTransport(_handler_for("", status=429))
    with _mock_client(transport):
        stream = asyncio.run(provider.generate(_MESSAGES, "default", "default", None))
        try:
            asyncio.run(_drain(stream))
        except TransportError:
            pass
        else:
            raise AssertionError("a 429 was not classified as TransportError")


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
