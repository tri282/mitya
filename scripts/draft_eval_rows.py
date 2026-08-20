# scripts/draft_eval_rows.py
"""Row drafter for the pre-step-6 labeling pilot (mitya-v2 P0,
plans/mitya-v2/05-impl/phase-0.md step 5's coverage table, plans/mitya-v2/
04-plan/phase-0-eval-power.md step 6).

**Every row this script prints is a CANDIDATE, never a decision.** It never
writes test/retrieval_eval.json, never assigns an `id` (row IDs are immutable
per A9 and must only be assigned when a row is accepted -- never to a row that
might still be rejected), and it makes no network/API/GPU call: it only reads
data/rag/structure.json, data/rag/summaries.json, data/rag/entities.json, and
test/retrieval_eval.json (read-only, to avoid re-drafting already-covered
names/entities) and writes a JSON array of drafts to test/drafts/. A human
verifies every row against the corpus before it is hand-copied into
test/retrieval_eval.json with a real `id`.

Six workloads, each targeting a named gap in the step-5 coverage table:

  cross-book              0 rows currently span/compare >1 book.
  cross-translation       emits PAIRS -- same question, two spellings of the
                           same entity, sharing a target -- because
                           entity_fold_rate has nothing to compute from
                           without a pair (step-5 table). For this workload
                           --n counts PAIRS, not rows (each pair -> 2 rows).
  book-named-no-chapter   book name in the QUERY TEXT ONLY, no "filters"
                           field, no resolvable part/chapter -- the organic
                           R6 gap. A row with "filters" tests a different
                           path (build_hard_filter, already covered by
                           r-0020/r-0021) and is deliberately not emitted here.
  minor-entity            targets entities NOT already mentioned in any
                           existing test/retrieval_eval.json query, so it
                           avoids the handful of major names (Raskolnikov,
                           Myshkin, Ivan, Dmitri, ...) the current set skews
                           toward.
  structural               covers the three novels the current 2 STRUCTURAL
                           rows miss: Crime and Punishment, The Idiot, The
                           Possessed.
  negative-control         more DEFAULT-routing broad/unresolvable-scope rows
                           (R14 pattern), varying book and theme.

Usage:
    python scripts/draft_eval_rows.py --workload structural --n 6
    python scripts/draft_eval_rows.py --workload cross-translation --n 8 --seed 1
    python scripts/draft_eval_rows.py --demo
"""

import argparse
import json
import logging
import os
import random
import re
import sys
from datetime import date

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config
from src.rag.metrics import FAMILIES

logger = logging.getLogger(__name__)

THEME_WORDS = [
    "suffering", "guilt", "redemption", "faith", "murder",
    "poverty", "freedom", "confession", "sacrifice", "madness",
]

NEGATIVE_TEMPLATES = [
    "recap what happens with {theme} in {book}",
    "summarize {book}'s treatment of {theme}",
    "give an overview of {theme} across {book}",
]


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _draft(draft_key: str, source: str, query: str, targets: list[dict], note: str, **extra) -> dict:
    """One candidate row. No `id` -- ids are assigned only on human acceptance."""
    row = {"_draft_key": draft_key, "_source": source, "query": query, "targets": targets, "note": note}
    row.update(extra)
    return row


def _flatten_entities(entities: dict) -> list[dict]:
    """entities.json: name-key -> list of {canonical, aliases, book} variant
    dicts -- the same top-level key can name two different people in two
    different books (e.g. "Adelaida" in Brothers Karamazov vs. The Idiot), so
    flatten to one entry per variant, each carrying every spelling it has."""
    out = []
    for key, variants in entities.items():
        for v in variants:
            names = list(dict.fromkeys([v["canonical"], *v.get("aliases", [])]))
            out.append({"key": key, "canonical": v["canonical"], "names": names, "book": v["book"]})
    return out


def _chapters_mentioning(name: str, book: str, summaries: dict) -> list[str]:
    """Summary keys "Book|part|chapter" whose prose mentions `name`
    (case-insensitive substring), sorted by (part, chapter)."""
    needle = name.lower()
    hits = [k for k, text in summaries.items() if k.startswith(book + "|") and needle in text.lower()]
    hits.sort(key=lambda k: tuple(int(x) for x in k.split("|")[1:]))
    return hits


