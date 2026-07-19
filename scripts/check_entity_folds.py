# scripts/check_entity_folds.py
"""Gate check for the name fold against entities.json (phase 3, step 4).

Two questions, neither answerable by reading the ruleset:

  1. What does fold() MERGE that should have stayed apart? Reported as collision
     buckets. A bucket holding two identities from the same book is a hard
     failure -- book-scoped resolution cannot separate them, so a query for one
     expands with the other's aliases. Cross-book buckets are tolerable: the
     active_book filter separates them when set, and without a filter the
     resolver already skips ambiguous names rather than guessing.

  2. Does a query spelled by a DIFFERENT translator resolve against this
     Garnett-built index? Reported as the probe. Note the probe list and the
     ruleset were built in the same pass, so a high score here is weaker
     evidence than it looks -- the P4 retrieval eval is the honest check.

Baseline for comparison is normalize()-only, so the report attributes collisions
to the transliteration rules rather than to pre-existing name ambiguity in the
corpus (Dostoevsky reuses given names heavily and always did).

Run:  python -m scripts.check_entity_folds
      python -m scripts.check_entity_folds --verbose   # list every merge won
"""

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path

from src.config import Config
from src.rag.names import fold, normalize

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

Identity = tuple[str, str]  # (canonical, book)

# Spellings from translators other than Garnett, plus a few Garnett spellings as
# controls. Extend freely -- this is the regression net for the ruleset.
PROBE = [
    ("Dunya", "Dounia / Avdotya Romanovna"),
    ("Sonya", "Sonia Marmeladova"),
    ("Razumikhin", "Razumihin"),
    ("Porfiry Petrovich", "Porfiry Petrovitch"),
    ("Svidrigailov", "Svidrigailov"),
    ("Luzhin", "Luzhin"),
    ("Myshkin", "Muishkin"),
    ("Nastasya Filippovna", "Nastasia Philipovna"),
    ("Rogozhin", "Rogojin"),
    ("Ganya", "Gania"),
    ("Kolya Ivolgin", "Colia Ivolgin"),
    ("Fyodor Pavlovich", "Fyodor Pavlovitch"),
    ("Smerdyakov", "Smerdyakov"),
    ("Alyosha", "Alyosha"),
    ("Dmitry Karamazov", "Dmitri Karamazov"),
    ("Grushenka", "Grushenka"),
    ("Verkhovensky", "Verhovensky"),
    ("Stavrogin", "Stavrogin"),
    ("Shatov", "Shatov"),
    ("Kirillov", "Kirillov"),
    ("Ippolit", "Hippolyte Terentieff (Idiot) / Ippolit Kirillovitch (BK)"),
    ("Lizaveta Prokofyevna", "Lizabetha Prokofievna Epanchina"),
    ("Zametov", "Zamyotov"),
    ("Ilyusha", "Ilusha"),
    ("Aleksei Fyodorovich", "Alexey Fyodorovitch Karamazov"),
    ("Sonja", "Sonia (German-tradition spelling)"),
]


def surfaces(data: dict) -> list[tuple[str, Identity]]:
    """Every string that should resolve to a given (canonical, book)."""
    out = []
    for key, records in data.items():
        for record in records:
            identity = (record["canonical"], record.get("book", "?"))
            for name in [key, record["canonical"], *record.get("aliases", [])]:
                out.append((name, identity))
    return out


def bucket(pairs, keyfn) -> dict[str, set[Identity]]:
    buckets: dict[str, set[Identity]] = defaultdict(set)
    for name, identity in pairs:
        buckets[keyfn(name)].add(identity)
    return buckets


def collisions(buckets) -> dict[str, set[Identity]]:
    return {k: v for k, v in buckets.items() if len(v) > 1}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="list every spelling merge won")
    args = parser.parse_args()

    data = json.loads(Path(Config.ENTITIES_PATH).read_text(encoding="utf-8"))
    pairs = surfaces(data)

    norm_buckets = bucket(pairs, normalize)
    fold_buckets = bucket(pairs, fold)
    base_collisions = collisions(norm_buckets)
    fold_collisions = collisions(fold_buckets)

    # A fold bucket is the fold's fault only if it pairs identities that were not
    # ALREADY ambiguous under normalize(). Comparing bucket keys would be wrong:
    # the fold rewrites the key, so every bucket would look new.
    already_ambiguous = {
        frozenset((a, b))
        for idents in base_collisions.values()
        for a in idents for b in idents if a != b
    }

    introduced: dict[str, set[Identity]] = {}
    same_book_failures: dict[str, set[Identity]] = {}
    for key, idents in fold_collisions.items():
        ordered = sorted(idents)
        novel = [
            frozenset((a, b))
            for i, a in enumerate(ordered)
            for b in ordered[i + 1:]
            if frozenset((a, b)) not in already_ambiguous
        ]
        if not novel:
            continue
        introduced[key] = idents
        if len({book for _, book in idents}) == 1:
            same_book_failures[key] = idents

    logger.info("entities: %d keys, %d surface strings", len(data), len(pairs))
    logger.info(
        "collision buckets: normalize()=%d  fold()=%d  (fold introduced %d)",
        len(base_collisions), len(fold_collisions), len(introduced),
    )

    if same_book_failures:
        logger.error("\nSAME-BOOK COLLISIONS -- gate failure, fix before shipping:")
        for key, idents in sorted(same_book_failures.items()):
            logger.error("  key=%r", key)
            for canonical, book in sorted(idents):
                logger.error("    - %s (%s)", canonical, book)
    else:
        logger.info("no same-book collisions")

    if introduced:
        logger.info("\ncross-book merges introduced by the fold (tolerable, review anyway):")
        for key, idents in sorted(introduced.items()):
            if key in same_book_failures:
                continue
            logger.info("  %-24s %s", key, " | ".join(f"{c} ({b})" for c, b in sorted(idents)))

    logger.info("\ncross-translation probe:")
    index = fold_buckets
    hits = 0
    for query, meant in PROBE:
        found = index.get(fold(query))
        if found:
            hits += 1
            if args.verbose:
                logger.info("  HIT  %-24s -> %s", query, sorted(found)[0][0])
        else:
            logger.warning("  MISS %-24s (means: %s)", query, meant)
    logger.info("  %d/%d resolved", hits, len(PROBE))

    if same_book_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
