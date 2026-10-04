# Mitya v2 — Architecture

**What this document is:** the shape of the system as it stands after v2 — what the pieces are, what
each does, and what one request touches on its way through. It shows; it does not argue.

**Where the rest lives.** Every choice below was made for a reason, and the reason is in
[`01-overview.md`](01-overview.md)–[`14-unchanged-from-v1.md`](14-unchanged-from-v1.md). Every number
lives in [`EVIDENCE-PLATFORM.md`](EVIDENCE-PLATFORM.md), [`EVIDENCE-MEMORY.md`](EVIDENCE-MEMORY.md)
or [`EVIDENCE-OPTIMIZER.md`](EVIDENCE-OPTIMIZER.md); this document states almost none of them.

**This document is standalone; the tier-2 set is not.** `01`–`14` cover only what v2 changed. Here
the components inherited unchanged from v1 are named and described beside the new ones. Why an
inherited component is the way it is lives in the v1 set on branch `main`: `git show main:docs/v1/…`.

Start here, then [`00-INDEX.md`](00-INDEX.md) for the reading order.

---

## 1. What Mitya is

A question-answering system over Dostoevsky's five novels that answers **in Dostoevsky's voice** and
shows **which chapters the answer was grounded in**, with per-account identity, conversation memory,
and generation served from hosted model APIs behind a two-family gateway.

| | Subsystem | What it is |
|---|---|---|
| **Style** | the persona | The prompt's persona head and the fine-tune it came from. v2 trained no weights and froze the local arm out of the request path: `git show main:docs/v1/FINE_TUNE.md` |
| **Substance** | the RAG layer | Parsed, chunked, hybrid-indexed corpus with a router, retriever, reranker, two offline artifacts, and conversation memory competing in the same budget. See [`RAG.md`](RAG.md) |
| **Surface** | the SPA | A Vue 3 client over the `/chat` SSE endpoint, static-served by the same FastAPI process. Unchanged from v1 |
| **Platform** | the operating layer | Auth, tenancy, admission control, telemetry, versioned config identity, the provider gateway, the CI eval gate, the traffic harness, the optimizer |

Style and substance still share no code and still meet in exactly one place — **the prompt**. What v2
added is a second thing meeting them there: **memory**, arriving as data inside the same token budget
the passages are drawn from.

**The v1 constraint is gone.** Nothing generates on the 6GB GPU. The card still sizes any local-arm
run; it no longer sizes the request path.

## 2. Components

| Component | What it does | v2 |
|---|---|---|
| `src/rag/ingest.py`, `src/rag/names.py` | Structural parse, chunking, dense + sparse embedding, Qdrant upsert; transliteration folding applied to index and query alike | — |
| `src/rag/entities.py` | Loads the curated entity map and expands a query with canonical names and aliases | scan returns occurrence order, not a `set` |
| `src/rag/router.py` | Pure-function routing between DEFAULT and STRUCTURAL | intent keywords moved onto the config artifact |
| `src/rag/retrieve.py` | Dense + sparse prefetch in one round trip, native RRF fusion, FlashRank rerank | — |
| `src/rag/context.py` | Formats the passages block and the Sources list | `expand_spans()` added beside the formatter: merges adjacent served spans and de-duplicates their overlap. **Not reached at the shipped expansion radius of 0**; see §5.2 step 12 |
| `src/rag/generate.py` | Composes the prompt and streams tokens through the gateway | composition C |
| `src/rag/cache.py` | Exact-match answer cache | keyed also on config and corpus version; **bypassed by memory-bearing requests** |
| `src/api/app.py` | FastAPI: SSE endpoint, auth routes, health and readiness, filter tree, static mount | auth, admission, typed errors, the ladder |
| `src/memory.py`, `src/memory_store.py` | Three tiers, one fence, the allocator seam; two persisted tables with retention, caps and per-tenant purge | new |
| `src/summarizer.py` | The post-answer write, on its **own** model and therefore its own quota window | new |
| `src/auth.py`, `src/db.py` | argon2id, opaque server-side sessions, login limits that reject rather than sleep; one stdlib `sqlite3` file, single writer | new |
| `src/limits.py` | Admission control middleware (per-tenant and total in-flight, 429) and the per-`(provider, model)` rate limiter | new |
| `src/gateway/` | Two native provider adapters, a local adapter off the request path, a fake arm, retries, the spend breaker, the fault injector, the degradation ladder | new |
| `src/pricing.py` | Rate rows carrying their pricing page and read date; the three-way spend split — metered, unpriced, unmetered | new |
| `src/config_artifact.py`, `src/config.py` | The content-hashed frozen config artifact, and the facade resolving every knob through the active one | new |
| `src/observability.py`, `src/metrics.py` | One span per stage boundary, feeding trace and metric from the same instrumentation | new |
| `src/guard.py` | The deterministic, model-free input/output guard — the only security layer permitted to gate | new |
| `src/harness/`, `scripts/gate.py`, `src/optimizer.py` | Traffic harness (public API only), CI eval gate, config search and its cost objective | new |