def _books(summaries: dict) -> list[str]:
    return sorted({k.split("|")[0] for k in summaries})


def _target_from_key(key: str) -> dict:
    book, part, chapter = key.split("|")
    return {"book": book, "part": int(part), "chapter": int(chapter)}


WORKLOAD_FAMILY = {
    "structural": "structural",  # R19: own denominator, hit is near-tautological
    "minor-entity": "primary",
    "cross-book": "primary",
    "cross-translation": "entity_pair",
    "book-named-no-chapter": "book_named_no_chapter",
    "negative-control": "negative_control",
}

# --- per-workload generators --------------------------------------------------
# Every generator takes (rng, n, **data) and returns up to n candidate rows
# (2n for cross-translation, since it drafts pairs). `**_` swallows the data
# kwargs a given generator doesn't need.

def draft_structural(rng: random.Random, n: int, structure: dict, summaries: dict, **_) -> list[dict]:
    missing_books = ["Crime and Punishment", "The Idiot", "The Possessed"]
    rows: list[dict] = []
    i = 0
    tries = 0
    while len(rows) < n and tries < n * 20:
        tries += 1
        book = missing_books[i % len(missing_books)]
        i += 1
        parts = structure[book]["parts"]
        part = rng.choice(list(parts.keys()))
        chapter = rng.choice(parts[part]["chapters"])
        key = f"{book}|{part}|{chapter}"
        if key not in summaries:
            continue
        label = (structure[book].get("part_label") or "part").lower()
        query = f"What happens in {book} {label} {part} chapter {chapter}"
        target = _target_from_key(key)
        rows.append(_draft(
            f"structural-{len(rows) + 1:03d}", f"summaries.json:'{key}'", query, [target],
            "STRUCTURAL-routing candidate (step-5 gap: only Notes from the Underground and "
            "Brothers Karamazov currently covered) -- verify router.route() resolves this "
            "phrasing to STRUCTURAL with this exact scope before accepting",
            expected_route="STRUCTURAL", expected_scope=target,
        ))
    return rows


def draft_cross_translation(rng: random.Random, n: int, entities: dict, summaries: dict, **_) -> list[dict]:
    """--n is the number of PAIRS; each pair yields 2 rows sharing one target."""
    variants = [v for v in _flatten_entities(entities) if len(v["names"]) >= 2]
    rng.shuffle(variants)
    rows: list[dict] = []
    pair_idx = 0
    for v in variants:
        if pair_idx >= n:
            break
        chapters = _chapters_mentioning(v["canonical"], v["book"], summaries)
        if not chapters:
            continue
        target = _target_from_key(rng.choice(chapters[: min(3, len(chapters))]))
        name_a, name_b = v["names"][0], rng.choice(v["names"][1:])
        pair_idx += 1
        tag = f"xlate-{pair_idx:03d}"
        for suffix, name in (("a", name_a), ("b", name_b)):
            rows.append(_draft(
                f"{tag}{suffix}", f"summaries.json:'{target['book']}|{target['part']}|{target['chapter']}'",
                f"What does {name} do in this part of {v['book']}?", [target],
                f"cross-translation pair {tag} ({name_a!r} vs {name_b!r}, entity {v['key']!r} / "
                f"{v['canonical']!r}) -- entity_fold_rate has nothing to compute from until a pair "
                "like this is accepted (step-5 table); verify BOTH spellings resolve via "
                "EntityMap.resolve() to the same canonical entity and target chapter",
                _pair=tag,
            ))
    return rows


def draft_book_named_no_chapter(rng: random.Random, n: int, summaries: dict, **_) -> list[dict]:
    keys = list(summaries.keys())
    rng.shuffle(keys)
    rows: list[dict] = []
    for key in keys:
        if len(rows) >= n:
            break
        target = _target_from_key(key)
        words = re.findall(r"[A-Za-z']+", summaries[key])[:8]
        if not words:
            continue
        phrase = " ".join(words).lower()
        query = f"In {target['book']}, what happens regarding {phrase}"
        rows.append(_draft(
            f"book-named-{len(rows) + 1:03d}", f"summaries.json:'{key}'", query, [target],
            "R6 organic gap: book name in QUERY TEXT ONLY, no 'filters' field, no part/chapter "
            "digits -- _scan_query_for_scope() should currently discard the book mention and "
            "search all 5 novels; verify the query text has no resolvable part/chapter before "
            "accepting, and reword the auto-generated phrase if it reads awkwardly",
        ))
    return rows


