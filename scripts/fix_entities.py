# scripts/fix_entities.py
"""One-off curation pass over data/rag/entities.json (phase 3, step 4).

Follows the 2026-07-17 precedent: explicit operation dicts, a warning on every
op whose target has moved, backup before writing, dry-run by default. Hand-
editing a 255-key JSON file is neither auditable nor reversible; this is.

Two kinds of change:

  1. Explicit ops below -- wrong-entity bugs found during the cross-translation
     review (a character filed as an alias of a different character, a Brothers
     Karamazov name attached to a Crime and Punishment entity), plus aliases for
     spellings other translators use.
  2. A computed pass -- drop aliases that src/rag/names.fold() can now derive.
     These were hand-added as a stopgap before names.py existed. Rather than
     hardcode the list, the script recomputes it, so it stays correct if the
     ruleset changes.

Run:  python -m scripts.fix_entities            # dry run, prints the diff
      python -m scripts.fix_entities --apply    # writes, after backing up
"""

import argparse
import json
import logging
import shutil
from pathlib import Path

from src.config import Config
from src.rag.names import fold

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# entities.json.bak is the PRE-CURATION state from 2026-07-17. Never overwrite
# it -- it is the only copy of what the file looked like before the manual gate.
BACKUP_SUFFIX = ".pre-names.bak"


# --- Split a character out of another character's alias list ------------------
# Colia Ivolgin is Gania's younger brother, filed as three aliases of Gania
# himself. Garnett writes "Colia" (237x) and "Nicolai Ardalionovitch" (8x);
# "Kolya" (P&V) needs no alias -- fold() derives it from Colia (c->k, ya->ia).
SPLITS = [
    {
        "from_key": "GavrilaArdalionovitch",
        "from_book": "The Idiot",
        "move_aliases": ["Colia", "Nicolai Ardalionovitch", "Nicholai Ardalionovitch"],
        "new_key": "Colia",
        "new_canonical": "Nicolai Ardalionovitch Ivolgin",
        "new_book": "The Idiot",
    },
]

# --- Aliases that belong to a different person entirely -----------------------
REMOVE_ALIASES = [
    # The holy fool of The Possessed is not the Turgenev caricature.
    {"key": "Karmazinov", "book": "The Possessed", "alias": "Semyon Yakovlevitch",
     "why": "distinct character; standalone entity SemyonYakovlevitch already exists"},
    # Sofya Ivanovna is a Brothers Karamazov name on a Crime and Punishment entity.
    {"key": "SofyaSemyonovna", "book": "Crime and Punishment", "alias": "Sofya Ivanovna",
     "why": "cross-book contamination; BK name on a C&P person"},
]

# --- Spellings other translators use, that no rule can derive -----------------
ADD_ALIASES = [
    # P&V readers call him Ippolit. Today that query lands on the BK prosecutor
    # Ippolit Kirillovitch; with a book filter on The Idiot it is a hard miss.
    {"key": "Hippolyte", "book": "The Idiot",
     "aliases": ["Ippolit", "Ippolit Terentyev"]},
    # Garnett's text: Lizabetha 185x, Elizabetha 6x. Both occur, so both match.
    {"key": "LizabethaProkofievna", "book": "The Idiot",
     "aliases": ["Lizaveta Prokofyevna", "Elizabetha"]},
    # Identity certain, P&V spelling UNVERIFIED against a text -- see commit note.
    {"key": "Bachmatoff", "book": "The Idiot", "aliases": ["Bakhmutov"]},
    {"key": "Bielokonski", "book": "The Idiot", "aliases": ["Belokonskaya"]},
    # Bare surname, unambiguous: one Syetotchkin in the corpus (Notes, 2x).
    {"key": "AntonAntonitch", "book": "Notes from the Underground",
     "aliases": ["Syetotchkin"]},
]

# --- Reported, not applied ----------------------------------------------------
# A bare surname shared by several characters is genuinely ambiguous. Attaching
# it to one of them invents a confidence the data does not have; attaching it to
# all of them reproduces the current miss through the documented "several
# candidates -> skip expansion" path (RAG.md section 4). Neither is an
# improvement, so this is surfaced for a human instead of guessed at.
AMBIGUOUS_SURNAMES = ["Epanchin", "Hohlakov"]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_record(data: dict, key: str, book: str) -> dict | None:
    for record in data.get(key, []):
        if record.get("book") == book:
            return record
    return None


