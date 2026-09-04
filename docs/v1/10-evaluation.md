# 10 — Evaluation

Two instruments, measuring two different things, built at different times and never merged.
**Retrieval** is measured against hand-labeled gold chapters by a deterministic script. **Answer
quality** is measured by an LLM judge over generated answers. They are separate because they fail
separately: retrieval can put the right chapter in the context window and the model can still
ignore it, and that is exactly what happened.

Every number below lives in [`EVIDENCE.md`](EVIDENCE.md); this document explains what was built,
what was chosen, and what the choices cost.

---

## 10.1 The retrieval eval set

The set is hand-labeled `query → (book, part, chapter)` — one gold chapter per query, no graded
relevance, no multi-label rows ([E22](EVIDENCE.md#E22)). That single-label shape is the most
consequential decision in the whole evaluation, and everything in 10.2 follows from it.

Rows were not written freehand. Each was authored **against a specific `summaries.json` key** and
carries a `note` citing it, so a row asserts something the corpus is known to contain rather than
something the author remembered; character spellings were checked against the project's character
reference. This is the difference between an eval set and a list of questions.

**The families**, chosen so that a failure localizes rather than diffusing into an aggregate:

| Family | What it exercises |
|---|---|
| Plot-event grounding | ordinary dense/sparse retrieval on concrete scenes |
| Entity resolution | the entity map's expansion of a name into its aliases |
| Cross-translation folds | the transliteration folds, on spellings the corpus does **not** use |
| Book-title aliases | the title alias map, via rows carrying an explicit hard filter |
| `STRUCTURAL` routing | the router's summary path, with an expected route asserted |
| Negative controls | that the router stays `DEFAULT` on broad thematic phrasing |

The negative controls are scored and reported, but in **their own bucket, outside the headline**.
Their purpose is to prove a *non*-event: summary-shaped intent without a resolvable chapter scope
must not take the structural path. Folding them into the headline would let a routing property
trade against a retrieval property in one number, which is how a metric stops meaning anything.

The cross-translation rows are deliberate canaries. They are spelled the way a reader of a
non-Garnett translation would type them — `Zosima` against the corpus's `Zossima`, `Mikhailovna`
against `Mihailovna` — so a lost fold or a dropped query prefix surfaces on *exactly those rows*
rather than as a diffuse drop nobody can attribute. Both were checked against the entity map first
to confirm no existing alias already made the fold redundant; a candidate row that would have
tested nothing was rejected on that basis.

**The set's known weakness was stated at the time and never fixed.** It is Garnett-heavy: only two
rows exercise the cross-translation folding that the entire entity layer was built for. Aggregate
recall therefore measures that layer against the wrong workload — a fact that matters again in
[`11-ablation.md`](11-ablation.md). And before the P7 driver fix, the harness called neither the
router nor the hard-filter builder, so the six rows documented as router and alias cases verified
nothing they claimed to verify.

---

## 10.2 Why the headline is recall-shaped

Each query has exactly one gold chapter. Under that labeling, `recall@k` is a **hit rate**: did the
correct chapter reach the top-k at all. That is deliberately the upstream question. A chapter that
never enters the candidate set cannot be recovered by any prompt, any reranker ordering, or any
generation-side fix — retrieval failure is terminal in a way that ranking failure is not.

Precision is not merely uninformative here, it is undefined in any useful sense: with one gold
label, `precision@5` is bounded above by 0.2 and is a deterministic function of recall. NDCG needs
graded relevance the set does not carry. So the choice was not recall-over-precision on the merits
of each; it was recall as the only metric the labeling schema can support.

**What that choice implies, and it is a real cost.** A recall-shaped headline is blind to *where* in
the top-k the gold chapter landed, and rank position is what actually reaches the model — only three
chunks survive into the prompt. It is also blind to the other k−1 results: a pipeline that returns
the right chapter alongside four irrelevant ones scores identically to one that returns it cleanly.
**MRR is carried alongside precisely to cover the first blind spot** — it is the rank-sensitive
companion, and the two metrics disagreeing is informative rather than contradictory. In the ablation
they disagree on the entity layer, one arm gaining a row while losing rank quality.

The harness measures the **fusion leg, pre-rerank**. Since only three chunks are reranked, rerank is
structurally invisible at k=5 and must be measured in a separate k=3 bucket against its own
baseline. **The two buckets are not comparable and were never compared** ([E23](EVIDENCE.md#E23)).

---

## 10.3 The headline moved, and the denominator moved with it

The retrieval headline has been quoted as three different numbers. All three are in
[E23](EVIDENCE.md#E23) side by side; the point of this section is not which is right but what the
disagreement teaches.

**0.731 (19/26)** was the P4 figure, still quoted in the repository README. **0.720 (18/25)** is the
v1 number: after the driver fix, and after the two negative controls were pulled out of the headline
into their own bucket. **0.7105 (27/38)** is a later correction, produced when the eval set had grown.

The three values are within three points of each other, which is exactly what makes them dangerous:
they look like the same measurement drifting slightly. They are not. **The denominator is the tell —
26, then 25, then 38.** The move from 26 to 25 is a change in *what counts as a scored row*: the
negative controls stopped being graded as retrieval. The move from 25 to 38 is a change in *the
instrument itself*: a larger, differently-composed set. Neither is a regression or an improvement in
the pipeline. The pipeline did not change.

The operational rule this produces: **a recall figure quoted without its denominator is not a
metric.** At 25 primary rows one row is worth 0.040 recall, so the entire spread between the highest
and lowest of the three headline values is smaller than three rows of the set they were measured on.
Any comparison across these numbers that does not first reconcile denominators is comparing noise to
noise. This is the same arithmetic that later withdrew the ablation.

---

## 10.4 Answer quality, and the LLM judge

Retrieval says the right text arrived. It says nothing about whether the model used it. Answer
quality is measured on a separate 36-query set spanning three themes — Motivation, Philosophy,
Ambiguity — all open, unscoped and thematic, with no hard filters, because that is the shape of
question a reader actually brings ([E24](EVIDENCE.md#E24)).

**Judge design.** One teacher call per answer scores `persona_retention` (1–5), `groundedness`
(1–5), a `fabricated_events` boolean, and a free-text rationale. Two design points carry weight:

- **Groundedness is judged against the retrieved passages block**, not against citations in the
  answer. The persona is explicitly trained to never mention external sources, so an
  inline-citation check would score zero on every well-behaved response. The judge is therefore
  asked whether the claims are *supported by* the passages, which is the property that actually
  matters and is harder to check.
- **The free-text rationale was kept**, and it earned its place — the qualitative failure mode in
  10.5 came out of the rationales, not the scores.

**The risk, stated plainly: the judge shares a model family with the teacher that generated the
project's offline artifacts.** Both are the same Gemini Flash Lite model. That is a correlated-error
problem in two directions. Any systematic blind spot in the family — a register it finds natural, a
kind of vagueness it does not penalize — is present on both sides of the measurement, and errors
that a genuinely independent judge would catch can pass. Practically, the judge was scoring the
outputs of a student trained on data from a sibling model. This was accepted for cost and quota
reasons under a free tier ([E13](EVIDENCE.md#E13)), and it was never validated: **no
judge-calibration check against human scores was ever run** ([E30](EVIDENCE.md#E30)). The
groundedness scores should be read as internally consistent, not as calibrated.

**The experiment run on this set was a prompt-composition A/B** — passages framed as the author's own
recollections inside the system prompt, versus passages carried in the user turn — 36 queries × 2
compositions, every generation judged. **There was no winner.** The gaps are around a tenth of a
point on a 1–5 scale at n=36, and the composition scoring marginally higher also fabricates
marginally more. The planned "delete the loser" prune was not taken because there was no loser; both
branches remain behind a config flag. That is a null result honestly reported, and it is the second
time in this document that an effect turned out to be smaller than the instrument.

---

## 10.5 The ceiling was the fine-tune, not the pipeline

This is the most consequential finding in v1 and it was **established by measurement and elimination,
not assumed** ([E25](EVIDENCE.md#E25)). The claim is that answer quality was capped by the
fine-tuned model, and that no amount of retrieval or prompt work would have moved it. Four things
established it:

1. **The failure was invariant to the prompt.** Groundedness came in at roughly **2.5 / 5 on *both*
   compositions**. A prompt-plumbing problem moves when the plumbing moves. This did not move — the
   A/B was, in effect, a controlled experiment on the serving layer that came back null, and a null
   result from a well-formed intervention is evidence about where the fault is *not*.
2. **The dominant failure mode was named, not inferred.** The judge's rationales describe answers
   that drop the persona and deliver generic academic meta-commentary on the question instead of
   answering from the passages. That is a *behavioural* signature of the training data, not of
   retrieval: the passages were present and were talked around.
3. **The serving layer was eliminated by trace, statically.** The persona head in the generation path
   is byte-identical to the training script's system prompt, and the GGUF embeds the same chat
   template the fine-tune was trained under. There is no template drift and no persona drift between
   training and serving to blame.
4. **The residual traces back to the training data.** The training answers came from a weak teacher
   pass on an older quantized base. Academic meta-commentary in the student's outputs is the
   teacher's own register, reproduced.

Set against that, retrieval was delivering the correct chapter into the context window roughly seven
times in ten, with the router matching on every expected-route row and complete artifact coverage.
Retrieval was not the binding constraint.

**The consequence, recorded at the time:** the A/B was the wrong experiment for the state of the
system. It measured a prompt seam while the actual constraint sat two layers upstream, in the
training data. Revisiting it is only worthwhile after a better teacher pass and a stronger base
model. One part of the ceiling *was* serving-fixable — stochastic empty completions, addressed with
a temperature floor and a zero-token retry — and that is the exception that shows the rest is not.

---

## 10.6 What the evaluation did not do

Stated here rather than left to inference; the full list is [E30](EVIDENCE.md#E30).

- **No confidence intervals on any metric, anywhere in v1.** This is not a cosmetic gap. It is the
  direct cause of the ablation withdrawal in [`11-ablation.md`](11-ablation.md).
- **The eval set was never grown**, despite three separate notes saying it should be — 27 rows, ~25
  effective, 2 cross-translation rows.
- **No judge calibration** against human scores on any subset.
- **No held-out evaluation of the fine-tune itself** — the finding in 10.5 is diagnostic, not a
  benchmark of the model against its base.
- **Zero rows measure summary answer quality.** The `STRUCTURAL` route has routing rows only.