## 3. Offline artifacts

Everything expensive is built once, before the service runs, and looked up at request time.

| Artifact | What it holds |
|---|---|
| `data/rag/structure.json` | The `book/part/chapter` tree; also serves `GET /books` |
| The `dostoevsky` Qdrant collection | One point per chunk, each with a dense and a sparse vector and a structural payload |
| The corpus manifest | Asserts **which embedding model built the index**, checked at startup and in the gate ([VP9](EVIDENCE-PLATFORM.md#VP9)) |
| `entities.json` | Canonical names and aliases per name **per book**, machine-extracted then human-gated |
| `summaries.json` | One neutral summary per chapter, keyed by the same structural triple |
| The frozen config artifact | The versioned serving configuration; its content hash **is** its version |
| The GGUF model | Retained, not retrained, not on the request path |

The service **refuses to boot** on a missing artifact, a bad config pointer, an artifact whose
contents no longer hash to its filename, or an unavailable argon2id.

## 4. The API surface

| Endpoint | Purpose |
|---|---|
| `POST /chat` | `{query, filters?, session_id?}` + session cookie → an SSE stream of the answer |
| `POST /auth/login`, `POST /auth/logout` | Opaque session cookie, rotated on login; `HttpOnly` + `Secure` + `SameSite=Lax` |
| `GET /auth/session` | The current session's identity. Requires an authenticated session |
| `GET /health`, `GET /ready` | Dependency and readiness status. **Readiness is config-only** — "configured and credentialed", not "the provider is reachable" |
| `GET /books`, `GET /metrics`, `/` | The parse output verbatim; the Prometheus scrape; `StaticFiles(frontend/dist)`, mounted **last** |

`/chat`, `/books` and `/auth/session` require an authenticated session; an unauthenticated caller
gets a `401` with a generic body and **no SSE frames**.

The SSE stream is named events with JSON payloads: `token` (many), then `sources`, then `meta`. The
cache path also emits `done`; **the default path emits no terminal event**
([VP31](EVIDENCE-PLATFORM.md#VP31)). Registration is closed and accounts are seeded by a script. The
inbound `X-Tenant-ID` header was **removed**, not supplemented: tenant derives from the authenticated
session and from nothing else.

## 5. The request path

### 5.1 The picture

```
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ BROWSER   Vue 3 SPA (frontend/dist)  —  unchanged from v1                │
  │ fetch + ReadableStream + hand-rolled SSE parser                          │
  └─────────────────────────────────────┬────────────────────────────────────┘
                                        │ POST /chat  {query, filters?, session_id?}   ·   Cookie: session
                                        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ FastAPI   src/api/app.py                                                 │
  │ auth        src/auth.py     opaque server-side session → tenant · 401    │
  │ admission   src/limits.py   in-flight cap, per-tenant and total · 429    │
  └─────────────────────────────────────┬────────────────────────────────────┘
                                        │ authenticated tenant · query · filters
                                        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ retrieval lock   asyncio.BoundedSemaphore(1)                             │
  │ held from here until build_context() returns — never over generation     │
  └─────────────────────────────────────┬────────────────────────────────────┘
                                        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ memory read   src/memory.py · src/memory_store.py                        │
  │ recent turns (process) · semantic profile · episodic digests             │
  │ → memory_bearing?  the predicate the cache below reads                   │
  └─────────────────────────────────────┬────────────────────────────────────┘
                                        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ answer cache   src/rag/cache.py                                          │
  │ exact match on normalized query + filters + config + corpus version      ├──hit──▶ ①
  │ a memory-bearing request neither reads nor writes it                     │
  └─────────────────────────────────────┬────────────────────────────────────┘
                                        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ degradation ladder   src/gateway/ladder.py   next_rung(readiness, hit)   ├──not PRIMARY──▶ ③
  └─────────────────────────────────────┬────────────────────────────────────┘
                                        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ route()   src/rag/router.py    pure function, no model                   │
  └─────────────────┬───────────────────────────────────────┬────────────────┘
            DEFAULT │                                       │ STRUCTURAL ②
                    ▼                                       ▼
  ┌────────────────────────────────────┐  ┌────────────────────────────────────┐
  │ EntityMap.resolve()                │  │ chapter summary lookup             │
  │ canonical + aliases appended       │  │ + dense excerpts, chapter-filtered │
  └─────────────────┬──────────────────┘  └─────────────────┬──────────────────┘
                    ▼                                       │
  ┌────────────────────────────────────┐                    │
  │ retrieve()   src/rag/retrieve.py   │                    │
  │ dense leg + BM25 sparse leg,       │                    │
  │ one round trip, native RRF fusion  │                    │
  └─────────────────┬──────────────────┘                    │
                    ▼                                       │
  ┌────────────────────────────────────┐                    │
  │ rerank()   FlashRank, CPU          │                    │
  │ the retrieval latency budget       │                    │
  └─────────────────┬──────────────────┘                    │
                    └───────────────────┬───────────────────┘
                                        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ build_context()   src/rag/context.py                                     │
  │ (expand_spans() merges adjacent spans: NOT reached at radius 0)          │
  │ passages block  +  Sources list (built in code, never generated)         │
  └─────────────────────────────────────┬────────────────────────────────────┘
                                        │ ◀── retrieval lock released here
                                        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ allocate()   src/memory.py   one token budget, three tiers competing     │
  │ passages admitted whole · memory takes what is left · fenced as data     │
  └─────────────────────────────────────┬────────────────────────────────────┘
                                        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ build_messages()   src/rag/generate.py   composition C                   │
  │ persona → precedence marker → memory block → conversation →              │
  │ reinforcement tail        ·        user turn carries the question only   │
  └─────────────────────────────────────┬────────────────────────────────────┘
                                        │ one chat-completions request
                                        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ provider gateway   src/gateway/                                          │
  │ model id → vendor · rate limiter · spend breaker · retry · fault inject  │
  │ error mapping by meaning: 429 · 413 · 422 refusal · 502 · 503            │
  └─────────────────────────────────────┬────────────────────────────────────┘
                                        │ native API, not the compatibility endpoint
                                        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ hosted model API   two provider families, chosen on divergence           │
  │ the local llama-server arm survives as a subclassed adapter, off path    │
  └─────────────────────────────────────┬────────────────────────────────────┘
                                        │ token stream + usage
                                        ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ SSE out   token* → sources → meta        (default path emits no done)    ├──post-answer──▶ ④
  └─────────────────────────────────────┬────────────────────────────────────┘
                                        │ text/event-stream, same connection
                                        ▼
                                   [ BROWSER ]

   ①  cache short-circuit   The stored answer as one token event, then sources, meta, done. No
                            ladder, router, retrieval, gateway or model. A memory-bearing request
                            never takes this path in either direction.

   ②  STRUCTURAL route      Chapter summary by (book, part, chapter) plus dense-only excerpts hard-
                            filtered to that chapter. No sparse leg, no fusion, no rerank; rejoins
                            at build_context().

   ③  degradation ladder    PRIMARY → CHEAPER → CACHED → RETRIEVAL_ONLY → UNAVAILABLE, and it
                            descends on configuration rather than on outage, because readiness is
                            a config read. See the table in 5.4.

   ④  post-answer write     The turn is appended to the in-process transcript; on the first turn of
                            a new session the previous one is summarized into an episodic digest
                            and profile rows are extracted — in band, in the same admission slot,
                            on the summarizer's own model and quota. A failed write is logged and
                            counted and never fails the turn.
```

### 5.2 The walk-through

1. **Browser → FastAPI.** The SPA `POST`s the query and reads the response body as a stream; it never uses `EventSource`, which cannot POST.
2. **Auth.** The session cookie resolves to a server-side row, re-derived per request, and yields the tenant. No cookie, no row, expired row → 401 before the stream opens.
3. **Admission.** Pure-ASGI middleware takes one in-flight slot, bounded per tenant and in total, released on all six exits including client disconnect; over the cap it **rejects with 429** rather than queueing.
4. **The retrieval lock.** `BoundedSemaphore(1)` is taken, scoped to route → memory read → cache → ladder → entities → retrieve → rerank → context, and released before the messages are built.
5. **Route.** `route()` returns STRUCTURAL or DEFAULT from the query, the filters, the structure map and the summary set. No model, no classifier. It runs first so the cache key can carry the route.
6. **Memory read.** Recent turns from process memory, profile rows and episodic digests from SQLite. Their existence is the `memory_bearing` predicate.
7. **Cache.** Exact match on the normalized query, the filter dict, the config version and the corpus version. A memory-bearing request neither reads nor writes it and takes neither branch of the hit rate.
8. **Ladder.** `next_rung()` reads readiness and the cache-hit fact and returns the serving tier.
9. **Query expansion and retrieval (DEFAULT).** `EntityMap.resolve()` appends canonical names and aliases in occurrence order; one Qdrant round trip carries a dense and a BM25 sparse prefetch under the same hard filter, fused server-side by native RRF.
10. **Rerank (DEFAULT).** FlashRank, a CPU cross-encoder, reads query and passage together and cuts the fused pool to the served spans. It is nearly the whole retrieval latency budget ([VP7](EVIDENCE-PLATFORM.md#VP7)).
11. **Summary lookup (STRUCTURAL).** Path ②, rejoining at context assembly.
12. **Context assembly.** The passages block, each span labelled in its own novel's part vocabulary, and the Sources list deduped on the same triple. The lock is released here. **The merge of adjacent served spans is a separate step and does not run as shipped:** `expand_spans()` lives beside the formatter in the same module, but the call site guards it with `if radius:` on the versioned expansion radius, which the promoted artifact carries at **0**. So the promoted `neighbor_collision_policy: align_only` is inert on the request path, and the passages block contains the reranked hits unmerged, with any shared chunk overlap duplicated. Recorded as a known defect ([`13-decisions-and-open.md`](13-decisions-and-open.md) §2), not as behaviour.
13. **Allocation.** The active policy admits the passages block **whole** and gives the three memory tiers what is left of one token budget, each block fenced and framed as data, with provenance per block.
14. **Prompt composition.** Composition C: persona, a precedence marker naming each block and declaring it data, the memory block, the conversation block, a reinforcement tail. The user turn carries the question only.
15. **The gateway.** One function maps the model id onto a vendor. The rate limiter reserves input plus maximum output against the `(provider, model)` window; the spend breaker prices the worst case; the fault injector wraps the adapter when enabled. The adapter targets the vendor's **native** API.
16. **Generation.** The **first token is pulled before the response body opens**, so a transport failure, a refusal or a size rejection becomes a typed status rather than a hung 200. After the first token the 200 is committed and a failure becomes a terminal SSE `error` event.
17. **Streaming out.** `token` events carry prose only. `sources` follows the last token, built in code from retrieval metadata and never parsed out of the answer. `meta` is emitted last, after the cache write.
18. **Post-answer write.** Path ④, inside the same admission slot.
19. **Back in the browser.** `token` appends, `sources` replaces the turn's source list, `meta` fills a debug row and never gates rendering — the debug row now also carries the rung, the arm and the memory-bearing predicate (`mitya-v2-ui` topic, `status/mitya-v2-ui/STATUS.md`).

### 5.3 What crosses each boundary

| Boundary | What crosses |
|---|---|
| SPA → FastAPI | JSON: query text, optional `book/part/chapter` filter, a session id scoping the transcript; a session cookie carrying identity |
| Auth → the rest of the process | An authenticated tenant string, on a ContextVar, the logging filter, the root span, and an allowlisted set of metric labels under a bounded budget |
| FastAPI → SQLite | Session lookup; profile and episodic reads keyed by tenant; the post-answer writes |
| FastAPI → Qdrant | One search call: two prefetch vectors, one optional `must` filter, a fusion directive. Back: scored points with `{book, part, chapter, canonical_entities, text}` payloads |
| Allocator → prompt | A plan of blocks, each with a token count and a provenance label; memory rendered inside one shared fence string |
| Gateway → provider | One native chat-completions request. Back: a token stream, a usage object, and — on one family — a refusal arriving inside an HTTP 200 |
| FastAPI → SPA | `token` prose, then a `sources` array built from payload metadata, then a `meta` object |
| Every stage → the operator plane | One span per boundary, stamped with tenant, route, config version and corpus version |

### 5.4 The alternate paths

**Cache short-circuit ①.** Taken only for a memory-free request whose normalized query, filters,
config version and corpus version all match a stored entry. The tenant is **not** in the key and
never will be: the cached population is memory-free, and a memory-free population over a shared
corpus is tenant-invariant ([VM9](EVIDENCE-MEMORY.md#VM9)).

**STRUCTURAL route ②.** Summary-intent keyword **and** a resolvable chapter scope, both required; a
resolved scope whose summary is missing falls back to DEFAULT at route time.

**Degradation ladder ③.**

| Rung | Condition | What the client gets |
|---|---|---|
| `PRIMARY` | generation configured and credentialed | The full path above |
| `CHEAPER` | — | **Unreachable by construction**: readiness folds primary and cheaper into one flag, and routing was closed as not-adopted ([VP16](EVIDENCE-PLATFORM.md#VP16)) |
| `CACHED` | generation unavailable, cache hit | The stored answer |
| `RETRIEVAL_ONLY` | generation unavailable, no cache hit | An HTTP 200 with an empty answer body and the source ids. **The client (`mitya-v2-ui` topic) now renders a specific notice for this rung instead of a bare empty turn** — observed under an unset provider credential, `status/mitya-v2-ui/STATUS.md`. The decision to serve 503 instead is recorded and **not implemented** |
| `UNAVAILABLE` | retrieval also unavailable | 503 |

Because readiness is a config read rather than a live probe, **the ladder descends when the
deployment is misconfigured, not when the provider is down**: on a provider outage the request fails
the way it always did.

## 6. Serving arms

**Two hosted provider families** carry the request path, chosen on divergence across streaming shape,
error vocabulary and rate-limit surface, and adapted through each vendor's **native** API. **A fake
arm** makes generation a no-op, and every harness ramp, auth and admission measurement runs there at
zero spend. **The local `llama-server` arm** survives as a subclass of the OpenAI-dialect adapter — a
statement about the wire format, not about a vendor — is never the default, and logs a warning when
selected.

Refusal, transport failure and success are three distinct states in the type system, because one
family returns a refusal inside an HTTP 200. The gateway normalizes **failure vocabulary**, not
response shape: quota → 429, oversize → 413, refusal → 422, transport → 502, spend breaker → 503.

## 7. Identity, tenancy and admission

Identity is an opaque server-side session, so authorization is re-derived per request and revocation
is immediate. Tenancy is threaded onto spans, logs and an allowlisted set of metric labels under an
enforced cardinality budget: a malformed value collapses to `unknown`, an over-budget value to
`other`, and the two sinks are deliberately distinct.

Admission control is a **bound on how many wait, not on how long**. It replaced the unbounded queue
with fast rejection; the retrieval lock is still mutual exclusion, so most admitted requests queue
inside the box. Narrowing that lock to retrieval moved the concurrency knee from **1 to 4**
([VP27](EVIDENCE-PLATFORM.md#VP27)). **Per-tenant fairness is a non-result and stays open**
([VP35](EVIDENCE-PLATFORM.md#VP35)).

## 8. The measurement plane

None of this is on the request path, and all of it is what v2 is for.

| Piece | What it does |
|---|---|
| **Versioned config identity** | A content hash over the frozen, normalized config. `Config` resolves through the **active artifact**, never the environment — an env var cannot switch arms, promoting an artifact does |
| **Telemetry** | One span per stage boundary, feeding trace and metric together; provenance on every eval run — config version, corpus version, git SHA, **whether the tree was dirty**, timestamp, serving model, composition |
| **Operator plane** | Prometheus and Grafana in Compose, dashboard checked in; RED panels plus spend, where spend is three-way — metered, unpriced, unmetered |
| **Eval sets** | Append-only with immutable row ids; two versions compare on their intersection, and denominators are per-family, never pooled |
| **The CI eval gate** | Invariants, cost and sampled quality, with **no hand-set threshold anywhere**, and no judge. It **cannot see grounding**, which was measured ([VP21](EVIDENCE-PLATFORM.md#VP21)) |
| **Harness and optimizer** | Ramps, fault injection, ladder walks and multi-turn replay over the public API only; and a default-deny allowlist over the versioned config searched by an objective that makes **no provider call at all** |

## 9. What the system does not do

Stated here because an unstated absence reads as a pass. Each is argued in
[`13-decisions-and-open.md`](13-decisions-and-open.md).

- **The memory fence does not hold.** The charter's *no instruction authority* invariant is **OPEN**: memory-mediated indirect injection succeeded against live controls by more than one mechanism ([VM11](EVIDENCE-MEMORY.md#VM11)). It is an existence result and never a rate. The typed schema bounds format, not content.
- **Retrieval never sees the conversation.** The query is resolved from the current turn alone; memory reaches the prompt and never the retriever. The repair was built, measured, and **ships off** ([VO8](EVIDENCE-OPTIMIZER.md#VO8)).
- **The gate is a retrieval-and-cost regression tripwire, not an answer-quality gate.** Its quality half ships empty by decision.
- **Memory's value is unmeasured.** Its cost is exact; no instrument here can say what it bought ([VM8](EVIDENCE-MEMORY.md#VM8)).
- **`RETRIEVAL_ONLY` still serves an empty 200.** The 503 is decided and not built.
- **No real traffic exists, at any point.** Every load and session figure is synthetic by deliberate construction.

## 10. Where to read next

The substance layer, stage by stage: [`RAG.md`](RAG.md). Why any of it is the way it is:
[`00-INDEX.md`](00-INDEX.md), then `01`–`14`, with [`14-unchanged-from-v1.md`](14-unchanged-from-v1.md)
for what was deliberately left alone and `git show main:docs/v1/FINE_TUNE.md` for the fine-tune. Any
number, with its unit, date, producer and provenance: the three `EVIDENCE-*.md` logs.
