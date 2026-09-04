# Mitya RAG — Lean Metadata RAG

Goal: Ground the fine-tuned Dostoevsky model in his 5 novels via metadata-tagged hybrid retrieval, answering in-character with a programmatic Sources list, on a single 6GB GPU.  
*Requirements: Mitya GGUF `q4_k_m` served by `llama-server`; `data/rag/*.txt` (5 clean plaintext novels); Qdrant local mode; all retrieval components on CPU; ≥16GB system RAM.*  

This is the settled v1 design (2026-07-13). It supersedes the previous contents of this file and the `2026-07-12` redesign draft. Rulings referenced below as R1–R24 / RR1–RR9 come from the adversarial review that settled it; the ones that still bind the code are restated inline where they apply.

---

## I. Architecture

### 1. Components

```txt
src/rag/
  ingest.py       # per-book parse -> structural units -> chunk -> embed -> Qdrant upsert -> structure.json
  entities.py     # load entities.json; resolve query terms -> canonical entity + aliases (query expansion)
  router.py       # deterministic heuristic: DEFAULT | STRUCTURAL
  retrieve.py     # hard filter + expanded query + hybrid(dense+BM25) native RRF + FlashRank rerank
  context.py      # assemble prompt context + collect deduped source metadata
  generate.py     # httpx streaming call to llama-server + programmatic Sources
  cache.py        # diskcache, exact-match only
src/api/
  app.py          # FastAPI: POST /chat (SSE), GET /health, GET /books
scripts/
  build_entities.py    # teacher-LLM extract -> merge/dedupe -> entities.json (human review after)
  build_summaries.py   # teacher-LLM, one pass per chapter -> summaries.json
  ingest_corpus.py     # runs ingest over data/rag/*.txt
```

*Note: offline artifact builds (`build_entities.py`, `build_summaries.py`) reuse the existing `src/generation.py` `Generator` (Gemini/Ollama teacher). **Why:** Mitya itself would summarize in persona voice and contend for the GPU; the teacher runs once, offline, in a neutral voice — summaries are internal context, never shown to the user.*

### 2. Data flow

```txt
UI (query, filters, session_id)
        |
        v
   +---------+  exact-match hit
   |  API    |-------------------> cached answer
   +---------+
        | miss                        [Semaphore(1) from here down]
        v
     router
        |-- STRUCTURAL (intent AND resolvable chapter scope)
        |         |
        |         v
        |   summaries.json[chapter]           DEFAULT
        |   + top-2 dense excerpts               |
        |     (filtered to chapter)              v
        |         |                  entity resolve -> expand query
        |         |                              |
        |         |                              v
        |         |            nomic embed ("search_query:" prefix)
        |         |                              |
        |         |                              v
        |         |            Qdrant: must-filter (UI scope)
        |         |            prefetch dense(50) + bm25(50)
        |         |               -> native RRF -> top 25
        |         |                              |
        |         |                              v
        |         |                 FlashRank rerank -> top 3
        |         |                              |
        +---------+------------------------------+
                             v
                     context builder
             (prompt context + deduped source meta)
                             |
                             v
        llama-server  (persona system prompt + context)  [SSE stream]
                             |
                             v
              append programmatic Sources list
                             |
                             v
                    log + cache store
```

---

## II. Ingestion & Offline Artifacts

### 1. Per-book structural parsing

Heading formats differ per book — a small rule table, not one generic regex:

* `crime_and_punishment.txt` — `Part N` + `Chapter N`.
* `the_possessed.txt` — `Part N` + `Chapter N`.
* `the_brothers_karamazov.txt` — `Book N`; chapter numbering **resets inside each book**, so the parser carries `(current_book, chapter)` state. Book count is read from the file at impl, not assumed.
* `the_idiot.txt` — `Part N`; chapter heading scheme unconfirmed, resolve during impl.
* `notes_from_the_underground.txt` — 2 parts, numbered sections as chapters.

**Why** per-book rules over a uniform blind split: `book/part/chapter` payloads are what make hard filters, the STRUCTURAL path, and the Sources list correct — a uniform splitter would produce chunks with no trustworthy structural labels. Bounded one-time work over 5 static files.

Ingest also emits **`data/rag/structure.json`** — the per-book part/chapter tree with unit counts. It is simultaneously: the parser verification artifact, the backing data for `GET /books`, and the key space `summaries.json` must cover.

**Verify:** diff `structure.json` unit counts per book against reference chapter counts (manual spot-check) before embedding anything.

### 2. Chunking & embedding

