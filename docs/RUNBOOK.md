# Runbook — building the RAG artifacts

How to run the offline builds by hand, what to check, and what to do when they
break. Design lives in `docs/RAG.md`; this is the operational side.

Everything here is **offline batch work**. Nothing in this runbook needs the GPU,
and none of it should run while the API server is up (phase 2 takes an exclusive
lock on the vector DB).

---

## Where things stand

| Artifact | Built by | Status |
|---|---|---|
| `data/rag/structure.json` | `python -m src.rag.ingest --parse-only` | done — 231 chapters |
| `data/vector_db/` | `scripts/ingest_corpus.py --rebuild` | done — 3284 points |
| `data/rag/summaries.json` | `scripts/build_summaries.py` | done — 231/231 |
| `data/rag/entities.raw.json` | `scripts/build_entities.py --force` | **stale — rebuild required, see below** |
| `data/rag/entities.responses.jsonl` | `scripts/build_entities.py` | written by the rebuild |
| `data/rag/entities.review.md` | `scripts/review_entities.py` | regenerate after the rebuild |
| `data/rag/entities.json` | you, by hand | **needs review pass** |

> **Why entities must be rebuilt (2026-07-16).** The entity map was keyed by
> reference name alone. Names are not unique across the corpus — Katerina
> Ivanovna is in C&P *and* BK, Nikolay is a C&P painter *and* Stavrogin — so the
> second book's person was merged into the first's entry, unioning two people's
> aliases and keeping the wrong book label. `RAG.md` section 4 now keys by
> `(name, book)` and the schema is a list per name. The old 625-entry file cannot
> be repaired in place: the per-chapter responses were never saved, so the
> chimeras cannot be separated. This rebuild also starts the response log that
> makes the *next* such fix free (`--remerge`).

---

## Before you start

**1. Key.** `.env` needs `GOOGLE_API_KEY=...`. `.env` is gitignored; never paste
the key anywhere else.

**2. Check today's quota.** https://aistudio.google.com/rate-limit

Look at the row for **Gemini 3.1 Flash Lite**. Cells read `usage / limit`. The
entity rebuild needs **231 of the 500/day**. Summaries are already done, so
that is the whole budget — but the dashboard is ground truth, not your memory of
what you ran: check it before starting, not after.

> The dashboard lists models that do not work. Gemini 2.5 Flash shows a healthy
> `0 / 20` and returns `404 ... no longer available to new users` when called.
> `list_models()` lies the same way. An actual call is the only ground truth.

**3. Sanity check, no API calls:**

```bash
source .venv/bin/activate
python scripts/build_entities.py --dry-run
```

Expect `chapters total=231`, `est calls=231`, and `dry run: no API calls made`.

---

## The run

Budget: **231 calls**, ~15-20 min (rate-limited to 15 req/min, so the wall clock
is mostly the limiter sleeping — this is expected, not a hang).

**Never run two builders at once.** Each process has its own `RateLimiter`
believing it owns the full 15 req/min, so two together bill ~30 and trip the
429 circuit breaker. Nothing enforces this but you.

### Step 1 — trial first (3 calls)

```bash
source .venv/bin/activate
python scripts/build_entities.py --dry-run     # 0 calls; expect todo=231
python scripts/build_entities.py --force --limit 3
python -m json.tool data/rag/entities.raw.json | head -30
```

**Check the shape is a list per name** (RAG.md section 4):

```json
{ "Raskolnikov": [ {"canonical": "...", "aliases": ["..."], "book": "Crime and Punishment"} ] }
```

Every top-level key is a person's reference name. No places, no concepts. Names
not mangled.

**If this looks wrong, stop here.** You have spent 3 calls, not 231.

> Do not skip the trial. On 2026-07-16 the full 80-chapter run was launched
> without one and burned 35 calls re-discovering a bug a 3-call trial would have
> shown in ten seconds.

### Step 2 — full rebuild (~228 calls, ~15 min)

`--force` regenerates every chapter against the corrected key space. It moves any
previous response log aside to `.bak` so the log always matches the artifact.

```bash
python scripts/build_entities.py --force
```

**Check the tail:**

- `processed=228 failed=0` — a handful of failures is fine (re-run picks them
  up); `failed=` in the dozens means something systematic, read the error and stop.
- `people: N across M reference names` — **N > M is the point.** M = N means no
  name was shared across books, which would mean the rebuild did not work.
- `N names shared across books (e.g. ...)` — expect Katerina Ivanovna, Nikolay,
  Dmitri, Marya, Lizaveta. This line is the fix, visible.

### Step 3 — regenerate the review aid (0 calls)

```bash
python scripts/review_entities.py
```

Writes `data/rag/entities.review.md`: mention counts from the novel text, merge
candidates, and unusable aliases, per book. This is what makes the gate below
tractable — it is a reading order, not a verdict.

### Step 4 — the human review gate (design II.4)

The builder writes **`entities.raw.json`** only. It never writes `entities.json`,
so re-running cannot clobber your corrections.

```bash
cp data/rag/entities.raw.json data/rag/entities.json
# then edit entities.json by hand, using entities.review.md as the worklist
```

**Work the review aid's sections in order** — they are ordered by damage:

1. **Unusable aliases.** Aliases like `He`, `Him`, `mother`, `Father` match
   ordinary words. Aliases are what P4 matches a name query on (RAG.md section
   4), so one of these drags arbitrary chapters into the results. Drop the alias,
   keep the person. Last run: 27 entities, worst was `Jesus` with `He`/`Him`/
   `Lord` scoring 1292 "mentions" in C&P — every pronoun in the book.