def draft_minor_entity(rng: random.Random, n: int, entities: dict, summaries: dict, existing_text: str, **_) -> list[dict]:
    def is_major(v: dict) -> bool:
        return any(name.lower() in existing_text for name in v["names"])

    minor = [v for v in _flatten_entities(entities) if not is_major(v)]
    rng.shuffle(minor)
    rows: list[dict] = []
    for v in minor:
        if len(rows) >= n:
            break
        chapters = _chapters_mentioning(v["canonical"], v["book"], summaries)
        if not chapters:
            continue
        target = _target_from_key(rng.choice(chapters))
        rows.append(_draft(
            f"minor-{len(rows) + 1:03d}", f"summaries.json:'{target['book']}|{target['part']}|{target['chapter']}'",
            f"What role does {v['canonical']} play in {v['book']}?", [target],
            f"minor-entity coverage gap (step-5 table): {v['canonical']!r} does not appear in any "
            "existing test/retrieval_eval.json query -- targets a name away from the handful of "
            "major characters the current set skews toward",
        ))
    return rows


def draft_negative_control(rng: random.Random, n: int, summaries: dict, **_) -> list[dict]:
    books = _books(summaries)
    rows: list[dict] = []
    while len(rows) < n:
        book = rng.choice(books)
        theme = rng.choice(THEME_WORDS)
        query = rng.choice(NEGATIVE_TEMPLATES).format(theme=theme, book=book)
        chapters = _chapters_mentioning(theme, book, summaries) or [k for k in summaries if k.startswith(book + "|")]
        target = _target_from_key(rng.choice(chapters))
        rows.append(_draft(
            f"negctrl-{len(rows) + 1:03d}", f"summaries.json:'{target['book']}|{target['part']}|{target['chapter']}'",
            query, [target],
            "broad thematic phrasing, no resolvable part/chapter -- expect router.route() == "
            "DEFAULT (R14); target is best-effort grounding for the denominator, not a claim "
            "the phrasing resolves to it",
            expected_route="DEFAULT", negative_control=True,
        ))
    return rows


def draft_cross_book(rng: random.Random, n: int, summaries: dict, **_) -> list[dict]:
    books = _books(summaries)
    rows: list[dict] = []
    tries = 0
    while len(rows) < n and tries < n * 30:
        tries += 1
        book_a, book_b = rng.sample(books, 2)
        theme = rng.choice(THEME_WORDS)
        chapters_a = _chapters_mentioning(theme, book_a, summaries)
        chapters_b = _chapters_mentioning(theme, book_b, summaries)
        if not chapters_a or not chapters_b:
            continue
        target_a = _target_from_key(rng.choice(chapters_a))
        target_b = _target_from_key(rng.choice(chapters_b))
        query = f"How does {book_a}'s treatment of {theme} compare to {book_b}'s?"
        rows.append(_draft(
            f"crossbook-{len(rows) + 1:03d}",
            f"summaries.json:'{book_a}|{target_a['part']}|{target_a['chapter']}' + "
            f"'{book_b}|{target_b['part']}|{target_b['chapter']}'",
            query, [target_a, target_b],
            "cross-book coverage gap (step-5 table: 0 rows span/compare >1 book); verify BOTH "
            "target chapters are genuinely relevant, not just a theme-word coincidence -- "
            "theme-keyword matching is a coarse net, not proof of relevance",
        ))
    return rows


GENERATORS = {
    "cross-book": draft_cross_book,
    "cross-translation": draft_cross_translation,
    "book-named-no-chapter": draft_book_named_no_chapter,
    "minor-entity": draft_minor_entity,
    "structural": draft_structural,
    "negative-control": draft_negative_control,
}


