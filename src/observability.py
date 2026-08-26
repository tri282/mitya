"""Request-scoped correlation id -- the join key every telemetry plane shares (P1 step 1).

One `request_id` is minted at the API edge and carried for the life of the
request, so that given only an id, every record about that request is findable
and nothing describing a request lacks one (plan `phase-1-telemetry-config.md`
step 1).

Propagation is a `contextvars.ContextVar` rather than a threaded parameter.
The pipeline modules (`src/rag/retrieve.py`, `router.py`, `entities.py`,
`context.py`, `generate.py`, `cache.py`) are also called from offline scripts
that have no request at all, so threading the id would mean seven signature
changes plus a sentinel argument at every offline call site. A ContextVar
crosses `await` boundaries and is isolated per asyncio task, which a module
global is not -- and the streamed token phase of `/chat` is exactly where a
module global would start attributing tokens to the wrong request once the
`Semaphore(1)` of v1 is lifted.

This module is deliberately independent of the telemetry-family decision (C1):
a correlation id plus a logging filter is required under log-derived,
span-tree, and hybrid alike.
"""

import contextvars
import logging
import re
import uuid
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

if TYPE_CHECKING:
    # Import-time only. src.pricing imports src.config, which this module must
    # not pull in at import: configure_logging() runs before config is read.
    from src.pricing import Cost

# "-" rather than None or "": offline callers (scripts, eval harness) log
# through the same format string and must render as an obvious non-request
# rather than crashing the formatter or printing "None".
NO_REQUEST = "-"

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=NO_REQUEST
)

# Default is None, not Config.DEFAULT_TENANT, so that creating this ContextVar
# does not import src.config at module scope -- src.pricing's TYPE_CHECKING
# note above explains why that import must stay lazy. `current_tenant()`
# resolves None to Config.DEFAULT_TENANT on read instead.
_tenant: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant", default=None
)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] [%(tenant)s]: %(message)s"


def new_request_id() -> str:
    """Mint an id: 32 lowercase hex, the same shape as an OTel trace id.

    Used only where there is no active trace (offline scripts, and requests on
    URLs excluded from tracing such as /health). When a trace exists the id is
    *derived* from it -- see RequestIdMiddleware -- so that the value in a log
    line can be pasted into the trace UI verbatim, with no reformatting and no
    second identifier to reconcile.

    128 random bits, so it is unguessable and one request's telemetry is not
    enumerable from another's. That removes enumeration; it is not an
    authorization control, which is P5's problem.
    """
    return uuid.uuid4().hex


def current_request_id() -> str:
    return _request_id.get()


def set_request_id(request_id: str) -> contextvars.Token:
    return _request_id.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


# ---------------------------------------------------------------------------
# Tenant (v2 P1 step 4; A12 -- one synthetic tenant until P5 makes identity
# real). Same ContextVar/filter/middleware shape as request_id above -- this
# is that mechanism again with a different value and a validation step,
# deliberately not a second mechanism.
# ---------------------------------------------------------------------------

# UNAUTHENTICATED until P5: anyone who can reach the endpoint can claim any
# tenant by sending this header. Acceptable now (single operator, synthetic
# traffic, loopback) -- see parse_tenant() for the validation that keeps it
# from becoming a log-injection or metrics-cardinality problem in the
# meantime.
TENANT_HEADER = b"x-tenant-id"

# Allowlist-shaped, not a registry of known tenant ids -- there is no tenant
# registry until P5. Lowercase alnum + hyphen, 1-32 chars.
#
# \Z, NOT $. In Python `$` matches at end-of-string *or immediately before a
# trailing newline*, so `^...$` accepts "acme\n" -- and this value is
# interpolated into LOG_FORMAT's [%(tenant)s], which makes a trailing newline a
# log-injection primitive: one forged line per request. An interior newline was
# already rejected (\n is not in the class); it is specifically the trailing one
# that slipped through. \Z anchors at end-of-string and nothing else.
_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}\Z")


