# test/test_log_content.py
"""v3 P0 B2 verify (P0-F6 (3)): query text stays out of log records. Two instruments: a planted query
string is absent from every captured log record of a served request on each branch that logs, and
a static scan finds no logging call passing the query except as a length.

Plain-assert script. Offline: FakeProvider and stubs, temp SQLite. Run from the repo root.
"""

import ast
import asyncio
import logging
import sys
import textwrap

sys.path.insert(0, ".")
sys.path.insert(0, "test")

import chat_harness as h  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from src.gateway.fake import FakeProvider, Fixture  # noqa: E402
from src.gateway.ladder import Rung  # noqa: E402
from src.gateway.retry import TransportError  # noqa: E402

h.use_temp_db()

PLANTED = "zq-planted-7f3a Did Rodion hide the axe under the stairs?"
SOURCES_SCANNED = ("src/api/app.py", "src/rag/entities.py")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _fixture() -> Fixture:
    return Fixture(
        chunks=("Marvel ", "not."), prompt_tokens=10, completion_tokens=2,
        prompt_rate=1.0, completion_rate=4.0,
    )


def _captured(**run_kw):
    """Serve one request carrying PLANTED; returns the records, and the exception if it raised."""
    handler, root = _Capture(), logging.getLogger()
    saved_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    error = None
    try:
        asyncio.run(h.run_chat(query=PLANTED, **run_kw))
    except (HTTPException, TransportError) as exc:
        error = exc
    finally:
        root.removeHandler(handler)
        root.setLevel(saved_level)
    return handler.records, error


def _assert_clean(records: list[logging.LogRecord], label: str) -> None:
    assert records, f"{label}: no records captured, the check would be vacuous"
    for record in records:
        assert PLANTED not in record.getMessage(), f"{label}: {record.name}: {record.getMessage()[:120]}"
        assert PLANTED not in repr(record.args), f"{label}: {record.name} passes the query as an argument"


def _chat_lines(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records if r.getMessage().startswith("chat:")]


def test_live_branch_logs_no_query_text() -> None:
    records, _ = _captured(state=h.make_state(), provider=FakeProvider(fixture=_fixture()))
    _assert_clean(records, "live")
    lines = _chat_lines(records)
    assert any("query_chars=%d" % len(PLANTED) in line for line in lines), lines


def test_cached_branch_logs_no_query_text() -> None:
    cached = {"answer": "He did.", "sources": h.SOURCES, "footer": "Sources:", "model": "stub",
              "finish_reason": "stop"}
    records, _ = _captured(state=h.make_state(cached=cached))
    _assert_clean(records, "cached")
    assert any("cache_hit=true" in line for line in _chat_lines(records))


def test_no_context_branch_logs_no_query_text() -> None:
    records, _ = _captured(
        state=h.make_state(), provider=FakeProvider(fixture=_fixture()),
        build_context=lambda *a, **kw: ("", []),
    )
    _assert_clean(records, "no_context")
    assert any("no_context=true" in line for line in _chat_lines(records))


def test_filter_relaxed_branch_logs_no_query_text() -> None:
    records, _ = _captured(
        state=h.make_state(), provider=FakeProvider(fixture=_fixture()),
        build_hard_filter=lambda filters: object(),
        retrieve=lambda q, e, b, query, flt: [] if flt is not None else list(h.POINTS),
    )
    _assert_clean(records, "filter_relaxed")
    assert any("filter_relaxed=true" in line for line in _chat_lines(records))


def test_retrieval_only_and_unavailable_rungs_log_no_query_text() -> None:
    records, _ = _captured(
        state=h.make_state(), next_rung=lambda readiness, cache_hit: Rung.RETRIEVAL_ONLY,
    )
    _assert_clean(records, "retrieval_only")
    assert any("passages only" in line for line in _chat_lines(records))
    records, error = _captured(
        state=h.make_state(), next_rung=lambda readiness, cache_hit: Rung.UNAVAILABLE,
    )
    assert isinstance(error, HTTPException) and error.status_code == 503
    _assert_clean(records, "unavailable")
    assert any("no rung can serve" in line for line in _chat_lines(records))


def test_mid_stream_failure_logs_no_query_text() -> None:
    def _stream(messages, usage):
        async def _gen():
            yield "Marvel "
            raise TransportError("boom")
        return _gen()

    records, _ = _captured(state=h.make_state(), stream_answer=_stream)
    _assert_clean(records, "mid_stream")
    assert any("mid-stream failure" in line for line in _chat_lines(records))


def _leaks(source: str) -> list[int]:
    """Line numbers of logging calls that pass the query other than inside len()."""
    leaks = []
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and getattr(node.func.value, "id", None) in ("logger", "log")
                and node.func.attr in ("debug", "info", "warning", "error", "exception", "critical")):
            continue
        for arg in node.args:
            inside_len = {id(n) for call in ast.walk(arg)
                          if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "len"
                          for n in ast.walk(call)}
            for sub in ast.walk(arg):
                is_query = (isinstance(sub, ast.Attribute) and sub.attr == "query") or (
                    isinstance(sub, ast.Name) and sub.id == "query")
                if is_query and id(sub) not in inside_len:
                    leaks.append(node.lineno)
    return leaks


def test_no_logging_call_in_the_serving_path_passes_the_query() -> None:
    for path in SOURCES_SCANNED:
        with open(path, encoding="utf-8") as f:
            assert _leaks(f.read()) == [], path


def test_the_static_scan_is_moved_by_a_leaking_call() -> None:
    assert _leaks('logger.info("q=%r", body.query)') == [1]
    assert _leaks('logger.info("q=%r", query)') == [1]
    assert _leaks('logger.info("q=%d", len(body.query))') == []


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