2. **Merge candidates.** Fragments of one person under several keys
   (`Sofya`/`Sonia`, `Katerina` under three). Now book-local: a pair here is
   always two entries in the *same* novel, so a shared name across books is no
   longer offered as a merge. Last run: 229 pairs.
3. **Rare.** <= 2 mentions in a whole novel is usually someone named once in
   passing. Last run: 240.
4. **Roster.** Sorted by mentions; the real cast is at the top.

**Also watch for:**

- **Canonical names come from world knowledge, not the text.** The model writes
  "Sofya Semyonovna Marmeladova" where the chapter only says "Sonia". Correct and
  desirable for canonical keys — but it means the model reaches outside the
  corpus, which is hallucination surface on minor characters. Check the ones you
  do not recognise.
- **Under-extraction.** It missed a real name that other models caught. Absence
  is harder to spot than error, and no report can flag it — check the roster
  against `docs/CHARACTERS.md`, which exists for exactly this.
- **Spellings are normalised away from the text.** Canonicals come out as
  "Ilyusha Snegiryov" and "Elizabetha Prokofievna" where these translations write
  *Ilusha* and *Lizabetha*. Aliases are what P4 matches on, so the text's own
  spelling must survive in `aliases` even when the canonical is normalised.

P4 reads `entities.json`, the reviewed file. Not the raw one.

---

## When it breaks

**`daily quota is exhausted` / repeated 429**
Working as designed — it stops after 3 consecutive 429s instead of burning the
rest of the quota failing. Progress is saved. Re-run after reset; it skips what
is done.

**`fatal API error 404`**
The model id is wrong or retired. Do not retry — it will fail identically
forever. Confirm the id is live:

```bash
python -c "
from google import genai
from src.config import Config
c = genai.Client(api_key=Config.GOOGLE_API_KEY)
print([m.name for m in c.models.list() if 'flash-lite' in m.name])"
```

**Interrupted (Ctrl-C, crash, reboot)**
Re-run the same command. Progress checkpoints per chapter with atomic writes; a
kill mid-write cannot truncate the file.

**Want to redo everything**
`--force`. On entities this ignores the checkpoint and regenerates all 231 —
231 calls. It still writes only `entities.raw.json`, so your reviewed
`entities.json` survives.

**Changed how the merge works and need to rebuild — without paying again**

```bash
python scripts/build_entities.py --remerge
```

Rebuilds `entities.raw.json` and the checkpoint from
`entities.responses.jsonl`, which holds every teacher response verbatim. **Zero
API calls.** This exists because `entities.raw.json` is a merge, and a merge is
lossy: when the `(name, book)` bug was found, the only copies of 231 chapters'
extractions were already fused inside the merged file, so fixing the merge cost
all 231 calls again. It will not cost that twice.

Only covers chapters built *after* the log existed (2026-07-16 onward).

**`ContentBlocked` / `finish_reason=PROHIBITED_CONTENT`**
Gemini's non-configurable filter — it sits above the four `HARM_CATEGORY_*`
settings, and the `BLOCK_NONE` we already pass cannot reach it. Not retryable:
the refusal is deterministic for a given prompt, so it raises immediately rather
than burning three calls to hear the same no. It is *output*-side, not the
chapter text: the same chapters pass entity extraction untouched. `build_summaries.py`
answers it with `FALLBACK_PROMPT_TMPL`, which reframes the task as index-card
fact extraction; that cleared both refused chapters first try.

**A run seems frozen**
It is probably the rate limiter sleeping (up to 60s at a time). `--dry-run` never
sleeps; use it to prove the wiring is fine.

---

## Changing model

All limits are env-overridable — no code change:

```bash
TEACHER_MODEL=models/some-other-model
TEACHER_RPM=15
TEACHER_TPM=250000
TEACHER_RPD=500
```

**Quotas are not derivable from the model id.** Read the new model's row off the
dashboard and set all three. Getting `TEACHER_RPD` wrong just means the server
429s instead of the local guard stopping cleanly — the breaker catches it either
way, but the error is uglier.

Why this model: it is the only free-tier model with a daily quota big enough for
a 231-chapter pass (500/day vs 20 for everything else), and it beat the field on
JSON validity and latency anyway. Full comparison: `test/bakeoff/RESULTS.md`.

---

## Notes for whoever runs this next

**The SDK retries 429s five times by default, and every attempt is billed.**
`google-genai` ships `_RETRY_ATTEMPTS = 5` with `429` in its retriable set. One
rate-limited call becomes five charged requests. `src/rag/teacher.py` sets
`attempts=1` and owns retry itself for exactly this reason. If you write new
code against this API, do the same, or your quota will vanish faster than your
call count explains.

**"No output" is not proof of "no calls."** A run that produced no file may still
have spent quota. Check the dashboard, not the filesystem.

**Two SDKs, one dead.** `google-generativeai` is EOL. Nothing imports it any
more (`src/generation.py` was migrated to `google-genai`), but the requirements
line is still there. Removing it is safe once nothing regresses.

**`ollama` is declared in requirements.txt but is not installed in the venv**, so
`src/generation.py` cannot be imported without it, even on the gemini path
(the import is unconditional at module scope). Install it, or make the import
lazy, before running the fine-tune dataset builder.
