# test/test_harness_rungs.py
"""Verify: src/harness/rungs.py (mitya-v2 P4 step 8, schedule Batch C).

Plain-assert script, not pytest. Offline: `httpx.MockTransport` for the two
HTTP-forceable rungs; a source-level check for the mapping requirement.

**schedule-p4.md Batch C Verify's second named test**: "the rung forcer's
conditions map onto `Readiness`'s fields, not onto a second parallel health
notion invented in the harness." `src/harness/rungs.py` cannot import
`src.gateway.ladder` itself (step 1's rule, mechanically checked by
`test_harness_boundary.py`), so THIS file does the cross-check, from outside
`src/harness/`.
"""

import asyncio
import sys

sys.path.insert(0, ".")

import httpx  # noqa: E402

from src.gateway.ladder import Readiness  # noqa: E402
from src.harness.client import HarnessClient  # noqa: E402
from src.harness.rungs import FORCE_PROCEDURE, RUNG_CONDITIONS, force_cached, force_primary  # noqa: E402


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def _stream_body(cache_hit: bool, rung: str) -> str:
    return (
        _sse("token", '{"text": "answer"}')
        + _sse("sources", '{"sources": [], "footer": ""}')
        + _sse(
            "meta",
            '{"request_id": "r", "route_name": "DEFAULT", "cache_hit": %s, "cost_usd": 0.0, '
            '"cost_metered": false, "model": "fake", "no_context": false, '
            '"filter_relaxed": false, "chunk_ids": [], "rung": "%s"}'
            % ("true" if cache_hit else "false", rung),
        )
        + _sse("done", "{}")
    )


def test_rung_conditions_are_named_from_readiness_fields_only() -> None:
    """Every field name RUNG_CONDITIONS cites must be a real field on the
    application's Readiness dataclass -- never a renamed or invented one."""
    readiness_fields = set(Readiness.__dataclass_fields__)
    for rung, fields in RUNG_CONDITIONS.items():
        unknown = set(fields) - readiness_fields
        assert not unknown, f"rung {rung!r} cites unknown field(s) {unknown} -- not in Readiness"
    # And every rung the ladder can enter (per next_rung()'s vocabulary) has
    # an entry, even if it is the empty tuple (cheaper_model, stated as
    # unreachable rather than omitted).
    assert set(RUNG_CONDITIONS) == {
        "primary_model", "cached_answer", "cheaper_model", "retrieval_only", "503",
    }


def test_force_procedure_documents_every_rung_including_unreachable_ones() -> None:
    assert set(FORCE_PROCEDURE) == set(RUNG_CONDITIONS)
    for rung, procedure in FORCE_PROCEDURE.items():
        assert isinstance(procedure, str) and procedure, rung


def test_force_primary_over_http() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = _stream_body(cache_hit=False, rung="primary_model")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    async def run():
        async with HarnessClient(transport=httpx.MockTransport(handler)) as client:
            return await force_primary(client, "why did raskolnikov")

    result = asyncio.run(run())
    assert result.rung == "primary_model", result.rung
    assert result.cache_hit is False


def test_force_cached_over_http_second_call_is_a_hit() -> None:
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        is_second = calls["n"] >= 2
        body = _stream_body(cache_hit=is_second, rung="cached_answer" if is_second else "primary_model")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    async def run():
        async with HarnessClient(transport=httpx.MockTransport(handler)) as client:
            return await force_cached(client, "why did raskolnikov")

    first, second = asyncio.run(run())
    assert first.rung == "primary_model", first.rung
    assert second.rung == "cached_answer", second.rung
    assert second.cache_hit is True
    assert calls["n"] == 2


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    test_rung_conditions_are_named_from_readiness_fields_only()
    print("ok: RUNG_CONDITIONS names only real Readiness fields")
    test_force_procedure_documents_every_rung_including_unreachable_ones()
    print("ok: FORCE_PROCEDURE documents every rung")
    test_force_primary_over_http()
    print("ok: force_primary over HTTP")
    test_force_cached_over_http_second_call_is_a_hit()
    print("ok: force_cached over HTTP, second call is a cache hit")
    print("ALL RUNG FORCER CHECKS PASSED")
