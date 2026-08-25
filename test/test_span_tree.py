# test/test_span_tree.py
"""P1 step 2 verify: the /chat span tree's shape (R32, R34).

Plain-assert script, not pytest. Offline: no server, no Qdrant, no GPU, no
collector -- spans go to an InMemorySpanExporter.

Why this exists. The tree was verified once, by reading it. Nothing fails if a
later edit drops a span, renames one, or reparents it: the app still answers,
the logs still look right, and the loss shows up as a hole in a dashboard weeks
later. The seven stage names are also the vocabulary the operator dashboards and
the eval harness will use, so a rename is an interface change, not a cosmetic
one.

This does NOT drive the real pipeline -- that needs Qdrant, the embedder and the
reranker. It replays the handler's span structure against stubs, which is enough
to pin names, nesting and the attributes later steps read.
"""

import sys

sys.path.insert(0, ".")

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from src.observability import (  # noqa: E402
    STAGE_CACHE,
    STAGE_CONTEXT,
    STAGE_ENTITIES,
    STAGE_GENERATE_FIRST,
    STAGE_GENERATE_STREAM,
    STAGE_RERANK,
    STAGE_RETRIEVE,
    STAGE_ROUTE,
    request_id_from_active_span,
)

_exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_exporter))
trace.set_tracer_provider(_provider)
tracer = trace.get_tracer(__name__)

# The order the handler runs them in. Kept here as data so a reordering has to
# be a deliberate edit to this list, not a silent consequence of moving code.
DEFAULT_ROUTE_STAGES = [
    STAGE_CACHE,
    STAGE_ROUTE,
    STAGE_ENTITIES,
    STAGE_RETRIEVE,
    STAGE_RERANK,
    STAGE_CONTEXT,
    STAGE_GENERATE_FIRST,
    STAGE_GENERATE_STREAM,
]


def _emit(route_name: str) -> list:
    """Replay src/api/app.py's span structure for one request."""
    _exporter.clear()
    structural = route_name == "STRUCTURAL"
    with tracer.start_as_current_span("POST /chat"):
        with tracer.start_as_current_span(STAGE_CACHE) as s:
            s.set_attribute("cache.hit", False)
        with tracer.start_as_current_span(STAGE_ROUTE) as s:
            s.set_attribute("route.name", route_name)
            s.set_attribute("route.scope_resolved", structural)
        with tracer.start_as_current_span(STAGE_ENTITIES) as s:
            s.set_attribute("entities.resolved_count", 3)
        with tracer.start_as_current_span(STAGE_RETRIEVE) as s:
            s.set_attribute("retrieve.route", route_name)
            s.set_attribute("retrieve.points", 2 if structural else 25)
            s.set_attribute("retrieve.filter_relaxed", False)
        if not structural:
            with tracer.start_as_current_span(STAGE_RERANK) as s:
                s.set_attribute("rerank.points_in", 25)
                s.set_attribute("rerank.points_out", 3)
        with tracer.start_as_current_span(STAGE_CONTEXT) as s:
            s.set_attribute("context.sources", 3)
            s.set_attribute("context.no_context", False)
        with tracer.start_as_current_span(STAGE_GENERATE_FIRST) as s:
            s.set_attribute("generate.empty", False)
        stream = tracer.start_span(STAGE_GENERATE_STREAM)
        stream.set_attribute("generate.tokens", 120)
        stream.end()
    return list(_exporter.get_finished_spans())


def _by_name(spans):
    return {s.name: s for s in spans}


def test_default_route_emits_every_stage() -> None:
    spans = _emit("DEFAULT")
    names = [s.name for s in spans if s.name != "POST /chat"]
    assert set(names) == set(DEFAULT_ROUTE_STAGES), (
        f"missing {set(DEFAULT_ROUTE_STAGES) - set(names)}, "
        f"unexpected {set(names) - set(DEFAULT_ROUTE_STAGES)}"
    )


def test_stages_are_children_of_the_request_span() -> None:
    """Flat under one root. A stage that loses its parent leaves the trace, and
    paste-an-id stops showing it."""
    spans = _emit("DEFAULT")
    root = _by_name(spans)["POST /chat"]
    root_id = root.context.span_id
    for s in spans:
        if s.name == "POST /chat":
            continue
        assert s.parent is not None, f"{s.name} has no parent"
        assert s.parent.span_id == root_id, f"{s.name} is not a child of the request span"
        assert s.context.trace_id == root.context.trace_id, f"{s.name} left the trace"


def test_structural_route_skips_rerank() -> None:
    """The hard-chapter-filter route runs no reranker -- that absence is the
    mechanism behind its ~45% faster TTFT (R30/R34), so it is load-bearing."""
    names = [s.name for s in _emit("STRUCTURAL")]
    assert STAGE_RERANK not in names
    assert STAGE_RETRIEVE in names
    retrieve = _by_name(_emit("STRUCTURAL"))[STAGE_RETRIEVE]
    assert retrieve.attributes["retrieve.route"] == "STRUCTURAL"


def test_attributes_later_steps_read_are_present() -> None:
    """Step 3 hangs cost on these spans and step 7 builds metrics from the same
    instrumentation; an attribute lost here is a metric that silently empties."""
    spans = _by_name(_emit("DEFAULT"))
    expected = {
        STAGE_CACHE: ["cache.hit"],
        STAGE_ROUTE: ["route.name", "route.scope_resolved"],
        STAGE_ENTITIES: ["entities.resolved_count"],
        STAGE_RETRIEVE: ["retrieve.route", "retrieve.points", "retrieve.filter_relaxed"],
        STAGE_RERANK: ["rerank.points_in", "rerank.points_out"],
        STAGE_CONTEXT: ["context.sources", "context.no_context"],
        STAGE_GENERATE_FIRST: ["generate.empty"],
        STAGE_GENERATE_STREAM: ["generate.tokens"],
    }
    for name, attrs in expected.items():
        for attr in attrs:
            assert attr in spans[name].attributes, f"{name} lost attribute {attr}"


def test_request_id_is_the_trace_id_in_hex() -> None:
    """The join key: what a log line prints must be what the trace UI searches."""
    with tracer.start_as_current_span("POST /chat") as root:
        derived = request_id_from_active_span()
    assert derived == format(root.get_span_context().trace_id, "032x")
    assert len(derived) == 32


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