* **Chunks:** 500 tokens, 100 overlap, counted with the **embedder's tokenizer**; chunks never cross chapter boundaries (produced within structural units). *Note: budget ~1.3x when converting to Mistral tokens — 3 chunks land around 2k prompt tokens, inside the 4096 ctx.*
  * **500/100 is UNTESTED, not validated** (v2 CP-7h, closed 2026-09-03, D-25). The chunking ablation was **deferred past v2 with its price attached**: running it is cheap (~22.5 min of embedding per arm — the arms are iso-token, so they do not get cheaper — ~105 MB of new collections, serialized against every gate run on local-mode Qdrant's single file lock), but a **winner** would re-freeze the corpus a third time and invalidate every number v2 P7 produced, at a floor of ~570 metered draws (≥2 days of free-tier quota) to restore. **No chunking number exists in this project.** Re-entry: the corpus is re-frozen for an unrelated reason, the domain stops being novels, D2 lands, or Qdrant moves to server mode.
  * The **`chunk_overlap = 100`** value is load-bearing beyond ingestion: the merge-only context saving (**−2.19% tokens**, v2 Batch H) *is* overlap dedup (`src/rag/context.py:145`) and is quotable only with that value attached.
* **Dense:** `nomic-embed-text-v1.5`, full 768-dim, via **fastembed** (ONNX, CPU). Prefixes applied explicitly: `search_document:` at ingest, `search_query:` at query time — wrong prefixes silently degrade retrieval.
* **Sparse:** BM25 via fastembed `Qdrant/bm25`, with **`Modifier.IDF`** set on the collection's sparse vector config — fastembed emits TF-side weights only; without the IDF modifier "BM25" degrades to term frequency.

**Why** nomic over bge-small/e5: strong retrieval quality at 137M params, long input cap (8192), asymmetric query/document prefixes, runs well quantized on CPU. **Why** fastembed for both legs over sentence-transformers: it is already required for BM25, serves nomic as quantized ONNX (no torch inference path, no `trust_remote_code`), one library instead of two. Matryoshka truncation is cut — at ~4k vectors the memory savings are meaningless.

### 3. Qdrant collection

* Local mode (`QdrantClient(path=...)`), one point per chunk: dense vector + sparse vector + payload `{book, part, chapter, canonical_entities, text}`.
* Pin `qdrant-client>=1.10` (Query API / native fusion).

**Why** Qdrant local over a server container: zero-ops for a solo static corpus. *Note: local mode holds an exclusive process lock — run `ingest_corpus.py` only while the API is stopped.*

**Run ingest:**  

```bash
source .venv/bin/activate
python scripts/ingest_corpus.py
```

**Verify:** collection point count matches chunk count logged by ingest; a smoke query for "Raskolnikov axe" returns Crime and Punishment chunks.

### 4. Entity map (`entities.json`)

Teacher-LLM extracts `PERSON` mentions + aliases per chapter, merged per `(name, book)`; one human review pass fixes it.

A reference name maps to a **list** of the people who go by it — one entry per book:

```json
{
  "Raskolnikov": [
    {
      "canonical": "Rodion Romanovich Raskolnikov",
      "aliases": ["Rodya", "Rodka", "Rodion"],
      "book": "Crime and Punishment"
    }
  ],
  "Katerina Ivanovna": [
    {
      "canonical": "Katerina Ivanovna Marmeladova",
      "aliases": ["Katerina"],
      "book": "Crime and Punishment"
    },
    {
      "canonical": "Katerina Ivanovna Verkhovtseva",
      "aliases": ["Katya", "Katerina"],
      "book": "The Brothers Karamazov"
    }
  ]
}
```

**Why a list and not `{name: {...}}`** (the shape this section originally specified): reference names are not unique across the corpus, so a bare name cannot identify a person. Katerina Ivanovna is Marmeladov's wife in C&P *and* Verhovtsev's daughter in BK; Nikolay is a painter in C&P and Stavrogin in The Possessed; Dmitri, Marya and Lizaveta collide the same way. The flat shape had no way to hold both, so the builder merged whichever came second into the first — unioning two people's aliases, taking the longer canonical, and keeping the first book's label. That produced entries like `Nikolay` labeled `Crime and Punishment` carrying the canonical `Nikolay Vsyevolodovitch Stavrogin`, who appears 287 times in The Possessed and never in C&P. The list models the ambiguity that is actually in the data instead of silently resolving it wrong.

**Resolution is book-scoped.** A bare name query is genuinely ambiguous and the resolver must see that, rather than be handed one arbitrary winner. With a hard book filter active, take the candidate whose `book` matches. With no filter and more than one candidate, the name does not identify a person: skip expansion rather than guess (expanding with the wrong novel's aliases is worse than not expanding).

**Runtime use — query expansion, not payload boosting.** Resolved entities have their canonical name + aliases appended to the query text before embedding; the BM25 leg then naturally ranks entity-bearing chunks higher and the dense leg gets a richer query. Candidates are filtered to the active hard filter's book first; if none survive, or if none is uniquely identified, expansion is skipped.

**Why** expansion over the originally drafted Qdrant `should`-boost: Qdrant filters are binary — a `should` clause adds no score, and a should-only filter *excludes* non-matching points, which would have silently gutted recall. Expansion achieves the boost through a mechanism that exists, at zero extra queries; the reranker dominates final ordering anyway. `canonical_entities` stays in the payload for eval and future client-side boosting.

**Name normalization is code, not data.** Two folds live in `src/rag/names.py`, applied **symmetrically** to the alias/canonical match index and to the incoming query. Both are **derived match keys only**: the `canonical` field keeps its true spelling (shown in Sources, fed to the LLM) and is never overwritten.

1. `normalize()` — casefold, NFKD diacritic strip, ligature expansion (`Æ`→`Ae`), and separator collapse over whitespace/hyphen/apostrophe **and sentence/quote punctuation** (`? ! ; : " ( ) …`). Handles mechanical variance (`Svidrigaïlov`/`Svidrigailov`, case, spacing) and keeps a query-final name matchable — `What happens to Dunya?` must fold to the same token as the bare `Dunya`, or the trailing mark strands the last token and the whole-token index lookup misses.
2. `fold()` — `normalize()` plus an ordered **transliteration** ruleset, because the corpus is Constance Garnett only while readers know these novels through other translators. A P&V reader types `Dunya`, `Razumikhin`, `Sonya` where Garnett wrote `Dounia`, `Razumihin`, `Sonia`; without the fold the entity never resolves, no expansion happens, and the BM25 leg searches tokens that occur nowhere in the corpus. Rules cover the regular correspondences: щ (`tch`/`stch`/`shch`/`tsch`), `kh`↔`h`, `ph`↔`f`, `zh`↔`j`, `ff$`↔`v`, `ou`↔`u`, ё (`yo`↔`e`), `ya`/`yu`↔`ia`/`iu`, `y`↔`i`, Garnett's French `c` (`Colia`↔`Kolya`), German-tradition `ja`/`ju`/`w`, `x`↔`ks`, and doubled-consonant collapse. Order is load-bearing — several rules manufacture input for later ones.

Consequences:

* `entities.json` stores only **irregular** variants — genuine alternate names, nicknames, and transliterations no rule can derive (`Muishkin`↔`Myshkin`, `Shigalov`↔`Shigalyov`, `Ferdishenko`↔`Ferdyshchenko`). Anything a rule reaches — including `Rogojin`↔`Rogozhin` (`zh`) and `Dmitri`↔`Dmitry` (`y`) — does **not** belong in the file.
* The dense leg embeds **raw** query text (embeddings absorb spelling fuzz on their own); the folds apply to the BM25 / entity-match leg where exact tokens matter.
* Qdrant payload carries a derived normalized field for filter/exact match; the display `canonical` stays verbatim.
* Fold output is not human-readable (`fold("Count") == "kunt"`) — harmless precisely because it is applied to both sides and never surfaces. Expansion appends the verbatim `canonical`, never a fold key.
* Matching runs on **token boundaries**, not substrings: `liza ⊂ lizaveta` otherwise resolves "Lizaveta Prokofyevna" to "Liza".
* The fold merges some genuinely distinct cross-book identities (diminutives like `Katia`/`Kostia`/`Nastia`, `-off`/`-ov` surname pairs). Book-scoped resolution above absorbs these when a filter is active; unfiltered they degrade to extra appended canonicals, which is soft noise. **Same-book** collisions are not absorbed and are a build-time gate failure.

*Interim state:* `entities.json` currently also carries hand-added ASCII folds (e.g. `Svidrigailov` alongside `Svidrigaïlov`) as a pre-P4 stopgap. Once `names.py` lands these are redundant and can be stripped — keep the irregular aliases, drop the mechanical folds. The curation passes are reproducible via `scripts/fix_entities.py`, gated by `scripts/check_entity_folds.py`.

*Out of scope for `entities.json`:* **book titles** need their own alias map (`Demons`/`The Devils` → `The Possessed`). A wrong title becomes a hard filter matching zero points — a silent empty result, worse than a miss.

### 5. Chapter summaries (`summaries.json`)

One teacher-LLM pass per chapter (keys drawn from `structure.json`), pre-generated up front.

**Why** keep the summary path at all: chunk-RAG structurally cannot answer "what happens in chapter N" — the answer is spread across a whole chapter. Pre-generation is bounded one-time work over a static corpus and removes the failure mode entirely for scoped summary queries.

**Verify:** every `(book, part, chapter)` key in `structure.json` has a summary; log any gaps (they fall back to DEFAULT at runtime).

---

## III. Query Pipeline

### 1. Router (`router.py`)

* **STRUCTURAL** only when **both** hold: a summary-intent keyword (`summarize`, `what happens in`, `overview of`, `recap`) **and** a resolvable chapter/part scope — from the UI filter, or a light `book-title + chapter/part N` regex resolved against `structure.json`.
* **DEFAULT** otherwise. Pure function, zero latency.

**Why** deterministic over an ML classifier (DeBERTa/NLI/logreg from the original design): two routes, one of which is only valid with a resolvable chapter key — a keyword+scope test is exact where a classifier would merely be probabilistic, and it costs nothing. **Why** the AND condition: the STRUCTURAL path is a `summaries.json` lookup keyed by chapter — intent without a resolvable chapter has nothing to look up; thematic "summarize X's philosophy" queries are served fine by DEFAULT.

### 2. Retrieval (`retrieve.py`)

* **Hard filter:** UI-supplied `book/part/chapter` becomes a Qdrant `must` filter.
* **Hybrid + fusion:** Query API `prefetch` — dense (nomic, `search_query:` prefix) limit 50 + sparse (BM25) limit 50 — fused server-side with **native RRF** to top 25.
* **Rerank:** FlashRank (`ms-marco-MiniLM-L-12-v2`, ONNX, CPU) 25 → top 3. Always on; `RERANK_ENABLED` config kill-switch, no runtime latency governor.

**Why** RRF over weighted raw-score blending: dense cosine and BM25 scores live on incomparable scales; per-query-type weight tables (original design) are hand-tuned magic numbers, while rank fusion is scale-free and robust. **Why** Qdrant-native RRF over client-side: no hand-rolled merge code, one round trip; the internal k is not tunable, which is fine — per-leg prefetch limits are the actual knobs. **Why** FlashRank over bge-reranker/mxbai: no torch/GPU dependency, ~100MB ONNX, sufficient for 25→3 on a small literary corpus. **Why** no dynamic rerank fallback (drafted earlier): 25 pairs on CPU cost ~0.3–0.5s — inside budget; a mid-flight abort is machinery for a problem that doesn't exist.

### 3. Context builder (`context.py`)

* **DEFAULT:** top-3 reranked chunks.
* **STRUCTURAL:** chapter summary + top-2 dense excerpts hard-filtered to that chapter.
* Emits the prompt-context block and the source list, **deduped on `(book, part, chapter)` preserving rank order** (top-3 chunks often share a chapter). STRUCTURAL lists the summarized chapter first.

### 4. Prompt composition (the persona/grounding seam)

The fine-tune's training rows never contained context in the prompt, and its system prompt forbids referencing external sources — injected passages are out-of-distribution. Composition strategy:

* System prompt starts with the **verbatim fine-tuned persona text** (stay near the training distribution), then appends the passages framed as the author's **own recollections**:

```txt
You are Fyodor Dostoevsky. Answer the user's question in the first person,
incorporating your deep literary, existential, and philosophical tone.
Give no editorial comments and references to external sources.

Recall these passages from your own novels; let their substance inform
your answer, but never mention passages, sources, or quotations:

[1] (The Brothers Karamazov, Book 5, Chapter 4) "...chunk text..."
[2] ...
```

* User turn carries the **question only**. `max_tokens = 400`.

**Why** programmatic Sources over inline citations (original design demanded strict `[Book I, Chapter 2]` citations): the model was fine-tuned to *refuse* external references — inline citation enforcement fights the persona head-on and fails unpredictably; appending the deduped source list in code makes grounding display independent of model cooperation, and the "own recollections" framing aligns with the conditioning instead of fighting it. *Note: residual risk — the model may still ignore or parrot context; gated by the answer eval, with context-in-user-turn as the A/B fallback (see VII).*

### 5. Generation (`generate.py`)

Streams from `llama-server`'s OpenAI-compatible `/v1/chat/completions` via **httpx** (connect timeout 5s, read 120s); after the stream ends, the Sources list is emitted as a final SSE event:

```txt
Sources:
* The Brothers Karamazov — Book 5, Chapter 4
* Crime and Punishment — Part 3, Chapter 2
```

> **REVERSED at v2 CP2 (2026-08-28).** The rationale below argued against a multi-provider
> layer and it no longer holds — not because it was wrong, but because its premise changed.
> `generate.py` is now a **gateway client**: it selects a provider from the versioned
> `Config.SERVING_MODEL`, runs it through the spend guard, and coerces the result back into
> the same `AsyncIterator[str]` + usage dict its callers already read. `/chat` never learns
> which vendor served it. See §IV.8 below and `plans/mitya-v2/research/p2-findings.md` R61.
>
> The original reasoning is kept, not deleted, because the *test* it applied was correct and
> is worth reusing: a provider abstraction earns its keep only against providers that
> genuinely differ. CP-α selected Google + Groq on exactly that basis (3/3 divergence on
> streaming shape, error taxonomy and rate-limit semantics, R53), and the two adapters really
> are different — one is a named-event SSE machine that returns refusals inside HTTP 200, the
> other a flat OpenAI chunk stream. Against one localhost endpoint the objection stood.

**Why** httpx over LiteLLM (drafted, and already in requirements) — *the original v1 rationale,
superseded above*: LiteLLM abstracts 100+ providers to call exactly one localhost endpoint — a
heavy dependency with its own churn; the whole need is a ~30-line streaming client. Remove
`litellm`, add `httpx`.

Note that httpx survives the reversal: the v2 adapters are still ~30-line streaming clients over
httpx (`src/gateway/{google,groq,local}.py`). What changed is that there are now three of them
behind one contract, not that a framework was adopted.

### 6. Cache (`cache.py`)

diskcache (SQLite-backed), **exact-match only**, keyed on `normalize(query) + filters`, no TTL (static corpus), stores only successful non-empty answers, `CACHE_ENABLED` flag.

**Why** diskcache over redis: no external service for a solo local deploy. **Why** exact-match only:
originally an argument from cost (a semantic cache adds an embedding index for a single-user app
whose hit rate is near zero anyway); **at v2 CP2 this became a measurement.** A semantic retrieval
cache keyed on query cosine was probed and **refuted**: `corr(cosine, retrieved-chunk overlap) =
+0.192` over 1204 pairs, and a paraphrase at cosine 0.9586 shared **zero** chunks with its source.
The mechanism is that BM25 and the FlashRank cross-encoder are lexical, so a dense-vector key is
blind to two of the three retrieval signals (R50). The conclusion is unchanged; the grounds are now
empirical, and re-opening it requires a key that captures the lexical signal — which in the limit is
the exact-match key already here. The cache exists as the product's cache boundary, kept at ~15
lines. *Note: hit rate is expected to be low and that is accepted; cached answers also skip response variation — acceptable at exact-match granularity.*

The cached value is `{answer, sources, footer}`, so a cache hit re-emits the `sources` event and grounding stays visible on a repeat question. `cache.get` treats any stored value that doesn't match this dict shape — i.e. a legacy answer-only entry written before this change — as a **cache miss**, re-generating once and self-healing on the next hit; no migration script, no key-namespace bump. See §IV.1 below for the corresponding SSE change.

---

## IV. API & Operations

### 1. Endpoints (`src/api/app.py`)

* `POST /chat` — `{query, filters?, session_id?}`; SSE stream of answer tokens, then a `sources` event, then a `meta` event. ~~`session_id` is log-correlation only in v1 (conversation memory is out of scope).~~ **Amended at v2 CP6 (2026-08-31):** `session_id` now keys the recent-turns tier and the lazy episodic trigger — see §IV.8. It is still optional; a request without one is memory-free and cacheable.
* `GET /health` — llama-server reachability + Qdrant collection presence/count + artifact load status.
* `GET /books` — the `structure.json` tree, feeds the filter UI.
* `POST /auth/login`, `POST /auth/logout`, `GET /auth/session` — **v2 CP5.** `/chat`, `/books` and
  `/auth/session` require an authenticated session; an unauthenticated caller gets `401` with a
  generic body and no SSE frames.

Bind uvicorn and llama-server to `127.0.0.1` by default. Query text appears in local logs only — fine for a single-user deployment.

**Auth and tenant identity (v2 CP5).** Accounts, sessions and login-attempt records live in one
SQLite file (`stdlib sqlite3`, `CREATE TABLE IF NOT EXISTS` at startup, no ORM and no migration
tool — P6's memory tables arrive the same way). Passwords are argon2id via `cryptography`'s OpenSSL
binding, which cost no new dependency. Session tokens are **opaque and server-side, not JWT** —
single server, no federation, and revocation is the property that matters; only the token's hash is
stored. The cookie carries `HttpOnly`, `Secure`, `SameSite=Lax` and is **rotated on login**, which
closes session fixation. Login is rate-limited **per account and per IP**, and the limiter *rejects*
rather than sleeps: a login limiter that sleeps converts password guessing into a
connection-holding denial of service. Unknown-account and wrong-password failures are
indistinguishable by status, body, and timing — the unknown branch verifies against a decoy hash so
the difference is a dict lookup rather than a 40 ms KDF. Registration is **closed**; accounts come
from `scripts/seed_account.py`.

The tenant is resolved **once**, at the session-cookie check, and carried on `request.state` into
P1's existing telemetry dimension — no second round trip per request, and it is a metric label on
four instruments. v1's unauthenticated `X-Tenant-ID` header was **removed, not supplemented**: an
authenticated tenant that a header can override is not authenticated.

**The answer cache is deliberately NOT tenant-keyed** (charter A12(2)). The corpus is shared, so
tenants ask overlapping questions about the same documents; tenant-keying would discard most of the
cost saving that is the cache's whole purpose, and with no per-tenant documents there is nothing to
leak. ~~Tenant enters the key at **CP6**, the moment per-tenant memory reaches the prompt.~~ CP5's
cross-tenant leak test is therefore **vacuous by construction** and is recorded as such rather than
reported as an isolation pass — `test/test_cache_isolation.py` is written to start failing at CP6 if
memory reaches the prompt without the key changing. That conditional is the real isolation test.

> **CORRECTED at v2 CP6 (2026-08-31) — struck above, not deleted, because the rule that replaced it
> is only legible next to the one it replaced.**
>
> **Tenant never enters the key.** Two corrections stack here. First (**G-P12(a)**): the charter row
> reading *"cache keys include tenant and memory state — first enforced CP5"* was wrong about the
> checkpoint, and is corrected to **CP6**; A12(2) and `decisions-p5.md` C9's **retraction** moved it,
> because under a shared corpus there are no per-tenant documents to leak. `src/rag/cache.py:58-60`
> was already built to the corrected rule — the document was the stale half.
>
> Second (**G-P12(b)**, settled on a measurement): putting memory state in the key was measured and
> **abandoned**. A correct per-session or per-turn memory component drops the hit rate from **0.528
> to 0.000**, and the only fingerprint that recovers anything (a lossy 8-bucket one, 0.157) recovers
> it *by colliding differing memory states* — its hit rate **is** its cross-tenant collision rate.
> So a memory-bearing request is **not cached at all**: `/chat` bypasses `AnswerCache` get *and* set
> whenever the context plan carries a memory block. The surviving cached population is memory-free
> and therefore tenant-invariant under A12(2), and the isolation invariant is enforced by the
> **bypass** rather than by the key. **C9 closes on this: the per-tenant cache is never built.**
>
> The tripwire fired as designed and was amended with the decision, not around it:
> `test_the_tripwire_can_actually_fire` and
> `test_a_memory_bearing_request_bypasses_the_cache_entirely` now assert the bypass arm. And because
> "bypassed" and "missed" are different populations, the `/chat` log line and the cache metrics both
> carry `cache_population` (`memory_bearing` | `memory_free`) — without it, counting `cache_hit=false`
> lines silently pools the two and reports a hit rate for a population that was never offered the
> cache.

**Shipped for the UI build (`mitya-ui`):** an `event: meta` SSE event, added for the UI's debug panel, carrying `{route_name, chunk_ids, ttft_ms, total_ms, filter_relaxed, no_context, cache_hit}` — fields the pipeline already computed and previously only logged. Always-on, no request flag (single local user; query text is already in local logs). Placement:
- **Live path:** `token`* → `sources` → **`meta`** (stream close). Emitted *after* `sources` and after `cache.set`, at the point `total_ms`/`ttft_ms` are already computed and logged, so `total_ms` is a truthful end-to-end figure.
- **Cache path:** `token` → **`sources`** (re-emitted from the stored value, see §III.6) → **`meta`** (`cache_hit: true`, `ttft_ms: null`, `route_name: "CACHE"`) → `done`.
The client must treat `meta` as optional and non-terminal for forward compatibility. `meta` is emitted **last** so `total_ms` is a truthful end-to-end figure; the live path has no `done` event by design and the client relies on stream close.

### 2. Concurrency & timeouts

* **Retrieval** is serialized by `asyncio.Semaphore(1)` — the embedder is not re-entrant. v2 CP4
  narrowed this lock to release **before** generation (previously it covered both), which moved the
  concurrency knee from 1 to 4 and raised throughput 1.74x.
* **Admission control** (v2 CP5, `src.limits.TenantConcurrency`): bounded in-flight requests, per
  tenant (`CHAT_MAX_INFLIGHT_PER_TENANT`, default 2) and in total (`CHAT_MAX_INFLIGHT_TOTAL`,
  default 6), held for the whole call including the SSE body. It **rejects rather than queues** — a
  limiter that sleeps until capacity frees is how an unbounded queue is spelled. Over capacity is
  `429` with the reason attributed (`tenant`, `total`, `fairness`).
* Per-request budget enforced; `503` when llama-server is unreachable, `504` on timeout — errors surface to the client as structured JSON, never a hung stream.

**What the bound actually bought, measured (CP5, R117-R119).** Before it, the system never rejected
anything at any concurrency: it queued silently to a 62 s p99 with a 100% success rate. After it,
the same load at the same level rejects in ~4 s. But admitted requests still showed p95 of
**12-30 s** against a 2.1 s idle baseline, and system throughput sat near **0.25 rps** — the knee,
not the gate's capacity. `CHAT_MAX_INFLIGHT_TOTAL=6` admits more than the pipeline can serve while
retrieval remains mutually exclusive, so most admitted requests queue *inside* the box. **The bound
caps how many wait, not how long any one waits.** Sizing the gate to the measured knee is open work.

**Per-tenant fairness is not settled** (gap G-P10). Two mechanisms were built and one was retired:
a token-pressure fair-share guard in the gateway that **never fired** — it needed 80% of the
provider's TPM window, and admission control caps in-flight work at roughly 3% of that, so the load
that would raise pressure is precisely what admission refuses — and a least-served rule in the
admission gate, which does fire but whose effect could not be measured (a 90 s window serves ~25
requests across 5 tenants, and a share ratio computed from single-digit counts is noise). The
retired guard passed nine tests while being unreachable in production; that is a worse state than
absent, because green tests asserted it worked.

### 3. Failure modes

* **Qdrant collection missing/empty:** fail fast at startup, log points at `scripts/ingest_corpus.py`.
* **Zero chunks under a hard filter:** retry once without the filter (log `filter_relaxed=true`); still zero → answer without context, omit Sources, log `no_context=true`.
* **Missing chapter summary:** STRUCTURAL falls back to DEFAULT (logged).
* **Models load once** at FastAPI lifespan startup (fastembed, FlashRank, entities, summaries, structure) — never lazily per query.

#### The degradation ladder (v2 CP4, P4 Batch A0 + C-run)

Charter §V's five rungs, as **wired and walked** rather than as written. `next_rung()`
(`src/gateway/ladder.py`) is consulted per request by `/chat` and the served rung is reported in the
`meta` SSE event and as an `X-Mitya-Rung` header on error exits, so a client can see which rung
answered it without inferring.

| rung | entered when | walked? |
|---|---|---|
| `primary_model` | generation configured and credentialed | **yes** |
| `cheaper_model` | — | **unreachable by construction** |
| `cached_answer` | generation unavailable **and** the query has a cached answer | **yes** |
| `retrieval_only` | generation unavailable, retrieval up, no cached answer | **yes** |
| `503` / `UNAVAILABLE` | retrieval down too | not walked |

**Coverage: 3/5.** That fraction never appears without this caption:

> `cheaper_model` is unreachable by construction: `Readiness` folds primary and cheaper into one
> `generation_available` flag (`src/gateway/ladder.py`), and routing was closed as not-adopted
> (R69), so there is no second generation arm to fall to. Maximum attainable coverage is 4/5.

`content_refused` is wired (`app.py` pre-stream and mid-stream) and is an outcome of one attempt,
**not a rung** — `next_rung()` never returns it, and it is reported outside the fraction.

Two properties of the ladder are counter-intuitive and were both measured, not assumed:

* **A cache hit does not enter `cached_answer`.** `next_rung()` returns `primary_model` whenever
  generation is available, *regardless of `cache_hit`* — so on a healthy deployment a cache hit is a
  primary-rung cache hit. The `cached_answer` rung requires generation to be **unavailable** as well.
* **`generation_available` means "configured and credentialed", not "the provider is reachable"**
  (P4 Batch A0). `credential_status()` is a config check, deliberately not a live probe: `/health`
  is polled by the SPA and the serving model's `rpm` is small, so probing would spend the quota the
  deployment exists to serve with. A provider **outage** therefore does not descend the ladder — it
  surfaces as a 502 through `TransportError`. In practice the ladder descends on **misconfiguration**.

**`retrieval_only` is an internal rung, not a product state (CP-4c).** Entered and observed, it
serves an **HTTP 200 with an empty answer body and the retrieved `chunk_ids`** — retrieval succeeds,
no prose is generated, and a client rendering only the answer text shows a blank success. Because a
blank 200 is less legible than a failure, the decision is that ordinary callers get a **503** and the
rung stays reachable for operators. That change is owed, not yet built; the SPA render was never
observed, and if it is and the passages render legibly the decision is revisitable.

#### Provider-fault taxonomy (v2 CP-4b, P4 step 7)

Faults are injected **in the gateway** as a `FaultProvider` wrapping the vendor adapter below
`guarded_generate()`, behind `Config.FAULT_INJECT` — a **deployment** field, off by default, never in
the serving artifact's hash. Injecting at the client edge cannot reach in-band provider refusals;
using a provider's own error behaviour would make the taxonomy a statement about one vendor rather
than about the system, which is what the gateway exists to prevent.

An injected fault is **faithful** iff the client observes the same status code, the same class
(in-band provider error vs transport failure) and the same `Retry-After` presence as a genuine
occurrence. Where no genuine occurrence is on record, the row is reported **fidelity unverified** and
is excluded from the numerator **and** denominator both.

**Of 8 injected rows, 1 has a genuine client signature; the other 7 are fidelity unverified.** The
taxonomy is published as this table, never as a percentage — a `1/1` is not a rate.

| class | injection point | faithful? | client observes |
|---|---|---|---|
| **429, absorbed** (n < `MAX_GENERATION_ATTEMPTS`) | gateway | **yes** (242 real occurrences) | **200**, `ttft_ms` inflated by the honoured wait |
| 429, exhausting | gateway | unverified | 502 |
| provider timeout | gateway | unverified | 502, identical to above |
| in-band refusal | gateway | unverified | 422 |
| budget exhaustion, app-side | spend-guard config | unverified | 503, no provider call |
| budget exhaustion, provider-side | — | subsumed | wears the 429 signature exactly |
| cold cache | corpus repeat rate | n/a — **a miss, not a fault** | 200, elevated TTFT |

**A ceiling no injector design can raise:** five distinct causes — a 429, a connect failure, a read
timeout, a 5xx, and an in-band provider `error` frame — all reach the client as the same opaque
**502 `"provider unreachable"`**. So the honest claim after a fault run is *"injected X, the client
saw the 502 X shares with four other causes"*, never *"the X-specific response"*. **503 is
overloaded too** (`UNAVAILABLE`, the spend guard, a stale readiness read), and the exception paths
carry no `meta` event, so there is no served-rung field to disambiguate with.

**Measured, and the reason the one verified row matters:** an injected `429:1` carrying
`retry_after=2.0` produced a 200 with TTFT **+2449 ms and +2416 ms** over the un-faulted baseline,
inside the 2000-2500 ms jitter band `src/rag/generate.py` computes. That is the first end-to-end
evidence in this project that a provider's own `Retry-After` is honoured on the serving path.

### 4. Logging

Stdlib `logging` (no `print`), structured key-value per request: `query`, route, filters, resolved entities, `chunk_ids` (not chunk text), cache hit, `ttft_ms`, `total_ms`, fallback flags.

### 5. Config (`src/config.py` additions)

Extend the existing `Config` class (typed values), plus `Config.validate_rag()` called from the lifespan — fails fast on missing paths/artifacts via `logging`, not `print`.

* Paths: `RAG_DIR`, `VECTOR_DB_DIR`, `ENTITIES_PATH`, `SUMMARIES_PATH`, `STRUCTURE_PATH`.
* Retrieval: `EMBED_MODEL`, `RERANK_MODEL`, `PREFETCH_LIMIT=50`, `TOP_K_FUSED=25`, `TOP_K_RERANK=3`, `RERANK_ENABLED=true`.
* Serving: `LLAMA_SERVER_URL=http://127.0.0.1:8080`, `CTX_SIZE=3072`, `MAX_TOKENS=400`, `CACHE_ENABLED=true`.

**Why** extending the plain `Config` over pydantic-settings: matches the existing idiom, a dozen fields don't earn a new dependency; validation at startup covers the real failure (missing artifacts).

> **REVERSED at v2 CP1 step 5 (2026-08-27) — kept, not deleted, because the reason it expired is the useful part.** The dependency argument is *void*, not merely outweighed: pydantic 2.13.4 ships as a FastAPI dependency and is already an idiom here (`src/api/app.py`'s request models), so the "new dependency" the ruling weighed never existed. The ~18 knobs that decide what a correct answer *is* now live in `src/config_artifact.py` as a frozen pydantic model, content-addressed by `sha256(canonical_json)[:12]`; `Config` keeps the deployment half and serves the rest through a metaclass facade, so no call site changed. `pydantic-settings` is still **not** added — it solves env-loading, which is the half being kept as-is. What forced the change was not elegance but P7: its search space *is* a set of config versions, so config has to be a value you can instantiate N of rather than a process-lifetime singleton built from env at import. See `docs/RAG.md` §IV.7 below and `plans/mitya-v2/research/p1-findings.md` R43.

### 7. Telemetry, config identity, and the operator plane (v2 CP1)

Added at v2 CP1, and described here because §IV is where the operational
contract lives. Three planes over one instrumentation, not three
instrumentations:

* **Traces** — an OpenTelemetry span tree per request: a FastAPI root span plus
  eight stage children (`route`, `cache.check`, `entities.resolve`, `retrieve`,
  `rerank`, `context.build`, `generate.first_token`, `generate.stream`). The
  root carries the slice keys — `tenant`, `route`, `config_version`,
  `corpus_version` — and the stage spans carry their own detail plus a cost
  stamp. Export is opt-in: with no `OTEL_EXPORTER_OTLP_ENDPOINT` the SDK builds
  spans and drops them, so a dev run needs no docker. `OTEL_ENABLED=false` is
  the stronger switch — no provider, no instrumentation at all.
* **Metrics** — the same instrumentation read by a second `MeterProvider` with a
  Prometheus reader, served at `/metrics`. RED plus spend. **`tenant` is
  deliberately not a metric label**: the header is unauthenticated until CP5 and
  validated only for shape, so it bounds the *form* of a value and not the *set*
  of them — one series per distinct value, unboundedly. It becomes a label when
  identity is authenticated.
* **Logs** — unchanged, and still the place `tenant` and `request_id` appear per
  line.

The join key across all three is `request_id`, which **is the trace id in hex**
(`test/test_span_tree.py`), so an id from a log line pastes directly into
Jaeger's trace lookup. Measured at CP1 step 10: median 4 ms to the full tree.

**Config identity.** `config/serving/<hash>.json` holds one version per file;
`config/serving/CURRENT` is a one-line pointer naming the current one, which is
what "current production config" means on a machine with no deploy pipeline.
Reverting is a pointer write, not an edit. `MITYA_CONFIG_VERSION` overrides it
for one process. Every path is fail-closed: a missing pointer, an absent
version, an invalid artifact, or a file whose contents no longer hash to its
name all refuse to start. `config/corpus/manifest.json` records what the index
was built from, and the serving config's `EMBED_MODEL` must equal the
manifest's or the API refuses to serve — closing a silent-degradation path
where a model swap without a re-ingest compared query vectors against an index
built by a different model.

**Operator plane.** Prometheus scrapes `/metrics`; Grafana renders a RED+spend
dashboard provisioned from files. Both run under Compose, both bound to
loopback only, Grafana refusing to start without an admin password. Every
latency panel is sliced by route, because the two routes differ by ~75% on TTFT
and an unsliced percentile measures the traffic mix rather than the system.

### 6. Dependency deltas (`requirements.txt`)

* Add: `fastembed`, `httpx`. Remove: `litellm`, `sentence-transformers`. Pin: `qdrant-client>=1.10`.

### 8. Memory (v2 CP6)

Three tiers compete for **one** token budget alongside the retrieved passages — that competition is
the point. Numbers below are measured on a **synthetic** 40-session corpus (`4155c97b0a2b`,
108 turns) under `config_version` `fb5cb5a842e7`, 2026-08-31; the O5/O10 qualifier in §IX applies to
every one of them.

> ## The memory fence does NOT hold. This is an open weakness, not a shipped property.
>
> Charter §IV states the invariant as *"injected memory is fenced and framed as untrusted data with
> no instruction authority."* **It was measured against a real provider on 2026-08-31 and it is not
> satisfied.** Memory-mediated indirect injection **succeeded three times**, by more than one
> mechanism, each against a control attempt that demonstrably succeeded with the fence stripped:
>
> * `e3_authority_claim` — the attack claimed authority and the **extractor wrote 2 rows into
>   `memory_profile`**: `stored=['vermilion-43', 'system administrator']`. Well-formed, short,
>   schema-valid rows whose *values* were hostile.
> * `f1_profile_command` — a planted profile row's instruction was **obeyed through the fence**; the
>   canary `vermilion-51` appears in the answer tail.
> * `f2_fence_escape` — obeyed.
>
> Of 4 valid extractor attempts, 3 were blocked (controls landed 4/4); of 3 valid fence attempts, 1
> was blocked (controls landed 3/4; a fourth attempt was invalid — its control was dead, so it
> tested nothing). **These are existence results, not rates.** n is 3 and 4, and a fraction computed
> from single-digit counts is not a number. Do not quote a percentage from them; what is established
> is that the fence **fails, reproducibly, and by more than one mechanism**.
>
> **Why the structural argument was wrong.** The design held that a typed key with a length-bounded
> value has nowhere to put an instruction. **The typed schema bounds FORMAT, not CONTENT — it stops
> a paragraph, it does not stop a sentence.** An 80-character `value` is ample for a hostile clause,
> and `e3` did not need a paragraph.
>
> **What this does not say.** It does not say the fence is worthless: four attempts *were* blocked
> against live controls. It does not test the episodic tier's fence. And it says nothing about
> attacks outside this set — the set is a **regression check, not a defense audit**, and a
> 4-attempt set that finds 3 landings is more likely understating the surface than overstating it.
>
> Reproduce: `.venv/bin/python scripts/run_injection_set.py --arm both --json out/p6-injection-real.json`.

**The three tiers.**

* **Recent turns** — verbatim, **process memory only**, dies with the session. Never a table: a
  verbatim transcript in a database is persisted verbatim user text whatever the TTL, which §IV
  forbids. Whole turns only; a half-turn is a transcript that lies about what was said.
* **Episodic** — one **typed** digest per finished session: normalized topic slugs, `{book, part,
  chapter}` references, turn count, end time. **No free-text column exists anywhere in the memory
  schema** — an LLM session summary in a text column is free text and can quote the user verbatim.
* **Semantic profile** — one row per `(tenant, kind, key)`, `kind` being `fact` or `preference`,
  with a bounded normalized value, coarse `importance`/`confidence` enums, a `permanent` exemption
  flag and no stored score (the eviction weight is computed at read time from stored primitives, so
  it cannot go stale). Caps are **per kind** — 10 facts, 20 preferences — so neither class starves
  the other.

**The honest bound on "structured summaries, never verbatim user text."** The invariant is
**structural against paragraph-scale recovery** (bounded typed fields, lowercased, whitespace
collapsed, punctuation stripped at write) and **enforced by test against clause-scale recovery** (an
adversarial turn, asserted against raw *and* normalized store contents). Claiming "structural"
without that qualifier would be an invariant defended by a test that cannot fail.

**The allocator is a swappable policy object.** Same inputs (tenant, session, query, budget), same
output (a `ContextPlan` of blocks with tokens and **provenance** per block, visible on P1's spine).
Adding a policy is a subclass plus a registry entry; selecting one is the versioned
`ALLOCATION_POLICY` field and nothing else. The shipped policy (`passages_first`) admits retrieval
**whole** — passages are never truncated to make room for memory, because cutting a passage
mid-sentence corrupts the grounding the system exists to provide — and memory takes what is left.
**P6 built the seam and deliberately did not pick the winner** (O4); CP7 searches it.

**Writes.** The summarizer is an ordinary gateway consumer on `models/gemini-3.1-flash-lite`
(serving stays `models/gemini-3.5-flash-lite`), so memory's write cost lands in cost attribution and
counts against quota like any other spend. The write is **in band**: after the final answer token,
before the stream closes, in the same admission slot. **A failed write never fails the turn** — the
answer is already delivered; the failure is logged and counted, and memory is left at its previous
consistent state. Per-turn profile extraction reads the **question only**, never the answer, so
retrieved corpus text cannot enter the store through that path. The episodic digest is written
**lazily**: session *k-1* is summarized on the first turn of session *k* for the same tenant, which
needs no session-end signal, no new endpoint and no client contract — at the stated cost that a
tenant's **last** session is never summarized. Writes are size- and rate-capped
(`MAX_WRITES_PER_MINUTE_PER_TENANT`), and both persisted tables carry `writer_model`.

**Retention and purge.** Recent turns are bounded by the session. Both persisted tiers share a
time-boxed staleness window — `MEMORY_RETENTION_DAYS`, default **90**, an `[estimate]` that is a
user-data posture and **not a finding** — plus the per-kind count caps, enforced **lazily on the
write path and at read**, never by a job. Purge is a `DELETE`, valid because **no persisted row
mixes two tenants' content**; `ON DELETE CASCADE` from `accounts` carries onto both tables. **The
retention window ships implemented and unexercised**: the frozen corpus crosses no time-boxed
boundary at any duration, so the window is validated by a unit test with an injected clock and
**never by a clean run**. Purge is complete only under single-process operation — a second worker
would hold a second copy of the in-RAM transcripts that no `DELETE` reaches.

**What memory costs, measured.**

| | serving | summarizer | total | per session |
|---|---|---|---|---|
| with memory | $0.213968 | $0.022100 | $0.236068 | **$0.005902** |
| no-memory baseline | $0.177713 | $0.000000 | $0.177713 | **$0.004443** |

**Memory multiplies cost per session by 1.328x.** 62% of the delta is *serving* (the input tokens
injection adds) and 38% is the write — a memory design that counted only its write cost would
understate the bill by nearly two thirds. Memory took **44% of all context tokens** (127,609 against
passages' 162,267). Storage is negligible and bounded by the cap rather than by traffic: 200.3 bytes
per episodic row, **100.2 bytes per session per tenant** retained. The episodic cap fired 20 times
in a 40-session run with clean recency eviction — **the first cap in this project to fire against
real data**. Figures are list-rate shadow pricing on a free tier (`metered: False`): the real
ceiling is quota, not money.

> **And P6 cannot say what that 1.328x bought.** The phase built a multi-turn success metric, fed it
> a deliberately starved allocation — one arm withheld 68 memory blocks and 8,547 tokens while the
> served passages did not change by one byte — and the metric **did not move at all**: delta
> **0.000** on all four scored families, 95 turns scored of 108. The mechanism is that retrieval is
> issued on the query alone and never reads the transcript, so a metric scored on *retrieved
> chapters* cannot see an allocation that changes only which *memory* tiers reach the prompt. The
> metric was rejected by its own test, and no substitute was bought: authoring per-turn gold would
> re-freeze the corpus and invalidate P4's provenance, and an LLM judge was already measured unable
> to arbitrate quality on this project (§VII.2) and is non-gating by policy in any case.
>
> **So the memory system ships with its value to answer quality UNMEASURED — not demonstrated.** If
> memory turned out to add nothing, nothing in this phase would detect it. That is a known,
> recorded, accepted hole, not a passing grade. The rejected metric is retained as a
> **retrieval-damage guard**: it cannot show memory helping, and it *can* show a future allocation
> crowding out the passages block, which is what CP7's non-inferiority check asserts against.

**Per-tier budget utilization, and one zero that must not be misread.**

```
tier            turns present    tokens     note
passages            108/108      162,267
recent_turns         68/108       60,453    ~889 tokens per turn present
episodic            105/108       67,156    ~640 per turn present; absent only on session 1
profile               0/108             0   UNEXERCISED BY THE CORPUS -- not "unused"
```

**`profile` reads zero for a corpus reason, not a tier reason.** The frozen corpus is 108 literary
questions about Dostoevsky authored for retrieval evaluation; it contains no user preferences and no
user facts, so `[]` is the *correct* extractor output on every one of its turns — verified by a
known-answer check in which a fact-bearing turn produced two typed rows and a corpus turn produced
none. Profile *storage*, eviction, retention and purge are covered by unit tests, and profile
*injection* by the injection set, which plants rows directly. **Extraction from realistic
conversational traffic is covered by nothing in this phase**, and no artifact here should be read as
covering it.

**Context sizing.** `CTX_SIZE` had drifted into meaning two unrelated things: the local arm's 6GB-GPU
fit, and the hosted path's self-imposed input cap. On the hosted path it was raised to **6144** with
the memory budget **306 → 3072**, because nothing in the request path starts llama-server and a
6GB-GPU constraint has no hardware meaning there — while at 306 tokens the whole-turn admission rule
left the recent-turns tier contributing **zero blocks on essentially every turn** against a live
provider (a single prior turn measured 708-738 tokens). **The local rule is unchanged: llama-server
still runs `-c 3072`**, and any local run promotes an artifact carrying `ctx_size=3072`. One
consequence: P6's serving latency and cost rows are a **new arm** — not comparable to CP4's or CP5's.

**Known limitations, stated so they do not read as passes.**

* The fence does not hold (above). Charter §IV's memory invariant is **open**.
* **No multi-turn success metric exists**, so no allocation policy can be claimed better than
  another. O4 is not retired.
* **Per-tenant fairness is still unresolved** (§IV.2, G-P10). P6 produced no fairness number and
  none is implied by its silence.
* The **in-band write's latency cost is unmeasured.** The write occupies the admission slot for its
  duration; that duration was not timed against CP5's 2.1 s idle baseline.
* The two summarizer ids are assumed to meter on **separate upstream quota windows**. Our limiter
  scopes by `(provider, model)` and a full two-arm run produced no 429, but the per-model separation
  was measured on a *different* provider and remains an inference for this one.

---

## V. Hardware Budget

> **HISTORICAL as of v2 CP2 (2026-08-28) — superseded by hosted serving, not deleted.**
> Nothing in the `/chat` request path starts `llama-server` any more; the default serving model
> is `models/gemini-3.5-flash-lite` and `local-q4km` is a frozen chart line, not a live arm
> (charter A3). The numbers below are kept verbatim for two reasons: the **frozen quality
> baseline cites them**, and `src/gateway/local.py` still adapts llama-server for offline
> evaluation, so the budget remains real for that path. `docs/DESIGN.md` §V carries the same
> figures and the two copies must stay consistent.
>
> One number here outlived the hardware and caused a measurement defect: `-c 3072` is why
> `Config.MAX_TOKENS` was **400** — sized so a ~1742-token prompt plus its completion fitted the
> local context window. Carried into hosted serving it truncated every answer 11a measured, and
> was raised to **1024** once that was found (R66). A constraint of the hardware being escaped is
> not a product decision, and this section is where that confusion started.

6GB VRAM is the binding constraint. **GPU = LLM only; CPU = everything else.** Within a request the phases are sequential (CPU retrieval → GPU generation), so there is no GPU contention by construction.

* Mistral-7B `q4_k_m` weights ≈ **4.4GB**.
* KV cache — Mistral v0.3 is GQA (32 layers, 8 KV heads, head_dim 128): 128KiB/token fp16 → **0.50GB @ 4096 ctx**, ≈ **0.27GB at `q8_0`**.
* Compute buffers + CUDA context ≈ 0.5–0.8GB.
* **Total ≈ 5.2–5.5GB** — fits with full offload at 4096 ctx and quantized KV, assuming a headless GPU. If the desktop compositor renders on this card, drop to `-c 3072`. `q5_k_m` (~4.9GB weights) has no headroom at any useful context — serve `q4_k_m`.

**Launch the model server:**  

```bash
llama-server -m models/quantizations/mitya-2.0_q4_k_m.gguf \
  -ngl 99 -c 4096 -fa on \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --parallel 1 --host 127.0.0.1 --port 8080
```

*Note: V-cache quantization requires flash attention (`-fa`) — without it the flag is ignored. The current `scripts/start_server.sh` launches `llama-cli`, which serves no HTTP API; it must be rewritten to `llama-server` as above.*

System RAM: Python service ≈ 1.5GB resident (fastembed nomic ~0.5GB, FlashRank ~0.1–0.3GB, Qdrant local negligible at ~4k points, FastAPI ~0.3GB) + llama-server's mmapped weights ≈ 4.5GB — comfortable on a 16GB machine.

**Verify:** after warmup, `nvidia-smi` shows < 5.7GB used; a `curl` chat completion streams tokens.

---

## VI. Latency Budget

Measured on the real card (GTX 1060 6GB), P7 `scripts/measure_latency.py` + the P6 server request log. These replace the pre-build estimates; the estimate is kept in parentheses where it was materially off.

* **Retrieval only: ~2.25s** (est. ≤0.6s). Rerank dominates — FlashRank `ms-marco-MiniLM-L-12-v2` on CPU, 25→3, is ~1.96s of it; embed + Qdrant fuse are ~0.3s. This is the honest floor; cutting it needs a smaller/GPU reranker or fewer candidates (out of scope, §VIII).
* **TTFT: ~7.9s** (est. ≤3s). ~2k-token prompt prefill on the 6GB card is slower than estimated.
* **Total: ~21.3s** for ≤400 decode tokens (est. ≤20s) at ~23.6 tok/s.
* **Cache hit: ~0.008s** (est. <50ms) — well under budget.

The retrieval and TTFT numbers exceed the pre-build estimates; they are accepted for a solo local deploy (hardware fit > latency). No cold cache-miss end-to-end is recorded here: the only clean generation numbers are P6's (above); `measure_latency.py`'s cache-miss line is unreliable when the query is already cached.

### v2 CP1 evidence record (measured 2026-08-27, n=24 over two arms)

The v1 figures above are **unsliced**, and CP1's first finding is that an
unsliced percentile is a statement about the traffic mix rather than about the
system. Sliced by route, on the same card, cold llama-server, cache disabled:

| | DEFAULT | STRUCTURAL |
|---|---|---|
| TTFT mean | 7.738s | 4.430s |
| TTFT p50 / p95 | 7.500 / 7.950 | 4.500 / 4.950 |
| request p50 / p95 | 15.0 / 23.0 | 15.0 / 24.0 |
| `rerank` p95 | 2.933s | *(not on this path)* |
| `retrieve` p95 | 0.488s | 0.450s |
| `generate.first_token` p95 | 7.375s | 4.900s |
| `generate.stream` p95 | 14.333s | 19.000s |

`route`, `cache.check`, `entities.resolve` and `context.build` all sit at the
0.0005s bucket floor — below the histogram's resolution, which is where they
belong. The 75% TTFT gap between routes is the whole argument for publishing
`route` as a dimension.

**Instrumentation overhead: below the detectable floor.** Not zero — *below the
floor*, which is a different claim and the only one the data supports. Two cold
arms, instrumented vs `OTEL_ENABLED=false`, same query set:

* TTFT (DEFAULT, n=8/8): delta **-66 ms**, 95% CI [-188, +57] ms, MDE ~175 ms
  (2.25% of TTFT). The point estimate is *negative*, which instrumentation
  cannot cause; it is noise.
* Total wall-clock (n=12/12): MDE ~4.2s (22% of mean) — useless as a comparison,
  because total latency carries answer-length variance that dwarfs anything
  being measured. TTFT is the right instrument for this question.

**Debug-workflow join: median 4 ms** from a bare `request_id` to the full
9-11 span tree, because `request_id` *is* the trace id.

**Storage growth (RAM, not disk — R32's Jaeger is in-memory, so this measures a
process's RSS and is bounded by `MEMORY_MAX_TRACES=50000`).** ~110 KiB of Jaeger
RSS per traced request over n=8, i.e. **~107 MiB per 1k requests**. Treat as an
upper bound: Go's allocator does not return freed memory promptly, so RSS delta
overstates the per-trace cost, and n=8 is small.

**Prompt-cache warning, and it invalidates naive re-runs.** llama-server caches
prompt prefixes across requests. Re-running the same query set without
restarting it dropped STRUCTURAL TTFT from 4325 ms to **327 ms** and the mean
total from 17.4s to 12.3s. Any A/B of latency must restart llama-server between
arms, or arm two wins by construction. Every figure in this section is
cold-start.

---

## VII. Evaluation

### 1. Retrieval-only (primary tuning tool)

* ~30 labeled `query -> relevant chapter/chunk` pairs; report `recall@5` and `MRR`. Lets retrieval (prefixes, expansion, rerank) be tuned independently of generation — also the tripwire for RR5 (prefix misuse).

> **CORRECTION (v2 phase 0, 2026-08-21) — the ablation below is WITHDRAWN.** Every number in it
> was produced by a harness that did not mirror serving, and every delta in it sits below this
> eval set's detection threshold. It is kept in place as the record of what was published, not as
> a result. Three independent defects, in the order they were found:
>
> **1. R18 (2026-08-19) — every v1 retrieval number is a draw from a distribution.**
> `EntityMap._scan()` returned a `set`, so a two-entity query's alias blocks reordered under
> `PYTHONHASHSEED`: different expanded text, different embedding, different dense ranking.
> Measured bimodal `recall@5` 0.720 / 0.760 across 23 processes on a single commit. This was a
> **serving defect, not an eval artifact** — serving calls the same `resolve()`, so two users
> asking the identical two-entity question in different workers received different chunks. Fixed
> in `src/rag/entities.py` (`_scan()` returns an occurrence-ordered list); 12/12 identical across
> separate processes afterwards, spread exactly 0. The repeat-run check is now permanent and must
> be **cross-process**: one process fixes the hash seed and sees nothing. Generalization worth
> carrying: a set or dict whose iteration order reaches *model input* is a nondeterminism bug, not
> a style issue.
>
> **2. R19 (2026-08-20) — the harness never used the route it computed.** `eval_retrieval.py`
> called `route()`, asserted the result against `expected_route`, logged `route ok`, and then ran
> the DEFAULT hybrid path for every row regardless. `retrieve_structural_excerpts()` — the
> function production calls — was imported by `src/api/app.py` and `scripts/eval_quality.py` and
> by nothing in the retrieval harness. Structural queries are nearly pure metadata (a book title
> and two numbers), so the DEFAULT path missed all four while production finds every one at rank
> 1. The "router matched 6/6 expected-route rows on every arm" claim was this defect stated as a
> success: the router was right, and was then ignored. Post-fix, on the 45-row set, deterministic
> over 3 processes:
>
> ```
> before (broken harness):  PRIMARY n=43   recall@5 0.6512  MRR 0.4512
> after  (R19):             PRIMARY n=40   recall@5 0.7250  MRR 0.5287
>                           STRUCTURAL n=3 recall@5 1.0000  MRR 1.0000
> ```
>
> This R19 headline (`0.7250` / `0.5287`, PRIMARY n=40) was itself superseded again, 2026-08-29
> (Q4, `plans/mitya-v2/02-decide/decisions.md`): two rows misclassified as `primary` by a legacy
> family-key gap moved to `structural`. See §VII.1's live number below.
>
> `structural` became its own family with its own denominator: a row whose chapter is
> regex-resolved and hard-filtered **cannot miss**, and guaranteed hits inside the headline
> denominator is the `0.731` bug in a new costume. Any route added to `router.py` must be
> dispatched here too, or the harness silently measures a pipeline production does not run.
>
> **3. R20 (2026-08-20) — the deltas were below the detection threshold, and always will be.**
> Four arms re-measured post-fix at primary n=40, paired McNemar,
> `MDE = 2.8 x sqrt(p_disc / n)` at 80% power / alpha 0.05:
>
> | comparison | delta | p_disc | MDE | callable? |
> |---|---|---|---|---|
> | full vs dense-only | +0.025 | 0.075 | 0.121 | no |
> | full vs sparse-only | -0.025 | 0.075 | 0.121 | no |
> | full vs no-expansion | 0.000 | 0.100 | 0.140 | no effect to detect |
> | dense-only vs sparse-only | -0.050 | 0.150 | 0.171 | no |
> | dense-only vs no-expansion | -0.025 | 0.175 | 0.185 | no |
> | sparse-only vs no-expansion | +0.025 | 0.075 | 0.121 | no |
>
> Every observed delta is 3-7x below callable. The n that would resolve them is 470-940 rows;
> n=150 yields MDE ~0.072 and is short of all of them. **The MDE-vs-n curve does not flatten at a
> useful point — it never crosses one.** The sign also flipped from v1's reading (dense leg worth
> 2 rows, sparse leg 1), which is the tell: at n=40, `dense-only` (0.7500) reading above `full`
> (0.7250) is not one arm beating another, it is the same noise read a second time, and R72
> (2026-08-29, 106-row set, identical MDE) confirms no arm in this ablation is distinguishable
> from any other. The ablation is withdrawn as a set of noise readings, not re-ranked.
>
> **Rule adopted, binding on everything in this section and after it: publish the MDE alongside
> every delta, or do not publish the delta.** Corollary: no configuration knob may be selected
> against this set unless its effect clears ~14 recall points at n=40. RRF prefetch limits, leg
> selection, expansion on/off and chapter-dedupe (measured: +1 row) are all inside the noise. The
> **ACTIONABLE** item below — switching `rerank()` to the expanded query on a 2-row delta — is
> therefore **not adoptable on this evidence**, and was never taken.
>
> **What the set is for**, restated: a regression tripwire and a coverage instrument, not an
> arbitrator of small configuration choices. It has caught two real breaks (R18, R19) and zero
> tuning wins. Growth follows the coverage axis, not n — three structural rows costing 1.1
> minutes of verification exposed R19. The set is now 99 rows.

**Ablation (2026-08-16, `scripts/eval_retrieval.py --arm`, results in `test/ablation.json`) — WITHDRAWN, see the correction above.** 27 rows: 25 primary + 2 negative controls reported separately. Router matched 6/6 expected-route rows on every arm.

| Arm | k | recall@k | MRR | vs. baseline |
|---|---|---|---|---|
| **full** (shipped: expanded query, dense+BM25 RRF) | 5 | **0.720** (18/25) | 0.406 | — |
| no-expansion (entity map off) | 5 | 0.680 (17/25) | 0.487 | -1 row, **+0.081 MRR** |
| dense-only (BM25 leg dropped) | 5 | 0.680 (17/25) | 0.417 | -1 row |
| sparse-only (dense leg dropped) | 5 | 0.640 (16/25) | 0.404 | -2 rows |
| rerank — fused top-3 | 3 | 0.560 (14/25) | 0.373 | own baseline |
| rerank — FlashRank on **raw** query (shipped) | 3 | 0.520 (13/25) | 0.500 | **-1 row**, +0.127 MRR |
| rerank — FlashRank on **expanded** query | 3 | **0.600** (15/25) | **0.507** | +1 row, +0.134 MRR |

**Read the deltas against the floor: 25 rows means one row is worth 0.040 recall, so anything under ~0.08 is a single row and is noise.** On that rule only two results clear the bar, and both barely.

* **Hybrid fusion is doing real work, but modestly.** Full (0.720) beats both single legs; the dense leg is worth 2 rows, the sparse leg 1. Neither leg alone reproduces the fused score, so RRF is not decoration — but the effect is 1-2 rows, not a step change.
* **Entity expansion does not move aggregate recall at n=25, and costs MRR.** It buys exactly one row of recall (0.680 -> 0.720) while *lowering* MRR by 0.081 — i.e. expansion surfaces one document the raw query misses, and pushes correct documents down the ranking on the rest. The eval set is Garnett-heavy and only 2 rows exercise the cross-translation folding expansion was built for, so this measures the layer on the wrong workload rather than refuting it. Not a reason to remove it; a reason to grow the eval set before defending it.
* **ACTIONABLE — reranking the raw query is the wrong call.** The P6 impl-time judgment (carried forward unmeasured through P7) was to hand FlashRank the **raw** query. Measured: raw-query rerank *loses* a row against no reranking at all (0.560 -> 0.520), while expanded-query rerank *gains* one (0.560 -> 0.600) and edges MRR higher (0.507 vs 0.500). Both rerank arms beat the unreranked ordering on MRR, so the reranker earns its ~1.96s either way — it is being fed the wrong string. Switching `rerank()`'s query to the expanded form is a 1-line change in `src/api/app.py`; it is **not** taken yet, pending a second confirming run, since 2 rows is right at the noise floor.

**Correction to the P4 baseline (2026-08-16).** The long-quoted `recall@5 = 0.731 (19/26)` predates the P7 driver fix and counted the two negative-control rows inside the headline. On the 27-row set with controls split out, the shipped configuration scores **0.720 (18/25)**. `0.731` is not reproducible with any harness this project has run.

**Further correction (v2 phase 0, R18 + R19).** `0.720` is superseded in turn. It was measured under the entity-ordering defect, so it was one draw of a bimodal 0.720/0.760, and it was measured by a harness that ran the DEFAULT path for structural rows. Post-fix numbers, in the two forms worth quoting:

* **`recall@5 = 0.7250` (29/40), `MRR = 0.5287`** — `primary` family, 45-row set, current harness. Superseded below.

**Q4 correction (2026-08-29, `plans/mitya-v2/02-decide/decisions.md`).** `0.7250` is superseded
in turn. Two legacy rows (`r-0024`, `r-0025`) carried no `family` key and were derived into
`primary` by default; both are STRUCTURAL-routed and hit at rank 1 in every arm, so they are
structurally incapable of discordance and were inflating the denominator with guaranteed hits —
the `0.731` failure mode in a new costume (§VII.1, R19). `family_of()` now derives a row with
`expected_route == STRUCTURAL` as family `structural` regardless of the legacy key gap; the set
file itself is untouched (A9). Recomputed: primary n 40 → 38, hits 29 → 27,
**`recall@5 = 27/38 = 0.7105`**, **`MRR = 0.5039`**. `structural` n rises 10 → 12, recall stays
1.000. **`recall@5 = 0.7105` (27/38), `MRR = 0.5039` is the number to quote going forward.**
* **`0.760` (19/25)** — the same fix re-scored on v1's frozen 25-row intersection, where `r-0025` now hits. Quoted only when comparing against a v1 number on the same rows (A9: two set versions compare on their intersection).

`0.720` was never the retriever's recall on those rows. It was the recall of a pipeline production does not run.

**Pool recall, added 2026-08-30 (R81). Reported, not blocking; `0.7105` is unchanged.**
`recall@5` scores `fused[:5]`, but `rerank()` receives the whole `TOP_K_FUSED` pool of 25
(`src/rag/retrieve.py`), so `recall@5` is **neither an upper nor a lower bound** on what the served
top-3 contains — a target at pool rank 12 is a `recall@5` miss the reranker can still serve. Every
miss therefore folds together two failures with opposite remedies: a **pool miss** (target absent
from the pool) is a retrieval failure no downstream stage can undo, while a **pool hit below k** is
an ordering failure and is exactly the headroom a reranker exists to convert.
`scripts/eval_retrieval.py` now reports both per family:

```
full arm, 121-row set    recall@5   pool recall   headroom   pool misses
  primary   (n=38)        0.7105      0.9211       0.2105     3
  quotation (n=15)        0.8667      0.9333       0.0667     1
```

The pool is **not** saturated: `r-0032` and `r-0044` miss the pool in every arm and are
unreachable at `PREFETCH_LIMIT=50`. The `0.2105` primary headroom is the honest upper bound on
what any reranking work can buy, and the price — in recall, not seconds — of deferring rerank out
of CI (R79). `recall@5` keeps the blocking role: pool recall saturates by construction and would
yield too few discordant pairs to gate on, it is blind to every ordering defect, and swapping the
blocking metric is a measurement break to be taken deliberately, not absorbed as a side effect of
adding a diagnostic. Whether it should move remains a named open question.

### 2. Answer quality

* Four themed sets (Motivation / Philosophy / Ambiguity, 15 each; `Textual`, 5), **50 rows** at
  set version v2.0. `Textual` exists because every one of the original 36 rows resolved to the
  DEFAULT hybrid path, leaving the STRUCTURAL route unmeasured for answer quality even after R19
  made the harness dispatch on route.
* Judge (manual or LLM-judge): stays in-persona, grounded in the retrieved chunks, no fabricated events. Grounding is checked against the programmatic Sources, not inline citations.
* Includes the **prompt-composition A/B**: context-in-system ("recollections" framing) vs context-in-user-turn, judged on persona retention + groundedness.
* **A/B outcome (P7, 36 queries × A/B, LLM-judge):** no winner — A and B are within noise (persona 3.11 vs 3.22, groundedness 2.44 vs 2.53 on 1–5; B fabricates slightly more, 0.50 vs 0.47). **Both compositions kept** in `src/rag/generate.py`; the delete-loser prune is not taken. The eval instead surfaced two systemic issues dominating any A/B signal: meta-commentary answers (learned from the fine-tune's weak gemini-2.5-flash training outputs — model/data floor, prompt trace verified clean) and stochastic empty completions (fixed: empty-retry + sampling floor, `253a8ee`). Revisit A/B only after a better training-data/model pass.

> **CORRECTION (v2 phase 0, 2026-08-21) — the A/B numbers above are WITHDRAWN, and the
> measurement contract has changed.** The conclusion ("no winner, keep both") survives; the
> evidence offered for it does not.
>
> **1. R24 — ten of the 72 cells were empty answers scored 1/1.** 7 of 36 mode-A rows and 3 of 36
> mode-B rows carried `answer: ""` with `generation_error: null`. All ten were judged, all ten
> came back `persona_retention: 1, groundedness: 1` with rationales reading *"the response is
> empty"*, and all ten entered the published means indistinguishably from genuinely poor answers.
> Two layers had to fail and both did: `_collect_answer()` returned `("", None)` when a stream
> completed with zero tokens, and `judge_answer()` substituted `answer or "(empty response)"`,
> making the empty string judgeable rather than rejecting it. Both are fixed. Restricted to the 26
> pairs where both arms produced a non-empty answer:
>
> | axis | published (n=36) | corrected (n=26) |
> |---|---|---|
> | persona delta (B−A) | +0.111 | **−0.231** (sign flips) |
> | persona corr(A,B) | −0.059 | +0.092 |
> | persona MDE | 1.147 | 1.176 |
> | groundedness delta | +0.083 | +0.038 |
> | groundedness corr(A,B) | +0.625 | **+0.852** |
> | groundedness MDE | 0.551 | **0.395** |
>
> The arms also had different failure rates (7 empty vs 3), so part of what was reported as a
> comparison of answer quality was a comparison of generation reliability.
>
> **2. R23 — apply the §VII.1 rule here too: both deltas were far inside their MDEs.** +0.111
> persona against MDE 1.147, +0.083 groundedness against MDE 0.551. Composition B is therefore
> not *wrong*, it is **unjustified** — a different claim, and the one this section now makes. The
> two axes are not equally usable: groundedness pairs properly (r = +0.852 corrected), so query
> difficulty is a shared property the paired design cancels and more rows buy real power. Persona
> retention does not pair at all (r ≈ 0, `sd_diff` larger than either arm's own sd), so n is the
> wrong lever for it.
>
> **3. R25 — the generator carries 93% of persona-retention variance; the judge is exonerated.**
> Judge test-retest on *fixed* inputs gives within-variance 0.089. The same cells re-generated
> give 1.333. Averaging draws is the only lever on that axis — growing n and rewriting the rubric
> were both measured and both refuted. **The unit of measurement is now the cell mean over
> k = 3 draws**, not a single generation; a cell needs ≥2 usable draws or it is recorded as
> failed and excluded (`scripts/eval_quality.py --draws`). Baseline and candidate must share the
> same k or their means are not comparable.
>
> **4. R27 — `fabricated_events` is published as a boolean, never as a count.** The judge
> reproduces its own count on unchanged input 50% of the time; the boolean reaches 80%.
> Enumerate-then-count, tried as the fix, made it worse (0.200 agreement, 3x the variance). The
> count survives as `mean_fabricated_events_UNRELIABLE` — diagnostic evidence only, never gated,
> never compared across runs. The boolean does carry independent signal (corr −0.460 with
> groundedness; 6 of 30 cells scored low-grounded but clean), which is why the axis is kept at
> all. **The `0.50 vs 0.47` figures above are counts-era numbers and are not comparable to
> anything published after this correction.**
>
> **5. R28 — empty completions are retried four times, not once.** Exhausted-empty rate 19.4% →
> 8.3% at `EMPTY_RETRIES=4`. The residual is per-query and reproducible across independent runs at
> different settings — it is not prompt size (the failing prompts are *smaller* than the passing
> ones). Failures are recorded rather than dropped: excluding them flatters the configuration by
> deleting the queries it handles worst.

### 3. Latency

* Measured on the real card — see §VI (TTFT ~7.9s, total ~21.3s, retrieval ~2.25s, cache hit ~0.008s). Budgets revised there from data.

### 4. CI eval gate (P3, decided 2026-08-29)

**Three layers, one report, per `plans/mitya-v2/02-decide/decisions.md` Q1.**

| Layer | Metrics | Rule | Power |
|---|---|---|---|
| Invariants (1a) | route-match count, cross-process determinism spread, per-family denominators, canary/redaction, guard false-refusal count, config-version stamps | exact assertion, tolerance 0 | absolute — both real defects ever caught (R18, R19) were this class |
| Cost (1a, computed) | USD/answer from price table x token counts on the fixed prompt set | relative-regression: candidate <= production, exact arithmetic | exact |
| Sampled quality (1a retrieval; 1b leak/tell/style at promotion time only) | recall@k, coverage, MRR per family | paired McNemar non-inferiority: block iff candidate's deficit clears alpha 0.05; epsilon = the run-computed MDE, never a hand-set floor | ~0.12-0.18 at n=38 (R72) |

1a runs live-local, free, unattended, on every gate run. 1b (generation-dependent: source-leak
rate, assistant-tell rate, style distance, latency, measured cost) runs only at promotion time,
budgeted against all four provider rate limits with the minimum taken.

**Verdict vocabulary is three-valued** (BREAK / NOT-DETECTED / DETECTED), never a bare pass/fail,
so the gate's green check cannot launder "unmeasurable" into "fine." The gate's caption on a green
run is **"invariants hold, cost did not grow, retrieval not detectably worse (MDE m at n=N)"** —
never "quality preserved." A metric is NOT-DETECTED when `|delta| < MDE`; this passes the gate,
prints delta and MDE side by side, and is never recordable as an improvement (the evidence-log
template and P7's ranking function are both forbidden from reading a NOT-DETECTED delta as a
gain). The primary quality artifact is the per-row flip list, not the aggregate delta alone: which
rows changed and which direction, since retrieval hit/miss is exact and deterministic given config
(A8b) even though the population-level claim is bounded by the MDE.

**The measured negative (R20 + R72), published as a finding, not a footnote (A13):** no quality
improvement this system can produce is detectable at any reachable n — MDE 0.121-0.185 against
observed effects of 0.000-0.050, n_req 470-2195, on two independent measurement occasions (45-row
and 106-row sets, identical result). The sampled-quality comparator will fire on approximately no
real change this project will ever make; its live role is catastrophic-break detection (roughly 5+
net row flips), and the gate's routine detection power lives in the invariant and cost layers
instead.

**R73, now answered by R80 — and it is the phase's one positive result.** 0 of 40 primary queries
contained a quotation mark, so the BM25 leg was evaluated on the query distribution least able to
demonstrate lexical retrieval's advantage. 15 authored `quotation` rows (`r-0107..r-0121`, verbatim
spans extracted from each target chapter's own indexed chunk) settle it: `full` **0.8667** vs
`dense-only` **0.4667**, 6 discordant rows all one way, exact binomial **p = 0.0156** — the first
ablation arm in this project's history to produce a statistically callable retrieval effect. The
BM25 leg was never the problem; the instrument was. R20's growth rule (coverage, not n) is
vindicated concretely: 15 rows produced what 900 more of the old composition provably could not.
**Honest limit:** these rows are synthetic and maximally favourable to lexical retrieval by
construction, so the claim is *"BM25 acts when literals are present"*, not *"production traffic
needs BM25"* — and v2 has no real traffic to check (D3/O10).

**Step 7's positive control — the gate cannot see grounding (R77, branch B, predicted before the
run).** Stripping every passage from the prompt, a certain and total regression, moved
`source_leak_rate` and `assistant_tell_rate` by **exactly zero**, while the offline judge saw
groundedness fall **4.022 → 1.000** (5.4x its own MDE) and persona stay at **5.000 in both arms**.
This is not a power problem — a metric that does not respond is not fixed by more rows. Those
regexes measure persona *register*, a different construct from grounding. Consequences, binding:
groundedness is Tier 2 (offline, non-blocking, never gate machinery, consistent with A7); every
report must carry **"GROUNDING NOT CHECKED"** and `validate_report()` rejects one that does not;
and the gate is a retrieval-and-cost regression tripwire that may not be described as an
answer-quality gate. The same run measured that the ungrounded arm is **32% cheaper**
($0.001324 vs $0.001942 shadow per answer), so A13's "structurally incentivized toward invisible
degradation" is now a measurement with a named degenerate solution inside P7's search space.

**Calibration on five synthesized defects (R78) — `n = 5`, provisional.** v1's ablation arms could
not serve as labelled history (R20 withdrew them as noise), so the defects were synthesized:

| defect | shape | disposition | what fired |
|---|---|---|---|
| nondeterminism | R18 | blocks | `determinism_vs_baseline` |
| route corruption | R19 | blocks | `route_match_prod/_cand`, `determinism_vs_baseline` |
| no-RAG arm | R77 | does not block | nothing in Tier 1 |
| `prefetch_limit` 50 → 1 | large sampled effect | blocks | `recall_at_5` BREAK, 9 flips, −0.2105 vs MDE 0.2084 |
| the null | no defect | does not block | nothing, 0 flips |

Two readings the table hides. `prefetch_limit` 50 → **3** moved one row, in the *positive*
direction; only starving both legs to a single candidate broke it, and it cleared its own MDE by
**0.002** — the sampled layer is a catastrophe detector, not a quality gate. And the
nondeterminism defect was caught by the **baseline comparison**, not the within-run spread, so
R18-class detection depends on the baseline store (`test/gate/baseline-*.json`), which has no
retention policy — gap G-G is load-bearing, not housekeeping.

**Gate cost (R79), O8's central number.** A gate run costs **$0.00000 and ~30s**; three runs before
the rerank bucket moved to promotion-time measured 243.9 / 237.9 / 242.9s (2.5% spread), of which
the non-blocking serving-path bucket was 87% — FlashRank scoring 25 candidates per query over 106
rows on CPU, twice. Moving that bucket to promotion time (its detection value is zero on a routine
run) cut a run to **29.9s**, an 8.1x throughput gain. `[estimate] 10 gate runs/week, 1
promotion/week` → `[computed]` **$0.00/month and ~0.4 machine-hours** for 1a; 1b is *unmeasured,
not zero*, since `quality_1b` is empty pending its composition (G-B). **On the 1a leg the scarce
resource is time, not money** — P7 pays ~30s per promotion attempt and $0. Against CP2's
$0.001473/answer, one 45-draw promotion check costs the equivalent of ~59 served answers:
measurement dominates serving for a solo deploy.

**Residual risk that matters:** the guard certifies only "not worse by >= the measured MDE." A
promoted config can be up to roughly 12-18 recall points worse than production without detection.
This is bounded, not removed, by the invariant layer catching structural breaks and by the printed
MDE being part of every claim — the number is the honesty mechanism, not a fix.

---

## VIII. Out of Scope (v2+)

Semantic cache; ~~conversation memory~~ / `interpretive_bias`; interpretive-restraint hedging metric; any ML query classification; ~~Qdrant server deployment~~; ~~multi-user concurrency~~.

> **"Multi-user concurrency" and "Qdrant server deployment" REVERSED at v2 CP4 (2026-08-30) — struck,
> not deleted, because the reason each expired is the useful part.**
>
> **Multi-user concurrency** was out of scope because v1 served one user behind a global
> `Semaphore(1)`, itself a 6GB-GPU artifact (`llama-server --parallel 1` plus non-re-entrant CPU
> models). D3 makes the deployment multi-tenant and generation has left the box, so half that
> justification is simply gone. P4 measured the rest: the lock was the wall, `knee_concurrency = 1`,
> throughput flat from 1 to 16 concurrent clients. Narrowing it to retrieval and releasing before
> generation moved the knee to **4** and raised throughput **1.74x** at concurrency 4. Concurrency is
> now in scope and measured, not assumed.
>
> **Qdrant server deployment** was out of scope as a complexity the single-user design did not need.
> It is now **deferred, not excluded** — and for a reason that is worth keeping: the local-mode lock
> is an **OS file lock taken once per process**, so it constrains *offline eval running alongside
> serving*, and **not** concurrent retrieval inside one server. Concurrent retrieval was never
> blocked by it. Server mode answers concurrent *processes*, which is a P5 question, and it fires on
> a measured trigger rather than a preference.

> **"Conversation memory" REVERSED at v2 CP6 (2026-08-31) — struck, not deleted, for the same
> reason: the ground it stood on is the useful part.**
>
> It was out of scope because v1 served one user, one turn at a time, on a 6GB card whose whole
> context budget was spoken for by retrieved passages — there was no budget for a transcript and no
> tenant to attach one to. Both premises expired: generation left the box (CP2), tenant identity
> became real (CP5), and the hosted path's context ceiling has no 6GB meaning. **Memory now ships**
> — see §IV.8. What ships is narrower and better specified than "conversation memory" ever was:
>
> * **Three tiers, one budget.** Recent turns (verbatim, process memory, dies with the session),
>   episodic session digests, and a semantic profile — all competing for one token budget *against
>   the retrieved passages*, which is the point rather than a side effect.
> * **A swappable allocator.** Same inputs, same output; adding a policy is a subclass, selecting
>   one is a versioned config field. CP6 built the seam and deliberately did not pick the winner.
> * **Typed rows, no free text anywhere in the memory schema.** Not a policy against quoting — a
>   schema with nowhere for a paragraph to go.
> * **A lazy episodic trigger.** Session *k-1* is summarized on the first turn of session *k*: no
>   session-end signal exists in the API, and this needs none.
> * **Retention and purge.** Per-tier windows, a 90-day staleness bound on the persisted tiers
>   enforced lazily rather than by a job, and per-tenant purge as a `DELETE`.
>
> **`interpretive_bias` is NOT reversed and stays out of scope.** It was a separate idea that
> happened to share this line; nothing in CP6 revives it.
>
> **Two things the reversal does not buy, stated because an unstated absence reads as a pass.**
> The memory fence was measured and **does not hold** (§IV.8) — indirect injection through memory
> succeeded three times against validated controls, so charter §IV's memory invariant is open, not
> shipped. And CP6 produced **no multi-turn success metric**: the one candidate with external gold
> is arm-invariant by construction and was measured at delta 0.000 against a deliberately starved
> allocation, so memory ships with its value to answer quality unmeasured rather than demonstrated.

> **Distillation stays out of scope — CLOSED with a cost curve at v2 P7 (2026-09-03, O7 / R157), not
> merely deferred.**
>
> Serving a local distilled model is priced against a **rented reference instance** with measured
> wall-clock and a stated utilization assumption — never "the 1060 is already paid for", which is a
> deployment accident and not a cost argument. At a **24/7 rented instance the arithmetic collapses**:
>
> ```
> USD/answer = rate_per_hour x (seconds/answer / 3600) / utilization
> utilization = (answers/day x seconds/answer) / 86400
>            => USD/answer = rate_per_hour / answers_per_hour     # seconds/answer CANCELS
> ```
>
> **Model speed is not in the result.** At $0.09/hr and ~100 answers/day that is **$0.0216/answer**,
> against **$0.003982** on the promoted hosted arm (`gemini-3.5-flash-lite`) and **$0.001325** on the
> cheapest available one (Groq `gpt-oss-120b`) — both at the `max_tokens` ceiling, both list-rate at
> the free tier (`metered=False`, so actual spend is **$0.00** and "beating it" is not possible).
> Break-even is therefore **a traffic number and only a traffic number**: **1,630 answers/day** to
> beat Groq, **16.3x** current volume.
>
> **What distillation actually buys is the saturation ceiling, not the price.** One GPU saturates at
> **1,941 answers/day** at 44.5 s/answer; a 3x distilled model raises that to **5,822**. So it is a
> **capacity decision**, and this deployment has no capacity problem. **Re-entry:** sustained volume
> approaching four figures per day, **or** a move to per-second GPU billing — under which idle time
> is unbilled, the cancellation above does not happen, and local at **$0.001113/answer beats Groq
> immediately with no distillation at all**. That regime is unpriced here because a 7B q4 model must
> stay resident to answer in 44.5 s, and per-request cold load has never been measured.

---

## IX. Residual Risks (summary)

* **Chunk size and overlap are UNTESTED (v2 CP-7h, closed 2026-09-03, D-25).** 500/100 has never been
  compared against any alternative; the ablation was deferred with its price attached (§II.2). The
  risk is not that 500/100 is wrong — it is that **no evidence exists either way**, and the
  merge-only **−2.19%** token saving is a *consequence* of `chunk_overlap = 100` rather than an
  independent result.
* **The memory fence does not hold (v2 CP6, measured 2026-08-31).** Indirect injection through
  memory **succeeded three times** against validated unfenced controls — once by getting hostile
  values written into the profile store, twice by having a planted instruction obeyed through the
  fence. Existence, not a rate (n=3-4). Charter §IV's *"no instruction authority"* invariant is
  **open**, and the typed schema bounds format, not content. Full detail and reproduce command in
  §IV.8. This is the largest open weakness in the serving path.
* **Retrieval is single-turn, and a follow-up question retrieves on a pronoun (v2 P7, R148).**
  Retrieval is issued on the **query alone and never reads the transcript** (R127 `[measured]` — the
  property that made CP-6b's multi-turn metric arm-invariant: 68 memory blocks and 8,547 tokens were
  withheld and the served passages did not differ by one byte). **Memory does not close this**: it is
  injected into the prompt for *generation*, and never reaches the retriever. So on *"what happens to
  him next?"* the retriever embeds and BM25-matches that literal string — no antecedent, nothing for
  `EntityMap.resolve()` to expand — and the answer is composed in character from passages fetched for
  a query that names nobody. **No eval row in v2 can see this**: both eval sets are single-turn by
  construction, so the failure cannot appear in `recall@5`, in the gate, or in any published number.
  The fix is contextual rewriting / coreference resolution *before* retrieval, which is a serving-path
  change plus a model call on the latency path, and it needs **multi-turn retrieval gold that does not
  exist** — a P0-shaped labeling cost, not a knob. **Deferred to v3** (owner, 2026-09-02). Stated here
  because it is a known hole with no instrument, which is the same category as the fence above.
* **Memory's value is unmeasured (v2 CP6).** It costs **1.328x per session** and consumes 44% of
  context tokens; the phase could not build a metric that detects a bad allocation, so no claim is
  made that memory improves answers. See §IV.8.
* Persona may ignore or parrot injected context (fine-tune never saw context in prompts) — **materialized** in the P7 eval: groundedness ~2.5/5 and frequent meta-commentary answers, on both A and B. Root cause is the weak fine-tune (bad gemini-2.5-flash training data + old q4 7B), not the recollections framing or prompt plumbing (trace verified clean, §VII.2). Not resolvable at the serving layer; needs a better training-data/model pass. *(Corrected 2026-08-21, R24: the `~2.5/5` figure includes ten empty answers the judge scored 1/1. The risk still materialized — it is the qualitative finding that stands, not the number.)*
* The Idiot's heading scheme and BK's book count are verified from the files at impl (`structure.json` counts are the tripwire).
* Latency measured on the real card (§VI); retrieval (~2.25s) and TTFT (~7.9s) exceed pre-build estimates, accepted for a solo local deploy.
* FlashRank exact model id and fastembed's nomic prefix behavior are impl-time verifications.
* Entity-map quality depends on the teacher LLM + one human pass; misses degrade gracefully to plain hybrid+rerank.
* ~0.5GB VRAM headroom assumes a headless GPU; compositor on the same card forces `-c 3072`.
