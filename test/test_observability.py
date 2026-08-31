# test/test_observability.py
"""P1 step 1 verify: the request_id join key (src/observability.py).

Plain-assert script, not pytest (pytest is not installed in .venv -- see
plans/mitya-rag/05-impl/phase-4.md). Offline: no server, no Qdrant, no GPU, no
network. The ASGI middleware is exercised directly against a stub app, which is
the whole point of it being pure ASGI.

Four things here are invisible when wrong and permanent once later phases build
on them:

    1. The id survives `await`. If it did not, every record emitted during the
       streamed token phase would lose its join key -- and that phase is most of
       the request.
    2. Concurrent tasks do not share an id. v1's Semaphore(1) hides this today;
       P4's load ramp will not.
    3. The id is DERIVED from the active trace, not minted beside it. OTel's
       server span is created outside every user middleware, so a separately
       minted id would disagree with the trace id and paste-the-id would resolve
       to two different things. An inbound X-Request-ID is never read --
       correlation is W3C traceparent, which also removes the injection surface.
    4. Every record reaching the handler renders under LOG_FORMAT. A record
       without the attribute raises KeyError inside logging and the line is lost
       -- silently, because logging swallows formatting errors to stderr.
"""

import asyncio
import io
import logging
import sys

sys.path.insert(0, ".")

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402

from src.observability import (  # noqa: E402
    LOG_FORMAT,
    NO_REQUEST,
    TENANT_HEADER,
    RequestIdFilter,
    RequestIdMiddleware,
    current_request_id,
    current_tenant,
    new_request_id,
    parse_tenant,
    request_id_from_active_span,
    reset_request_id,
    reset_tenant,
    set_request_id,
    set_tenant,
)
from src.config import Config  # noqa: E402

trace.set_tracer_provider(TracerProvider())
_tracer = trace.get_tracer(__name__)


def test_sentinel_outside_request() -> None:
    assert current_request_id() == NO_REQUEST, (
        "offline callers must render as an explicit non-request, not None"
    )


def test_survives_await() -> None:
    async def run() -> str:
        token = set_request_id("abc")
        try:
            await asyncio.sleep(0)
            return current_request_id()
        finally:
            reset_request_id(token)

    assert asyncio.run(run()) == "abc"


def test_concurrent_tasks_do_not_bleed() -> None:
    """Two interleaved requests keep their own ids.

    Each task sets the id, yields control (forcing the other task to run in
    between), then reads it back. A module-level global passes every other test
    in this file and fails this one.
    """

    async def one(request_id: str, out: dict) -> None:
        token = set_request_id(request_id)
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            out[request_id] = current_request_id()
        finally:
            reset_request_id(token)

    async def run() -> dict:
        out: dict = {}
        await asyncio.gather(one("req-a", out), one("req-b", out))
        return out

    seen = asyncio.run(run())
    assert seen == {"req-a": "req-a", "req-b": "req-b"}, seen
    assert current_request_id() == NO_REQUEST, "task-local ids must not leak to the caller"


def test_id_is_derived_from_the_active_trace() -> None:
    """The request id IS the trace id, rendered the way trace UIs display it."""
    assert request_id_from_active_span() is None, "no span in scope -> nothing to derive"

    with _tracer.start_as_current_span("root") as span:
        derived = request_id_from_active_span()
        expected = format(span.get_span_context().trace_id, "032x")
        assert derived == expected, (derived, expected)
        assert len(derived) == 32 and all(c in "0123456789abcdef" for c in derived)


def test_minted_id_matches_trace_id_shape() -> None:
    """Where no trace exists the minted id must be indistinguishable in shape,
    or log parsing and dashboards need two formats."""
    minted = new_request_id()
    assert len(minted) == 32 and all(c in "0123456789abcdef" for c in minted)
    assert minted != new_request_id(), "ids must not repeat"


def test_filter_renders_under_log_format() -> None:
    """A record with no id still formats -- KeyError here loses the line."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())

    log = logging.getLogger("test_observability.filter")
    log.propagate = False
    log.setLevel(logging.INFO)
    log.addHandler(handler)

    log.info("outside a request")
    token = set_request_id("req-xyz")
    try:
        log.info("inside a request")
    finally:
        reset_request_id(token)

    lines = stream.getvalue().splitlines()
    assert f"[{NO_REQUEST}]" in lines[0], lines[0]
    assert "[req-xyz]" in lines[1], lines[1]
    assert f"[{Config.DEFAULT_TENANT}]" in lines[0], lines[0]


def test_filter_renders_tenant_under_log_format() -> None:
    """Same as above, isolating the tenant field: a record with no tenant set
    must render the default, and a bound tenant must render verbatim."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())

    log = logging.getLogger("test_observability.tenant_filter")
    log.propagate = False
    log.setLevel(logging.INFO)
    log.addHandler(handler)

    log.info("outside a request")
    token = set_tenant("acme")
    try:
        log.info("inside a request")
    finally:
        reset_tenant(token)

    lines = stream.getvalue().splitlines()
    assert f"[{Config.DEFAULT_TENANT}]" in lines[0], lines[0]
    assert "[acme]" in lines[1], lines[1]


