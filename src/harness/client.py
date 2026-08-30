"""External harness client (mitya-v2 P4 step 1).

Public API only. This module speaks HTTP to a base URL via `httpx` and
imports nothing from `src.rag`, `src.api`, or `src.gateway` -- it has no more
access than any other caller of the running server. That discipline is the
whole point of a harness: if a number cannot be gotten this way, it is a gap
in the API's observability, not a reason to reach past this client (see the
gap list in `plans/mitya-v2/05-impl/phase-4.md`).

All request construction goes through `_headers()`/`_request()` /
`_stream()` -- the three points every call funnels through. R86 measured
exactly 2 harness call sites that need credentials once the API requires them
(D7, P5): `/books` and `POST /chat`. Because both route through the same
header-building seam, wiring auth in later is a one-line edit to `_headers()`,
not a hunt across the client.

`/chat` is Server-Sent Events. `stream_answer`'s failure shape in
`src/api/app.py` is asymmetric (R87) and this client records the asymmetry as
a first-class field rather than collapsing it:

  - A fault BEFORE the first token (empty completion storm exhausted, quota
    exceeded, request too large, provider unreachable, spend guard tripped,
    content refused) is mapped to a real HTTP status code on a response whose
    SSE body never opens (`app.py`'s `_abort()` call sites, 400/413/422/429/
    502/503). Recorded as `fault_phase="pre_first_token"`.
  - A fault AFTER the first token cannot change the already-committed 200
    status; it arrives instead as a terminal SSE `error` event
    (`app.py:603-613`). Recorded as `fault_phase="post_first_token"`.
  - No fault at all: `fault_phase=None`, terminal event is `"done"`.

TTFT is measured client-side, at the first `token` SSE event actually
observed -- this is deliberately independent of the server's own `ttft_ms` in
the `meta` event (which is `None` on a cache hit, since the server does not
time a generation stage that did not run). Both numbers are useful and they
answer different questions; this client keeps only its own, since the
server's is already in `meta` if a caller wants it too (`ChatResult` does not
duplicate it).
"""

import json
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
# 30s covers a cold-cache generation (v1 baseline ~21.3s total, R0); 5s
# connect covers a server that is simply not up yet.
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


@dataclass
class ChatResult:
    """Everything about one `/chat` call the client could observe from
    outside. Fields left `None` mean the corresponding server event never
    arrived, not that the value was zero."""

    query: str
    status_code: int | None  # None only when no HTTP response arrived at all (transport failure)
    ttft_ms: float | None
    total_ms: float
    route: str | None  # meta.route_name
    cache_hit: bool | None
    cost_usd: float | None
    cost_metered: bool | None
    model: str | None
    no_context: bool | None
    filter_relaxed: bool | None
    chunk_ids: list[str] | None
    terminal_event: str | None  # "done" | "error" | None (stream ended without either)
    fault_phase: str | None  # "pre_first_token" | "post_first_token" | None (success)
    error_detail: str | None
    answer_chars: int
    request_id: str | None  # correlates back to the server's own logs/spans
    rung: str | None  # served Rung.value (Batch C step 8, R93/R94); None on transport failure


