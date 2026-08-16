# test/test_entities.py
"""Phase 4 verify: EntityMap.resolve()'s active_book alias resolution (D3 fix).

Plain-assert script, not pytest (pytest is not installed in .venv -- see
plans/mitya-rag/05-impl/phase-4.md). Builds a small entities.json fixture on
disk (does not touch data/rag/entities.json, which is the curated P3
artifact) so this only exercises EntityMap's own logic.

Usage:
    python test/test_entities.py
"""

import json
import logging
import os
import sys
import tempfile

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.rag.entities import EntityMap

logger = logging.getLogger(__name__)

# Minimal fixture: one record whose book is a canonical title ("The
# Possessed") as entities.json records always carry (see review finding:
# every entities.json `book` value is one of the five canonical titles).
FIXTURE = {
    "NikolayVsyevolodovitch": [
        {
            "canonical": "Stavrogin",
            "aliases": ["Nikolay Vsyevolodovitch", "Nicolas"],
            "book": "The Possessed",
        }
    ],
}


def _write_fixture() -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(FIXTURE, f)
    return path


def main() -> None:
    failures = 0
    fixture_path = _write_fixture()

    def check(description: str, fn) -> None:
        nonlocal failures
        try:
            fn()
            logger.info("[PASS] %s", description)
        except AssertionError as exc:
            failures += 1
            logger.error("[FAIL] %s: %s", description, exc)

    entity_map = EntityMap(path=fixture_path)

    def resolves_with_canonical_active_book():
        _, resolved = entity_map.resolve("Stavrogin walked in", active_book="The Possessed")
        assert resolved == ["Stavrogin"], f"resolved={resolved!r}"

    def d3_resolves_with_raw_alias_active_book():
        # D3 regression: P6's obvious call is
        # entity_map.resolve(query, filters.get("book")), and a user-selected
        # filter is a raw title like "Demons", not the canonical "The
        # Possessed" entities.json records actually carry. Pre-fix, the raw
        # exact compare (record["book"] == active_book) never matched, so
        # expansion silently disabled for every book-filtered request.
        _, resolved = entity_map.resolve("Stavrogin walked in", active_book="Demons")
        assert resolved == ["Stavrogin"], f"resolved={resolved!r} (expected expansion via alias)"

    def unmapped_active_book_raises():
        try:
            entity_map.resolve("Stavrogin walked in", active_book="Not A Real Book")
        except ValueError:
            return
        raise AssertionError("expected ValueError for unmapped active_book")

    check("canonical active_book still resolves", resolves_with_canonical_active_book)
    check("D3: raw alias active_book resolves via Config.resolve_book_title()", d3_resolves_with_raw_alias_active_book)
    check("unmapped active_book raises ValueError", unmapped_active_book_raises)

    os.unlink(fixture_path)

    logger.info("=" * 60)
    logger.info("%d passed, %d failed", 3 - failures, failures)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
