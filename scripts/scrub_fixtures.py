"""Copy recorded provider fixtures into test/fixtures/providers/ with response headers cut to an allowlist.

Raw recordings stay in gitignored plans/; only the scrubbed copy is committed (OI46, schedule-p0 V rulings
2026-10-20). Run from the repo root: `python scripts/scrub_fixtures.py`.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCES = (
    REPO / "plans/mitya-v3/04-plan/phase--1/fixtures",
    REPO / "plans/mitya-v3/04-plan/phase-0/fixtures",
)
DEST = REPO / "test/fixtures/providers"

# Only what replay reads: the content type and the rate-limit family. Everything else
# (set-cookie, x-litellm-*, cf-*, server-timing, request and generation ids) is dropped.
ALLOWED_HEADERS = frozenset({"content-type", "retry-after"})
ALLOWED_PREFIXES = ("x-ratelimit-",)
SENTINEL = "sk-SENTINEL-mitya-p-1-0f3c9a"

logger = logging.getLogger(__name__)


def header_allowed(name: str) -> bool:
    lowered = name.lower()
    return lowered in ALLOWED_HEADERS or lowered.startswith(ALLOWED_PREFIXES)


def scrub_record(record: dict) -> dict:
    out = dict(record)
    if "headers" in out:
        out["headers"] = {k.lower(): v for k, v in out["headers"].items() if header_allowed(k)}
    return out


def violations(root: Path) -> list[str]:
    """Every sentinel hit and every non-allowlisted header under `root`; names positions, never values."""
    found: list[str] = []
    for path in sorted(root.rglob("*.json")):
        text = path.read_text(encoding="utf-8")
        if SENTINEL in text:
            found.append(f"{path}: sentinel")
        headers = json.loads(text).get("headers", {})
        found.extend(f"{path}: header {name}" for name in headers if not header_allowed(name))
    return found


def copy_fixtures(sources: list[Path], dest: Path) -> list[Path]:
    written: list[Path] = []
    for source in sources:
        for path in sorted(source.glob("*/*.json")):
            target = dest / path.parent.name / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            record = scrub_record(json.loads(path.read_text(encoding="utf-8")))
            target.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            written.append(target)
    return written


def main(sources: list[Path], dest: Path) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    existing = [s for s in sources if s.is_dir()]
    if not existing:
        logger.error("no fixture source directory exists: %s", [str(s) for s in sources])
        return 1
    written = copy_fixtures(existing, dest)
    bad = violations(dest)
    for line in bad:
        logger.error("%s", line)
    logger.info("copied %d fixtures to %s; %d violations", len(written), dest, len(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", action="append", type=Path, help="source root, repeatable")
    parser.add_argument("--dest", type=Path, default=DEST)
    args = parser.parse_args()
    sys.exit(main(args.src or list(SOURCES), args.dest))
