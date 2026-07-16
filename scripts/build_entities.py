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

Every response is also appended verbatim to entities.responses.jsonl, so a merge
bug costs a code fix and --remerge rather than another 231 billed calls.

Usage:
    python scripts/build_entities.py --dry-run       # print the plan, 0 API calls
    python scripts/build_entities.py --limit 3       # first 3 chapters only
    python scripts/build_entities.py                 # full run (~231 calls, ~20 min)
    python scripts/build_entities.py --force         # ignore checkpoint, redo all
    python scripts/build_entities.py --remerge       # rebuild from the log, 0 API calls
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config
from src.rag.ingest import Unit, parse_all
from src.rag.teacher import QuotaExhausted, TeacherClient, TeacherError, estimate_tokens

logger = logging.getLogger(__name__)

CHECKPOINT_PATH = os.path.join(Config.RAG_DIR, "entities.progress.json")

# Every teacher response, exactly as parsed, one JSON object per line.
#
# entities.raw.json is a merge, and a merge is lossy: it cannot be un-done or
# re-done differently. When the (name, book) keying bug was found, the only
# copies of 231 chapters' extractions were already fused inside the merged file,
# so fixing the merge meant paying for all 231 calls again. This log makes the
# next merge fix free -- see --remerge.
RESPONSES_PATH = os.path.join(Config.RAG_DIR, "entities.responses.jsonl")

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


