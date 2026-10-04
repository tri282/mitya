# Mitya v2 — The Substance Layer

**What this document is:** the RAG subsystem at feature level, as v2 left it — what each stage does
and what it hands to the next. Why any of it was chosen is
[`01-overview.md`](01-overview.md)–[`14-unchanged-from-v1.md`](14-unchanged-from-v1.md); every number
is in the three `EVIDENCE-*.md` logs. Stages inherited unchanged from v1 are described here too;
their reasoning lives in the v1 set on branch `main` (`git show main:docs/v1/06-retrieval.md`).

For the whole-system view and the end-to-end request diagram, see [`DESIGN.md`](DESIGN.md).

---

## 1. The corpus and the structural parse

**Input:** clean plaintext of the five novels, Constance Garnett throughout, one file per novel.
**The parser** is a rule table with **one handler per book**, because the five novels use five
different heading conventions and no shared regex covers them; it emits ordered structural units, one
per chapter, each carrying its `book`, `part` and `chapter`. **Output:** `data/rag/structure.json`,
the `book/part/chapter` tree, which does three jobs — the parser's own tripwire against a silent
miscount, the body of `GET /books`, and the key space the chapter summaries must cover exactly.

**v2 did not touch any of it.** No novel was added and no parser was changed; the **document** corpus
stamp never moved across the whole of v2 ([VP9](EVIDENCE-PLATFORM.md#VP9)). It is a different object
from the synthetic **session** corpus the harness replays, which was re-frozen twice
([VP26](EVIDENCE-PLATFORM.md#VP26)). One rule, two objects; they never merge.

## 2. The name layer

`src/rag/names.py` sits underneath the entity map and the router, applied **symmetrically** to index
keys and to incoming queries, with the true canonical spelling never overwritten: `normalize()`
casefolds and collapses diacritics, ligatures and separators, `fold()` adds an ordered
transliteration ruleset, `fold_tokens()` matches on token boundaries and never substrings. Book
titles are a separate map, and `resolve_book_title()` **raises** on an unmapped title.

## 3. Offline artifacts

**The entity map — `entities.json`.** For a name appearing in a query, the canonical spelling plus
every alias it is known by, keyed **per name per book**, machine-extracted per chapter and then cut
down by a human review gate. It is used as **query expansion** — canonical names and aliases appended
to the query text before either retrieval leg runs — never as a filter and never as a score boost.
With a hard book filter the matching book's record is taken; with no filter and more than one
candidate, expansion is skipped rather than guessed.

The artifact's **contents** are unchanged in v2. One line of its code is not: the scan returned a
`set`, so a multi-entity query's alias blocks reordered under the process hash seed and changed the
expanded query text, its embedding and the dense ranking. That was a live serving defect and a
measurement defect at once, and fixing it is what made the frozen v1 retrieval baseline reproducible
([VP4](EVIDENCE-PLATFORM.md#VP4)).

**Chapter summaries — `summaries.json`.** One neutral, third-person summary per chapter, keyed by the
same `(book, part, chapter)` triple the chunk payloads carry, at full coverage. It serves the query
family top-k over chunks structurally cannot — *what happens in chapter 5*, whose answer is spread
across a whole chapter. Untouched in v2.

**The corpus manifest — new in v2.** The collection records vector dimension and distance and *not*
model identity, and the embedding library ships many models at the serving dimension — so an embedder
swap is loud only when the dimension differs, and a dimension mismatch fails at ingest rather than at
query. **The dangerous direction, ingest with one model and serve with another, is exactly the silent
one.** The manifest asserts model identity, checked at startup and in the gate, and records which of
its fields were **read** from the store and which **asserted** from configuration
([VP9](EVIDENCE-PLATFORM.md#VP9)).

## 4. Indexing

**Chunking.** 500-token chunks with 100 tokens of overlap `[E6 (v1)]`, windowed with the **embedder's
own tokenizer** rather than a generic one. **Chunks never cross a chapter boundary** — the window
resets per unit — because every chunk has to carry one truthful structural label. Point ids are a
deterministic counter, so a re-ingest of an unchanged corpus overwrites rather than duplicates.

> **These values are untested, not validated.** v1 recorded them as read from source, never as a
> measured optimum, and v2 did not measure them either. **No chunking number exists in this
> project.** The ablation was scoped, priced and closed as a deliberate deferral with four named
> re-entry conditions ([VO11](EVIDENCE-OPTIMIZER.md#VO11)).

**The vectors.** `nomic-embed-text-v1.5` dense, cosine, through **fastembed as quantized ONNX on
CPU** — no torch in the serving process — with the model's asymmetric task prefixes applied by this
code rather than the library, and unconditionally, so under a different embedder they become literal
noise. Sparse is fastembed's BM25: the document side emits term-frequency weights only and the IDF
half is computed server-side, which is why the collection declares the IDF modifier.

**The collection.** One Qdrant collection, `dostoevsky`, in **local embedded mode** against
`data/vector_db/` — no server process. `qdrant-client>=1.10` is pinned, because the Query API and
server-side native fusion below are what that floor buys. Two named vectors per point. Each point's payload —
`{book, part, chapter, canonical_entities, text}` — is the contract with everything downstream: hard
filters read the three structural fields, context assembly reads `text`, and the Sources list is
built from those same three fields, so a citation shown to a user is derived from the object
retrieval matched.

**Operational rule, unchanged and load-bearing:** local mode holds an **exclusive OS file lock** on
`data/vector_db/`, so the API server and any ingest, eval or gate run cannot run at the same time.
Server mode has been deferred repeatedly; the first shape that actually needs it is running chunking
arms concurrently ([VP30](EVIDENCE-PLATFORM.md#VP30), [VO11](EVIDENCE-OPTIMIZER.md#VO11)).

## 5. The query pipeline

**The router.** Two routes, decided by a **pure function** with no model in it. **STRUCTURAL**
requires a summary-intent keyword **and** a resolvable `(book, part, chapter)` scope — taken from the
UI filter or from a title-plus-chapter pattern in the query, and validated against both the structure
map and the summary set. **DEFAULT** is everything else. Intent without a resolvable chapter has
nothing to look up, and a resolved scope whose summary is missing falls back to DEFAULT at route
time. The behaviour is unchanged in v2; its intent-keyword set moved out of the module onto the
versioned config artifact, because a knob the optimizer may search cannot be a module constant.

**Hybrid retrieval.** Both legs run as Qdrant prefetch clauses in a **single round trip**, each
capped, each carrying the same hard filter if the UI supplied one. The dense leg answers thematic and
paraphrased questions; the sparse leg answers proper names, unusual transliterations and
near-quotations — and v2 is where the sparse leg was first shown to carry a callable effect, on a
query family v1's eval set did not contain ([VP25](EVIDENCE-PLATFORM.md#VP25)). The hard filter is
strict and loud: an unmapped book title raises, and a non-integral part or chapter value is rejected,
because either would silently become a filter matching zero points.

**Fusion.** Qdrant's **native Reciprocal Rank Fusion**, server-side, in the same call that issued the
prefetches. Rank fusion, not score blending — cosine and BM25 scores are on incomparable scales, and
RRF keeps only the ordering; over a single prefetch list it is order-preserving, so a one-leg run is
a valid query rather than a degenerate one.

**Rerank.** FlashRank, an ONNX cross-encoder on CPU, reads the query and each passage *together* and
narrows the fused pool to the served spans, fed the **raw** query rather than the expanded one. v2
did not re-tune it and did attribute it for the first time: **it is the retrieval path,
near-entirely** ([VP7](EVIDENCE-PLATFORM.md#VP7)). The narrowing *after* scoring is free, so the
**fused pool size** is the latency knob and the rerank cut is not.

**The STRUCTURAL path** does not run that funnel: the chapter summary directly, plus dense-only
excerpts hard-filtered to that chapter — no sparse leg, no fusion, no rerank, because the funnel
exists to find the chapter and the chapter is already found. It is more than an order of magnitude
faster, which is why any latency or cost comparison must hold the route mix fixed.

**Context assembly.** `build_context()` is a pure formatter: it retrieves nothing and decides nothing
about ranking. It produces **the passages block**, each span numbered and labelled with its own
`(book, part, chapter)` in that novel's own part vocabulary, and **the Sources list**, deduped on the
same triple with rank order preserved, and v2 did not change it.

What v2 *added* is a separate step in the same module, `expand_spans()`, which merges adjacent served
spans and de-duplicates their overlap; it was measured identical to baseline on recall and coverage
row by row and cheaper on 28 of 97 rows, and its policy field was promoted to `align_only`
([VO9](EVIDENCE-OPTIMIZER.md#VO9)). **It does not run as shipped.** The call site guards the step on
the versioned neighbour-expansion radius, and the promoted artifact carries that radius at **0**, so
merging and overlap dedup are unreachable on the request path and the promoted policy is inert.
Neighbour *expansion* itself ships at zero because it measured out at zero
([VO9](EVIDENCE-OPTIMIZER.md#VO9)); the merge underneath it ships promoted and unreached, which is a
different statement and is recorded as an open defect
([`13-decisions-and-open.md`](13-decisions-and-open.md) §2).

## 6. Memory: three tiers in one budget

New in v2. Memory here means three named tiers with different lifetimes and different shapes, **not a
transcript store**:

| Tier | Lifetime | Shape |
|---|---|---|
| **Recent turns** | dies with the session | Verbatim, **process memory only** — never a table. Whole turns only |
| **Episodic** | persisted, retention-bounded | One **typed** digest per finished session: normalized topic slugs, structured chapter references, turn count, end time |
| **Semantic profile** | persisted, retention-bounded | One row per `(tenant, kind, key)`, bounded normalized value, coarse enums |

**There is no free-text column anywhere in the schema** — not a policy against quoting, a schema with
nowhere for a paragraph to go. The bound this buys is stated exactly: it stops a paragraph, **it does
not stop a sentence** ([VM2](EVIDENCE-MEMORY.md#VM2)).

**One budget, and the competition is the architecture.** The three tiers do not get slots; they
compete with the retrieved passages for one token budget, behind a swappable **allocator seam**. The
shipped policy admits the passages block **whole** and gives memory the remainder — passages are
never truncated to make room for memory. A starved control policy exists to make the seam testable,
and **no winner was picked**, because no metric here can tell allocations apart
([VM8](EVIDENCE-MEMORY.md#VM8)).

**Reads and writes.** The tiers are read early, before the cache, because their existence is the
cache-bypass predicate. The write is post-answer and in band: the turn is appended to the process
transcript, and on the first turn of a new session the *previous* session is summarized into an
episodic digest with profile rows extracted alongside, on the summarizer's **own** model and quota
window. HTTP and SSE have no session-end signal, so the next session's first turn is the trigger —
the stated cost being that **a tenant's last session is never summarized**. A failed write is logged
and counted and never fails the turn.

**Retention, caps, purge.** One time-boxed staleness window over both persisted tiers, enforced
lazily on the write path and at read, never by a job. Store caps are separate from the injection
budget, and the distinction is load-bearing. **Purge is a `DELETE`** asserting that no persisted row
mixes two tenants' content — verified on both tables and the process store, complete only under
single-process operation.

> **The fence does not hold.** One fence string frames every injected tier as untrusted data with no
> instruction authority. Measured against a real provider with unfenced controls, memory-mediated
> indirect injection **succeeded by more than one mechanism**, and the charter invariant is **OPEN**
> ([VM11](EVIDENCE-MEMORY.md#VM11)). It is an existence result and **never a rate** — the
> denominators are single-digit and no pass rate for it may be written anywhere. The typed schema
> bounds **format, not content**.

**And memory never reaches the retriever.** It reaches the prompt, for generation, and nothing else.
The query is resolved from the current turn alone ([VO8](EVIDENCE-OPTIMIZER.md#VO8)).

## 7. Prompt composition and the persona/grounding seam

The prompt is where the substance layer meets the persona, and the two are in tension: the fine-tune
the persona head came from was trained on rows that never contained retrieved context, so injected
passages are out-of-distribution for it — and v2 added a second out-of-distribution guest, memory.

**Composition C** is what ships, and the one thing v2 promoted: system message → persona → a
**precedence marker** naming each block and declaring it data → the memory block → the conversation
block → a **reinforcement tail**; the user turn carries the question only. It replaced a composition
that ended its system message with the memory block — the most-attended position holding the
least-trusted bytes — and which was **set-identical to an unfenced control**
([VM14](EVIDENCE-MEMORY.md#VM14)).

**The grounding rule: persona depends on the model; grounding does not.** The Sources list is **never
generated** — it is built in code from retrieval metadata and emitted as its own SSE event after the
token stream ends. The model is never asked to cite, so it cannot fabricate a citation. What that
costs is per-sentence attribution, which does not exist.

**Underneath sits a deterministic, model-free guard**, the only security layer permitted to gate,
because it is free, offline and stable. Two of its three layers are **output-side** — a canary
detecting the prompt leaking, and a persona-break check — and its similarity layer was measured and
demoted, because similarity measures topic and the difference that matters is intent
([VM23](EVIDENCE-MEMORY.md#VM23)).

**Decoding.** A streaming client against the gateway with an answer-token ceiling, a temperature
floor and `top_p` below the server defaults. The ceiling is an **API parameter, not an instruction**,
and raising it from its v1 value was recorded as a **measurement break** enforced in the CI gate
rather than in prose ([VP18](EVIDENCE-PLATFORM.md#VP18)).

## 8. The cache

`src/rag/cache.py` is a thin layer over `diskcache`. The key is the normalized query plus the filter
dict plus the **config version and corpus version**; exact match only, no TTL, no semantic lookup.
The value is `{answer, sources, footer}`, written only for successful non-empty answers.

**A hit short-circuits everything** — no ladder, no router, no retrieval, no gateway, no model —
emitting the whole answer as one `token` event, then the stored `sources`, a `meta`, and `done`.

**A memory-bearing request bypasses it entirely, in both directions.** The tenant is not in the key
and never will be: putting memory state in the key does not reduce the hit rate, **it eliminates it**,
and the only fingerprint that recovers any does so by colliding differing memory states, which is the
leak the key was for ([VM9](EVIDENCE-MEMORY.md#VM9)). Hit rate is therefore reported over the
memory-free population only, never pooled, with the bypassed population on its own instrument. A
semantic L2 layer was built as a probe and **refuted**, and provider prefix caching cannot engage at
this prompt shape ([VP17](EVIDENCE-PLATFORM.md#VP17)).

## 9. The provider gateway and the serving arms

Generation left the GPU. **One function maps a model id onto a vendor**, and it is the only such
place; adding a provider is an adapter module plus a branch there. The streaming signature — an async
iterator of strings plus a caller-owned usage dictionary — is what kept the cutover to one function.

Two hosted provider families carry the path, chosen on **divergence** across streaming protocol,
error taxonomy and rate-limit surface, and adapted through each vendor's **native** API rather than
its OpenAI-compatible endpoint. A fake arm makes generation a no-op for zero-spend measurement, and
the local `llama-server` arm survives as a subclass of the OpenAI-dialect adapter — a statement about
the wire format, not about a vendor — never the default.

The gateway normalizes **failure vocabulary**, not response shape: quota → 429, oversize → 413,
**content refusal → 422**, transport → 502, spend breaker → 503. Refusal is a third state in the type
system beside success and transport failure, because one family returns a refusal inside an HTTP 200,
and a mid-stream refusal becomes a terminal SSE error instead. Around it: a per-`(provider, model)`
sliding-window limiter reserving input **plus maximum output**; retries in two classes sharing
**one** attempt budget, the window closing at the first emitted token; a spend breaker pricing the
worst case; a fault injector wrapping the gateway rather than the client. Rate rows carry their
pricing page and read date, and every row ships unmetered — so every USD figure here is a **list-rate
estimate**, never a bill ([VP14](EVIDENCE-PLATFORM.md#VP14)).

## 10. The operational surface

- **Telemetry.** One `start_as_current_span` per stage boundary, feeding trace and metric from the same instrumentation. The root span carries tenant, route, config version and corpus version, so every answer traces to the configuration and corpus that produced it.
- **Config identity.** A content hash over the frozen, normalized config. `Config` resolves through the **active artifact**, not the environment — an environment variable cannot switch arms; promoting an artifact does. Fields split three ways: *versioned* (changes what a correct answer is), *deployment*, *secret* — and the build asserts no secret can enter the artifact.
- **The CI eval gate.** Invariants, cost and sampled quality, with **no hand-set threshold anywhere** and no LLM judge. It **cannot see grounding**: strip every passage and its mechanical metrics do not move ([VP21](EVIDENCE-PLATFORM.md#VP21)). It is a retrieval-and-cost regression tripwire, and its quality half ships empty by decision.
- **Auth and tenancy.** argon2id, opaque server-side sessions, login limits that reject rather than sleep, and admission control bounding in-flight requests per tenant and in total. Tenant is a metric label only under an enforced cardinality budget.
- **The harness.** Ramps, fault injection, ladder walks and multi-turn replay over a synthetic session corpus that is pre-registered and **unvalidated by choice**, with every figure reported per family and never pooled ([VP26](EVIDENCE-PLATFORM.md#VP26)).
- **Logging.** One structured key-value line per request: query, route, filters, resolved entities, chunk ids (not chunk text), cache hit, timings and the fallback flags.

**Failure behaviour** — fail fast at startup, degrade at request time, never hang. A missing
artifact, a bad config pointer, an artifact that no longer hashes to its filename, or an unavailable
argon2id is a startup failure. At request time: 401 without a session; 429 over the admission cap or
a login limit; 400 on an unmapped title or non-integral filter; a hard filter matching zero chunks
retried unfiltered, and still-zero answered without context; a missing summary falling back to
DEFAULT; a mid-stream death surfaced as a terminal `error` event.

## 11. Where to read next

Why each stage is the way it is: [`00-INDEX.md`](00-INDEX.md), then `01`–`14`, in particular
[`08-memory.md`](08-memory.md), [`09-prompt-security.md`](09-prompt-security.md),
[`10-optimizer.md`](10-optimizer.md) and [`14-unchanged-from-v1.md`](14-unchanged-from-v1.md). The
whole-system view and the request diagram: [`DESIGN.md`](DESIGN.md).
