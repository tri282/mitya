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
import subprocess
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
    # Second entity so a query can match two records: the order their alias
    # blocks land in is what R18's defect randomized.
    "PyotrStepanovitch": [
        {
            "canonical": "Pyotr Verhovensky",
            "aliases": ["Pyotr Stepanovitch", "Petrusha"],
            "book": "The Possessed",
        }
    ],
}

# R18 regression. resolve()'s output feeds the dense embedder, so its word
# order is model input, not cosmetics. _scan() used to return a set: with two
# entities matched, the alias blocks swapped under PYTHONHASHSEED, giving the
# same query a different embedding in different processes (measured as recall@5
# flipping 0.720/0.760 across eval runs). Hash randomization is per-process, so
# this can only be checked by running separate processes at different seeds.
_ORDER_PROBE = """
import json, sys
sys.path.insert(0, {repo!r})
import logging; logging.disable(logging.CRITICAL)
from src.rag.entities import EntityMap
q, names = EntityMap(path={fixture!r}).resolve("Stavrogin met Pyotr Stepanovitch")
print(json.dumps([q, names]))
"""


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

    def r18_expansion_order_is_hash_seed_independent():
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        code = _ORDER_PROBE.format(repo=repo, fixture=fixture_path)
        outputs = set()
        for seed in ("0", "1", "12345"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                 text=True, env=env, check=True).stdout.strip()
            outputs.add(out)
        assert len(outputs) == 1, f"expansion varies by hash seed: {outputs}"
        expanded, names = json.loads(outputs.pop())
        # Occurrence order in the query, not set order: Stavrogin first.
        assert names == ["Stavrogin", "Pyotr Verhovensky"], f"names={names!r}"
        assert expanded.index("Nicolas") < expanded.index("Petrusha"), expanded

    def unmapped_active_book_raises():
        try:
            entity_map.resolve("Stavrogin walked in", active_book="Not A Real Book")
        except ValueError:
            return
        raise AssertionError("expected ValueError for unmapped active_book")

    check("canonical active_book still resolves", resolves_with_canonical_active_book)
    check("D3: raw alias active_book resolves via Config.resolve_book_title()", d3_resolves_with_raw_alias_active_book)
    check("unmapped active_book raises ValueError", unmapped_active_book_raises)
    check("R18: expansion order is hash-seed independent", r18_expansion_order_is_hash_seed_independent)

    os.unlink(fixture_path)

    logger.info("=" * 60)
    logger.info("%d passed, %d failed", 4 - failures, failures)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