def apply_splits(data: dict) -> int:
    changed = 0
    for op in SPLITS:
        source = find_record(data, op["from_key"], op["from_book"])
        if source is None:
            logger.warning("SPLIT skipped: no %r in %r", op["from_key"], op["from_book"])
            continue

        moved = [a for a in op["move_aliases"] if a in source["aliases"]]
        missing = set(op["move_aliases"]) - set(moved)
        if missing:
            logger.warning("SPLIT %r: aliases already gone: %s", op["new_key"], sorted(missing))
        if not moved:
            continue

        source["aliases"] = [a for a in source["aliases"] if a not in moved]

        if find_record(data, op["new_key"], op["new_book"]) is not None:
            logger.warning("SPLIT %r: target already exists, merging aliases", op["new_key"])
            target = find_record(data, op["new_key"], op["new_book"])
            target["aliases"] = sorted(set(target["aliases"]) | set(moved))
        else:
            data.setdefault(op["new_key"], []).append({
                "canonical": op["new_canonical"],
                "aliases": sorted(moved),
                "book": op["new_book"],
            })
        logger.info("SPLIT %s <- %s (off %s)", op["new_canonical"], sorted(moved), op["from_key"])
        changed += 1
    return changed


def apply_removals(data: dict) -> int:
    changed = 0
    for op in REMOVE_ALIASES:
        record = find_record(data, op["key"], op["book"])
        if record is None:
            logger.warning("REMOVE skipped: no %r in %r", op["key"], op["book"])
            continue
        if op["alias"] not in record["aliases"]:
            logger.warning("REMOVE skipped: %r not on %r", op["alias"], op["key"])
            continue
        record["aliases"].remove(op["alias"])
        logger.info("REMOVE %r from %s -- %s", op["alias"], record["canonical"], op["why"])
        changed += 1
    return changed


def apply_additions(data: dict) -> int:
    changed = 0
    for op in ADD_ALIASES:
        record = find_record(data, op["key"], op["book"])
        if record is None:
            logger.warning("ADD skipped: no %r in %r", op["key"], op["book"])
            continue
        new = [a for a in op["aliases"] if a not in record["aliases"]]
        if not new:
            continue
        record["aliases"] = sorted(set(record["aliases"]) | set(new))
        logger.info("ADD %s -> %s", new, record["canonical"])
        changed += len(new)
    return changed


def prune_derivable(data: dict) -> int:
    """Drop aliases fold() can now derive from the canonical or a kept alias."""
    pruned = 0
    for key, records in data.items():
        for record in records:
            seen = {fold(record["canonical"])}
            kept: list[str] = []
            for alias in record["aliases"]:
                folded = fold(alias)
                if folded in seen:
                    logger.info("PRUNE %r from %s (fold-derivable)", alias, record["canonical"])
                    pruned += 1
                    continue
                seen.add(folded)
                kept.append(alias)
            record["aliases"] = kept
    return pruned


def report_ambiguous(data: dict) -> None:
    for surname in AMBIGUOUS_SURNAMES:
        owners = [
            (record["canonical"], record["book"])
            for records in data.values()
            for record in records
            if surname.casefold() in record["canonical"].casefold()
        ]
        if len(owners) > 1:
            logger.warning(
                "AMBIGUOUS bare surname %r matches %d entities -- not added, decide by hand:\n    %s",
                surname, len(owners), "\n    ".join(f"{c} ({b})" for c, b in owners),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    path = Path(Config.ENTITIES_PATH)
    data = load(path)
    before_keys = len(data)
    before_aliases = sum(len(r["aliases"]) for recs in data.values() for r in recs)

    changed = apply_splits(data) + apply_removals(data) + apply_additions(data)
    pruned = prune_derivable(data)
    report_ambiguous(data)

    after_keys = len(data)
    after_aliases = sum(len(r["aliases"]) for recs in data.values() for r in recs)
    logger.info(
        "keys %d -> %d | aliases %d -> %d | %d explicit ops, %d pruned",
        before_keys, after_keys, before_aliases, after_aliases, changed, pruned,
    )

    if not args.apply:
        logger.info("DRY RUN -- nothing written. Re-run with --apply.")
        return

    backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    shutil.copy2(path, backup)
    logger.info("backup -> %s", backup)
    path.write_text(
        json.dumps(dict(sorted(data.items())), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("wrote %s", path)


if __name__ == "__main__":
    main()
