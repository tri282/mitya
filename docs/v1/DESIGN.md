# Mitya v1 — Architecture

**What this document is:** the shape of the system. What the pieces are, what each one does, and
what a single request touches on its way through. It shows; it does not argue. Every choice below
was made for a reason, and the reason lives in [`01-overview.md`](01-overview.md) through
[`12-decisions.md`](12-decisions.md); every number lives in [`EVIDENCE.md`](EVIDENCE.md).

Start here, then read [`00-INDEX.md`](00-INDEX.md) for the reading order.

---

## 1. What Mitya is

A local question-answering system over Dostoevsky's five novels that answers **in Dostoevsky's
voice** and shows **which chapters the answer was grounded in**, running on one consumer desktop
with a single 6.0 GB GPU ([E1](EVIDENCE.md#E1)).

It is three things bolted together at one seam:

| | Subsystem | What it is |
|---|---|---|
| **Style** | the fine-tune | A Mistral-7B-Instruct-v0.3 QLoRA fine-tune, merged and GGUF-quantized, served by `llama-server`. It supplies the voice. See [`FINE_TUNE.md`](FINE_TUNE.md) |
| **Substance** | the RAG layer | A parsed, chunked, hybrid-indexed corpus with a router, a retriever, a reranker and two offline artifacts. It supplies the passages. See [`RAG.md`](RAG.md) |
| **Surface** | the SPA | A Vue 3 single-page client over the `/chat` SSE endpoint, served as static files by the same FastAPI process |

The two backend subsystems share no code. They meet in exactly one place — **the prompt** — and the
consequences of that seam are the most interesting part of the design
([`07-generation.md`](07-generation.md)).

---

## 2. Components

**Style side**

| Component | What it does |
|---|---|
| `src/ingestion.py`, `scripts/build_dataset.py` | Turns the novels into a synthetic first-person Q&A dataset in ChatML |
| `src/training.py` | QLoRA adaptation of the 4-bit base model; holds the canonical persona system prompt |
| `scripts/quantize.sh` | Merges the adapter, converts to an F16 GGUF master, quantizes to three levels |
| `llama-server` | Serves the quantized GGUF over an OpenAI-compatible HTTP API. The only GPU resident |

**Substance side**

| Component | What it does |
|---|---|
| `src/rag/ingest.py` | Per-book structural parse, chunking, dense + sparse embedding, Qdrant upsert |
| `src/rag/names.py` | Name normalization and transliteration folding, applied to index and query alike |
| `src/rag/entities.py` | Loads the curated entity map; expands a query with canonical names and aliases |
| `src/rag/router.py` | Pure-function routing between the DEFAULT and STRUCTURAL paths |
| `src/rag/retrieve.py` | Dense + sparse prefetch, native RRF fusion, FlashRank rerank |
| `src/rag/context.py` | Formats the passages block and builds the deduped Sources list |
| `src/rag/generate.py` | Composes the prompt and streams tokens from `llama-server` |
| `src/rag/cache.py` | Exact-match answer cache over `diskcache` |
| `src/api/app.py` | FastAPI: the SSE endpoint, the health check, the filter tree, the static mount |

**Surface**

| Component | What it does |
|---|---|
| `frontend/src/api/*` | The only network boundary: typed SSE event union, hand-rolled stream parser |
| `frontend/src/composables/*` | `useChat`, `useHealth`, `useBooks` over one plain reactive module |
| `frontend/src/styles/tokens.css` | "The Evening Reading Room": one dim theme, one accent, one scale |

---

## 3. Offline artifacts

Everything expensive is built once, before the service runs, and looked up at request time.

| Artifact | Built by | What it holds |
|---|---|---|
| The GGUF model | the fine-tune pipeline | The served weights, one quantization level of three ([E4](EVIDENCE.md#E4)) |
| `data/rag/structure.json` | the structural parser | The `book/part/chapter` tree over 231 chapters ([E5](EVIDENCE.md#E5)); also serves `GET /books` |
| The `dostoevsky` Qdrant collection | `scripts/ingest_corpus.py` | 3,284 points ([E8](EVIDENCE.md#E8)), each with a dense and a sparse vector and a structural payload |
| `entities.json` | a teacher LLM, then a human review gate | Canonical names and aliases per name **per book** ([E10](EVIDENCE.md#E10)) |
| `summaries.json` | a teacher LLM | One neutral summary per chapter, keyed by the same structural triple ([E12](EVIDENCE.md#E12)) |

The service loads all four at startup and refuses to boot if any is missing
([`08-api.md`](08-api.md)).

---

## 4. The API surface

| Endpoint | Purpose |
|---|---|
| `POST /chat` | `{query, filters?, session_id?}` → an SSE stream of the answer |
| `GET /health` | llama-server reachability, Qdrant point count, artifact load status; 200 or 503 |
| `GET /books` | The parse output verbatim; feeds the book/part/chapter filter UI |
| `/` | `StaticFiles(frontend/dist, html=True)`, mounted **last** |

The SSE stream is a sequence of named events with JSON payloads: `token` (many), then `sources`,
then `meta`. The live path ends by stream close; the cache path also emits `done`. Full contract,
including the failure table, in [`08-api.md`](08-api.md).

v1 is **unauthenticated and single-user**, bound to loopback, with one request in flight at a time.

---

## 5. The request path

### 5.1 The picture

Solid boxes and arrows are v1. Dashed boxes are the three v2 additions, named and not described.

```
                              ┌───────────────────────────────────────────────────────┐
                              │ BROWSER   Vue 3 SPA (frontend/dist)                   │
                              │ fetch + ReadableStream + hand-rolled SSE parser       │
                              └───────────────────────────┬───────────────────────────┘
                                                          │ POST /chat {query, filters?}
                                                          ▼
                              ┌───────────────────────────────────────────────────────┐
                              │ FastAPI   src/api/app.py · Semaphore(1)               │╌╌╌╌╌▶┌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┐
                              └───────────────────────────┬───────────────────────────┘      ┊ v2  auth / session             ┊
                                                          │ normalized query + filter dict   └╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┘
                                                          ▼
                              ┌───────────────────────────────────────────────────────┐
                              │ cache  src/rag/cache.py                               │──hit──▶ ①
                              │ exact match on normalized query + filters, no TTL     │
                              └───────────────────────────┬───────────────────────────┘
                                                          │ miss
                                                          ▼
                              ┌───────────────────────────────────────────────────────┐
                              │ route()  src/rag/router.py   DEFAULT | STRUCTURAL     │
                              └───────────┬───────────────────────────────┬───────────┘
                                  DEFAULT │                               │ STRUCTURAL ②
                                          ▼                               ▼
                              ┌───────────────────────┐       ┌───────────────────────┐
                              │ EntityMap.resolve()   │       │ chapter summary       │
                              │ + canonical + aliases │       │  + 2 dense excerpts   │
                              └───────────┬───────────┘       │    (chapter-filtered) │
                                          ▼                   └───────────┬───────────┘
                              ┌───────────────────────┐                   │
                              │ retrieve()  Qdrant    │                   │
                              │  dense  768-d  → 50   │                   │
                              │  sparse BM25   → 50   │                   │
                              │  RRF fusion    → 25   │                   │
                              └───────────┬───────────┘                   │
                                          ▼                               │
                              ┌───────────────────────┐                   │
                              │ rerank()  FlashRank   │                   │
                              │  25 → 3 chunks        │                   │
                              └───────────┬───────────┘                   │
                                          └───────────────┬───────────────┘
                                                          ▼
                              ┌───────────────────────────────────────────────────────┐
                              │ build_context()  src/rag/context.py                   │
                              │ passages block + deduped Sources list                 │
                              └───────────────────────────┬───────────────────────────┘
                                                          ▼
                              ┌───────────────────────────────────────────────────────┐
                              │ compose  src/rag/generate.py                          │╌╌╌╌╌▶┌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┐
                              │ persona head (verbatim from training) + passages      │      ┊ v2  memory store (conversation)┊
                              │ user turn = question only                             │      └╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┘
                              └───────────────────────────┬───────────────────────────┘
                                                          ▼
                              ┌───────────────────────────────────────────────────────┐
                              │ llama-server  /v1/chat/completions (SSE-streamed)     │╌╌╌╌╌▶┌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┐
                              │ Mitya GGUF q4_k_m · the only GPU resident             │      ┊ v2  hosted provider API        ┊
                              └───────────────────────────┬───────────────────────────┘      └╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┘
                                                          ▼
                              ┌───────────────────────────────────────────────────────┐
                              │ SSE out   token* → sources → meta → close             │
                              └───────────────────────────┬───────────────────────────┘
                                                          │ text/event-stream, back to the
                                                          ▼ browser on the same connection
                                                     [ BROWSER ]

   ①  cache short-circuit  ─  the whole answer as one token event, then sources, then meta,
                              then done. No router, no retrieval, no model.
   ②  STRUCTURAL route     ─  summary lookup by (book, part, chapter). No sparse leg, no
                              fusion, no rerank; rejoins at build_context().

   solid = v1        ╌╌╌ dashed = v2 additions (named only)
```

### 5.2 The walk-through

1. **Browser → FastAPI.** The SPA `POST`s `{query, filters?, session_id?}` to `/chat` and reads the
   response body as a stream. It never uses `EventSource`, which cannot POST.
2. **Admission.** The handler acquires `asyncio.Semaphore(1)`, held across retrieval *and*
   generation, and released in the generator's `finally`.
3. **Cache lookup.** The normalized query plus the filter dict is the key. A hit returns
   `{answer, sources, footer}` and takes path ①.
4. **Routing.** `route()` sees the query, the filters, the structure map and the summary set, and
   returns STRUCTURAL or DEFAULT. No model, no classifier.
5. **Query expansion (DEFAULT).** `EntityMap.resolve()` appends canonical names and aliases for any
   character the query names. The expanded string is what both retrieval legs see.
6. **Retrieval (DEFAULT).** One Qdrant round trip carries two `Prefetch` clauses — the dense nomic
   vector and the BM25 sparse vector, each capped at 50 candidates and each carrying the same hard
   filter — fused server-side by native RRF down to 25 ([E15](EVIDENCE.md#E15)).
7. **Rerank (DEFAULT).** FlashRank, a CPU cross-encoder, reads query and passage together and cuts
   25 to 3.
8. **Summary lookup (STRUCTURAL).** Path ②: the chapter summary plus two dense-only excerpts hard-
   filtered to that chapter, and nothing else.
9. **Context assembly.** `build_context()` formats the passages block, each passage labelled with
   its own `(book, part, chapter)` in that novel's own part vocabulary, and builds the Sources list
   deduped on the same triple.
10. **Prompt composition.** The system message opens with the persona head byte-identical to the
    training prompt, then the passages framed as the author's own recollections. The user turn
    carries the question only.
11. **Generation.** An `httpx` stream against llama-server's `/v1/chat/completions`. The **first
    token is pulled before the response body opens**, so an unreachable or slow model is a 503 or a
    504 with a JSON body rather than a hung 200.
12. **Streaming out.** `token` events carry prose only. `sources` follows the last token, built in
    code from retrieval metadata and never parsed out of the answer. `meta` is emitted last of all,
    after the cache write, so its `total_ms` is truthful.
13. **Back in the browser.** `token` appends, `sources` replaces the turn's source list, `meta`
    fills a debug row and never gates rendering. The turn completes on `done` or on stream close,
    whichever arrives.

### 5.3 What crosses each boundary

| Boundary | What crosses |
|---|---|
| SPA → FastAPI | JSON: query text, optional `book/part/chapter` filter, a session id used only for log correlation |
| FastAPI → Qdrant | One search call: two prefetch vectors, one optional `must` filter, a fusion directive |
| Qdrant → FastAPI | Scored points with `{book, part, chapter, canonical_entities, text}` payloads |
| FastAPI → llama-server | One chat-completions request: system message (persona + passages), user message (question) |
| llama-server → FastAPI | A token stream |
| FastAPI → SPA | `token` prose, then a `sources` array built from payload metadata, then a `meta` object |

---

## 6. Where the 6 GB lands

**One component touches the GPU.** `llama-server` holds the quantized model and nothing else. The
embedder, the BM25 encoder, Qdrant and the reranker are CPU-only, Qdrant runs in local embedded mode
against a directory, and within a request retrieval finishes before generation starts. Contention
does not exist by construction rather than by tuning ([E1](EVIDENCE.md#E1)).

That property is what pays for the rest of the design — and what it costs, in latency and in
rejected options, is [`01-overview.md`](01-overview.md) §2 and [`12-decisions.md`](12-decisions.md).

**Operational consequence worth knowing before running anything:** Qdrant local mode holds an
exclusive process lock on `data/vector_db/`, so the API server and any ingest or eval script cannot
run at the same time ([`04-indexing.md`](04-indexing.md)).

---

## 7. Where to read next

- The substance layer, feature by feature: [`RAG.md`](RAG.md)
- The style pipeline, stage by stage: [`FINE_TUNE.md`](FINE_TUNE.md)
- Why any of it is the way it is: [`00-INDEX.md`](00-INDEX.md), then `01`–`12`
- Any number, with its unit, date, producer and provenance: [`EVIDENCE.md`](EVIDENCE.md)