def draft_rows(workload: str, n: int, seed: int, structure: dict, summaries: dict,
                entities: dict, existing_text: str) -> list[dict]:
    rng = random.Random(seed)
    rows = GENERATORS[workload](
        rng, n, structure=structure, summaries=summaries, entities=entities, existing_text=existing_text,
    )
    # Stamp the denominator family here rather than in each generator: one
    # choke point means a new workload cannot silently ship unclassified rows.
    family = WORKLOAD_FAMILY[workload]
    for row in rows:
        row["family"] = family
        if family == "entity_pair":
            # The drafter's local `_pair` tag becomes the real, persisted link
            # entity_fold_rate needs -- `_`-prefixed keys are draft scratch and
            # are dropped when a row is accepted into the set.
            row["pair_id"] = row["_pair"]
    return rows


def _existing_query_text() -> str:
    existing = _load_json(os.path.join("test", "retrieval_eval.json"))
    return " ".join(r["query"].lower() for r in existing)


def main(workload: str, n: int, seed: int, out_dir: str) -> None:
    structure = _load_json(Config.STRUCTURE_PATH)
    summaries = _load_json(Config.SUMMARIES_PATH)
    entities = _load_json(Config.ENTITIES_PATH)
    existing_text = _existing_query_text()

    rows = draft_rows(workload, n, seed, structure, summaries, entities, existing_text)
    logger.info("drafted %d candidate rows for workload=%r (seed=%d)", len(rows), workload, seed)

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{workload}-{date.today().isoformat()}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    logger.info(
        "wrote %s -- CANDIDATES ONLY: verify each row against the corpus, then hand-copy "
        "accepted rows into test/retrieval_eval.json with a freshly assigned immutable id",
        out_path,
    )


def _demo() -> None:
    """Smallest runnable check -- drafts a few rows per workload against the
    real local corpus data and asserts the invariants the docstring promises.
    Not a test suite."""
    structure = _load_json(Config.STRUCTURE_PATH)
    summaries = _load_json(Config.SUMMARIES_PATH)
    entities = _load_json(Config.ENTITIES_PATH)
    existing_text = _existing_query_text()

    for workload in GENERATORS:
        rows = draft_rows(workload, 4, 1, structure, summaries, entities, existing_text)
        assert rows, f"{workload} drafted 0 rows against the real corpus"
        for row in rows:
            assert "id" not in row, "drafts must never carry an id -- assigned only on accept"
            assert row["_draft_key"] and row["_source"] and row["targets"]
            assert row["family"] == WORKLOAD_FAMILY[workload]
            assert row["family"] in FAMILIES
            if row["family"] == "entity_pair":
                assert row["pair_id"] == row["_pair"]
            for t in row["targets"]:
                parts = structure[t["book"]]["parts"]
                assert str(t["part"]) in parts and t["chapter"] in parts[str(t["part"])]["chapters"], (
                    f"{workload} drafted an unresolvable target {t}"
                )

    xlate = draft_rows("cross-translation", 3, 2, structure, summaries, entities, existing_text)
    assert len(xlate) % 2 == 0, "cross-translation must emit pairs"
    pairs: dict[str, list[dict]] = {}
    for row in xlate:
        pairs.setdefault(row["_pair"], []).append(row)
    for tag, members in pairs.items():
        assert len(members) == 2, f"pair {tag} did not emit exactly 2 rows"
        assert members[0]["targets"] == members[1]["targets"], f"pair {tag} targets diverged"
        assert members[0]["query"] != members[1]["query"], f"pair {tag} used the same spelling twice"

    bn = draft_rows("book-named-no-chapter", 4, 3, structure, summaries, entities, existing_text)
    for row in bn:
        assert "filters" not in row, "book-named-no-chapter rows must not carry a filters field"
        assert not any(ch.isdigit() for ch in row["query"]), (
            "book-named-no-chapter query leaked a digit (looks resolvable, defeats the point)"
        )

    logger.info("scripts/draft_eval_rows.py self-check passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Draft candidate eval rows for a human to verify (mitya-v2 P0 step 6 pilot)."
    )
    ap.add_argument("--workload", choices=sorted(GENERATORS), help="gap workload to draft for")
    ap.add_argument("--n", type=int, default=10,
                     help="rows to draft (pairs, for cross-translation)")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed, for reproducible drafts")
    ap.add_argument("--out-dir", default=os.path.join("test", "drafts"))
    ap.add_argument("--demo", action="store_true", help="run the self-check instead of drafting")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.demo:
        _demo()
    else:
        if not args.workload:
            ap.error("--workload is required unless --demo")
        main(args.workload, args.n, args.seed, args.out_dir)