def test_tenant_default_outside_request() -> None:
    assert current_tenant() == Config.DEFAULT_TENANT


def test_tenant_survives_await() -> None:
    async def run() -> str:
        token = set_tenant("acme")
        try:
            await asyncio.sleep(0)
            return current_tenant()
        finally:
            reset_tenant(token)

    assert asyncio.run(run()) == "acme"


def test_tenant_concurrent_tasks_do_not_bleed() -> None:
    """Same shape as test_concurrent_tasks_do_not_bleed above, for tenant."""

    async def one(tenant: str, out: dict) -> None:
        token = set_tenant(tenant)
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            out[tenant] = current_tenant()
        finally:
            reset_tenant(token)

    async def run() -> dict:
        out: dict = {}
        await asyncio.gather(one("tenant-a", out), one("tenant-b", out))
        return out

    seen = asyncio.run(run())
    assert seen == {"tenant-a": "tenant-a", "tenant-b": "tenant-b"}, seen
    assert current_tenant() == Config.DEFAULT_TENANT, "task-local tenant must not leak to the caller"


def test_parse_tenant_none_and_valid() -> None:
    assert parse_tenant(None) == Config.DEFAULT_TENANT
    assert parse_tenant(b"acme-corp") == "acme-corp"
    assert parse_tenant("acme-corp") == "acme-corp"


def test_parse_tenant_lowercases() -> None:
    assert parse_tenant(b"Acme") == "acme"


def test_parse_tenant_hostile_inputs_fall_back_and_do_not_leak() -> None:
    """Each hostile value falls back to the default AND cannot forge a second
    log line -- a raw newline/CR reaching the formatter is what a log
    injection looks like, and %r must render it as an escape, not reproduce
    it. Separately, the per-request log line (LOG_FORMAT's `%(tenant)s`)
    only ever carries the safe, already-resolved tenant -- never the raw
    header -- which the middleware tests above cover end to end."""
    # "uppercase" is deliberately not here: the shape check runs AFTER
    # lowercasing (plan §3), so a pure-case difference is normalization, not
    # an invented identity -- test_parse_tenant_lowercases covers that it is
    # accepted, not rejected. Everything below fails the shape check even
    # after lowercasing.
    hostile = {
        "newline": b"evil\nWARNING forged line",
        "carriage_return": b"evil\rWARNING forged line",
        # TRAILING newline/CR, not interior -- the case that actually got
        # through. Python's `$` matches end-of-string OR just before a trailing
        # newline, so `^[a-z0-9][a-z0-9-]{0,31}$` ACCEPTED b"acme\n" and
        # returned it verbatim into LOG_FORMAT's [%(tenant)s]: one forged log
        # line per request. The interior cases above never caught it, because
        # \n is not in the character class and only the trailing position is
        # special. Fixed by anchoring with \Z.
        "trailing_newline": b"acme\n",
        "trailing_crlf": b"acme\r\n",
        "trailing_cr": b"acme\r",
        "over_length": b"a" * 33,
        "leading_hyphen": b"-tenant",
        "empty": b"",
        "non_ascii": "t\xe9nant".encode("latin-1"),
        "semicolons": b"tenant;DROP TABLE",
    }

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("src.observability")
    log.propagate = False
    log.setLevel(logging.WARNING)
    log.addHandler(handler)
    try:
        for name, raw in hostile.items():
            stream.truncate(0)
            stream.seek(0)
            result = parse_tenant(raw)
            assert result == Config.DEFAULT_TENANT, (name, result)
            logged = stream.getvalue()
            # Exactly one line out of one log call -- a raw newline/CR in the
            # rejected value would otherwise split into a second, forged line
            # that a log scraper reads as an independent record.
            assert logged.count("\n") == 1, (name, logged)
    finally:
        log.removeHandler(handler)


def test_valid_shapes_are_not_rejected() -> None:
    """Sanity check on the other side of the boundary -- shape validation must
    not reject ordinary values."""
    for ok in (b"default", b"a", b"a" * 32, b"tenant-42"):
        assert parse_tenant(ok) == ok.decode("latin-1")


