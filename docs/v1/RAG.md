# Mitya v1 — The Substance Layer

**What this document is:** the RAG subsystem at feature level. What each stage does and what it
hands to the next. Why any of it was chosen is [`03-corpus-parsing.md`](03-corpus-parsing.md)
through [`07-generation.md`](07-generation.md); every number is in [`EVIDENCE.md`](EVIDENCE.md).

For the whole-system view and the end-to-end request diagram, see [`DESIGN.md`](DESIGN.md).

---

## 1. The corpus and the structural parse

**Input:** clean plaintext of the five novels, Constance Garnett throughout, one file per novel — a
separate ingest from the fine-tune's OCR'd PDFs ([`02-finetune.md`](02-finetune.md)).

**The parser** is a small rule table with **one handler per book**, because the five novels use five
different heading conventions and no shared regex covers them. It emits ordered structural units, one
per chapter, each carrying its `book`, `part` and `chapter`, and hands them to indexing.

**Output:** `data/rag/structure.json`, the `book/part/chapter` tree over 231 chapters
([E5](EVIDENCE.md#E5)). That one file does three jobs — the parser's own tripwire against a silent
miscount, the body of `GET /books` that populates the filter UI, and the key space the chapter
summaries must cover exactly.

---

## 2. The name layer

`src/rag/names.py` sits underneath the entity map and the router, applied **symmetrically** to index
keys and to incoming queries, with the true canonical spelling never overwritten. `normalize()`
casefolds and collapses diacritics, ligatures and separators; `fold()` adds an ordered
transliteration ruleset; `fold_tokens()` matches on token boundaries, never substrings.

The fold makes a Pevear/Volokhonsky reader's `Dunya` reach Garnett's `Dounia`. What no rule reaches
lives in the entity map as a curated alias instead ([`03-corpus-parsing.md`](03-corpus-parsing.md)
§5). **Book titles are a separate map**, and `resolve_book_title()` **raises** on an unmapped title
rather than passing it through.

---

## 3. Offline artifacts

Two artifacts are built once by a teacher LLM, before the service ever runs, and read as dictionary
lookups at request time.

### 3.1 The entity map — `entities.json`

**What it is:** for a name appearing in a query, the canonical spelling plus every alias that name is
known by, keyed **per name per book** so that Katerina Ivanovna in one novel is a different record
from Katerina Ivanovna in another.

**How it was built:** one teacher call per chapter over all 231 chapters, merged per book, then a
**human review gate** that cut 678 extracted people to 256 keys ([E10](EVIDENCE.md#E10)). The machine
artifact and the reviewed artifact are separate files, every teacher response is logged verbatim so a
re-merge costs zero API calls, and `check_entity_folds.py` fails on a same-book fold collision.

**How it is used at request time:** as **query expansion**, not as a filter and not as a score boost.
Canonical names and aliases are appended to the query text before either retrieval leg runs. With a
hard book filter the matching book's record is taken; with no filter and more than one candidate,
expansion is skipped rather than guessed.

### 3.2 Chapter summaries — `summaries.json`

One neutral, third-person summary per chapter, keyed by the same `(book, part, chapter)` triple the
chunk payloads carry, at 231 of 231 chapters ([E12](EVIDENCE.md#E12)). It serves the query family
top-k over chunks structurally cannot — "what happens in chapter 5", whose answer is spread across a
whole chapter ([`05-offline-artifacts.md`](05-offline-artifacts.md)).

---

## 4. Indexing

**Chunking.** 500 tokens with 100 of overlap ([E6](EVIDENCE.md#E6)), windowed with the **embedder's
own tokenizer** rather than a generic one, so a chunk that says it fits the encoder does.
**Chunks never cross a chapter boundary** — a unit is one chapter and the window resets per unit —
because every chunk has to carry one truthful structural label. Point ids are a deterministic counter
over (book order × unit order × window order), so a re-ingest of an unchanged corpus overwrites
rather than duplicates.

**The dense vector.** `nomic-ai/nomic-embed-text-v1.5`, 768 dimensions, cosine, served through
**fastembed as quantized ONNX on CPU** — no torch in the serving process. The model's asymmetric task
prefixes are applied by this code, not by the library: `"search_document: "` at ingest,
`"search_query: "` at query time.

**The sparse vector.** fastembed's `Qdrant/bm25`. The document side emits term-frequency weights
only; the IDF half is computed server-side, which is why the collection declares `Modifier.IDF` on
the sparse vector.

**The collection.** One Qdrant collection, `dostoevsky`, in **local embedded mode** against
`data/vector_db/` — no server process. Two named vectors per point, `dense` and `bm25`, over 3,284
points ([E8](EVIDENCE.md#E8)). Each point's payload — `{book, part, chapter, canonical_entities,
text}` — is the contract with everything downstream: hard filters read the three structural fields,
context assembly reads `text`, and the Sources list is built from those same three fields, so a
citation shown to a user is derived from the object retrieval matched. `canonical_entities` is
populated but not read in the request path.

**Ingest verifies itself:** after upsert it compares Qdrant's `count()` against the chunk count it
logged and warns on a mismatch, and the count is re-confirmed at FastAPI startup.

---

## 5. The query pipeline

### 5.1 The router

Two routes, decided by a **pure function** with no model in it. **STRUCTURAL** requires a
summary-intent keyword **and** a resolvable `(book, part, chapter)` scope — taken from the UI filter
or from a "book title + chapter N" pattern in the query, and validated against both the structure map
and the summary set. **DEFAULT** is everything else. The conjunction is the design: intent without a
resolvable chapter has nothing to look up, and a resolved scope whose summary is missing falls back
to DEFAULT at route time.

### 5.2 Hybrid retrieval

Both legs run as Qdrant `Prefetch` clauses in a **single round trip**, each capped at 50 candidates,
each carrying the same hard filter if the UI supplied one ([E15](EVIDENCE.md#E15)):

| Leg | What it is good at |
|---|---|
| **Dense** (nomic, 768-d, cosine) | Thematic and paraphrased questions, where no term overlaps the text |
| **Sparse** (BM25 with IDF) | Proper names, unusual transliterations, near-quotations of the text |

The hard filter is strict and loud: an unmapped book title raises, and a non-integral part or chapter
value is rejected, because either would silently become a filter matching zero points.

### 5.3 Fusion

Qdrant's **native Reciprocal Rank Fusion**, server-side, in the same call that issued the prefetches,
down to a fused top-25 ([E15](EVIDENCE.md#E15)). Rank fusion, not score blending: cosine and BM25
scores are on incomparable scales, and RRF keeps only the ordering. RRF over a single prefetch list
is order-preserving, so a one-leg run is a valid query rather than a degenerate one.

### 5.4 Rerank

**FlashRank** (`ms-marco-MiniLM-L-12-v2`, ONNX, CPU) reads the query and each passage *together* and
returns the top 3 ([E15](EVIDENCE.md#E15)). It is fed the **raw query**, not the expanded one. It is
the most expensive stage in the retrieval path by a wide margin
([`06-retrieval.md`](06-retrieval.md) §5.1), behind a static kill-switch flag and no runtime governor.

### 5.5 The STRUCTURAL path

STRUCTURAL does not run that funnel. It takes the chapter summary directly, plus **2 dense-only
excerpts hard-filtered to that chapter** — no sparse leg, no fusion, no rerank, because the funnel
exists to find the chapter and the chapter is already found.

### 5.6 Context assembly

`build_context()` is a pure formatter: it retrieves nothing and decides nothing about ranking. It
produces **the passages block**, each passage numbered and labelled with its own
`(book, part, chapter)` in that novel's own part vocabulary (*The Brothers Karamazov* reads "Book 5"
where *Crime and Punishment* reads "Part 5"), and **the Sources list**, deduped on the same triple
with rank order preserved, the summarized chapter first on the STRUCTURAL route.

---

## 6. Prompt composition and the persona/grounding seam

The prompt is where the RAG layer meets the fine-tune, and the two are in tension: the fine-tune was
trained on rows that never contained retrieved context, and its persona instructs it to give no
references to external sources. Injected passages are out-of-distribution for the model that has to
use them ([`07-generation.md`](07-generation.md)).

**How the prompt is composed** (`Config.PROMPT_COMPOSITION`, two implemented modes). **A, the
shipped default:** the system message opens with the **verbatim fine-tuned persona head**,
byte-identical to `src/training.py`'s system prompt, followed by the retrieved passages framed as the
author's *own recollections*; the user turn carries the question only. **B:** the persona head
unmodified, with both the passages and the question moved into the user turn.

**The grounding rule:** *persona depends on the model; grounding does not.* The Sources list is
**never generated** — it is built in code from retrieval metadata and emitted as its own SSE event
after the token stream ends. The model is never asked to cite, so it cannot fabricate a citation.
What that costs is per-sentence attribution, which does not exist.

**Decoding.** A small `httpx` streaming client against llama-server's `/v1/chat/completions`: an
answer-token budget, a temperature floor and `top_p` below the server defaults, and **one
empty-completion retry** that fires only when an attempt yields nothing, so a normal stream is never
buffered and time-to-first-token is unaffected.

---

## 7. The cache

`src/rag/cache.py` is roughly fifteen lines over `diskcache` (SQLite-backed, `data/cache/`). The key
is the normalized query plus the filter dict — exact match only, no TTL, no semantic lookup; the
value is `{answer, sources, footer}`, written only for successful non-empty answers, and anything
stored that is not that dict is treated as a miss and regenerated once.

**A hit short-circuits everything** — no router, no retrieval, no model. It emits the whole answer as
**one** `token` event, then the stored `sources`, then a `meta` with `cache_hit: true` and
`route_name: "CACHE"`, then `done`. The semaphore is acquired *before* the cache is checked, so a hit
is not a fast lane around an in-flight generation.

The cache is the architecture's cache **boundary** — the seam where a real deployment would put
something better ([`12-decisions.md`](12-decisions.md)).

---

## 8. The API and operational surface

Four routes ([`08-api.md`](08-api.md)): `POST /chat` (SSE), `GET /health` (a real dependency check,
200 or 503), `GET /books` (the parse output verbatim), and the `frontend/dist` static mount,
registered **last** so it does not swallow the API routes.

**The SSE contract.** `token` events carry prose only, then one `sources` event, then `meta` last of
all — emitted after the cache write, so its `total_ms` is truthful. The live path ends by stream
close and emits no `done`; the cache path emits `done`. `meta` carries
`{route_name, chunk_ids, ttft_ms, total_ms, filter_relaxed, no_context, cache_hit}` and must never
gate rendering.

**Concurrency.** `asyncio.Semaphore(1)` across retrieval and generation both; concurrent submits
queue rather than fail. Models and artifacts load once at lifespan startup.

**Failure behaviour** — fail fast at startup, degrade at request time, never hang. A missing artifact
or an empty collection is a startup failure; an unreachable or slow llama-server is a 503 or 504
resolved **before** the stream opens; an unmapped title or non-integral filter value is a 400; a hard
filter matching zero chunks retries unfiltered and marks `filter_relaxed`; still zero chunks answers
without context and marks `no_context`; a missing summary falls back to DEFAULT; a mid-stream death
is a terminal `event: error`. The full table is [`08-api.md`](08-api.md) §4.

**Operational rules.** Qdrant local mode holds an **exclusive process lock** on `data/vector_db/`, so
the API server and any ingest or eval script cannot run at the same time; the static mount raises at
startup if `frontend/dist/` is missing, so `npm run build` precedes any uvicorn boot, launched from
the repo root. **Logging** is one structured key-value line per request: query, route, filters,
resolved entities, `chunk_ids` (not chunk text), cache hit, `ttft_ms`, `total_ms`, and the fallback
flags.

---

## 9. Where to read next

The reasoning behind every stage above, in order: [`03-corpus-parsing.md`](03-corpus-parsing.md),
[`04-indexing.md`](04-indexing.md), [`05-offline-artifacts.md`](05-offline-artifacts.md),
[`06-retrieval.md`](06-retrieval.md), [`07-generation.md`](07-generation.md),
[`08-api.md`](08-api.md). What it was measured on: [`10-evaluation.md`](10-evaluation.md) and
[`11-ablation.md`](11-ablation.md).
