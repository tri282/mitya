# scripts/build_summaries.py
"""Phase 3: build data/rag/summaries.json via the teacher LLM.

One call per chapter, keyed by the canonical (book, part, chapter) tuple from
P1's structure.json -- the same key space the chunk payloads use, so the
STRUCTURAL retrieval path in P4 can look a summary up directly.

Voice is neutral and factual on purpose. These summaries are never shown to the
user; Mitya answers in persona, but a persona-voiced summary would drift the
retrieval context and contend for the GPU to produce it (design I note).

Resumable: existing keys are skipped, so a run stopped by quota or Ctrl-C picks
up where it left off. Unlike entities, there is no human review gate here, so
this writes summaries.json directly.

Usage:
    python scripts/build_summaries.py --dry-run      # print the plan, 0 API calls
    python scripts/build_summaries.py --limit 3      # first 3 chapters only
    python scripts/build_summaries.py                # full run (~231 calls, ~20 min)
    python scripts/build_summaries.py --force        # regenerate everything
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config
from src.rag.ingest import Unit, parse_all
from src.rag.teacher import (
    ContentBlocked,
    QuotaExhausted,
    TeacherClient,
    TeacherError,
    estimate_tokens,
)

logger = logging.getLogger(__name__)

# Why each missing chapter is missing. summaries.json records successes only, so
# without this a failure reason lives in stderr and dies with the terminal --
# and the next run rediscovers it by paying for the same calls again.
CHECKPOINT_PATH = os.path.join(Config.RAG_DIR, "summaries.progress.json")

# Consecutive 429s that mean "daily quota gone", not "slow down".
MAX_CONSECUTIVE_429 = 3

PROMPT_TMPL = """You are performing neutral analytical text processing, not roleplay.
Do NOT write in a literary or in-character voice. Write a plain, factual,
neutral summary as if for an internal index, not for a reader of the novel.

Summarize what happens in the chapter below, in roughly 100-150 words,
covering plot events and which characters are involved. Neutral factual
register only.

Return JSON in EXACTLY this shape:

{{"{key}": "your summary text here"}}

CHAPTER TEXT:
---
{text}
---"""

# Used only after PROMPT_TMPL is refused with PROHIBITED_CONTENT (Gemini's
# non-configurable filter, which safety_settings cannot reach).
#
# The refusal is output-side, not input-side: the same chapter text passes
# build_entities.py unchanged. Asking the model to *narrate* what happens is
# what trips it; asking it to *extract* the same facts as catalog entries does
# not. Verified on the two chapters that PROMPT_TMPL refuses -- C&P|6|4 and
# BK|3|4 -- which this template summarizes on the first attempt.
#
# Kept as a fallback rather than promoted to the default: 229/231 chapters
# already have summaries from PROMPT_TMPL, and swapping the default would mean
# re-running all 231 to keep the register consistent.
FALLBACK_PROMPT_TMPL = """You are performing neutral analytical text processing, not roleplay.
Do NOT write in a literary or in-character voice. Do NOT narrate or describe
scenes. This is index-card style fact extraction, like a librarian's catalog
entry.

Read the chapter text below. Extract a plain list of neutral, factual index
entries describing what occurs (events, decisions, statements, arrivals,
departures) and which characters are involved. Write it as a single
paragraph of terse factual clauses, roughly 100-150 words, in the style of a
library abstract or court record, not a story.

Return JSON in EXACTLY this shape:

