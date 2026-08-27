# test/test_redaction.py
"""P1 step 9 verify: provider key material appears in no span attribute and
no log record (a standing gate -- the parent plan requires this test to run
in every later phase's verify, not as a one-off).

Plain-assert script, not pytest. Offline: no real network call, no real
provider key. A sentinel value is set onto Config.GOOGLE_API_KEY /
OPENROUTER_API_KEY / GROQ_API_KEY for the duration of the test and never the
real .env values -- see _sentinel_config() below.

Three surfaces, because the third is the one a naive test misses (a span
check alone, or a log check alone, both pass while an exception message still
carries the key):

    1. Span attributes -- every span (names, attribute keys AND values,
       events) exported through an InMemorySpanExporter across a replayed
       TeacherClient call, serialized whole rather than spot-checked field by
       field.
    2. Log records -- every record emitted during the same replay, asserted
       against the *formatted* string (src/observability.py's LOG_FORMAT),
       not record.msg -- %s args only become part of the line at format time.
    3. Exception text -- a provider SDK error's str() routinely carries the
       request URL, and a key in a query string rides along if the transport
       ever puts it there. `google-genai` (the only wired provider) sends the
       key via the `x-goog-api-key` header, never the URL (verified against
       the installed SDK below), so surface 1 covers that path honestly. This
       surface separately proves that `teacher.py`'s own error-formatting
       code (`f"fatal API error {e.code}: {e.message}"`) never reaches into
       the underlying response's request URL even when that URL DOES carry a
       secret -- the case that would apply to any future key-in-URL provider.

What this does NOT cover (R41's lesson: a validator and its test can share a
blind spot). This is a replay-shaped test: it proves the emission surfaces it
drives are clean, not that the live handler is. Two named gaps:

    * OpenTelemetry's own default URL-query redaction
      (`opentelemetry.util.http.PARAMS_TO_REDACT`) covers only
      `AWSAccessKeyId`/`Signature`/`sig`/`X-Goog-Signature` -- NOT a generic
      `key`/`api_key` query parameter. If a future provider (OpenRouter, Groq,
      or a Gemini REST fallback) is wired up putting its key in the URL
      instead of a header, `HTTPXClientInstrumentor`'s `http.url` span
      attribute WILL leak it, and this test's surface-1 check would not catch
      that shape of leak because it exercises the header-only path that is
      actually used today.
    * OPENROUTER_API_KEY and GROQ_API_KEY are exercised here only as inert
      Config values (P1 5b/5c note they are quota-registry rows with no
      transport yet, per teacher.py). There is no live call path to replay
      for them until they are wired.
"""

import io
import logging
import pathlib
import sys

sys.path.insert(0, ".")

import httpx  # noqa: E402
from google.genai import errors  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from src.config import Config  # noqa: E402
from src.observability import LOG_FORMAT, RequestIdFilter  # noqa: E402
from src.rag import teacher  # noqa: E402
from src.rag.teacher import TeacherClient, TeacherError  # noqa: E402

SENTINEL = "SENTINEL-SECRET-do-not-leak-4f9a1c"


class _sentinel_config:
    """Context manager: sentinel key material onto Config, restored after.

    Never reads the real .env values -- only ever writes and restores this
    made-up string.
    """

    ATTRS = ("GOOGLE_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY")

    def __enter__(self):
        self._saved = {a: getattr(Config, a) for a in self.ATTRS}
        for a in self.ATTRS:
            setattr(Config, a, SENTINEL)
        return self

    def __exit__(self, *exc):
        for a, v in self._saved.items():
            setattr(Config, a, v)


def _span_tree_text(spans) -> str:
    """Serialize every span's name, attribute keys+values, and events into one
    string -- a substring search over this is the "serialized whole" check,
    not a field-by-field spot check."""
    chunks = []
    for s in spans:
        chunks.append(s.name)
        for k, v in s.attributes.items():
            chunks.append(f"{k}={v}")
        for e in s.events:
            chunks.append(e.name)
            for k, v in e.attributes.items():
                chunks.append(f"{k}={v}")
    return "\n".join(chunks)


