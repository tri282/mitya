# 12 — Decisions: What Was Rejected, and What Comes Next

A design is defined as much by what it refuses as by what it ships. This document records the
options that were considered and turned down, each with the reason and — more importantly — the
**constraint that forced the reason**. Almost every rejection below traces to one of three
constraints: a **6.0 GB VRAM ceiling** fully allocated to the LLM ([E1](EVIDENCE.md#E1)), a **solo
local deployment** with one user and no operator, and a **static corpus** of five novels that never
changes.

A rejection is not a permanent verdict. Section 12.4 says which of these v2 revisited.

---

## 12.1 Rejected on the GPU budget

The 6 GB card runs the quantized 7B model and nothing else. Every retrieval component is CPU-only,
and within a request retrieval and generation are sequential, so contention does not exist by
construction. Anything that wants VRAM is competing with the model.

**SPLADE and learned sparse encoders.** The strongest available upgrade to the sparse leg, and the
clearest rejection in the project. A neural sparse encoder is a **second GPU consumer against a
budget already fully allocated** ([E9](EVIDENCE.md#E9)). Statistical BM25 via fastembed costs no
VRAM and, with the IDF modifier set on the collection, is a real lexical retriever rather than a
term-frequency approximation. The tradeoff is accepted quality on paper for a component that fits;
under the project's stated priority order, hardware fit outranks goal fit.

**A GPU reranker, or a stronger cross-encoder.** FlashRank was chosen over `bge-reranker` and
`mxbai` on **dependency profile, not measured ranking quality** — no torch, no GPU, a small ONNX
model ([E15](EVIDENCE.md#E15)). This is a rejection made on constraint rather than evidence, and it
should be read that way: **no reranker bake-off was ever run** ([E30](EVIDENCE.md#E30)). Reranking
later turned out to dominate retrieval latency ([E28](EVIDENCE.md#E28)), and the honest options for
cutting it — a smaller reranker, a GPU reranker, or fewer candidates — were all declared out of
scope, the middle one impossible.

**A dynamic rerank fallback**, drafted and cut. A mid-flight abort when reranking overruns a latency
budget is machinery for a problem that was estimated not to exist. The estimate was later measured
wrong by roughly four times — and the decision was *kept anyway*, because on a solo local deploy the
correct response to a slow-but-working stage is to accept it, not to add a governor with its own
failure modes.

---

## 12.2 Rejected as complexity without a corresponding problem

**Sentence-transformers, as the embedding runtime.** fastembed was already required for the BM25 leg.
Serving nomic through it as well means **one library instead of two**, a quantized ONNX CPU path with
no torch inference dependency, and no `trust_remote_code` ([E7](EVIDENCE.md#E7)). The constraint here
is dependency surface on a machine where torch is already present for training and is not wanted in
the serving path.

**Matryoshka truncation** of the dense vector. Truncating dimensions trades retrieval quality for
memory. At roughly three thousand vectors there is **no memory to save**, so the trade has one side.
Full 768 dimensions ([E7](EVIDENCE.md#E7)).

**ML query classification** for routing — DeBERTa, NLI, or logistic regression, all in the original
design. There are **two routes**, one of which is only valid when a chapter key actually resolves. A
keyword-plus-scope test is **exact** where a classifier is merely probabilistic, costs nothing, and
is trivially explainable when it misroutes ([E14](EVIDENCE.md#E14)). A classifier would have been a
probabilistic answer to a question with a deterministic one.

**Weighted score blending** across the dense and sparse legs. Cosine similarity and BM25 scores live
on incomparable scales; blending them requires hand-tuned per-query-type weight tables, which are
magic numbers with no principled way to set them. **Reciprocal rank fusion is scale-free**
([E15](EVIDENCE.md#E15)). Client-side fusion was rejected in turn: Qdrant's native RRF is one round
trip and no hand-rolled merge code, at the cost of an untunable internal constant that the per-leg
prefetch limits already substitute for.

**A semantic cache.** Adding an embedding index so that near-miss queries hit the cache is real
machinery. For a single user whose repeat-query rate is near zero, it optimizes an event that
scarcely happens. The exact-match `diskcache` layer is about fifteen lines, and its purpose is to
establish the cache **boundary** in the architecture rather than to deliver a hit rate
([E18](EVIDENCE.md#E18)). A low hit rate is the accepted outcome, not a defect — though it was never
actually observed. **Redis** was rejected on the same axis: no external service for a solo deploy.

**Inline citations in the answer.** Rejected not for cost but because they are incompatible with the
model: the fine-tune is explicitly conditioned to refuse external references, so citation markers are
out-of-distribution text the persona actively resists. Grounding is instead shown as a
**programmatic Sources list assembled from retrieval metadata** after the stream closes. This also
determined how the judge scores groundedness ([E24](EVIDENCE.md#E24)) — against the retrieved
passages, never against markers in the text.

---

## 12.3 Deferred by scope, not rejected on merit

These were sound ideas priced against a solo local deployment and found to be solving somebody
else's problem.

| Deferred | Why, and the constraint behind it |
|---|---|
| **Qdrant server mode** | Local embedded mode is zero-ops for a static corpus and one process. The cost is real and was accepted: local mode holds an **exclusive process lock**, so ingest and every eval script can only run with the API server stopped ([E8](EVIDENCE.md#E8)) |
| **Multi-user concurrency** | llama-server runs single-parallel and the embedder is not re-entrant, so a `Semaphore(1)` makes the service a **queue by construction**: concurrent submits wait, they do not fail ([E20](EVIDENCE.md#E20)). Correct for one user; the queue depth was never measured because there was never a second submitter |
| **Authentication and tenancy** | v1 shipped **unauthenticated and single-user by design**, with uvicorn and llama-server both bound to loopback and query text reaching local logs only ([E19](EVIDENCE.md#E19)). This is a defensible posture for a local binary and an indefensible one for anything reachable — the decision is a deployment-shape decision, not a security opinion |
| **Conversation memory** | The `session_id` on `/chat` is **log correlation only**. Multi-turn state changes prompt composition, cache keying, and context assembly at once; it was kept out so that the single-turn grounding path could be evaluated cleanly |
| **`PREFETCH_LIMIT` / `TOP_K_FUSED` sweeps** | Tuning, not attribution — explicitly outside the ablation's question ([E26](EVIDENCE.md#E26)) |
| **Chunk-size and embedding-model sweeps** | Each requires a full re-ingest. Never run: **no embedder bake-off and no chunk-size sweep exists** ([E30](EVIDENCE.md#E30)). The only competitive selection v1 ran was for the teacher model ([E13](EVIDENCE.md#E13)) |
| **An interpretive-restraint hedging metric** | Designed, never built. Answer quality was capped upstream of anything it would have measured ([E25](EVIDENCE.md#E25)) |

---

## 12.4 Forward pointers to v2

Stated as one-liners without detail, and deliberately without numbers. Each is a v2 outcome; none of
the reasoning behind them belongs to v1.

- **Serving moved off the 6 GB GPU to hosted model APIs.** The constraint that forced most of 12.1 no
  longer binds the serving path.
- **Authentication and multi-tenancy were added.**
- **Conversation memory was added.**
- **Qdrant server mode remains deferred.**
- **SPLADE remains out.**
- **Prompt injection was measured, and the shipped prompt was rewritten as a result.** v1 measured
  nothing here at all.
- **The ablation withdrawal came out of the v2 re-analysis** — see [`11-ablation.md`](11-ablation.md)
  and [E27](EVIDENCE.md#E27).

Two of those are worth reading together. The GPU move invalidates the *premise* of several v1
rejections without invalidating the *reasoning*; a decision correctly made under a constraint should
be revisited when the constraint lifts, not treated as a permanent judgment. And the ablation
withdrawal is what a documented decision rule buys you: it made a v1 conclusion checkable by a later
pass that had no stake in defending it.

---

## 12.5 Closing the set: what v1 never measured

The full list is [E30](EVIDENCE.md#E30) and it should be read in full, because **an unstated absence
reads as a pass**. The shape of it:

- **The fine-tune was never evaluated as a model.** No held-out evaluation, no perplexity, no style
  comparison against the base Mistral, no comparison across the three quantization levels that were
  built. The claim that it writes like Dostoevsky was never quantified. Given that the fine-tune
  turned out to be the system's binding constraint ([E25](EVIDENCE.md#E25)), this is the largest
  single gap in the project.
- **No component was selected competitively except the teacher.** No embedder bake-off, no reranker
  bake-off, no chunk-size or overlap sweep. The choices in 12.1 and 12.2 are reasoned, and reasoning
  is not measurement.
- **No confidence intervals anywhere**, and no judge calibration against human scores. The first of
  those directly caused the withdrawal in [`11-ablation.md`](11-ablation.md).
- **The eval set was never grown**, despite repeated notes saying it should be — which is the same
  gap seen from the other side.
- **Whole paths were never measured at all:** summary answer quality on the `STRUCTURAL` route,
  entity-map coverage against the human character reference, cold cache-miss end-to-end latency,
  cache hit rate in use, concurrency under load, accessibility.
- **Some artifacts are not reproducible.** The curated entity map exists on disk and is not
  versioned; the human curation gate that produced it cannot be replayed by any script. Parts of the
  regression suite are gitignored, so fixes are committed while the tests proving them are not.

None of these is a defect in what was built. They are the boundary of what can be claimed about it,
and stating that boundary is the last thing a documentation set owes its reader.
