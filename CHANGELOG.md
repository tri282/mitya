# Changelog

One entry per version, newest first. Figures carry their evidence anchor: `E<n>` in
`docs/v1/EVIDENCE.md`, `VP`/`VM`/`VO<n>` in `docs/v2/EVIDENCE-*.md`.

## v3 (unreleased, in research)

The intent-conditioned serving question: which requests deserve which structure, and can the system
prove per request that the cheaper structure did not cost quality. No code yet; nothing below is
measured, and every choice waits on a number.

- **Thesis.** Route each request to the cheapest execution path that meets its quality bar, accept
  model output only through mechanical checks, and prove cost on real bills.
- **Kill test first.** Run every candidate path on every labelled row and compare the best path per
  row against the best single path. If that bound is small, routing is not built, and the negative
  is published as the result.
- **Constraints.** Hosted model APIs only, no local GPU, no dedicated or self-hosted serving, nothing
  that bills while idle, prompt-only generation, quality compared against a frontier-class model.
- **Planned first.** Every paid call metered and budgeted before it is sent, summed on one ledger per
  request. Then one batched retrieval re-baseline (chunking, embedder, fusion, rerank, vector store
  mode), with one corpus re-freeze.
- **Then, if the kill test passes.** An input guard, an output acceptance contract, an intent
  cascade, per-intent memory, a read-only tool loop for cross-book questions, and a retrieval cache
  where its mechanism allows.
- **Carried unchanged.** v2's measurement contract: mechanical metrics gate, judge metrics report,
  no number without its denominator, config identity on every run, append-only eval sets.

## v2 (tag `v2`, 2026-09-03)

The delivery question: at a fixed quality bar, what is the cheapest and most reliable way to serve
grounded answers, and can the system prove it stays that way. Documentation: `docs/v2/`.

**Added**
- Measurement contract: versioned eval sets with immutable row ids, a judge with attributed
  variance, MDE stated on every claim (`docs/v2/02-measurement-contract.md`).
- Per-stage telemetry and a content-hashed, frozen serving config; every span, metric and eval row
  carries its version [VP7].
- Provider gateway over two families (Google, Groq) through native APIs, with typed errors and
  pricing rows [VP12]. Model routing simulated and rejected before it was built [VP16].
- CI eval gate, three layers, no hand-set threshold [VP20]; measured unable to see grounding [VP21].
- Traffic harness, fault injection and a degradation ladder; concurrency knee 1 to 4 [VP27].
- argon2id auth, opaque sessions, per-tenant and total admission; p99 at level 4 from 64,405 ms to
  4,429 ms [VP34].
- Three-tier conversation memory in one token budget, with retention and per-tenant purge [VM5].
- Config optimizer over an allowlisted search space [VO1].

**Changed**
- Generation moved off the GPU to hosted APIs; output ceiling 400 to 1024 tokens, recorded as a
  measurement break [VP18].
- Prompt composition C: jailbreak violations 0.5625 to 0.1250 over 16 attempts [VM14, VM20];
  groundedness +0.724 against MDE 0.343 [VM18]. Prompt extraction unmoved at 1/8 [VM15].
- Fused retrieval depth 25 to 10: retrieval 2,121 to 917 ms, recall@5 flat [VO6].
- Entity map scan made order-stable; recall had been bimodal across processes.
- Retrieval baseline re-derived: `recall@5 = 0.7105` (27/38) on the primary family [VP4].

**Retired**
- The fine-tune from the request path (kept as a local arm, never the default).
- `Semaphore(1)` over the whole request; narrowed to retrieval through context assembly.

**Known open**
- Retrieval never sees the conversation (63% of multi-turn turns contentless) [VO8].
- Chunking untested [VO11]. The memory fence does not hold [VM11]. Fairness is a non-result [VP35].
- The ladder descends on misconfiguration, not on outage. Every rate row unmetered; actual spend
  $0.00 [VO3]. No real traffic.

## v1 (tag `v1`, 2026-08-16)

The hardware question: can a grounded, in-character assistant run on one 6GB consumer GPU (GTX
1060). It could. Documentation: `docs/v1/`.

**Added**
- Fine-tune: Mistral-7B-Instruct-v0.3, QLoRA r=32 / alpha=32 on all 7 projections, 3 epochs over
  6,979 synthetic ChatML pairs, merged and quantized to Q4_K_M / Q5_K_M / Q8_0 via `llama.cpp`.
- RAG: 5 novels, 231 chapters, 3,284 chunks (500 tokens, 100 overlap); dense nomic-embed-text-v1.5
  plus BM25, native RRF, FlashRank rerank to 3 spans; entity map of 256 canonical keys and 441
  aliases; summaries for all 231 chapters; Sources list built in code.
- Serving: FastAPI over SSE, llama-server, exact-match disk cache, `Semaphore(1)`.
- UI: Vue 3 + Vite + TS single-page chat client.

**Measured**
- 5.0 of 6.0 GB VRAM at 4096 context, full offload [E1].
- Retrieval ~2.25 s (rerank-bound), TTFT ~7.9 s, end to end ~21.3 s, cache hit ~0.008 s [E28].
- `recall@5 = 0.720` (18/25) as v1 closed, superseded by v2's re-derivation [E23].
- Answer quality capped by the fine-tune, not the pipeline [E25].
- The per-stage ablation was withdrawn: no delta cleared the MDE (`docs/v1/11-ablation.md`).
