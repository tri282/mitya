# 13 — Decisions and open items: what v2 refused, what stays open, the honest headline

**Delta.** New in v2. v1 shipped a system; v2 spent most of its effort deciding what *not* to build,
and the refusals are the more defensible half of the work. Each refusal below names the reason and the
constraint that produced it, because *"we did not build it"* is not a finding — **"we did not build
it, here is what it would have cost, and here is why the expected result did not justify that"** is.

Numbers live in the three evidence logs and are cited by anchor.

---

## 1. What v2 refused

### Routing — rejected on cost, before it was built

Three routing families were simulated offline over the recorded cost matrix, with the classifier's own
call cost and the cascade's wasted cheap-tier calls charged to their own totals. **Every family costs
more than always sending traffic to the cheapest arm**, and not marginally: the best routed option is
**74% more expensive** than the trivial policy ([VP16](EVIDENCE-PLATFORM.md#VP16)). No implementation
was needed to learn that.

Two things make this stronger than an arithmetic result. **The assumed hierarchy is inverted** — the
cheaper arm here is the *larger* model, because one vendor's rate card is cheaper per token and the
other's completion leg dominates a long-form answer. There is no cost/capability ladder to climb.
And routing pays **only if the expensive arm buys something measurable**, which this project's
instruments were already known to be unable to show; under non-inferiority, an arm that is not
distinguishably better is not worth paying more for, so routing's premium buys nothing *by
construction*.

Stated so it is not over-read: the genuinely expensive tier was never priced — its daily request quota
would need over a week for a single pass — so the conclusion is *routing is not adopted among the arms
measurable on the free tier*, never *routing never pays*.

### Per-tenant cache keying — closed on a measurement

The plan said tenant-key the answer cache; the charter said the opposite; the code carried a comment
saying a third thing. The contradiction was resolved by measuring what a memory-aware key does to the
hit rate. **A per-session memory fingerprint takes it to exactly zero**, against a baseline above a
half on the same corpus ([VM9](EVIDENCE-MEMORY.md#VM9)) — because the cache's entire measured value
lives in duplicate follow-ups *across* sessions, which is precisely what any session-scoped key
separates.

The middle ground is a leak rather than a compromise: a bucketed fingerprint recovers hit rate **by
mapping differing memory states onto one key**, so its hit rate and its cross-tenant collision rate
are the same number, and the curve has no knee. **Decided: memory-bearing requests bypass the cache
entirely and the tenant never enters the key.** The per-tenant cache was never built.

### Distillation — closed with a cost curve, not a benchmark

At a 24/7 rented instance, USD per answer is `rate ÷ answers-per-hour` — **seconds per answer cancels
out of the expression entirely**, so a faster model does not change the figure by one cent. Break-even
against the cheapest hosted arm is **1,630 answers/day**, over sixteen times this deployment's traffic
([VO12](EVIDENCE-OPTIMIZER.md#VO12)). Distillation's only lever is the saturation ceiling, and this
project has no capacity problem: **it is a capacity decision, not a cost decision.**

The falsifier is named rather than hidden. The cancellation depends on 24/7 rental; under per-second
billing with idle unbilled it does not happen, and local serving would beat the cheapest hosted arm
immediately at any traffic with no distillation at all. That regime was not entered because per-request
cold load is priced nowhere in this project — recorded as the open assumption, not resolved.

### The chunking ablation — deferred with the price attached

Running three chunk-size arms costs roughly an hour of local embedding and about a hundred megabytes
of collections; that is the cheap half. **The expensive half is succeeding.** A chunking change is an
index change, so promoting an arm re-freezes the corpus stamp — and the published retrieval headline,
the phase's cost saving, the groundedness comparison, the retrieval-blindness rate and the merge-only
token saving are all denominated in the current index ([VO11](EVIDENCE-OPTIMIZER.md#VO11)). *The null
branch buys a null for an hour; the success branch bills the entire phase's evidence base.*

The expected result was pre-registered as null, and the checkpoint's own framing — that a null is a
success of the method rather than a wasted budget line — **holds only when the budget line is the only
cost**. It is not, here. Deferred past v2 with four named re-entry conditions, so it is a drawer rather
than a silence.

### Neighbour expansion — measured at zero, and one arm nobody proposed promoted instead

The candidate entered on an arithmetic case that assumed a rerank depth the project had already pinned
on evidence, so **the arms were illegal before they were run**: with depth held, the arithmetic
inverts and expansion costs more rather than less. Measured, radius-1 expansion bought **+118% tokens
for a gain that is zero by construction** ([VO9](EVIDENCE-OPTIMIZER.md#VO9)), and narrowed the served
material while doing it.

The useful finding came out of the same run and nobody had proposed it: the expansion radius and the
merging of adjacent spans were one switch, so **merge-without-expansion was unreachable**. Separated,
it is identical to baseline row by row and strictly cheaper on every row that changes — a monotone
improvement, exact and counted, with no MDE to clear. It was promoted. Two conditions travel with it:
the saving **is** chunk-overlap deduplication, so it is quotable only with the overlap value attached
and is worth nothing at overlap zero; and **the request path does not reach it**, because the
separation landed in the context module and not at the call site that still guards the whole step on
a non-zero radius ([VO9](EVIDENCE-OPTIMIZER.md#VO9)). See the open item below.

### Qdrant server mode — deferred again

Deferred at CP4 on a measured trigger: the local-mode lock was established to be an **OS file lock and
therefore cross-process**, so it forbids a second *process* opening the store and does not constrain
retrieval inside one worker ([VP30](EVIDENCE-PLATFORM.md#VP30)). The phase-completion trigger did not
fire either — no run was lost to the lock. Deferred again at CP5, and again after.

It is worth recording that the deferral finally met the shape that needs it: three chunking arms
cannot be built concurrently with anything else, because separate collections in one local-mode
directory share the one lock. That is the first case where server mode is the blocker rather than a
convenience, and it is one of the chunking ablation's re-entry conditions.

### SPLADE — still out, and the rationale had aged

Excluded on a 6GB hardware constraint. The audit found the rationale had **outlived its premise in
part**: v1's own design spec cut it as YAGNI in a list with five other components, no measurement of
its footprint on this machine exists, and the contention half of the hardware argument was retired for
the serving path when serving left the GPU ([VO15](EVIDENCE-OPTIMIZER.md#VO15)). **It stays out**, and
the honest statement is that it is out on an unmeasured constraint plus a scope decision, not on a
measurement. Three sibling rationales were re-checked in the same pass; one came back clean, two were
narrowed. **None of the four licenses new work.**

---

## 2. What stays open

Plainly, and without softening. An unstated absence reads as a pass.

- **Fairness (G-P10).** Two mechanisms built, one retired because it could never fire, the other
  fires and **cannot be evaluated** — the whole system serves too few requests per run for a share
  ratio to be anything but noise ([VP35](EVIDENCE-PLATFORM.md#VP35)). What is delivered is **admission
  control**, which works: the unbounded queue's tail was replaced by fast rejection. Per-tenant
  fairness against a heavy neighbour is real; the contest between everyone else for the global slots
  distributes by arrival time, which under contention is arbitrary. Open in **both** directions, and
  blocked on sizing the admission gate to the measured throughput knee — a throughput decision, not a
  tuning nudge. Inherited unchanged through three phases.

- **The §IV memory-instruction-authority invariant.** *Injected memory is fenced and framed as
  untrusted data with no instruction authority* is **not satisfied**. Memory-mediated indirect
  injection succeeded against validated controls by more than one mechanism — writing hostile but
  schema-valid rows, and obeying a planted row's instruction through the fence
  ([VM11](EVIDENCE-MEMORY.md#VM11)). It is an **existence result and never a rate**: the denominators
  are single digits, and a four-attempt set finding three landings is more likely understating the
  surface than overstating it. The typed schema bounds **format, not content** — it stops a paragraph,
  not a sentence. Nothing in the later prompt work narrows this: those attack rows carried no passages
  and no planted memory rows, so they measure a different surface.

- **O4, and the absent multi-turn metric.** The allocator seam was built and the winner deliberately
  not picked, because the metric that would pick it does not exist. The candidate metric was **rejected
  by its own pre-registered test** — it moved by zero on every scored family against an extreme
  starvation arm, and it is **arm-invariant by construction**, since retrieval is issued on the query
  alone and no allocation policy can move a metric scored on retrieved chapters
  ([VM8](EVIDENCE-MEMORY.md#VM8)). The consequence is stated without hedging: **if memory adds nothing,
  nothing in v2 would detect it.** The later optimizer phase was explicitly forbidden to manufacture an
  allocation-quality objective to fill the hole.

- **`quality_1b` and the composed quality gate (G-B).** The gate's quality half **ships empty and
  cannot block** ([VP20](EVIDENCE-PLATFORM.md#VP20), [VO14](EVIDENCE-OPTIMIZER.md#VO14)). This is not
  a defect but a decision nobody has taken, and the measured options are all bad: the mechanical
  candidates cannot gate (their detectable effects are far larger than the movements they would need
  to catch), the judge is the only instrument that sees grounding, and charter policy bars a judge from
  CI. Choosing a composition means weighting grounding against persona, which is exactly what is left
  open. **The gate is a retrieval-and-cost regression tripwire, and after measurement it cannot be
  described as an answer-quality gate**: stripping every passage — a certain, total regression — moves
  nothing CI can see, and costs about a third less ([VP21](EVIDENCE-PLATFORM.md#VP21)).

- **Retrieval blindness, shipping unrepaired by decision.** Retrieval resolves the current query
  against client-supplied filters and nothing else; recent turns reach the prompt and never the
  retriever. On the session corpus **63% of multi-turn requests retrieve on a contentless string**
  ([VO8](EVIDENCE-OPTIMIZER.md#VO8)). The repair — prepending prior *questions* to the retrieval query
  at near-zero token cost — **fires and does not carry**: it moved the organic families and fell well
  short of the threshold that was fixed before the run on the family authored to test it. It ships at
  zero, the field stays versioned, and the dilution hypothesis that would explain the gap is carried
  forward as a named hypothesis rather than a result. This is the largest user-visible defect the final
  phase found, and shipping it is a deliberate call on a measured magnitude.

- **Chunking, untested.** 500/100 was read from source in v1 and never measured. **No chunking number
  exists in this project** ([VO11](EVIDENCE-OPTIMIZER.md#VO11)).

- **The promoted merge-only saving is not being realised, and the promoted policy field is inert.**
  The library separated radius from merging; the request path did not. It reads the radius and
  guards the only call to the expansion step on it, so at the promoted radius of zero the merge
  never runs and the promoted `align_only` policy changes nothing served
  ([VO9](EVIDENCE-OPTIMIZER.md#VO9)). Three code paths now disagree about whether adjacent served
  spans are merged: the serving path does not, the fence and benign evaluation harness does
  unconditionally, and the quality harness never calls the step at all. This is the project's own
  *harness runs a pipeline production does not run* pattern
  ([VP4](EVIDENCE-PLATFORM.md#VP4)) recurring at the very end of the phase. **Found by reading the
  call site against the artifact, after the phase closed. Recorded, not repaired.**

- **The `top_k_fused` promotion has no promotion record.** The serving artifact carries 10; the
  decision that accepted 10 states that nothing is promoted by it; the pointer move that carried it
  is Batch H's, whose own record is about merge-only. The value is not in dispute, since the
  artifact says what is served, but the *event* is undocumented, and this set states that rather than
  inventing a rationale for it ([VO1](EVIDENCE-OPTIMIZER.md#VO1)).

- **Smaller, carried, and each load-bearing somewhere.** The `retrieval_only` rung's product decision —
  users get a 503 rather than the empty HTTP 200 it serves today — is **decided and not built**, with
  two riders unsettled ([VP30](EVIDENCE-PLATFORM.md#VP30)). Pool recall is computed by the eval harness
  and **never read by the gate**, though it is the guard the optimizer's own audit specified. The
  determinism baseline store has no retention policy while the only regression class the gate reliably
  catches depends on it. The default generation path emits **no terminal event**, so a client cannot
  distinguish a completed answer from a truncated one. The memory budget is **not enforced** — the
  allocator charges raw turn text and emits fenced text, and the overshoot is largest as a fraction
  exactly where a cost search would push. The corruption check is labelled as though it exercises
  concurrency and does not.

- **One disagreement this set does not resolve.** The groundedness MDE at n=36 is published **three
  ways** — a closed form on the defective 36 cells, a closed form on the clean 26, and a paired
  bootstrap on the same 36 ([VP5](EVIDENCE-PLATFORM.md#VP5)). The bootstrap-versus-normal gap between
  the two 36-cell readings is a **method** difference and explicitly not a disagreement. The gap to the
  26-cell reading **is** one, and it is a **denominator change**, not a method change. **The final
  phase never adjudicated it**, and the stale value is mapped across documentation as an open tidy-up.
  It is presented here as an open item, not as resolved.

---

## 3. The honest headline

**v2 cut the cost of a served answer by a measured 15.6%, held out of sample, and did not promote it**
([VO7](EVIDENCE-OPTIMIZER.md#VO7)). The saving generalizes almost exactly — and the sentence that must
travel with it is *not* that the optimizer generalized well: the search had **no choice to make**. One
axis ranked on cost, and it is monotone, so a single-axis monotone search has no interior optimum and
cannot overfit. Those two claims produce the same number and are not the same claim.

It is unpromoted because **no quality verdict exists for it** — the blocking guard is invariant to
every axis that moved. This is the charter's own named failure mode arriving on the first real run: an
optimizer minimizing cost under an insensitive guard is structurally incentivized toward invisible
degradation, and it found the cheapest thing the gate cannot see.

**What promoted for a quality reason is a prompt.** Composition C cut injection violations by a factor of four and came
back **superior** on groundedness, well outside that run's MDE
([VM18](EVIDENCE-MEMORY.md#VM18)) — after the measurement showed **the shipped prompt had no fence at
all**: the shipped arm is *set-identical* to an unfenced control, the same nine of sixteen attempts
landing on both ([VM14](EVIDENCE-MEMORY.md#VM14)). Removing the prohibiting sentences changes nothing
because they were never doing anything.

**The two results point opposite ways on cost and are not netted.** The promoted persona is 115
tokens against the 52 it replaced, so the prompt that shipped costs about **+63 assembled prompt
tokens per request** ([VM20](EVIDENCE-MEMORY.md#VM20)), while the −2,072-token saving measured in the
same phase was not taken. Each figure is exact on its own arm; **no single number combines them**, and
this set does not manufacture one.

Alongside it, the alternative headline the platform work earned: **generation stopped being the
bottleneck and retrieval became it.** Reranking is the overwhelming majority of the retrieval path,
reproduced by three independent instruments across two machines, and it is the largest measured
inefficiency in the system.

### Three compressions this evidence forbids

| compression | why it is false |
|---|---|
| *"78% more secure"* | The reduction is **entirely one attack family**. Prompt extraction is **unmoved at 1/8 on the control, on the shipped arm, and on the promoted arm** ([VM15](EVIDENCE-MEMORY.md#VM15)). A single-family result reported as a system property. |
| *"Memory pays for itself"* | **Unmeasurable here by construction.** The cost is exact; the value has no instrument ([VM8](EVIDENCE-MEMORY.md#VM8)). |
| *"Chunking is fine at 500/100"* | **Untested, not validated.** The ablation was never run ([VO11](EVIDENCE-OPTIMIZER.md#VO11)). |

Two more that belong beside them: *"the fence blocks a third of attacks"* is not a statement the
evidence supports — the denominators are single digits and the result is an existence claim
([VM11](EVIDENCE-MEMORY.md#VM11)); and no starvation ratio predating the driver correction should be
quoted at all ([VP35](EVIDENCE-PLATFORM.md#VP35)).

### What may not be merged

- **Quality evidence on one serving model against a promoted config serving another.** The final
  quality leg ran on `3.1-flash-lite` after a quota miss forced a re-run; the promoted config serves
  `3.5-flash-lite`, and the security evidence is on `3.5`. Each comparison is internally valid — same
  arms, same model, same judge, same run. **Its transfer to the served model is an assumption, not a
  measurement: one claim each, never one claim together** ([VM20](EVIDENCE-MEMORY.md#VM20)).

- **Numbers across the session-corpus re-freezes.** The **session** corpus was re-frozen twice
  (`4155c97b0a2b` → `49b525f2a131` → `be54610a7f78`) and numbers do not compare across a stamp
  ([VP26](EVIDENCE-PLATFORM.md#VP26)). **These are two different objects under one rule**: the
  **document** corpus manifest `515c707a5d69` never changed across v2, and a reader who merges the two
  will conclude the vector index moved when it did not. The session re-freezes are cited as the
  *precedent* for what a document-corpus re-freeze would cost — the precedent is sound, the objects are
  not the same.

- **Cost figures across the `max_tokens` 400 → 1024 break.** Declared a measurement break, and the
  gate **enforces it** rather than trusting prose: a cost comparison between arms differing on that
  ceiling returns an error naming it ([VP18](EVIDENCE-PLATFORM.md#VP18)). The earlier per-answer prices
  are **superseded, not contradicted**. Arm-versus-arm comparisons at the same ceiling survive; the
  comparison against the frozen local baseline does not, because that baseline rarely reached the
  ceiling and the hosted arms reach it almost always.

- **The two prompt-token figures inside the optimizer phase** are the same config under two fixtures,
  before and after the fixture recalibration ([VO13](EVIDENCE-OPTIMIZER.md#VO13)). They do not compose.

---

## 4. The one-line summary

**v2's defensible claims are an exact cost saving nobody promoted, a prompt that measurably replaced a
fence that was doing nothing, a gate that provably cannot see the thing it is most often assumed to
check, and a set of refusals each priced before it was refused.** Its open items are named in the same
places its results are, at the same volume, because the project's own rule is that **an absence which
is not printed reads as a pass.**