def _drive(app, headers=None):
    """Run one ASGI request against `app`, returning (status, headers, body)."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/chat",
        "headers": headers or [],
    }
    sent: list = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


def test_middleware_sets_echoes_and_resets() -> None:
    observed: list[str] = []

    async def stub_app(scope, receive, send):
        # Read the id both before and after the response starts: the second read
        # is the streaming phase, which is where BaseHTTPMiddleware would have
        # lost it.
        observed.append(current_request_id())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(0)
        observed.append(current_request_id())
        await send({"type": "http.response.body", "body": b"ok"})

    app = RequestIdMiddleware(stub_app)

    status, headers, body = _drive(app)
    assert status == 200 and body == b"ok"
    echoed = headers[b"x-request-id"].decode()
    assert observed[0] == observed[1] == echoed, (observed, echoed)
    assert len(echoed) == 32
    assert current_request_id() == NO_REQUEST, "the id must not outlive the request"

    # Two requests, two ids.
    observed.clear()
    _, headers2, _ = _drive(app)
    assert headers2[b"x-request-id"] != headers[b"x-request-id"]

    # An inbound X-Request-ID is IGNORED, not adopted: correlation is
    # traceparent, and echoing caller-controlled text into logs forges lines.
    observed.clear()
    hostile = b"forged\nWARNING evil"
    _, headers3, _ = _drive(app, headers=[(b"x-request-id", hostile)])
    assert headers3[b"x-request-id"] != hostile
    assert observed[0] != hostile.decode("latin-1")
    assert len(observed[0]) == 32


def test_middleware_derives_id_from_surrounding_span() -> None:
    """With a span in scope the middleware must adopt its trace id, not mint."""
    observed: list[str] = []

    async def stub_app(scope, receive, send):
        observed.append(current_request_id())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RequestIdMiddleware(stub_app)

    async def run():
        with _tracer.start_as_current_span("server") as span:
            expected = format(span.get_span_context().trace_id, "032x")
            scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
            sent = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                sent.append(message)

            await app(scope, receive, send)
            start = next(m for m in sent if m["type"] == "http.response.start")
            return expected, dict(start["headers"])[b"x-request-id"].decode()

    expected, echoed = asyncio.run(run())
    assert observed[0] == expected == echoed, (observed, expected, echoed)


def test_middleware_binds_default_tenant_with_no_header() -> None:
    observed: list[str] = []

    async def stub_app(scope, receive, send):
        observed.append(current_tenant())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RequestIdMiddleware(stub_app)
    _drive(app)
    assert observed == [Config.DEFAULT_TENANT]
    assert current_tenant() == Config.DEFAULT_TENANT, "must not outlive the request"


def test_middleware_ignores_the_tenant_header() -> None:
    """CHANGED AT v2 P5 (D7). This test previously asserted the opposite --
    that X-Tenant-ID won over the default -- which P1 accepted explicitly and
    bounded to "until P5 makes identity real". P5 made it real: the tenant now
    comes from an authenticated session through the injected `tenant_resolver`,
    and an inbound header that could still set it would be a plain
    impersonation primitive. The flip is the security property, so the test
    flipped with it.

    `parse_tenant()` itself is unchanged and still tested above -- it survives
    as the SHAPE validator for an account id, not as an intake path."""
    observed: list[str] = []

    async def stub_app(scope, receive, send):
        observed.append(current_tenant())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RequestIdMiddleware(stub_app)
    _drive(app, headers=[(TENANT_HEADER, b"Acme-Corp")])
    assert observed == [Config.DEFAULT_TENANT], "the header must not set the tenant"


def test_middleware_binds_what_the_resolver_returns() -> None:
    """The P5 seam: whatever `tenant_resolver(scope)` returns is bound to the
    ContextVar and to `scope["state"]["tenant"]`, and None means anonymous."""
    observed: list[tuple] = []

    async def stub_app(scope, receive, send):
        observed.append((current_tenant(), scope["state"]["tenant"]))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    _drive(RequestIdMiddleware(stub_app, tenant_resolver=lambda scope: "acme-corp"))
    assert observed == [("acme-corp", "acme-corp")], observed

    observed.clear()
    _drive(RequestIdMiddleware(stub_app, tenant_resolver=lambda scope: None))
    assert observed == [(Config.DEFAULT_TENANT, Config.DEFAULT_TENANT)], observed


def test_middleware_hostile_tenant_header_falls_back() -> None:
    observed: list[str] = []
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())
    log = logging.getLogger("test_observability.mw_hostile")
    log.propagate = False
    log.setLevel(logging.INFO)
    log.addHandler(handler)

    async def stub_app(scope, receive, send):
        observed.append(current_tenant())
        log.info("request-scoped log line")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RequestIdMiddleware(stub_app)
    hostile = b"forged\nWARNING evil"
    _drive(app, headers=[(TENANT_HEADER, hostile)])
    # Since P5 the header is ignored outright rather than validated-then-
    # rejected, so this now holds for a stronger reason than it used to. The
    # log-injection assertion below is the part that still earns its keep.
    assert observed == [Config.DEFAULT_TENANT]
    # The per-request log line renders the resolved default, never the raw
    # header -- the whole point of falling back rather than sanitizing.
    logged = stream.getvalue()
    assert hostile.decode("latin-1") not in logged, logged
    assert f"[{Config.DEFAULT_TENANT}]" in logged, logged


def test_middleware_resets_on_exception() -> None:
    async def boom(scope, receive, send):
        raise RuntimeError("handler failed")

    app = RequestIdMiddleware(boom)
    try:
        _drive(app)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the middleware must not swallow handler errors")
    assert current_request_id() == NO_REQUEST
    assert current_tenant() == Config.DEFAULT_TENANT


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