def current_tenant() -> str:
    tenant = _tenant.get()
    if tenant is not None:
        return tenant
    from src.config import Config

    return Config.DEFAULT_TENANT


def set_tenant(tenant: str) -> contextvars.Token:
    return _tenant.set(tenant)


def reset_tenant(token: contextvars.Token) -> None:
    _tenant.reset(token)


def parse_tenant(raw: bytes | str | None) -> str:
    """Validate an inbound X-Tenant-ID header value, falling back to the
    default tenant on anything that doesn't fit the allowed shape.

    UNAUTHENTICATED until P5 (charter §IV) -- this reaches log lines, span
    attributes, and (step 7) Prometheus label values. A caller who sends a
    newline forges a log line; a caller who sends unbounded distinct values
    explodes a metric's label cardinality. So: reject, don't sanitize --
    sanitizing would invent a tenant id nobody sent -- and log the rejection
    at WARNING via %r, which renders control characters as escapes rather
    than reproducing them.
    """
    from src.config import Config

    if raw is None:
        return Config.DEFAULT_TENANT
    value = raw.decode("latin-1") if isinstance(raw, bytes) else raw
    candidate = value.lower()
    if _TENANT_RE.match(candidate):
        return candidate
    logging.getLogger(__name__).warning(
        "tenant: rejected header value %r -- falling back to default tenant %r",
        value, Config.DEFAULT_TENANT,
    )
    return Config.DEFAULT_TENANT


