# test/test_provider_fixtures.py
"""v3 P0 B2 verify (OI46, schedule-p0 V rulings 2026-10-20): the committed provider fixtures carry
no sentinel key and no response header outside the allowlist, and the check fails when either is planted.

Plain-assert script. Offline; reads only test/fixtures/providers and, when present, the raw P-1 recordings.
Run from the repo root.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from scripts.scrub_fixtures import (  # noqa: E402
    DEST,
    SENTINEL,
    SOURCES,
    copy_fixtures,
    header_allowed,
    scrub_record,
    violations,
)

SURFACES = {"openrouter", "baseten", "purdue", "jev"}
FORBIDDEN = (
    "set-cookie", "x-litellm-model-api-base", "x-litellm-key-spend", "cf-ray", "server-timing",
    "x-generation-id", "x-baseten-request-id", "access-control-allow-origin",
)


def _fresh_copy() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="mitya-fixtures-")) / "providers"
    shutil.copytree(DEST, tmp)
    return tmp


def test_committed_set_is_present_and_clean() -> None:
    assert DEST.is_dir(), "run `python scripts/scrub_fixtures.py` first"
    assert {p.name for p in DEST.iterdir() if p.is_dir()} >= SURFACES
    assert len(list(DEST.rglob("*.json"))) >= 13
    assert violations(DEST) == []


def test_sentinel_grep_over_the_whole_set() -> None:
    for path in DEST.rglob("*.json"):
        assert SENTINEL not in path.read_text(encoding="utf-8"), path


def test_only_allowlisted_headers_remain() -> None:
    for path in DEST.rglob("*.json"):
        for name in json.loads(path.read_text(encoding="utf-8")).get("headers", {}):
            assert header_allowed(name), f"{path}: {name}"
            assert name == name.lower(), f"{path}: {name} not normalized"


def test_scrub_record_drops_unlisted_and_keeps_listed() -> None:
    record = {"status": 200, "headers": {name: "v" for name in FORBIDDEN} | {
        "Content-Type": "text/event-stream", "X-Ratelimit-Limit-Requests": "15", "Retry-After": "3",
    }, "body": "x"}
    kept = scrub_record(record)["headers"]
    assert set(kept) == {"content-type", "x-ratelimit-limit-requests", "retry-after"}, kept
    assert scrub_record({"status": 200, "body": "x"}) == {"status": 200, "body": "x"}


def test_a_planted_forbidden_header_fails_red() -> None:
    for name in FORBIDDEN:
        tmp = _fresh_copy()
        target = next(tmp.rglob("stream_ok.json"))
        record = json.loads(target.read_text(encoding="utf-8"))
        record["headers"][name] = "planted"
        target.write_text(json.dumps(record), encoding="utf-8")
        found = violations(tmp)
        assert any(name in line for line in found), (name, found)


def test_a_planted_sentinel_fails_red() -> None:
    tmp = _fresh_copy()
    target = next(tmp.rglob("auth_error.json"))
    record = json.loads(target.read_text(encoding="utf-8"))
    record["body"] = f"invalid key {SENTINEL}"
    target.write_text(json.dumps(record), encoding="utf-8")
    assert any("sentinel" in line for line in violations(tmp))


def test_committed_set_equals_the_scrub_of_the_raw_recordings() -> None:
    """Only when the gitignored raw recordings exist locally."""
    sources = [s for s in SOURCES if s.is_dir()]
    if not sources:
        print("  (raw recordings absent; reproducibility check skipped)")
        return
    tmp = Path(tempfile.mkdtemp(prefix="mitya-scrub-"))
    copy_fixtures(sources, tmp)
    for path in tmp.rglob("*.json"):
        committed = DEST / path.relative_to(tmp)
        assert committed.exists(), f"{committed} not committed; re-run the scrub script"
        assert committed.read_text(encoding="utf-8") == path.read_text(encoding="utf-8"), committed


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
