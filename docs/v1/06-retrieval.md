# 06 — Retrieval

**What this document answers:** what happens between a question arriving and a prompt being built —
routing, the two retrieval legs, fusion, reranking, and context assembly.

`src/rag/router.py`, `src/rag/entities.py`, `src/rag/retrieve.py`, `src/rag/context.py`, wired in
`src/api/app.py`. Everything in this path runs on **CPU**: the GPU holds the served model and nothing
else ([E1](EVIDENCE.md#E1)), and within a request retrieval finishes before generation starts, so the
two never contend.

```
query
  ├─ route()                    STRUCTURAL or DEFAULT
  ├─ EntityMap.resolve()        query + canonical names + aliases
  ├─ retrieve()                 dense prefetch ─┐
  │                             sparse prefetch ─┴─ native RRF ─ fused top-k
  ├─ rerank()                   FlashRank, fused → 3
  └─ build_context()            passages block + deduped Sources list
```

---

## 1. The router

Two routes, decided by a pure function with no model behind it and no measurable latency
([E14](EVIDENCE.md#E14)).

- **STRUCTURAL** — a summary-intent keyword **and** a resolvable `(book, part, chapter)` scope, taken
  either from the UI filter or from a light "book title + chapter N" pattern in the query, validated
  against both the structure map and the summary set.
- **DEFAULT** — everything else.

**The conjunction is the whole design.** STRUCTURAL is a summary lookup keyed by chapter; intent
without a resolvable chapter has literally nothing to look up. "Summarize chapter 5 of *Crime and
Punishment*" is a lookup. "Summarize Raskolnikov's theory" is a thematic question that chunk
retrieval answers well, and it stays DEFAULT. An OR here would route the second query to a path with
no key.

**Why not a classifier.** The original design listed a learned classifier for this. It would be
strictly worse: with two routes, one of which is only *valid* when a chapter key resolves, the
keyword-plus-scope test is **exact** where a classifier is probabilistic, and it costs nothing to run
and nothing to train. Deterministic routing also means a routing error is a readable bug rather than
a distribution shift.

The route function takes the summary set as an argument rather than only the structure map — a
deliberate signature change, because the rule "a resolved scope with no summary falls back to
DEFAULT" can only be enforced where both artifacts are visible, and route time is that seam
([E14](EVIDENCE.md#E14)). It is latent in practice only because coverage is complete
([E12](EVIDENCE.md#E12)).

Routing was checked on every ablation arm and matched expectation every time
([E14](EVIDENCE.md#E14)) — worth noting less as a quality claim than as a control: an arm that broke
something it should not have touched would have shown up here.

---

## 2. Query expansion

Before either leg runs, `EntityMap.resolve()` appends canonical names and aliases for any character
the query names (`05-offline-artifacts.md`). The expanded string is what both legs see; matching runs
through the transliteration fold, so a non-Garnett spelling still resolves
([E10](EVIDENCE.md#E10), [E11](EVIDENCE.md#E11)).

One asymmetry worth stating: the **reranker is fed the raw query, not the expanded one**. That was
the shipped behaviour, and the ablation flagged the alternative as its single actionable finding —
which was deferred pending confirmation and never applied, and whose delta was later withdrawn as
noise anyway (`11-ablation.md`, [E27](EVIDENCE.md#E27)). It stands as an open question, not as a
tuned choice.

---

## 3. Hybrid retrieval: two legs, and why not one

**Dense alone is not sufficient for this corpus.** The queries people actually ask are heavy in
proper names, unusual transliterations, and near-quotations of the text — exactly the cases where
lexical match is strong and embedding similarity is diffuse. **Sparse alone is not sufficient
either**, because the other large query family is thematic and paraphrased, where no term overlaps
and only the embedding finds anything.

Both legs run as Qdrant `Prefetch` clauses in a **single round trip**, each capped at **50
candidates** ([E15](EVIDENCE.md#E15)), each carrying the same hard filter if the UI supplied one.
The prefetch limits are the real tuning knobs of this stage and were never swept
([E30](EVIDENCE.md#E30)).

Note what the evidence does and does not support here. The ablation's per-leg attribution numbers
were **withdrawn as noise** on later re-analysis; the one surviving effect is that **BM25 does
measurable work on quotation-style queries** ([E27](EVIDENCE.md#E27)). So the hybrid design is
justified by its failure-mode argument and by that one surviving effect — not by the delta table v1
originally read off its own ablation.

**The hard filter is strict and loud.** Book titles resolve through an alias map that **raises** on an
unmapped title rather than passing it through, because a wrong title becomes a filter matching zero
points — an empty result that looks like a legitimate miss. Non-integral part/chapter values are
rejected for the same reason: a string where the payload holds an integer matches nothing, silently,
on both legs at once ([E11](EVIDENCE.md#E11), [E20](EVIDENCE.md#E20)).

---

## 4. Fusion: native RRF

The two candidate lists are fused by **Qdrant's own Reciprocal Rank Fusion**, server-side, in the
same call that issued the prefetches, down to a fused top-k of **25** ([E15](EVIDENCE.md#E15)).

**Why rank fusion rather than score blending.** Cosine similarity and BM25 scores live on
incomparable scales; there is no correct constant to multiply one by to make it commensurate with the
other. The original design carried per-query-type weight tables — hand-tuned magic numbers that
would need re-tuning whenever the embedder, the corpus, or the query mix changed, and that nothing in
this project could have validated at the eval set's resolution
([E23](EVIDENCE.md#E23)). Rank fusion discards magnitude and keeps only ordering, which is the part
that is actually comparable.

**Why native rather than a hand-rolled merge.** Client-side fusion means two round trips, a merge
function to write, and a merge function to test. Native fusion is a query parameter. The cost is that
RRF's internal constant is not exposed — accepted, because the per-leg prefetch limits are the knobs
that matter and the constant is not one this project could have tuned honestly.

A side benefit that paid off later: **RRF over a single prefetch list is order-preserving**, so a
one-leg run is a valid query and not a degenerate one. That is what made the ablation a two-line,
default-preserving change rather than a fork of the retrieval code
([E15](EVIDENCE.md#E15), [E26](EVIDENCE.md#E26)).

---

## 5. Rerank

The fused 25 go to **FlashRank (`ms-marco-MiniLM-L-12-v2`, ONNX, CPU)**, which returns the top
**3** ([E15](EVIDENCE.md#E15)).

**Why a cross-encoder at all.** Fusion ranks by two independent notions of similarity to the query,
neither of which has read the query and the passage together. A cross-encoder does, and 25 → 3 is
exactly the size of problem it is good at: too few candidates to be expensive, too many to hand to a
7B model with a 3-chunk context budget.

**Why FlashRank.** Dependency profile, and the same constraint as everywhere else in this document:
no torch, no CUDA, a small ONNX model that runs on CPU while the GPU stays committed to the served
model ([E1](EVIDENCE.md#E1)). `bge-reranker` and `mxbai` are plausibly better rankers and were not
candidates, because a GPU reranker is not affordable here at any quality.

**Chosen by reasoning, not by a bakeoff.** No reranker comparison was ever run
([E30](EVIDENCE.md#E30)). The same honest caveat as the embedder in `04-indexing.md`: defensible on
constraints, undefended on ranking quality.

### 5.1 The cost, stated plainly

**Rerank is 87% of retrieval latency** ([E28](EVIDENCE.md#E28)). It is by a wide margin the most
expensive thing in the retrieval path, and everything else — embedding the query, both prefetches,
fusion — is a rounding error beside it.

This is a real trade, not an accident: quality bought with wall-clock, on a CPU, deliberately. Two
things about it are worth stating precisely.

First, **the design-time estimate was wrong by roughly 4×** ([E15](EVIDENCE.md#E15),
[E28](EVIDENCE.md#E28)). A dynamic mid-flight rerank abort had been drafted and was cut as machinery
for a problem that did not exist — and the measurement showed the problem does exist. The conclusion
was nonetheless kept: for a single local user, where the generation phase dominates end-to-end time
anyway ([E28](EVIDENCE.md#E28)), a latency governor adds a failure mode to save a second nobody is
waiting on alone.

Second, **the floor is honest**. Cutting rerank cost requires a smaller reranker, a GPU reranker
(impossible under the 6GB budget), or fewer candidates into it. All three were declared out of scope
for v1 rather than pretended away.

There is a kill-switch config flag, but it is static — always on, no runtime governor
([E15](EVIDENCE.md#E15)).

---

## 6. The STRUCTURAL path

STRUCTURAL does not run this funnel. It takes the chapter summary directly, plus **2 dense-only
excerpts hard-filtered to that chapter** ([E15](EVIDENCE.md#E15), [E16](EVIDENCE.md#E16)).

No sparse leg, no fusion, no rerank — because the chapter is already fully identified by the router's
resolved scope. Fusion and reranking exist to find the right chapter; when the filter has already
found it, they can only reorder within it, at the cost of the most expensive stage in the pipeline.

---

## 7. Context assembly and the Sources list

`build_context()` is a pure formatter — it retrieves nothing and decides nothing about ranking. It
takes the chosen points and produces two things ([E16](EVIDENCE.md#E16)):

- **The passages block**, each passage numbered and labelled with its own `(book, part, chapter)`.
  The label uses the per-book part terminology from the corpus metadata, so *The Brothers Karamazov*
  reads "Book 5" and *Crime and Punishment* reads "Part 5" — the structural vocabulary of each novel
  survives into the prompt rather than being flattened.
- **The Sources list**, **deduped on `(book, part, chapter)` with rank order preserved**. Dedup is
  not cosmetic: the top chunks frequently come from the same chapter, and a Sources list showing the
  same chapter three times tells the reader nothing about breadth of grounding. STRUCTURAL lists the
  summarized chapter first.

**The metadata rides through unbroken**, and that is the point of carrying it on every chunk since
indexing (`04-indexing.md`). The same three payload fields that the hard filter matched on are the
fields the label is rendered from and the fields the Sources list carries out of the API. A citation
shown to the user is derived from the retrieved object, not reconstructed from the answer text.

That distinction is the grounding contract: **Sources are programmatic**, built from retrieval
metadata after the stream ends, never parsed out of what the model wrote. The model is never asked to
cite, and cannot fabricate a citation, because it does not produce them
(`07-generation.md`).

One defensive branch: on a missing summary, STRUCTURAL falls back to the DEFAULT rendering rather
than emitting an empty quotation and citing a chapter that contributed nothing
([E16](EVIDENCE.md#E16)).

---

## 8. Fallbacks, and one that hides its own failure

Two, both in the request path ([E20](EVIDENCE.md#E20)):

- **A hard filter matching zero chunks retries once, unfiltered**, and logs the relaxation. A filtered
  question that dead-ends is worse than an answer with wider scope, provided the widening is visible
  in the log and the Sources list.
- **Zero chunks even unfiltered** produces an answer without context, with the Sources list omitted
  and the condition logged.

The honest note attached to the second: it is effectively **unprovable through the UI**. Because the
first fallback relaxes any starving filter and rerank always returns its top-k, no amount of
narrowing from the interface can actually starve the Sources list. The branch is verified by reading
the code, not by exercising it ([E20](EVIDENCE.md#E20)).

---

## 9. What this stage was measured on

The retrieval eval scores **the fused output before rerank** — which means, because the reranked
top-k is smaller than the eval's cutoff, **rerank is structurally invisible in the headline metric**
and had to be measured in its own bucket against its own baseline
([E23](EVIDENCE.md#E23)). The two are not comparable, and quoting them together would be wrong.

The headline figure, its three conflicting values, and the denominator that distinguishes them are in
`10-evaluation.md` and [E23](EVIDENCE.md#E23). Per-stage attribution — what each of these stages is
individually worth — is `11-ablation.md`, and its answer is mostly *unknown*: the deltas were
withdrawn as below the instrument's resolution ([E27](EVIDENCE.md#E27)).