class RequestIdFilter(logging.Filter):
    """Attach the current request id and tenant to every record passing
    through a handler.

    Installed on the *handler*, not on one logger: records from third-party
    libraries reach the same handler and would otherwise KeyError against
    LOG_FORMAT's `%(request_id)s` / `%(tenant)s`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        record.tenant = current_tenant()
        return True


def _instrument(handler: logging.Handler, formatter: logging.Formatter) -> None:
    if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
        handler.addFilter(RequestIdFilter())
    handler.setFormatter(formatter)


def configure_logging(level: int = logging.INFO) -> None:
    """basicConfig + the filter on every handler that currently exists. Idempotent.

    Walks *all* loggers, not just root, because uvicorn installs handlers on its
    own `uvicorn` / `uvicorn.access` / `uvicorn.error` loggers with
    `propagate=False` -- so root's formatter never reaches them, and
    `uvicorn.access` lines describe requests. Step 1's end-state is that nothing
    describing a request lacks an id, and an access log without one is the
    counterexample.

    Uvicorn configures its logging *after* importing the app, so this is called
    twice: once at import (covers our own loggers immediately, and anything that
    fails during import) and once from `lifespan()`, which runs late enough to
    catch uvicorn's. Re-invoking is safe -- handlers are mutated in place, never
    stacked.
    """
    logging.basicConfig(level=level, format=LOG_FORMAT)
    formatter = logging.Formatter(LOG_FORMAT)
    for handler in logging.getLogger().handlers:
        _instrument(handler, formatter)
    for name in list(logging.root.manager.loggerDict):
        for handler in getattr(logging.getLogger(name), "handlers", []):
            _instrument(handler, formatter)


class RequestIdMiddleware:
    """Pure-ASGI middleware: bind the request id for the whole call, echo it.

    The id is DERIVED from the active trace, not minted alongside it. OTel's
    FastAPI instrumentation overrides `build_middleware_stack`, so its server
    span is created *outside* every user middleware -- no `add_middleware`
    ordering can put this one first. Rather than fight that, the trace id is
    treated as authoritative and the request id is its hex form. One identifier,
    and a log line's id pastes straight into the trace UI.

    Where no span exists -- an excluded URL such as /health, or tracing switched
    off -- an id is minted so that nothing describing a request lacks one.

    Inbound correlation is W3C `traceparent`, which OTel's propagators already
    honour, not a hand-rolled `X-Request-ID` intake. That is the standard
    mechanism and it also removes an injection surface: an unvalidated inbound
    header reaching a log record forges log lines, and lets a caller collide
    deliberately with another request's id. The header is echoed on the way out,
    never read on the way in.

    Deliberately not `@app.middleware("http")` / `BaseHTTPMiddleware`, which runs
    the downstream app in a child task. Reading the id downstream would still
    work (a child task copies the context at creation), but a ContextVar set
    *inside* the endpoint is not visible back in the middleware, the extra task
    hop costs per request on a path we are about to measure to the millisecond,
    and it complicates streaming and exception handling. Plain ASGI has no task
    hop -- the id is set and reset in the same task that runs the entire call,
    streamed body included -- and it is drivable in a test with a hand-built
    scope dict and no server.

    Also binds the tenant (P1 step 4) in the same try/finally, reusing this
    middleware rather than adding a second one: a second ASGI hop would cost
    a task boundary on a path measured to the millisecond, the same reasoning
    that keeps this one plain ASGI in the first place. Unlike X-Request-ID,
    X-Tenant-ID IS read on the way in -- P4's load harness needs it to
    generate multi-tenant load without waiting for P5 -- so it goes through
    parse_tenant()'s validation rather than being ignored.
    """

    HEADER = b"x-request-id"

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = request_id_from_active_span() or new_request_id()
        request_id_token = set_request_id(request_id)
        tenant_token = set_tenant(parse_tenant(_header_value(scope, TENANT_HEADER)))

        async def send_with_header(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((self.HEADER, request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            reset_request_id(request_id_token)
            reset_tenant(tenant_token)


def _header_value(scope, name: bytes) -> bytes | None:
    """First value for an ASGI header name, or None. Scope headers are raw
    latin-1 bytes per the ASGI spec (never decoded until a caller asks)."""
    for key, value in scope.get("headers", []):
        if key == name:
            return value
    return None


# ---------------------------------------------------------------------------
# Tracing (P1 step 2; C1 settled by R32 -- OpenTelemetry span tree)
# ---------------------------------------------------------------------------
#
# The id minted above IS the trace id. A UUID4 is 128 bits and an OTel trace id
# is 128 bits, so `int(UUID(request_id))` is a lossless mapping in both
# directions -- one id, not two. Minting a separate trace id would split the
# join key R31 built: pasting a request_id into Jaeger would find nothing, and
# the log lines and the span tree would describe the same request under
# different names.

# Stage names. Fixed here rather than spelled at each call site so the operator
# dashboards, the eval harness and the span tree cannot drift into three
# different vocabularies for the same seven boundaries.
STAGE_CACHE = "cache.check"
STAGE_ROUTE = "route"
STAGE_ENTITIES = "entities.resolve"
STAGE_RETRIEVE = "retrieve"
STAGE_RERANK = "rerank"
STAGE_CONTEXT = "context.build"
# Generation is two spans, not one. The handler primes the stream eagerly (so a
# connect failure can still become a 503 rather than a hung stream), and the
# rest is consumed inside the SSE generator after the handler has returned. A
# single span would have to be attached in one and detached in the other; two
# spans measure the same thing without juggling OTel context across that
# boundary -- and the split is the more useful one anyway, since the first span
# IS time-to-first-token and carries the EMPTY_RETRIES attempts as httpx
# children (R33).
STAGE_GENERATE_FIRST = "generate.first_token"
STAGE_GENERATE_STREAM = "generate.stream"


def set_cost(span, cost: "Cost | None" = None, deferred_to: str | None = None) -> None:
    """Stamp the cost dimension on a stage span. Defaults to free.

    Named here beside the STAGE_* constants for the same reason they are named
    here: the operator dashboards, the eval harness and the span tree must not
    invent three vocabularies for one dimension.

    EVERY stage span gets this, including the six that run on our own hardware
    and cost nothing. An absent `cost.usd` reads as *unmeasured*; an explicit
    0.0 reads as *free*, which is the true claim and the one an aggregation two
    phases from now needs to be able to tell apart. It is one call per stage.

    Not speculative for the local stages either: `rerank` is currently 84% of
    the retrieval path (R34), and a hosted reranker is a metered API and a live
    P2/P7 option. That stage is the likeliest of the six to acquire a price.

    `deferred_to` names a sibling span that carries this stage's charge. It
    exists for one real case: `generate.first_token` IS prefill and does bill,
    but llama-server reports token usage in a final chunk that arrives long
    after that span has closed on the first token. Its 0.0 therefore means
    "charged on the named sibling", not "free" -- and without the marker a
    reader in Jaeger would draw exactly the wrong conclusion, which is the
    absent-vs-zero confusion this helper exists to prevent.
    """
    from src.pricing import FREE

    cost = cost if cost is not None else FREE
    span.set_attribute("cost.priced", cost.priced)
    span.set_attribute("cost.metered", cost.metered)
    if cost.usd is not None:
        span.set_attribute("cost.usd", cost.usd)
    if deferred_to is not None:
        span.set_attribute("cost.deferred_to", deferred_to)


def request_id_from_active_span() -> str | None:
    """The active trace id as 32 hex, or None when there is no valid span.

    This is the whole mapping: an OTel trace id is 128 bits and this renders it
    exactly as trace UIs display it, so a log line's `[abc123...]` is a search
    term in Jaeger with no conversion step.
    """
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid and ctx.trace_id:
        return format(ctx.trace_id, "032x")
    return None


def configure_tracing(app=None) -> trace.Tracer:
    """Install the tracer provider and, when an app is given, instrument it.

    Export is opt-in: with no OTLP endpoint configured the SDK builds spans and
    drops them. That keeps a dev run free of docker while leaving the
    instrumentation identical to production -- the thing that differs is where
    spans go, not whether the code creates them.
    """
    from src.config import Config

    provider = TracerProvider(
        resource=Resource.create({"service.name": Config.OTEL_SERVICE_NAME}),
    )

    if Config.OTEL_CONSOLE_EXPORT:
        # Local debugging without docker: spans printed as JSON on stdout. Also
        # the only way to see the tree in an environment where the host cannot
        # reach the collector.
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    if Config.OTEL_EXPORTER_OTLP_ENDPOINT:
        # Imported here, not at module scope: the exporter pulls in protobuf,
        # which has no business loading in offline scripts that never export.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = Config.OTEL_EXPORTER_OTLP_ENDPOINT.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        logging.getLogger(__name__).info("tracing: exporting spans to %s", endpoint)
    else:
        logging.getLogger(__name__).info(
            "tracing: no OTEL_EXPORTER_OTLP_ENDPOINT -- spans are built and dropped"
        )

    trace.set_tracer_provider(provider)

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        # The HTTP root span. Health checks are excluded: they are the loudest
        # traffic on the box and the least interesting, and at 100% sampling
        # they would dominate the trace store.
        # exclude_spans is not optional here. The ASGI instrumentation emits one
        # span per ASGI send/receive message, and an SSE response sends one
        # message PER TOKEN -- a 151-token answer produced ~155 zero-duration
        # `http send` spans against 8 that mean anything. That buries the stage
        # tree in the UI, and it is what step 10's storage-growth number would
        # otherwise be measuring. Time-to-first-token is already the
        # generate.first_token span and the total is the root span, so nothing
        # is lost.
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=provider,
            excluded_urls="health",
            exclude_spans=["send", "receive"],
        )
        # Every llama-server / provider call becomes a child span. This is what
        # makes an EMPTY_RETRIES storm legible: four attempts appear as four
        # children under one generate span instead of one opaque 20s block (R33).
        HTTPXClientInstrumentor().instrument(tracer_provider=provider)

    return trace.get_tracer(__name__)