{{"{key}": "your extracted index text here"}}

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
    """Temp file + atomic replace, so an interrupt cannot truncate the file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def extract_summary(result, key: str) -> str | None:
    """Prefer the exact key; fall back to the sole value if the model rekeyed it.
    Anything else is ambiguous and gets rejected rather than guessed at."""
    # The teacher sometimes wraps the requested object in a single-element array
    # (see iter_entries in build_entities.py -- same quirk, same model).
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        result = result[0]
    if not isinstance(result, dict):
        logger.error("  %s: expected an object, got %s", key, type(result).__name__)
        return None
    value = result.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if len(result) == 1:
        only = next(iter(result.values()))
        if isinstance(only, str) and only.strip():
            logger.warning("  %s: model used key %r; accepting the single value",
                           key, next(iter(result)))
            return only.strip()
    return None


def main(dry_run: bool, limit: int | None, force: bool) -> None:
    units_by_book = parse_all()
    units = [u for book_units in units_by_book.values() for u in book_units]

    summaries = {} if force else load_json(Config.SUMMARIES_PATH, {})
    failures = {} if force else load_json(CHECKPOINT_PATH, {})
    todo = [u for u in units if force or unit_key(u) not in summaries]
    if limit is not None:
        todo = todo[:limit]

    logger.info("chapters total=%d done=%d todo=%d", len(units), len(summaries), len(todo))
    logger.info("model=%s  est calls=%d against daily quota %d",
                Config.TEACHER_MODEL, len(todo), Config.TEACHER_RPD)
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

    def record_failure(key: str, err: Exception) -> None:
        failures[key] = {
            "kind": type(err).__name__,
            "error": str(err),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        save_json(CHECKPOINT_PATH, failures)

    for i, u in enumerate(todo, 1):
        key = unit_key(u)
        logger.info("[%d/%d] %s (%d left in daily budget)",
                    i, len(todo), key, client.limiter.remaining_today())
        try:
            result = client.generate_json(PROMPT_TMPL.format(key=key, text=u.text))
        except QuotaExhausted as e:
            logger.warning("stopping: %s", e)
            break
        except ContentBlocked as e:
            # The narrative prompt was refused. One extractive retry -- a
            # different task shape, not the same call again -- then give up.
            logger.warning("  %s blocked (%s); retrying with the extractive prompt", key, e)
            try:
                result = client.generate_json(FALLBACK_PROMPT_TMPL.format(key=key, text=u.text))
            except QuotaExhausted as e2:
                # Subclasses TeacherError, so without this arm the day running
                # out mid-fallback would be filed as a blocked chapter.
                logger.warning("stopping: %s", e2)
                break
            except TeacherError as e2:
                logger.error("  %s failed on the fallback prompt too: %s", key, e2)
                failed += 1
                record_failure(key, e2)
                consecutive_429 = 0
                continue
            logger.info("  fallback prompt accepted")
            consecutive_429 = 0
        except TeacherError as e:
            logger.error("  %s failed: %s", key, e)
            failed += 1
            record_failure(key, e)
            # A run of 429s means the daily quota is gone (possibly spent by an
            # earlier run), not that we are going too fast. Stop rather than burn
            # the remaining quota failing on every remaining chapter.
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

        summary = extract_summary(result, key)
        if summary is None:
            logger.error("  %s: no usable summary in response keys %s", key, list(result))
            failed += 1
            record_failure(key, TeacherError(f"no usable summary in response keys {list(result)}"))
            continue

        summaries[key] = summary
        processed += 1
        logger.info("  %d words", len(summary.split()))
        save_json(Config.SUMMARIES_PATH, summaries)
        if failures.pop(key, None) is not None:
            save_json(CHECKPOINT_PATH, failures)

    # Coverage against structure.json is the real acceptance check: a missing key
    # falls back to DEFAULT at runtime (design II.5), so gaps degrade rather than
    # break -- but they should be known, not discovered in production.
    missing = [unit_key(u) for u in units if unit_key(u) not in summaries]
    logger.info("done: processed=%d failed=%d", processed, failed)
    logger.info("coverage: %d/%d chapters", len(units) - len(missing), len(units))
    if missing:
        logger.warning("%d chapters without a summary (will fall back to DEFAULT at runtime): %s",
                       len(missing), ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else ""))
        for key in missing[:10]:
            record = failures.get(key)
            logger.warning("  %s: %s", key, record["error"] if record else "never attempted")
    logger.info("wrote %s", Config.SUMMARIES_PATH)
    if failures:
        logger.info("failure detail: %s", CHECKPOINT_PATH)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build summaries.json via the teacher LLM.")
    ap.add_argument("--dry-run", action="store_true", help="Print the plan and exit; makes no API calls.")
    ap.add_argument("--limit", type=int, default=None, help="Process at most N chapters (for a cheap trial).")
    ap.add_argument("--force", action="store_true", help="Regenerate every chapter, ignoring existing output.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    main(args.dry_run, args.limit, args.force)
