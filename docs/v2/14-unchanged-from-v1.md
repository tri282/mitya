# 14 — What v2 deliberately did not touch

This document does one job: convert *"we never wrote that down"* into *"we decided not to change
it."* Everything below is a v1 subsystem that v2 left alone, with the reason it was left and a
pointer to the v1 document that describes it.

**Two of these are not endorsements.** Chunking is **untested, not validated**, and the neighbour
expansion axis was left at zero because it **measured out at zero**. Both are stated precisely below
rather than folded into "unchanged".

**v1 lives on branch `main`.** Read a document with `git show main:docs/v1/<file>`; v1 evidence
anchors are cited as `[E6 (v1)]`.

---

## The corpus and its parse

Five novels, a per-book structural parser producing `book/part/chapter` labels, clean plaintext.
Nothing was added, nothing was re-parsed, and the document corpus stamp `515c707a5d69` never moved
across the whole of v2 ([VP9](EVIDENCE-PLATFORM.md#VP9)). v2's question is about delivery, not about
coverage, and re-freezing the corpus would have invalidated every number measured against it — see
the cascade priced under chunking below.

**Read:** `docs/v1/03-corpus-parsing.md`.

*Not to be confused with the synthetic **session** corpus, which is a v2 artifact and was re-frozen
twice ([VP26](EVIDENCE-PLATFORM.md#VP26)). Two objects, one rule; they never merge.*

## Chunking — untested, not validated

500-token chunks with 100-token overlap, counted with the embedder's own tokenizer `[E6 (v1)]`. v1
recorded these values as **read from source**, never as a measured optimum, and **v2 did not measure
them either**. No chunking number exists in this project.

The ablation was scoped, priced and closed as a **deliberate deferral with the price attached**
([VO11](EVIDENCE-OPTIMIZER.md#VO11)). The embedding cost of three arms is small — about an hour of
local CPU. The expense is elsewhere: a chunking arm is an *index* change, so promoting one re-freezes
the corpus stamp, and **numbers do not compare across a stamp**. That would re-price the token
saving that shipped, re-denominate the optimizer's cost saving, invalidate the judge run behind the
promoted prompt composition, and re-open the published retrieval headline. **The null branch buys a
null cheaply; the success branch bills the entire phase's evidence base.**

The reason recorded for the expected null is explicitly the owner's **domain prior and not a
measurement**: internal-docs RAG has structure to chunk against, while novels are continuous prose
already windowed inside a parsed chapter unit, leaving only a window width over homogeneous text.
That is consistent with a null and is evidence for nothing.

Four re-entry conditions are recorded so the deferral is a drawer and not a silence: the corpus is
re-frozen for an unrelated reason, the domain stops being novels, corpus-manifest work lands and
makes the arms distinguishable, or Qdrant moves to server mode and the arms stop serializing behind
one file lock ([VO11](EVIDENCE-OPTIMIZER.md#VO11)).

**Read:** `docs/v1/04-indexing.md`.

## The embedding model and the index

`nomic-embed-text-v1.5` dense vectors plus BM25 sparse, in one Qdrant collection `[E7 (v1)]`.
Unchanged: an embedder swap is a re-ingest, which is the same corpus re-freeze chunking is deferred
on, and the retrieval instrument could not have arbitrated the result anyway
([VP2](EVIDENCE-PLATFORM.md#VP2)).

What v2 *added* around it is a guard, not a change: the collection recorded its dimension and nothing
else, and the installed embedding library ships many models at that same dimension — so an embedder
swap was loud only when the dimension differed, and a dimension mismatch fails at ingest rather than
at query. **The dangerous direction — ingest with A, serve with B — was exactly the silent one.**
A corpus manifest now asserts the model identity at serving startup and in the gate
([VP9](EVIDENCE-PLATFORM.md#VP9)).

**Read:** `docs/v1/04-indexing.md`, and [`03-telemetry-and-config.md`](03-telemetry-and-config.md)
for the manifest.

## The entity map

678 extracted people narrowed to 256 keys by a human gate `[E10 (v1)]`. The artifact's **contents**
are unchanged. One line of its code was not: the scan returned a `set`, so a multi-entity query's
alias blocks reordered under the process hash seed and changed the expanded query text, its embedding
and the dense ranking. That was a live serving defect and a measurement defect at once, and it is
covered in [`02-measurement-contract.md`](02-measurement-contract.md)
([VP4](EVIDENCE-PLATFORM.md#VP4)).

**Read:** `docs/v1/05-offline-artifacts.md`.

## The chapter summaries

Full coverage over every parsed chapter, LLM-built and human-reviewed `[E12 (v1)]`. Untouched. They
back the STRUCTURAL route, whose recall is near-tautological by design — the router resolves the
chapter and a hard filter finds it — so there was nothing here for a v2 instrument to move
([VP4](EVIDENCE-PLATFORM.md#VP4)).

**Read:** `docs/v1/05-offline-artifacts.md`.

## The router families

Two routes, STRUCTURAL and DEFAULT, decided by a pure function: a summary-intent keyword **and** a
resolvable chapter scope. Unchanged, and v1's argument for it held up under v2's scrutiny — a
deterministic router makes a routing error a readable bug, and every gate run checks route agreement
as an invariant rather than as a statistic.

One relocation, no behaviour change: the intent-keyword set moved out of the router module onto the
versioned config artifact, because a knob the optimizer may search cannot live as a module constant
([VP9](EVIDENCE-PLATFORM.md#VP9)).

Separately, and worth knowing before assuming the router is fine: **retrieval never sees the
conversation**. The query is resolved from the current turn alone, and on the multi-turn corpus most
follow-ups are contentless probes. The repair was built, measured, and shipped **off** because it did
not clear its pre-registered threshold ([VO8](EVIDENCE-OPTIMIZER.md#VO8)) — that is a v2 decision,
recorded in [`10-optimizer.md`](10-optimizer.md), not a v1 inheritance.

**Read:** `docs/v1/06-retrieval.md`.

## Fusion and the rerank stack

Dense and sparse legs, native RRF fusion, FlashRank cross-encoder narrowing the fused pool to the
served spans `[E15 (v1)]`. The stack is unchanged and every knob on it was examined and left:

- `top_k_rerank` is **pinned at 3** on measurement — dropping to 2 buys a fraction of a cent per
  answer and costs hits on a whole family, at flat latency ([VO5](EVIDENCE-OPTIMIZER.md#VO5)).
- `prefetch_limit` is a **no-op in local mode** — zero movement in pool recall, served recall or
  latency across its whole domain, on every family ([VO5](EVIDENCE-OPTIMIZER.md#VO5)).
- `top_k_fused` turned out to be a **latency axis, not a cost axis**, and the optimizer's cost story
  is one knob elsewhere ([VO5](EVIDENCE-OPTIMIZER.md#VO5), [VO7](EVIDENCE-OPTIMIZER.md#VO7)).
- Rerank itself was not re-tuned but was **attributed for the first time**: it is the retrieval
  budget, entirely, at several times the whole retrieval latency target
  ([VP7](EVIDENCE-PLATFORM.md#VP7)). v1's design justification for its cost was wrong by roughly 5×,
  and the correction is a lever rather than a footnote — the narrowing *after* scoring is free, so
  the fused pool size is where latency lives, not the rerank cut.

**Read:** `docs/v1/06-retrieval.md`.

## Neighbour expansion — measured out at zero

Context assembly did change in v2, and it is important to say **which half**.

The proposal was to expand each reranked hit to its neighbouring chunks. It was admitted on an
arithmetic case that turned out to depend on trading the rerank cut down to 1 or 2 — and that knob is
pinned at 3, so the arms were **illegal before they were run**. With the cut fixed the arithmetic
inverts and expansion costs more, which is what the measurement then showed: radius-1 arms cost over
double the tokens, and the aligned one bought a gain that is **zero by construction**
([VO9](EVIDENCE-OPTIMIZER.md#VO9)).

**What shipped is merge-only.** Separating the radius from the merging exposed an arm nobody had
proposed: consolidating *adjacent already-retrieved* spans and de-duplicating their overlap, at
identical recall and coverage on every row, monotonically cheaper with zero rows dearer
([VO9](EVIDENCE-OPTIMIZER.md#VO9)). *Serving two adjacent chunks with their overlap duplicated was
always waste; the expansion framing concealed a defect.* One condition travels with that saving: it
**is** the chunk-overlap dedup, so it is worth nothing at zero overlap.

The expansion radius itself ships at **0**, and the backfill policy ships **off** — its effect sits
below its own detection floor, on a single discordant row, and this data cannot size the set that
would settle it ([VO9](EVIDENCE-OPTIMIZER.md#VO9)).

**Read:** `docs/v1/06-retrieval.md` §7, and [`10-optimizer.md`](10-optimizer.md).

## Also unchanged

- **The Vue SPA.** Explicitly out of scope for every batch. It has a cost: a product decision about a
  degraded serving rung was taken without the client render the rule required, because nobody looked
  ([VP30](EVIDENCE-PLATFORM.md#VP30)). **Read:** `docs/v1/09-frontend.md`.
- **The fine-tune.** Not retrained, not re-quantized, not deleted — v2 excludes weight training by
  charter, and the local arm was frozen out of the request path when serving moved to hosted APIs.
  **Read:** `docs/v1/02-finetune.md`, `docs/v1/FINE_TUNE.md`.

---

**The honest summary of this page:** the retrieval stack barely moved, and that is a result rather
than an omission. Every knob on it was either measured and left, or priced and deferred with the
price on the record. The one that was neither — chunking — says so in its own section.
