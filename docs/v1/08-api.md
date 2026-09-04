# 08 — API: endpoints, the SSE contract, concurrency, failure modes

`src/api/app.py`, 320 lines of FastAPI ([E19](EVIDENCE.md#E19)). It is the only process that touches
Qdrant, the embedder, the reranker, the offline artifacts, the cache and llama-server, and it is the
only thing the browser talks to.

---

## 1. The endpoint surface

Four routes, and the fourth is a mount ([E19](EVIDENCE.md#E19)):

| Endpoint | Purpose |
|---|---|
| `POST /chat` | `{query, filters?, session_id?}` → an SSE stream of the answer |
| `GET /health` | llama-server reachability, Qdrant collection presence and point count, artifact load status; **200 or 503** |
| `GET /books` | the `structure.json` tree, verbatim — feeds the book/part/chapter filter UI |
| `/` | `StaticFiles(frontend/dist, html=True)`, mounted **last** |

`/health` is a real dependency check rather than a liveness ping: it reaches llama-server, counts the
Qdrant collection, and reports how many entities, summaries and structure entries are loaded. It
returns 503 when any of those is wrong, which is what lets the frontend show a degraded indicator
instead of only discovering the problem when a query fails.

`/books` serves the parse output directly. The filter UI is therefore always consistent with what was
actually indexed — there is no second copy of the book structure to drift.

**The static mount is last, and that is load-bearing.** Starlette matches routes in registration
order, so a catch-all mount at `/` registered before the API routes would swallow them. The ordering
was verified against the AST rather than eyeballed ([E19](EVIDENCE.md#E19)). The accepted consequence
is that `StaticFiles` **raises at startup if `frontend/dist/` is missing**, making `npm run build`
mandatory before any uvicorn boot — including dev-proxy workflows that do not use the built assets at
all. A guarded mount was rejected: failing loudly at boot beats silently 404-ing every UI route.
A second sharp edge from the same line — `directory="frontend/dist"` resolves against the uvicorn
**process CWD**, so the server must be launched from the repo root.

**v1 shipped unauthenticated and single-user, by decision** ([E19](EVIDENCE.md#E19),
[E30](EVIDENCE.md#E30)). uvicorn and llama-server both bind `127.0.0.1`; query text appears in local
logs only; there is no session, no token, no tenancy, no rate limit. This is a scope decision, not an
oversight: the deployment target is one person on one machine with the GPU already saturated by their
own request. Anything else — auth, tenancy, multi-user concurrency — is v2, and `12-decisions.md`
carries the forward pointer.

---

## 2. The SSE contract

One request produces one stream of named events, each with a JSON payload, so the client has a single
parse path regardless of event type.

**Live path** ([E19](EVIDENCE.md#E19)):

```
event: token   {"text": "..."}     (many)
event: sources {"sources": [...], "footer": "..."}
event: meta    {...}
<stream close>
```

**Cache path:**

```
event: token   {"text": "<the whole answer>"}   (exactly one)
event: sources {...}                            (re-emitted from the stored value)
event: meta    {"cache_hit": true, "ttft_ms": null, "route_name": "CACHE", ...}
event: done    {}
```

**Order is the contract.** `token` events carry only prose. The `sources` event arrives **after** the
last token because the Sources list is built programmatically from retrieval metadata rather than
generated (`07-generation.md` §3) — it is known before the stream starts, but emitting it last keeps
the token stream pure and matches the reading order the UI wants. `meta` is emitted **last of all,
after `cache.set`**, so its `total_ms` is a truthful end-to-end figure rather than a measurement
taken before the work finished.

**`meta`** carries `{route_name, chunk_ids, ttft_ms, total_ms, filter_relaxed, no_context,
cache_hit}` — figures the pipeline already computed and previously only logged. It is always on, with
no request flag, because there is one local user and the query text is already in local logs. Clients
must treat it as **optional and non-terminal**: it must never gate rendering of prose or sources.

**Termination is asymmetric, and this is the contract's one wart.** The live path has **no `done`
event by design** — the client relies on stream close. The cache path emits `done`. A client must
therefore handle both: treat `done` as a terminal marker if it arrives, and treat stream close as
terminal regardless. The frontend does exactly this (`09-frontend.md`).

**Mid-stream failure cannot be an HTTP status.** Once the first token is emitted the 200 is
committed, so a llama-server death mid-answer surfaces as a terminal `event: error` with a detail
string rather than as a hung connection.

**`session_id` is log correlation only.** No conversation memory in v1 — the transcript is client-side
state and is never sent back ([E19](EVIDENCE.md#E19)).

### Why SSE

The traffic shape is one long unidirectional token stream per request, initiated by the client,
terminated by the server, with no client-to-server messages in between. That is exactly what SSE is
for.

- **Against WebSockets:** a bidirectional protocol with its own framing, its own lifecycle, its own
  reconnect semantics and no HTTP status codes for the failure cases — for a channel where the client
  never speaks after the request. It would also lose the pre-stream 503/504 mechanism described in §4.
- **Against polling:** the answer arrives over ~21 s ([E28](EVIDENCE.md#E28)) and the whole point is
  seeing it appear. Polling either wastes requests or adds latency to every token, and needs
  server-side partial-answer state that does not otherwise exist.

The one thing SSE costs here is that the browser's native `EventSource` **cannot POST**, so the
client hand-rolls the parser over `fetch` ([E21](EVIDENCE.md#E21)). That is a real cost — an entire
category of parsing hazards moves into frontend code (`09-frontend.md`) — and it was accepted rather
than restructuring `/chat` into a GET with a query-string payload.

---

## 3. Concurrency: why v1 serializes

`/chat` holds an `asyncio.Semaphore(1)` across **retrieval and generation both**, released in the
generator's `finally` ([E20](EVIDENCE.md#E20)). One request at a time, end to end.

This is not a simplification — it is what the hardware permits. Two independent reasons, either
sufficient on its own:

1. **The GPU is a single 6GB card running the quantized LLM and nothing else**
   ([E1](EVIDENCE.md#E1)), and llama-server runs `--parallel 1`. There is no second slot to schedule
   into. Admitting a second request would produce queueing inside llama-server rather than
   parallelism, with worse observability.
2. **The CPU-side models are not re-entrant.** The fastembed embedder in particular cannot serve two
   concurrent calls, which is why the semaphore covers retrieval and not merely generation.

**Behaviour under load: concurrent submits queue, they do not fail.** The second request waits and
then runs; nothing is rejected. Since one user submits sequentially, nothing ever queues in practice.

Two related decisions fall out of this. Models are loaded **once at lifespan startup** — embedder,
FlashRank, entities, summaries, structure, cache, semaphore — never lazily per query, so the first
request is not pathologically slow and no load races the semaphore. And the semaphore is acquired
*before* the cache is checked, which looks like a defect and is not: with a single sequential
submitter, nothing queues behind a draining stream ([E20](EVIDENCE.md#E20)). It was settled
statically as intentional and recorded rather than left to be re-discovered.

**Gap.** Queue depth and wait time under multiple submitters were **never measured**
([E30](EVIDENCE.md#E30)). `Semaphore(1)` makes it a queue by construction, but no load test exists.

---

## 4. Timeouts and failure modes

**The primary mechanism: prime the first token before opening the response body.** `/chat` pulls the
first token out of the generator *before* returning the `StreamingResponse`. Until that token
arrives, no HTTP status has been committed, so a connection failure can still become a **503** and a
timeout a **504**, with a JSON body — rather than a 200 that hangs and dies silently
([E20](EVIDENCE.md#E20)). This one ordering choice is what makes the failure surface honest.

Transport budgets are connect 5 s, read 120 s ([E17](EVIDENCE.md#E17)); the read timeout is generous
because prefill on this card is genuinely slow ([E28](EVIDENCE.md#E28)). There is no wall-clock
timeout on the whole request beyond that.

| Condition | Client sees |
|---|---|
| llama-server unreachable | **503** with `{"detail": "llama-server unreachable"}`, before any stream opens |
| llama-server times out on connect/read | **504**, same mechanism |
| Unmapped book title, or a non-integral part/chapter filter | **400** — `Config.resolve_book_title` raises, and it is caught rather than becoming a 500 |
| llama-server dies **mid-stream** | terminal `event: error` on an already-200 stream |
| Qdrant collection missing or empty | **startup failure** — the process refuses to boot |
| Hard filter matches zero chunks | retry once **unfiltered**, `filter_relaxed=true` in `meta` and in the log; answer proceeds normally |
| Still zero chunks after relaxing | answer generated **without context**, Sources omitted, `no_context=true` |
| Missing chapter summary on the STRUCTURAL route | silent fallback to DEFAULT, logged |
| Any other exception before the stream opens | semaphore released, exception propagates as a 500 |

**The principle running through that table: fail fast at startup, degrade gracefully at request
time, and never hang.** Artifact problems are structural and cannot be recovered from mid-request, so
`Config.validate_rag()` checks `structure.json`, `entities.json`, `summaries.json` and
`data/vector_db/` at startup and refuses to boot without them. Retrieval problems are per-query and
recoverable, so they degrade and mark themselves in `meta`. Every error path releases the semaphore —
a leaked permit would deadlock the whole service, and this is the reason for the `finally` in the
generator and the explicit releases on each pre-stream raise.

The failure behaviour was exercised on the real card at P6 rather than reasoned about: llama-server
killed produced an immediate 503 with no hung stream and a 503 from `/health`; an unmapped title
produced a 400; the `filter_relaxed` retry was triggered; a cache hit reported `cache_hit=true` with
filter-sensitive keys ([E20](EVIDENCE.md#E20)).

**One failure mode is effectively unprovable through the UI**, and is recorded as such:
`no_context=true` cannot be reached by narrowing the filter, because a hard filter matching zero
chunks relaxes and retries unfiltered, and rerank always returns its top-k. It is covered by code
reading only ([E20](EVIDENCE.md#E20)).

---

## 5. Logging

Stdlib `logging`, no `print`, one structured key-value line per request: query, route, filters,
resolved entities, `chunk_ids` (**not** chunk text), cache hit, `ttft_ms`, `total_ms`, and the
fallback flags ([E20](EVIDENCE.md#E20)). Chunk *ids* rather than chunk text keeps the log
proportionate while still making any answer reconstructible against the index — which is what made the
latency numbers in [E28](EVIDENCE.md#E28) recoverable from the server log rather than requiring a
purpose-built harness. The `meta` event is the same data, handed to the client instead of only to the
log.
