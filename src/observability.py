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
import uuid

# "-" rather than None or "": offline callers (scripts, eval harness) log
# through the same format string and must render as an obvious non-request
# rather than crashing the formatter or printing "None".
NO_REQUEST = "-"

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=NO_REQUEST
)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s"


def new_request_id() -> str:
    """Mint an id. UUID4 -- unguessable, so exposing it to the browser does not
    make one request's telemetry enumerable from another's (the P5 posture
    question is about *authorization*, which an unguessable id does not answer
    on its own; it only removes enumeration)."""
    return str(uuid.uuid4())


def current_request_id() -> str:
    return _request_id.get()


def set_request_id(request_id: str) -> contextvars.Token:
    return _request_id.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


def coerce_request_id(raw: str | None) -> str:
    """Accept a caller-supplied id only if it is a canonical UUID; otherwise mint.

    Echoing an arbitrary inbound header into logs is a log-injection primitive
    (newlines forge log lines) and a correlation-poisoning one (a caller can
    collide its id with another request's). Re-serializing through `uuid.UUID`
    guarantees the value that reaches a log record is canonical regardless of
    what arrived.
    """
    if not raw:
        return new_request_id()
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        return new_request_id()


class RequestIdFilter(logging.Filter):
    """Attach the current request id to every record passing through a handler.

    Installed on the *handler*, not on one logger: records from third-party
    libraries reach the same handler and would otherwise KeyError against
    LOG_FORMAT's `%(request_id)s`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
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
    """Pure-ASGI middleware: set the id for the whole request, echo it as a header.

    Deliberately not `@app.middleware("http")` / `BaseHTTPMiddleware`, which
    runs the downstream app in a child task. Reading the id downstream would
    still work (a child task copies the context at creation), but three things
    do not: a ContextVar set *inside* the endpoint is not visible back in the
    middleware, the extra task hop costs per request on a path we are about to
    measure to the millisecond, and it complicates streaming and exception
    handling. Plain ASGI middleware has no task hop at all -- the id is set and
    reset in the same task that runs the entire call, streamed body included --
    and it is drivable in a test with a hand-built scope dict and no server.
    """

    HEADER = b"x-request-id"

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = None
        for key, value in scope.get("headers", []):
            if key.lower() == self.HEADER:
                inbound = value.decode("latin-1")
                break
        request_id = coerce_request_id(inbound)
        token = set_request_id(request_id)

        async def send_with_header(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((self.HEADER, request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            reset_request_id(token)
