# Mitya RAG — Lean Metadata RAG

Goal: Ground the fine-tuned Dostoevsky model in his 5 novels via metadata-tagged hybrid retrieval, answering in-character with a programmatic Sources list, on a single 6GB GPU.  
*Requirements: Mitya GGUF `q4_k_m` served by `llama-server`; `data/rag/*.txt` (5 clean plaintext novels); Qdrant local mode; all retrieval components on CPU; ≥16GB system RAM.*  

This is the settled v1 design (2026-07-13). It supersedes the previous contents of this file and the `2026-07-12` redesign draft. The full refutation and ruling audit trail is in `docs/superpowers/specs/2026-07-13-rag-decisions.md`.

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

**Why** httpx over LiteLLM (drafted, and already in requirements): LiteLLM abstracts 100+ providers to call exactly one localhost endpoint — a heavy dependency with its own churn; the whole need is a ~30-line streaming client. Remove `litellm`, add `httpx`.

### 6. Cache (`cache.py`)

diskcache (SQLite-backed), **exact-match only**, keyed on `normalize(query) + filters`, no TTL (static corpus), stores only successful non-empty answers, `CACHE_ENABLED` flag.

**Why** diskcache over redis: no external service for a solo local deploy. **Why** exact-match only: a semantic cache adds an embedding index for a single-user app whose hit rate is near zero anyway; the cache exists as the product's cache boundary, kept at ~15 lines. *Note: hit rate is expected to be low and that is accepted; cached answers also skip response variation — acceptable at exact-match granularity.*

---

## IV. API & Operations

### 1. Endpoints (`src/api/app.py`)

* `POST /chat` — `{query, filters?, session_id?}`; SSE stream of answer tokens, then a `sources` event. `session_id` is log-correlation only in v1 (conversation memory is out of scope).
* `GET /health` — llama-server reachability + Qdrant collection presence/count + artifact load status.
* `GET /books` — the `structure.json` tree, feeds the filter UI.

Bind uvicorn and llama-server to `127.0.0.1` by default. Query text appears in local logs only — fine for a single-user deployment.

### 2. Concurrency & timeouts

* One request at a time through retrieval+generation: `asyncio.Semaphore(1)` (llama-server runs `--parallel 1`; the embedder is not re-entrant). Concurrent submits queue.
* Per-request budget enforced; `503` when llama-server is unreachable, `504` on timeout — errors surface to the client as structured JSON, never a hung stream.

### 3. Failure modes

* **Qdrant collection missing/empty:** fail fast at startup, log points at `scripts/ingest_corpus.py`.
* **Zero chunks under a hard filter:** retry once without the filter (log `filter_relaxed=true`); still zero → answer without context, omit Sources, log `no_context=true`.
* **Missing chapter summary:** STRUCTURAL falls back to DEFAULT (logged).
* **Models load once** at FastAPI lifespan startup (fastembed, FlashRank, entities, summaries, structure) — never lazily per query.

### 4. Logging

Stdlib `logging` (no `print`), structured key-value per request: `query`, route, filters, resolved entities, `chunk_ids` (not chunk text), cache hit, `ttft_ms`, `total_ms`, fallback flags.

### 5. Config (`src/config.py` additions)

Extend the existing `Config` class (typed values), plus `Config.validate_rag()` called from the lifespan — fails fast on missing paths/artifacts via `logging`, not `print`.

* Paths: `RAG_DIR`, `VECTOR_DB_DIR`, `ENTITIES_PATH`, `SUMMARIES_PATH`, `STRUCTURE_PATH`.
* Retrieval: `EMBED_MODEL`, `RERANK_MODEL`, `PREFETCH_LIMIT=50`, `TOP_K_FUSED=25`, `TOP_K_RERANK=3`, `RERANK_ENABLED=true`.
* Serving: `LLAMA_SERVER_URL=http://127.0.0.1:8080`, `CTX_SIZE=4096`, `MAX_TOKENS=400`, `CACHE_ENABLED=true`.

**Why** extending the plain `Config` over pydantic-settings: matches the existing idiom, a dozen fields don't earn a new dependency; validation at startup covers the real failure (missing artifacts).

### 6. Dependency deltas (`requirements.txt`)

* Add: `fastembed`, `httpx`. Remove: `litellm`, `sentence-transformers`. Pin: `qdrant-client>=1.10`.

---

## V. Hardware Budget

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

Honest numbers for a 6GB consumer card (prefill ~500–1000 t/s, decode ~30 t/s), replacing the aspirational 1.5s/10s targets:

* Retrieval (embed ~50ms + Qdrant <50ms + rerank 25 ~0.3–0.5s) ≤ **0.6s**.
* Prefill ~2k prompt tokens → **TTFT ≤ 3s**.
* Decode ≤ 400 tokens → **total ≤ 20s** (typical ~15s).
* Cache hit: < 50ms.

*Note: provisional until measured on the actual card; revise once, from data.*

---

## VII. Evaluation

### 1. Retrieval-only (primary tuning tool)

* ~30 labeled `query -> relevant chapter/chunk` pairs; report `recall@5` and `MRR`. Lets retrieval (prefixes, expansion, rerank) be tuned independently of generation — also the tripwire for RR5 (prefix misuse).

### 2. Answer quality

* Three themed sets (Motivation / Philosophy / Ambiguity), 30–50 each.
* Judge (manual or LLM-judge): stays in-persona, grounded in the retrieved chunks, no fabricated events. Grounding is checked against the programmatic Sources, not inline citations.
* Includes the **prompt-composition A/B**: context-in-system ("recollections" framing) vs context-in-user-turn, judged on persona retention + groundedness.

### 3. Latency

* Measure TTFT and total against VI on the real card; revise budgets once from measurements.

---

## VIII. Out of Scope (v2+)

Semantic cache; conversation memory / `interpretive_bias`; interpretive-restraint hedging metric; any ML query classification; Qdrant server deployment; multi-user concurrency.

---

## IX. Residual Risks (summary)

Full detail in `docs/superpowers/specs/2026-07-13-rag-decisions.md`.

* Persona may ignore or parrot injected context (fine-tune never saw context in prompts) — mitigated by the recollections framing, gated by the answer eval.
* The Idiot's heading scheme and BK's book count are verified from the files at impl (`structure.json` counts are the tripwire).
* Latency budgets are provisional until measured.
* FlashRank exact model id and fastembed's nomic prefix behavior are impl-time verifications.
* Entity-map quality depends on the teacher LLM + one human pass; misses degrade gracefully to plain hybrid+rerank.
* ~0.5GB VRAM headroom assumes a headless GPU; compositor on the same card forces `-c 3072`.