def test_httpx_span_never_carries_the_key() -> None:
    """Surface 1. google-genai's installed SDK sends the API key via the
    `x-goog-api-key` HEADER (verified: _api_client.py sets
    self._http_options.headers['x-goog-api-key'] = self.api_key, never the
    URL) -- so a real request shaped like TeacherClient's carries the key
    where HTTPXClientInstrumentor's default span attributes never look.
    Headers are not captured as span attributes unless `captured_headers` is
    configured, which src/observability.py's configure_tracing() does not do.

    The request targets a closed local port (127.0.0.1:1, connection
    refused) rather than a mock transport -- a mock transport skips
    `HTTPTransport.handle_request` entirely, which is the method
    `HTTPXClientInstrumentor` wraps, so it would create no span at all and the
    assertion below would pass vacuously. A refused loopback connection still
    exercises the real, instrumented transport with zero network egress.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = HTTPXClientInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        with _sentinel_config():
            client = httpx.Client(
                timeout=1.0, headers={"x-goog-api-key": Config.GOOGLE_API_KEY}
            )
            try:
                client.get("http://127.0.0.1:1/v1beta/models/gemini:generateContent")
            except httpx.ConnectError:
                pass  # expected -- nothing is listening there
            client.close()
    finally:
        instrumentor.uninstrument()

    spans = exporter.get_finished_spans()
    assert len(spans) >= 1, "instrumentation produced no span -- test would be vacuous"
    tree = _span_tree_text(spans)
    assert SENTINEL not in tree, tree


def test_log_records_never_carry_the_key() -> None:
    """Surface 2. Drives TeacherClient.generate_json() through a fatal
    provider error and a retryable-then-fatal sequence -- the two paths that
    log an error string built from the provider exception -- and asserts the
    *formatted* line (LOG_FORMAT, via RequestIdFilter, exactly what a file or
    terminal receives) never carries the sentinel."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    original_level = root.level
    root.setLevel(logging.DEBUG)

    original_sleep = teacher.time.sleep
    teacher.time.sleep = lambda _seconds: None  # no real waiting between retries

    try:
        with _sentinel_config():
            client = TeacherClient(max_retries=2)
            fake_response = httpx.Response(
                429,
                request=httpx.Request(
                    "POST",
                    f"https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent?key={Config.GOOGLE_API_KEY}",
                ),
            )
            err = errors.APIError(
                429,
                {"error": {"message": "Resource has been exhausted", "status": "RESOURCE_EXHAUSTED"}},
                response=fake_response,
            )

            def boom(*args, **kwargs):
                raise err

            client._client.models.generate_content = boom
            try:
                client.generate_json("What happens in part 1 chapter 1?")
                raise AssertionError("expected TeacherError")
            except TeacherError:
                pass
    finally:
        teacher.time.sleep = original_sleep
        root.removeHandler(handler)
        root.setLevel(original_level)

    logged = stream.getvalue()
    assert SENTINEL not in logged, logged


def test_exception_text_never_carries_the_key() -> None:
    """Surface 3, the highest-risk one. Simulate the worst case a future
    key-in-URL provider would produce: the underlying httpx.Response's own
    request URL DOES carry the sentinel (proven below, so the test is not
    vacuous), but teacher.py's error-formatting path
    (`f"fatal API error {e.code}: {e.message}"`) only ever reads `e.code` and
    `e.message` -- never `e.response.request.url` or `str(e)` -- so the
    TeacherError it raises must not propagate it."""
    with _sentinel_config():
        fake_response = httpx.Response(
            403,
            request=httpx.Request(
                "POST",
                f"https://api.example.com/v1/generate?key={Config.GOOGLE_API_KEY}",
            ),
        )
        err = errors.APIError(
            403,
            {"error": {"message": "Permission denied", "status": "PERMISSION_DENIED"}},
            response=fake_response,
        )
        # Sanity: the fake IS realistic -- the secret really is in the object
        # this test hands to teacher.py's formatting code.
        assert SENTINEL in str(err.response.request.url)

        client = TeacherClient(max_retries=1)

        def boom(*args, **kwargs):
            raise err

        client._client.models.generate_content = boom
        try:
            client.generate_json("query")
            raise AssertionError("expected TeacherError")
        except TeacherError as exc:
            assert SENTINEL not in str(exc), str(exc)


def test_key_in_url_is_redacted_from_the_span() -> None:
    """Surface 4, added after the agent-implemented batch flagged the gap.

    OTel ships PARAMS_TO_REDACT as ['AWSAccessKeyId', 'Signature', 'sig',
    'X-Goog-Signature'] -- AWS-shaped, with no generic `key`/`api_key`. The
    httpx instrumentation calls redact_url() on the URL before setting
    http.url, and redact_url() reads that list from its module global at call
    time, so configure_tracing() extends it (src/observability.py).

    This asserts the extension actually works end to end, through the real
    instrumented transport, on the URL shape a Gemini REST fallback or an
    OpenRouter/Groq wiring would produce. It is a live gate, not a note: it
    fails if the extend() is ever removed.
    """
    from opentelemetry.util import http as otel_http

    saved = list(otel_http.PARAMS_TO_REDACT)
    # Same extension configure_tracing() applies. Asserted below rather than
    # imported, because configure_tracing() needs a FastAPI app and an
    # exporter endpoint -- this pins the behaviour, and the check at the end
    # of this test pins that the two lists have not drifted apart.
    otel_http.PARAMS_TO_REDACT.extend(
        p for p in ("key", "api_key", "apikey", "access_token", "token")
        if p not in otel_http.PARAMS_TO_REDACT
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = HTTPXClientInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        with _sentinel_config():
            client = httpx.Client(timeout=1.0)
            try:
                # Refused loopback port -- real instrumented transport, zero
                # egress. See test_httpx_span_never_carries_the_key on why a
                # MockTransport would make this vacuous.
                client.get(f"http://127.0.0.1:1/v1/generate?key={Config.GOOGLE_API_KEY}")
            except httpx.ConnectError:
                pass
            client.close()
    finally:
        instrumentor.uninstrument()
        otel_http.PARAMS_TO_REDACT[:] = saved

    spans = exporter.get_finished_spans()
    assert len(spans) >= 1, "instrumentation produced no span -- test would be vacuous"
    tree = _span_tree_text(spans)
    # Not vacuous the other way either: the URL really did reach the span.
    assert "127.0.0.1:1" in tree, tree
    assert SENTINEL not in tree, tree

    # The list this test extends must stay the list configure_tracing()
    # extends. A drift here means the gate is testing a redaction the app
    # does not apply.
    source = (pathlib.Path(__file__).parent.parent / "src" / "observability.py").read_text()
    for param in ("key", "api_key", "apikey", "access_token", "token"):
        assert f'"{param}"' in source, f"{param} missing from configure_tracing()'s extend"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
