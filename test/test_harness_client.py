# test/test_harness_client.py
"""Verify: src/harness/client.py (mitya-v2 P4 step 1).

Plain-assert script (pytest is not installed in `.venv`, matches
test_gateway_google.py's idiom). Offline: `httpx.MockTransport` stands in for
the wire -- no network, no server, no GPU. `HarnessClient` takes `transport`
directly (no monkeypatch needed, unlike the gateway adapters which construct
their own `httpx.AsyncClient` internally).

Covers R87's asymmetry as the two must-hit cases: a status code that arrives
BEFORE any token (fault_phase="pre_first_token"), and a terminal SSE `error`
event on an already-committed 200 that arrives AFTER a token
(fault_phase="post_first_token").

Usage:
    python test/test_harness_client.py
"""

import asyncio
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx  # noqa: E402

from src.harness.client import HarnessClient  # noqa: E402

logger = logging.getLogger(__name__)


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def _mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_successful_chat_records_ttft_and_meta_fields() -> None:
    body = (
        _sse("token", '{"text": "Marvel"}')
        + _sse("token", '{"text": " not"}')
        + _sse("sources", '{"sources": [], "footer": ""}')
        + _sse("meta", '{"request_id": "abc", "route_name": "DEFAULT", "cache_hit": false, '
               '"cost_usd": 0.001, "cost_metered": true, "model": "openai/gpt-oss-120b", '
               '"no_context": false, "filter_relaxed": false, "chunk_ids": ["c1", "c2"]}')
        + _sse("done", "{}")
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    async def run():
        async with HarnessClient(transport=_mock(handler)) as client:
            return await client.chat("why did raskolnikov")

    result = asyncio.run(run())
    assert result.status_code == 200
    assert result.fault_phase is None, result.fault_phase
    assert result.terminal_event == "done"
    assert result.ttft_ms is not None and result.ttft_ms >= 0
    assert result.route == "DEFAULT"
    assert result.cache_hit is False
    assert result.cost_usd == 0.001
    assert result.model == "openai/gpt-oss-120b"
    assert result.chunk_ids == ["c1", "c2"]
    assert result.answer_chars == len("Marvel not")
    assert result.request_id == "abc"
    logger.info("ok: success path parses TTFT, terminal event and meta fields")


def test_pre_first_token_fault_is_a_status_code_with_no_stream() -> None:
    """R87: a fault before any token is a real HTTP status, never a terminal
    SSE event -- the body is never an SSE stream at all here."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "provider quota exhausted"})

    async def run():
        async with HarnessClient(transport=_mock(handler)) as client:
            return await client.chat("why did raskolnikov")

    result = asyncio.run(run())
    assert result.status_code == 429
    assert result.fault_phase == "pre_first_token", result.fault_phase
    assert result.terminal_event is None
    assert result.ttft_ms is None
    assert "quota exhausted" in result.error_detail
    logger.info("ok: pre-first-token fault recorded as a status code, no stream parsed")


def test_post_first_token_fault_is_a_terminal_sse_error_on_a_committed_200() -> None:
    """R87: a mid-stream failure cannot change the already-sent 200 -- it
    surfaces only as a terminal `error` SSE event."""
    body = (
        _sse("token", '{"text": "Marvel"}')
        + _sse("error", '{"detail": "provider unreachable"}')
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    async def run():
        async with HarnessClient(transport=_mock(handler)) as client:
            return await client.chat("why did raskolnikov")

    result = asyncio.run(run())
    assert result.status_code == 200
    assert result.fault_phase == "post_first_token", result.fault_phase
    assert result.terminal_event == "error"
    assert result.ttft_ms is not None  # the token DID arrive before the fault
    assert result.error_detail == "provider unreachable"
    logger.info("ok: post-first-token fault recorded on a committed 200, not a status code")


def test_transport_failure_has_no_status_code_at_all() -> None:
    """Connect refused / read timeout: no response ever arrives."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async def run():
        async with HarnessClient(transport=_mock(handler)) as client:
            return await client.chat("why did raskolnikov")

    result = asyncio.run(run())
    assert result.status_code is None
    assert result.fault_phase == "pre_first_token"
    assert result.ttft_ms is None
    logger.info("ok: transport failure recorded with no status code")


def test_get_endpoints_route_through_the_same_header_seam() -> None:
    """`/books` is the other of R86's two call sites -- confirm it (and the
    operator-plane routes) go through `_request()`/`_headers()`, not a
    separate ad hoc call."""
    seen_headers = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    async def run():
        async with HarnessClient(transport=_mock(handler)) as client:
            await client.metrics()
            await client.health()
            await client.ready()
            await client.books()

    asyncio.run(run())
    assert len(seen_headers) == 4
    logger.info("ok: all four GET routes went through the shared request seam")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    test_successful_chat_records_ttft_and_meta_fields()
    test_pre_first_token_fault_is_a_status_code_with_no_stream()
    test_post_first_token_fault_is_a_terminal_sse_error_on_a_committed_200()
    test_transport_failure_has_no_status_code_at_all()
    test_get_endpoints_route_through_the_same_header_seam()
    logger.info("all harness client tests passed")
