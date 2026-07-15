# scripts/build_entities.py
"""Phase 3: build data/rag/entities.raw.json via the teacher LLM.

One call per structural unit (chapter), NOT per chunk: 231 chapters against
3284 chunks, and the free-tier daily quota is 500 requests. Per-chunk is both
14x the cost and structurally impossible on this tier. Entities are merged per
book afterwards, so chunk-level granularity buys nothing.

Writes entities.raw.json (machine output). Design II.4 requires a human review
pass before the result is trusted; the reviewed copy lives at entities.json and
this script NEVER writes there, so re-running cannot clobber hand corrections.

Resumable: already-processed (book, part, chapter) keys are skipped, so a run
killed by quota exhaustion or Ctrl-C resumes where it stopped.

Usage:
    python scripts/build_entities.py --dry-run       # print the plan, 0 API calls
    python scripts/build_entities.py --limit 3       # first 3 chapters only
    python scripts/build_entities.py                 # full run (~231 calls, ~20 min)
    python scripts/build_entities.py --force         # ignore checkpoint, redo all
"""

import argparse
import json
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config
from src.rag.ingest import Unit, parse_all
from src.rag.teacher import QuotaExhausted, TeacherClient, TeacherError, estimate_tokens

logger = logging.getLogger(__name__)

CHECKPOINT_PATH = os.path.join(Config.RAG_DIR, "entities.progress.json")

# Consecutive 429s that mean "daily quota gone", not "slow down".
MAX_CONSECUTIVE_429 = 3

PROMPT_TMPL = """You are performing neutral analytical text processing, not roleplay.

Read the chapter text below (from the novel "{book}") and extract every PERSON
mentioned by name or clear alias. Do NOT include places, organizations, or
abstract concepts. For each distinct person, merge all name variants/aliases
you find into one entry.

Return JSON in EXACTLY this shape:

{{
  "Raskolnikov": {{"canonical": "Rodion Romanovich Raskolnikov",
                  "aliases": ["Rodya", "Rodka", "Rodion"],
                  "book": "Crime and Punishment"}}
}}

The top-level key is a short reference name for the person. "canonical" is the
fullest form of their name. "aliases" lists other name forms, diminutives and
nicknames used for them in the text (may be an empty list).
"book" must be exactly: "{book}"

CHAPTER TEXT:
---
{text}
---"""


