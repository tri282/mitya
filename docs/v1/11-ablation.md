# 11 — The Ablation, and Its Withdrawal

This document is not a success story and should not be read as one. The ablation was well designed,
cheaply run, and correctly executed; its conclusions were **later withdrawn as noise**. The
withdrawal is the more valuable half, and the mechanism behind it is the transferable lesson.

The narrative order below is deliberate: **what v1 measured → what v1 concluded → what a later
re-analysis found → what survives.** Sections 11.1–11.4 describe the state of belief on 2026-08-16.
Section 11.5 is a correction made afterwards and is labeled as such. Nothing in 11.1–11.4 is
retracted as *history*; what is retracted is the inference drawn from it.

Numbers, arms, and the full delta table are in [E26](EVIDENCE.md#E26); the correction is
[E27](EVIDENCE.md#E27).

---

## 11.1 The question the ablation asked

The earlier evaluations answered **"is retrieval working?"**. They did not answer **"what is each
stage worth?"** — and the stages had very different construction costs. The entity map alone cost
231 teacher-LLM chapter passes plus a human curation gate that cut 625 machine keys to 256
([E10](EVIDENCE.md#E10)), and nothing measured whether it moved recall by a single row. A pipeline
whose most expensive component has no attributed value is a pipeline carrying an unexamined
assumption.

So: absolute numbers into **deltas**. One run, one table, per-stage attribution.

---

## 11.2 Design: what made it cheap, and what the harness forced

Two properties of the design are worth more than the results were.

**The code change was two lines and default-preserving.** `retrieve()` gained a `legs` tuple
defaulting to `("dense", "bm25")` — byte-for-byte today's behaviour, so no caller changed — and the
eval script gained `--arm` and `--json` so arms accumulated into a results file instead of into
terminal scrollback. This was possible because RRF over a single prefetch list is order-preserving,
which makes a one-leg arm a *valid query*, not a degenerate one ([E15](EVIDENCE.md#E15)). An
ablation harness that requires a fork of the production path measures the fork.

**The run cost nothing.** Six arms over the eval set, on CPU, **under ten minutes total, with zero
API spend, no llama-server, and no GPU** ([E26](EVIDENCE.md#E26)). Nothing about the result was
gated on budget or hardware, which removes the usual excuse for not repeating it.

**Six arms.** A `full` baseline reproducing the shipped config; `no-expansion` to price the entity
map; `dense-only` and `sparse-only` to price each retrieval leg against fusion; and two rerank arms
— FlashRank on the raw query as shipped, and on the entity-expanded query, which closed a
carry-forward question from an earlier phase.

**The harness forced a split, and this is a methodological point rather than an implementation
detail.** The eval script scores the fusion leg *pre-rerank*, and only three chunks are reranked, so
rerank is structurally invisible at k=5. The four retrieval arms are therefore measured at k=5
against the headline, and the two rerank arms at **k=3 against their own unreranked baseline**. The
buckets are not comparable and were never compared. Reporting a rerank arm against the k=5 headline
would have been the easy mistake and it was avoided by reading the harness before designing the arms.

**A router check ran on every arm** and matched on all expected-route rows throughout
([E14](EVIDENCE.md#E14)). Routing is arm-independent by construction, so a mismatch would have meant
an arm broke something it had no business touching. This is a control, and it held.

---

## 11.3 What v1 concluded, on 2026-08-16

Three conclusions were written into the design docs at the time. They are recorded here as the
historical position, not as current claims.

- **Hybrid fusion does real but modest work.** The full pipeline beat both single legs; the dense leg
  was worth two rows and the sparse leg one. RRF was judged not to be decoration, but the effect was
  characterized as one to two rows rather than a step change.
- **Entity expansion buys one row of recall while lowering MRR.** It surfaced one document the raw
  query missed and pushed correct documents down the ranking elsewhere. This was framed *at the time*
  as measuring the layer on the wrong workload — only two of the twenty-five rows exercise
  cross-translation folding ([E22](EVIDENCE.md#E22)) — and therefore as an argument for growing the
  eval set, not for removing the layer.
- **One finding was flagged ACTIONABLE:** reranking the raw query lost a row against not reranking at
  all, while reranking the *expanded* query gained one. Both rerank arms improved MRR, suggesting the
  reranker was earning its ~1.96 s ([E28](EVIDENCE.md#E28)) but being fed the wrong string. The
  one-line switch was **not taken**, pending a confirming run, on the explicit grounds that a two-row
  difference sat at the noise floor.

That last hesitation is the important one. The correct instinct was present, in writing, and it was
applied to one conclusion and not to the other two.

---

## 11.4 The rule was written down before the run

Before any arm executed, the plan committed in writing to two things.

**A resolution floor:** at 25 primary rows, one row is worth 0.040 recall, therefore *"any delta
under ~0.08 is one row and is noise"*, and deltas were to be reported as `n/25` rather than as
percentages. **And a pre-commitment to a null result:** *"the delta table is the deliverable whatever
it says … 'measured each stage's contribution and found the entity layer flat at n=25' is a stronger
engineering signal than a number with no attribution."*

Both statements are in `plans/mitya-ablation/plan.md`, dated before the run. That single fact is what
makes section 11.5 a **checkable correction rather than a matter of taste** — the standard the
results failed was the project's own, stated in advance, not one imported afterwards to make a point.

---

## 11.5 LATER CORRECTION — the withdrawal

> **This section is a correction made after v1 closed. It is not what v1 believed at the time.**
> v1 shipped the table and the three conclusions of 11.3 on 2026-08-16 and carried them in its design
> documentation. The withdrawal came out of a subsequent re-analysis.

**Every per-stage attribution delta in 11.3 is withdrawn** ([E27](EVIDENCE.md#E27)). On re-analysis
the deltas sit **below the minimum detectable effect at every reachable sample size** — not merely
unproven at n=25, but unresolvable by any set the project could realistically build. None is
distinguishable from noise. This was found once (register **R20**) and independently reconfirmed
later (register **R72**); those registers live in v2 material outside this document's sources and are
cited as pointers only.

**The arithmetic was on the page the whole time.** The largest delta anywhere in the k=5 table is
0.080 — two rows — and it is the only one that even reaches the plan's own stated floor. Every other
delta in the table, at k=5 and at k=3 alike, is a single row. Applied strictly, **the plan's decision
rule admits nothing.** v1 wrote that only two results cleared the bar "and both barely", and then
reasoned from them anyway. The re-analysis is the same rule applied without the word "barely".

### The mechanism, which is the actual lesson

**No confidence interval was computed on any arm.** Not on the ablation, not anywhere in v1
([E30](EVIDENCE.md#E30)). Without one, a point estimate has no width, and a difference of one row
between two point estimates *looks identical to a result*. The delta table was internally consistent,
correctly computed, and reproducible — and it was still unreadable, because nothing in it carried the
information needed to tell an effect from sampling variation.

This is why a stated resolution floor was not enough on its own. The floor was a scalar rule of thumb
applied by hand, at the end, against numbers already framed as findings; a confidence interval is a
property of each measurement, computed with it, visible whenever it is quoted. One is a discipline
that can be talked past with the word "barely". The other cannot.

The generalization: **an instrument whose resolution is coarser than the effect being claimed
produces confident numbers, not missing ones.** That failure mode is invisible from the inside. It
does not throw, it does not look degraded, and it does not correlate with sloppiness anywhere else in
the run.

### What survives

**Exactly one effect: BM25 on quotation queries** (register **R80**). The sparse leg does measurable
work on queries that quote or near-quote the text — the case where lexical match is doing something
dense similarity structurally cannot. That is the one attribution claim v1's ablation can still
support, and note its shape: it is a *conditional* effect on an identifiable query family, not an
aggregate delta averaged across a heterogeneous set. Aggregates over 25 mixed rows were the wrong
readout for effects that live in subpopulations.

### What the withdrawal cost

**No shipped behaviour.** The ablation never produced a production change that was taken — the single
ACTIONABLE finding was explicitly deferred pending confirmation and was never applied in v1. What
was withdrawn is a *claim*, not a configuration. The pipeline running today is the pipeline that ran
before the ablation.

---

## 11.6 The reading

The project caught its own false positive. It did so because the decision rule was written down
before the run, which made the later re-analysis a check against a fixed standard rather than an
argument about interpretation, and because the arms and the results were kept in a file instead of a
terminal, which made them re-examinable at all.

What deserves to be carried forward is not the delta table — it is gone — but four things the
exercise produced: an ablation harness that costs nothing to re-run, a default-preserving code path
that means the arms measure production rather than a fork, a decision rule committed in advance, and
a precise diagnosis of why the whole thing was under-powered. The correct next move was never to
re-run the arms. It was to grow the instrument and to attach an interval to every number it emits.