class HarnessClient:
    """Async external client for the five routes in `src/api/app.py`."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        limits: httpx.Limits | None = None,
    ) -> None:
        # `limits=None` leaves httpx's own default (max_connections=100,
        # max_keepalive_connections=20) untouched -- fine for Batch A/B's
        # one-request-at-a-time callers. Batch C's ramp driver (step 5) passes
        # an explicit `httpx.Limits(max_connections=concurrency, ...)` so the
        # CLIENT's own pool is never the thing capping how many requests
        # actually reach the server -- a ramp that measures its own
        # bottleneck is the failure schedule-p4.md's Batch C Verify names
        # first. (Not passed unconditionally: `httpx.Limits()` with no
        # arguments means UNLIMITED, not "httpx's default", so a bare
        # `limits or httpx.Limits()` would silently change Batch A/B's
        # existing behavior.)
        kwargs = {"base_url": base_url, "timeout": timeout, "transport": transport}
        if limits is not None:
            kwargs["limits"] = limits
        self._client = httpx.AsyncClient(**kwargs)

    async def __aenter__(self) -> "HarnessClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        """The single seam P5's auth retrofit edits (D7, R86). Empty today --
        the harness predates auth (G7) -- but every request below reads
        headers from here, so adding a credential is one change, not a hunt
        across `/books` and `/chat`."""
        return {}

    async def _request(self, method: str, path: str, *, json_body: dict | None = None) -> httpx.Response:
        """Non-streaming requests: `/metrics`, `/health`, `/ready`, `/books`."""
        return await self._client.request(method, path, json=json_body, headers=self._headers())

    async def metrics(self) -> httpx.Response:
        return await self._request("GET", "/metrics")

    async def health(self) -> httpx.Response:
        return await self._request("GET", "/health")

    async def ready(self) -> httpx.Response:
        return await self._request("GET", "/ready")

    async def books(self) -> httpx.Response:
        return await self._request("GET", "/books")

    async def chat(
        self,
        query: str,
        filters: dict | None = None,
        session_id: str | None = None,
    ) -> ChatResult:
        """POST /chat and consume the SSE stream to completion.

        Errors are handled at two levels, matching the two shapes above: an
        `httpx.HTTPError` (connect refused, read timeout -- no response ever
        arrived) is caught around the whole call; a non-200 status on the
        response that DID arrive is read and returned without attempting to
        parse an SSE body that was never opened.
        """
        payload: dict = {"query": query}
        if filters:
            payload["filters"] = filters
        if session_id:
            payload["session_id"] = session_id

        t_start = time.monotonic()
        try:
            async with self._client.stream(
                "POST", "/chat", json=payload, headers=self._headers()
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    total_ms = (time.monotonic() - t_start) * 1000
                    return ChatResult(
                        query=query, status_code=resp.status_code, ttft_ms=None, total_ms=total_ms,
                        route=None, cache_hit=None, cost_usd=None, cost_metered=None, model=None,
                        no_context=None, filter_relaxed=None, chunk_ids=None, terminal_event=None,
                        fault_phase="pre_first_token",
                        error_detail=body.decode(errors="replace")[:500], answer_chars=0,
                        request_id=None,
                        # R93: every /chat error exit carries X-Mitya-Rung, since the
                        # exception paths emit no `meta` event to carry it instead.
                        rung=resp.headers.get("x-mitya-rung"),
                    )
                return await self._consume_stream(query, resp, t_start)
        except httpx.HTTPError as exc:
            # No response at all: connect refused, read timeout, etc.
            total_ms = (time.monotonic() - t_start) * 1000
            logger.warning("harness: chat transport failure query=%r: %s", query, exc)
            return ChatResult(
                query=query, status_code=None, ttft_ms=None, total_ms=total_ms,
                route=None, cache_hit=None, cost_usd=None, cost_metered=None, model=None,
                no_context=None, filter_relaxed=None, chunk_ids=None, terminal_event=None,
                fault_phase="pre_first_token", error_detail=str(exc), answer_chars=0,
                request_id=None,
                # No response ever arrived, so no header either -- distinct from
                # the pre_first_token-with-a-status-code case above, which reads
                # the rung header from a response that DID arrive.
                rung=None,
            )

    @staticmethod
    async def _consume_stream(query: str, resp: httpx.Response, t_start: float) -> ChatResult:
        """Parse `_sse()`'s `event: X\\ndata: {...}\\n\\n` frames
        (`src/api/app.py:183-186`) off an already-200 response."""
        t_first_token: float | None = None
        route = cache_hit = cost_usd = cost_metered = model = None
        no_context = filter_relaxed = chunk_ids = request_id = rung = None
        terminal_event: str | None = None
        error_detail: str | None = None
        answer_chars = 0

        event_name: str | None = None
        data_lines: list[str] = []
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
            elif line == "" and event_name is not None:
                # Blank line terminates one SSE frame -- dispatch, then reset.
                body = json.loads("".join(data_lines)) if data_lines else {}
                if event_name == "token":
                    if t_first_token is None:
                        t_first_token = time.monotonic()
                    answer_chars += len(body.get("text", ""))
                elif event_name == "meta":
                    route = body.get("route_name")
                    cache_hit = body.get("cache_hit")
                    cost_usd = body.get("cost_usd")
                    cost_metered = body.get("cost_metered")
                    model = body.get("model")
                    no_context = body.get("no_context")
                    filter_relaxed = body.get("filter_relaxed")
                    chunk_ids = body.get("chunk_ids")
                    request_id = body.get("request_id")
                    rung = body.get("rung")  # Batch C step 8 (R93/R94)
                elif event_name in ("done", "error"):
                    terminal_event = event_name
                    if event_name == "error":
                        error_detail = body.get("detail")
                event_name, data_lines = None, []

        total_ms = (time.monotonic() - t_start) * 1000
        ttft_ms = (t_first_token - t_start) * 1000 if t_first_token is not None else None
        fault_phase = "post_first_token" if terminal_event == "error" else None
        return ChatResult(
            query=query, status_code=resp.status_code, ttft_ms=ttft_ms, total_ms=total_ms,
            route=route, cache_hit=cache_hit, cost_usd=cost_usd, cost_metered=cost_metered,
            model=model, no_context=no_context, filter_relaxed=filter_relaxed,
            chunk_ids=chunk_ids, terminal_event=terminal_event, fault_phase=fault_phase,
            error_detail=error_detail, answer_chars=answer_chars, request_id=request_id,
            rung=rung,
        )
