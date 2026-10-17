# test/test_span_content.py
"""v3 P0 B1 verify (OI27): exported spans are content-free. A planted query string must not
appear in any span name, attribute or event of a served request.

The check is shown to move: it fails against deliberately leaking span processors (one per
surface), so a green run on the real handler is not a vacuous pass.

Plain-assert script. Offline: in-memory exporter, stubbed retrieval, no network. Run from the repo root.
"""

import asyncio
import json
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "test")

from opentelemetry.sdk.trace import SpanProcessor, TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

import chat_harness as h  # noqa: E402
import src.api.app as app_mod  # noqa: E402

h.use_temp_db()

NEEDLE = "PLANTED-QUERY-7f3a9c-do-not-export"


def span_text(span) -> str:
    return json.dumps(
        {
            "name": span.name,
            "attributes": {k: str(v) for k, v in (span.attributes or {}).items()},
            "events": [
                {"name": e.name, "attributes": {k: str(v) for k, v in (e.attributes or {}).items()}}
                for e in span.events
            ],
        },
        default=str,
    )


def find_leaks(spans, needle: str) -> list[str]:
    """Names of the exported spans that carry `needle` anywhere."""
    return [s.name for s in spans if needle in span_text(s)]


class LeakAttribute(SpanProcessor):
    def on_start(self, span, parent_context=None):
        span.set_attribute("request.query", NEEDLE)


class LeakName(SpanProcessor):
    def on_start(self, span, parent_context=None):
        span.update_name(f"{span.name} {NEEDLE}")


class LeakEvent(SpanProcessor):
    def on_start(self, span, parent_context=None):
        span.add_event("query", {"text": NEEDLE})


async def _tokens():
    for text in ("Rodion ", "hesitated."):
        yield text


def _export(leaker: SpanProcessor | None = None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    if leaker is not None:
        provider.add_span_processor(leaker)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test-span-content")
    saved = app_mod.tracer

    async def _go():
        with tracer.start_as_current_span("POST /chat"):
            await h.run_chat(h.make_state(), query=NEEDLE, stream_answer=lambda m, u: _tokens())

    app_mod.tracer = tracer
    try:
        asyncio.run(_go())
    finally:
        app_mod.tracer = saved
    return list(exporter.get_finished_spans())


def test_served_request_spans_are_content_free() -> None:
    spans = _export()
    assert len(spans) >= 6, f"only {len(spans)} spans exported -- the check below would be vacuous"
    assert find_leaks(spans, NEEDLE) == []


def test_the_check_fails_against_a_leaking_exporter_on_every_surface() -> None:
    for leaker in (LeakAttribute(), LeakName(), LeakEvent()):
        spans = _export(leaker)
        leaked = find_leaks(spans, NEEDLE)
        assert leaked, f"{type(leaker).__name__} leaked the query and the check did not notice"
        assert len(leaked) == len(spans), "every span was leaked into; every one should be flagged"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