def unit_key(u: Unit) -> str:
    return f"{u.book}|{u.part}|{u.chapter}"


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, obj) -> None:
    """Write via a temp file + atomic replace: a Ctrl-C mid-write must not
    truncate hours of accumulated teacher output."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def valid_entry(name: str, body) -> bool:
    return (
        isinstance(body, dict)
        and isinstance(body.get("canonical"), str)
        and isinstance(body.get("aliases"), list)
        and isinstance(body.get("book"), str)
    )


def merge_entities(dest: dict, new: dict, book: str, source_key: str) -> int:
    """Merge one chapter's extraction into the per-book accumulator.
    Same person seen in two chapters -> union the aliases, keep the longest
    canonical (usually the most complete form)."""
    added = 0
    for name, body in new.items():
        if not valid_entry(name, body):
            logger.warning("  %s: dropping malformed entry %r", source_key, name)
            continue
        body["book"] = book  # never trust the model to echo this correctly
        if name not in dest:
            dest[name] = {"canonical": body["canonical"],
                          "aliases": sorted({a for a in body["aliases"] if isinstance(a, str)}),
                          "book": book}
            added += 1
            continue
        existing = dest[name]
        if len(body["canonical"]) > len(existing["canonical"]):
            existing["canonical"] = body["canonical"]
        merged = set(existing["aliases"]) | {a for a in body["aliases"] if isinstance(a, str)}
        existing["aliases"] = sorted(merged)
    return added


def main(dry_run: bool, limit: int | None, force: bool) -> None:
    units_by_book = parse_all()
    units = [u for book_units in units_by_book.values() for u in book_units]

    entities = {} if force else load_json(Config.ENTITIES_RAW_PATH, {})
    done = set() if force else set(load_json(CHECKPOINT_PATH, []))

    todo = [u for u in units if unit_key(u) not in done]
    if limit is not None:
        todo = todo[:limit]

    est_tokens = sum(estimate_tokens(PROMPT_TMPL.format(book=u.book, text=u.text)) for u in todo)
    logger.info("chapters total=%d done=%d todo=%d", len(units), len(done), len(todo))
    logger.info("model=%s  est input tokens=%d", Config.TEACHER_MODEL, est_tokens)
    logger.info("est calls=%d against daily quota %d", len(todo), Config.TEACHER_RPD)
    if len(todo) > Config.TEACHER_RPD:
        logger.warning(
            "todo (%d) exceeds daily quota (%d): the run will stop at the limit and "
            "resume on the next run", len(todo), Config.TEACHER_RPD,
        )
    if dry_run:
        for u in todo[:10]:
            logger.info("  would process %s (~%d tokens)", unit_key(u), estimate_tokens(u.text))
        if len(todo) > 10:
            logger.info("  ... and %d more", len(todo) - 10)
        logger.info("dry run: no API calls made")
        return

    client = TeacherClient()
    processed = failed = 0
    consecutive_429 = 0

    for i, u in enumerate(todo, 1):
        key = unit_key(u)
        logger.info("[%d/%d] %s (%d left in daily budget)",
                    i, len(todo), key, client.limiter.remaining_today())
        try:
            result = client.generate_json(PROMPT_TMPL.format(book=u.book, text=u.text))
        except QuotaExhausted as e:
            logger.warning("stopping: %s", e)
            break
        except TeacherError as e:
            # One bad chapter must not kill a 20-minute run; record and move on.
            logger.error("  %s failed: %s", key, e)
            failed += 1
            # ...but a run of 429s means the daily quota is gone (spent by an
            # earlier run, which the local counter cannot see). Grinding through
            # the rest of the corpus would burn quota to fail 200 more times.
            if e.status == 429:
                consecutive_429 += 1
                if consecutive_429 >= MAX_CONSECUTIVE_429:
                    logger.error(
                        "%d chapters in a row hit 429: daily quota is exhausted. Stopping. "
                        "Progress is saved -- re-run after the quota resets to continue.",
                        consecutive_429,
                    )
                    break
            else:
                consecutive_429 = 0
            continue
        consecutive_429 = 0

        added = merge_entities(entities, result, u.book, key)
        done.add(key)
        processed += 1
        logger.info("  %d people (%d new); %d unique so far", len(result), added, len(entities))

        # Checkpoint every chapter: teacher calls are the expensive thing, never
        # pay for the same one twice.
        save_json(Config.ENTITIES_RAW_PATH, entities)
        save_json(CHECKPOINT_PATH, sorted(done))

    logger.info("done: processed=%d failed=%d skipped=%d", processed, failed, len(done) - processed)
    logger.info("unique people: %d", len(entities))
    logger.info("wrote %s", Config.ENTITIES_RAW_PATH)
    if failed:
        logger.warning("%d chapters failed; re-run to retry just those", failed)
    logger.info(
        "NEXT: human review pass required (design II.4). Review %s, then save the "
        "corrected copy as %s -- this script never writes there.",
        Config.ENTITIES_RAW_PATH, Config.ENTITIES_PATH,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build entities.raw.json via the teacher LLM.")
    ap.add_argument("--dry-run", action="store_true", help="Print the plan and exit; makes no API calls.")
    ap.add_argument("--limit", type=int, default=None, help="Process at most N chapters (for a cheap trial).")
    ap.add_argument("--force", action="store_true", help="Ignore the checkpoint and regenerate everything.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    main(args.dry_run, args.limit, args.force)