def append_response(key: str, book: str, result) -> None:
    """Append one chapter's parsed response. Line-buffered append, so an
    interrupt costs at most the line being written, never the log."""
    os.makedirs(os.path.dirname(RESPONSES_PATH), exist_ok=True)
    record = {
        "key": key,
        "book": book,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "response": result,
    }
    with open(RESPONSES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def remerge() -> None:
    """Rebuild entities.raw.json from the response log. Zero API calls.

    The point of RESPONSES_PATH: a merge bug becomes a code fix plus this, not
    another 231 billed calls.
    """
    if not os.path.exists(RESPONSES_PATH):
        logger.error("no response log at %s; nothing to re-merge (it only exists "
                     "for chapters built after the log was added)", RESPONSES_PATH)
        return

    entities: dict = {}
    done: set[str] = set()
    lines = 0
    with open(RESPONSES_PATH, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("skipping malformed line %d: %s", lineno, e)
                continue
            merge_entities(entities, record["response"], record["book"], record["key"])
            done.add(record["key"])
            lines += 1

    save_json(Config.ENTITIES_RAW_PATH, entities)
    save_json(CHECKPOINT_PATH, sorted(done))
    logger.info("re-merged %d responses covering %d chapters", lines, len(done))
    logger.info("names=%d people=%d", len(entities), total_people(entities))
    logger.info("wrote %s and %s", Config.ENTITIES_RAW_PATH, CHECKPOINT_PATH)


def valid_entry(name: str, body) -> bool:
    return (
        isinstance(body, dict)
        and isinstance(body.get("canonical"), str)
        and isinstance(body.get("aliases"), list)
        and isinstance(body.get("book"), str)
    )


def iter_entries(result) -> Iterator[tuple[str, Any]]:
    """Yield (name, body) pairs from either shape the teacher returns.

    The prompt asks for one object keyed by person: {"Raskolnikov": {...}}.
    About a third of the corpus instead comes back as a list of those same
    objects, one person each: [{"Raskolnikov": {...}}, {"Lizaveta": {...}}].
    The choice is deterministic per chapter at temperature 0, which is why the
    same 80 chapters failed every run until this flattened them. Nothing is
    lost in the list form -- the person key is intact inside each element.

    Pairs rather than a merged dict so a name repeated across elements reaches
    merge_entities and gets unioned, instead of one entry silently winning.
    """
    if isinstance(result, dict):
        yield from result.items()
    elif isinstance(result, list):
        for element in result:
            if isinstance(element, dict):
                yield from element.items()
            else:
                logger.warning("  dropping non-object list element %r", element)


def total_people(entities: dict) -> int:
    """Distinct people, not distinct names -- one name can cover several."""
    return sum(len(candidates) for candidates in entities.values())


def merge_entities(dest: dict, new, book: str, source_key: str) -> int:
    """Merge one chapter's extraction into the accumulator, keyed (name, book).

    dest maps a reference name to a LIST of the people who go by it, one entry
    per book (RAG.md section 4). Names are not unique across the corpus:
    Katerina Ivanovna is Marmeladov's wife in C&P and Verhovtsev's daughter in
    BK; Nikolay is a painter in C&P and Stavrogin in The Possessed. Keying by
    name alone fused each such pair into one entry carrying the first book's
    label -- a chimera that P4 would then filter on the wrong book.

    Same person seen in two chapters of the same book -> union the aliases, keep
    the longest canonical (usually the most complete form).
    """
    added = 0
    for name, body in iter_entries(new):
        if not valid_entry(name, body):
            logger.warning("  %s: dropping malformed entry %r", source_key, name)
            continue
        aliases = {a for a in body["aliases"] if isinstance(a, str)}
        # Match on book, never on the model's echo of it -- we know the book we
        # sent, so it is authoritative.
        existing = next((c for c in dest.get(name, []) if c["book"] == book), None)
        if existing is None:
            dest.setdefault(name, []).append(
                {"canonical": body["canonical"], "aliases": sorted(aliases), "book": book}
            )
            added += 1
            continue
        if len(body["canonical"]) > len(existing["canonical"]):
            existing["canonical"] = body["canonical"]
        existing["aliases"] = sorted(set(existing["aliases"]) | aliases)
    return added


def main(dry_run: bool, limit: int | None, force: bool) -> None:
    units_by_book = parse_all()
    units = [u for book_units in units_by_book.values() for u in book_units]

    entities = {} if force else load_json(Config.ENTITIES_RAW_PATH, {})
    done = set() if force else set(load_json(CHECKPOINT_PATH, []))

    todo = [u for u in units if unit_key(u) not in done]
    if limit is not None:
        todo = todo[:limit]

    # --force regenerates every chapter, so the old responses describe a build
    # that no longer exists. Keep the log matched to the artifact beside it.
    if force and not dry_run and os.path.exists(RESPONSES_PATH):
        backup = f"{RESPONSES_PATH}.bak"
        os.replace(RESPONSES_PATH, backup)
        logger.info("--force: moved the previous response log to %s", backup)

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

        # Log the response before merging it: the merge is lossy, this is not.
        append_response(key, u.book, result)

        added = merge_entities(entities, result, u.book, key)
        done.add(key)
        processed += 1
        logger.info("  %d extracted (%d new); %d people / %d names so far",
                    len(result), added, total_people(entities), len(entities))

        # Checkpoint every chapter: teacher calls are the expensive thing, never
        # pay for the same one twice.
        save_json(Config.ENTITIES_RAW_PATH, entities)
        save_json(CHECKPOINT_PATH, sorted(done))

    logger.info("done: processed=%d failed=%d skipped=%d", processed, failed, len(done) - processed)
    logger.info("people: %d across %d reference names", total_people(entities), len(entities))
    shared = {n: [c["book"] for c in cs] for n, cs in entities.items() if len(cs) > 1}
    if shared:
        # Precisely the case the old flat schema could not express.
        logger.info("%d names shared across books (e.g. %s)", len(shared),
                    "; ".join(f"{n}: {', '.join(bs)}" for n, bs in list(shared.items())[:3]))
    logger.info("wrote %s (responses logged to %s)", Config.ENTITIES_RAW_PATH, RESPONSES_PATH)
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
    ap.add_argument("--remerge", action="store_true",
                    help="Rebuild entities.raw.json from the logged responses. Makes no API calls.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    if args.remerge:
        remerge()
    else:
        main(args.dry_run, args.limit, args.force)
