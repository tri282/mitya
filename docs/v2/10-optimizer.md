# 10 — The optimizer: the search space, the objective, and what promoted

**New in v2.** v1 had no optimizer, no config search and no automated promotion path. CP7 added one,
ran it, and promoted almost nothing from it. That is the document.

Every number below lives in [`EVIDENCE-OPTIMIZER.md`](EVIDENCE-OPTIMIZER.md) (`VO1`–`VO16`) and is
cited by anchor. This file argues why the thing is shaped the way it is.

---

## 1. What "continuous optimization" was narrowed to

The phrase arrives from the ML literature carrying weight training with it. It was cut down to one
sentence before any code was written: **automated search over pipeline configuration, no weight
training, no GPU anywhere, no provider call in the loop.**

Three constraints forced that shape, each independently sufficient.

**The fine-tune is frozen.** v1 established that the answer-quality ceiling was the fine-tune, not
the retrieval pipeline, and v2's premise (A3) is that serving left the GPU. Reopening weights
reopens the hardware question v2 exists to close.

**The objective must not be able to buy its own result.** An optimizer permitted to touch the price
card drives a cost objective to zero without moving a token, so the multiplier in the objective has
to be unreachable **by construction**, not by classification or review ([VO2](EVIDENCE-OPTIMIZER.md#VO2)).
That single requirement disqualified every softer form of the search.

**The loop had to be free.** Not cheap — free. An unattended search spending metered quota per
candidate is a faucet, and a faucet inside a loop is the failure mode nobody sees until the quota
trips. The objective therefore makes **no provider call at all**, which is why the search's real
currency became wall clock and its cap was denominated in seconds
([VO10](EVIDENCE-OPTIMIZER.md#VO10)).

---

## 2. The search space: default-deny, one field at a time

`ServingConfig` is a versioned artifact with a few dozen fields. The optimizer sees an **explicit
allowlist**, admitted by a human one entry at a time, and everything else is born OUT. A field added
to the artifact later breaks the build until somebody places it — the partition is checked for
completeness in the proposer's own test suite, not asserted in prose.

Each admitted field records three properties as data: **proposable** (it churns `config_version`,
never `corpus_version` — a field needing a re-ingest is a corpus decision wearing a config costume);
**behavior, not belief** (it changes what the path *does or spends*, not what the system *believes*
about price, quota or quality — the rule that puts the rate card and the metric machinery
permanently out of reach); and **an instrument with power** (a named metric that can both score the
objective and hold the guard).

**The corollary is the most important structural fact about this search: an axis that is not a
versioned config field is invisible to it by construction.** Chunk size, embedding model, the
reranker's presence, the prompt's composition, the retrieval query's *content* — none are reachable
by the proposer, and no run can surface them. That is deliberate, since they are index- or
corpus-level changes and a search that re-ingests per candidate is not a search. But it means the
search's silence about them is not evidence, and several of the phase's largest findings came from
work deliberately run *outside* the loop for that reason.

The value domain is restricted the same way — an explicit tuple per axis, production's own value
included so the search can walk an axis back — which takes the reachable space from millions of
configurations to **81** ([VO2](EVIDENCE-OPTIMIZER.md#VO2)). Widening a tuple is a human's edit; this
is not a bandit over an unbounded box.

**One axis was excluded before the run, on a dry-run result, and it is the clearest justification the
allowlist has.** `allocation_policy`'s cheapest legal value does not *allocate* memory — it deletes
it. Left in, the phase's headline would have been "the optimizer found a 24% saving" where the saving
is *turn the memory system off*, and **no gate would have blocked it**, because the number the gate
blocks on cannot see memory at all ([VO4](EVIDENCE-OPTIMIZER.md#VO4)). That exclusion is the single
decision that kept the first run from producing a confident false result.

---

## 3. The objective: it ranks on one number and blocks on another, by decision

Three cost numbers exist: a **reconstructed** estimate computable from a config and a manifest
alone; an **assembled** estimate that requires retrieval and the memory read to have run; and a
**realised** figure from provider-reported usage ([VO3](EVIDENCE-OPTIMIZER.md#VO3)).
[`11-cost-and-performance.md`](11-cost-and-performance.md) takes them apart.

**P7 ranks on the assembled number and the gate blocks on the reconstructed one. That divergence is
D-1, a decision, not an accident**, taken in the project's stated priority order.

Ranking on the realised number was refused because it opens a metered faucet inside an unattended
loop, and because at the audit's own eval denominator it caps the search at **13 points per day**
([VO10](EVIDENCE-OPTIMIZER.md#VO10)). Ranking on the reconstructed number was refused because it
forfeits five of the nine axes: a candidate that bloats memory shows *no cost increase at all*,
invisible degradation in the objective's own dimension.

What the divergence costs is that a candidate can rank first and still be blocked, and E-run(a)
measured exactly that ([VO5](EVIDENCE-OPTIMIZER.md#VO5)). It is printed on every trace row rather
than reconciled silently — the honest handling of a seam that cannot be closed without giving up one
of the two properties.

The gate is deliberately asymmetric: the quality layer blocks only on a significant regression,
while the cost layer blocks on **any** increase, tolerance zero. Cost is exact and counted, so no
minimum-detectable-effect applies to it; quality is sampled, so one does.

---

## 4. What the search found: four of five axes did not survive measurement

E-run(a) ran an evolutionary search over the restricted domains, tried 25 mutations, reached the gate
four times, blocked nothing, promoted nothing, and terminated on a duplicate-candidate stall in about
nine minutes ([VO5](EVIDENCE-OPTIMIZER.md#VO5)). The plan pre-authorised a null result. What came
back is sharper than null, and the collapse is the result:

- **`allocation_policy` — degenerate.** One real value and a test arm, excluded before the run.
- **`prefetch_limit` — inert.** Local-mode Qdrant scores every point and ignores the parameter
  entirely, so the fused ordering is identical whatever the limit says. It costs nothing and buys
  nothing, and it refutes the pre-run hypothesis that deeper prefetch buys back the pool recall a
  smaller fused list gives up. Scoped: a *local-mode* finding, which would not hold under server
  mode with HNSW.
- **`top_k_rerank` — pinned.** Dropping it one notch buys cents per thousand answers against the
  loss of an entire family's grounding. The effect is below every MDE in the phase and so is not
  detectable by this project's own rule; **the pin rests on asymmetry, not significance**, and is
  recorded as such rather than dressed up as a measurement.
- **`top_k_fused` — the wrong currency entirely.** It turned out to be a **latency** axis, not a
  cost one. Cutting it from 25 to 10 is a **2.3× reduction in retrieval wall clock** and **0.04% of
  cost** ([VO6](EVIDENCE-OPTIMIZER.md#VO6), [VO7](EVIDENCE-OPTIMIZER.md#VO7)). The reason is that
  retrieval proper is flat; every millisecond saved is reranker work not done.

That leaves **one** cost axis: the memory budget. And one axis with three legal values is a sweep,
not a search. E-run(a) closed there.

The same sweep produced the phase's cleanest trap. Read on the `primary` family alone it shows a
clean quality gain; read per family it is mixed and nets out negative by a row. The per-family read
was taken second. **Pooling hid it.**

---

## 5. The 15.6% saving, which holds out of sample and did not ship

E-run(c) tested the accepted candidate against a held-out half of the eval set, using a
difference-in-differences so that the two halves holding different queries cancels out. The saving
is **15.6%, with an out-of-sample gap of −0.2%** — marginally *better* unseen
([VO7](EVIDENCE-OPTIMIZER.md#VO7)), and the known-answer check passed.

**The sentence that must travel with that number is not "the optimizer generalized well."** The gap
is near zero because the search had no choice to make: one axis ranked on cost, and it is monotone —
cheaper at every value, on every query. **A monotone single-axis search has no interior optimum, so
it cannot overfit.** The two claims produce the same number and are not the same claim.

**It was not promoted.** Not out of caution — because **no quality verdict exists for it**. The
gate's quality guard returned zero on every family, and zero *by construction*: it scores the
pre-rerank fused list, so a rerank-depth change never reaches it, a fused-depth change is invariant
under stable fusion, and a memory-budget change does not touch retrieval at all. The composed layer
that would have had an opinion, `quality_1b`, **ships empty by design** — CI must never call an LLM
judge (A7), so nothing could populate it.

Shipping it anyway would have violated the phase's own contract in the most direct way available:
promoting a change whose only guard is provably invariant to it is promoting on the *absence* of
evidence, and distinguishing those two things is what the phase is for. Cost exact, quality
**unavailable** — not "non-inferior", not "not detected", no verdict at all.

This is the failure mode the plan named in advance, observed on the first real run: **an optimizer
minimising cost under an insensitive guard is structurally incentivised toward invisible
degradation, and it found the cheapest thing the gate cannot see.**

---

## 6. Batch G: retrieval is blind to the conversation

Run outside the loop, because the axis is not on the allowlist and the defect is not in the config.

A pre-batch check asked whether one memory knob was a no-op, found the opposite knob was, and then
found something much larger than the question. **The retriever never sees the conversation.** The
query is resolved from the request body and client-supplied filters and nothing else; recent turns
reach the prompt and never the retriever. **63% of multi-turn requests retrieve on a contentless
string** ([VO8](EVIDENCE-OPTIMIZER.md#VO8)) — *"why does that matter later on?"* has nothing in it
to retrieve on.

That also explains why the knob had no instrument: a retrieval-recall metric is *structurally
invariant* to a memory axis that cannot reach retrieval. **Batch G had no guard because of a layer
nobody had traced, and the trace is the finding.**

The repair — prepending prior *questions* to the retrieval query, a handful of embedding tokens and
zero prompt tokens — **fires and does not carry.** It moves the organic families and reads well
below the promotion threshold fixed before the run on the family purpose-built to test it. So
`n_prev_queries` **ships at 0**, and the field stays versioned rather than deleted: a knob that
changes what reaches retrieval must churn the config version whoever moves it.

Two things about this deserve to be said plainly rather than softened.

**The blindness ships unrepaired, by decision. That is a real defect knowingly deferred.** It is the
largest user-visible defect the phase found, and the call is on measured magnitude, not an
oversight. Whether feeding the conversation forward helps the model *answer* — as opposed to
*retrieve* — stays untested, because that is a groundedness question and the judge-side guard is
empty. A live objection also stands against the naive repair: if the model answers from the
transcript while the programmatic Sources list cites what the *current* query retrieved, **the
citation lies.**

**The authored instrument came out weaker than the organic one.** The purpose-built family moved
less than the family that occurred naturally. The plausible mechanism — two prior turns lengthen the
embedded string and blur it where one sharpens it — is recorded as a **hypothesis carried to v3**,
not a finding, and the arm that would settle it was dropped from the final design. Batch G's three
instrument defects and one process failure are [`12-instruments.md`](12-instruments.md)'s.

---

## 7. Batch H: the axis measured out at zero, and an arm nobody proposed

Neighbour expansion was admitted on an arithmetic claim: serve one hit plus its neighbours instead
of three separate hits and the prompt gets both cheaper and more contiguous — *the rare candidate
where the cost objective and the quality hypothesis point the same way.*

**The arms were illegal before they were run.** Every one also moved rerank depth down, and rerank
depth had been pinned on measurement in the previous batch. The saving was never expansion's; it was
trading depth down and spending the proceeds on expansion. With depth fixed the arithmetic inverts
and expansion is straightforwardly expensive: radius 1 cost **+118%** for a gain the run's own
known-answer check proves is **zero by construction** ([VO9](EVIDENCE-OPTIMIZER.md#VO9)), and it
narrowed the served material while doing it.

What promoted is an arm nobody proposed. The expansion function returned early at radius 0, so
**merging adjacent spans without expanding them was unreachable** — radius and merging were one
switch. Separated, merge-only is identical to baseline on recall and coverage row by row, and
cheaper on a quarter of rows with **zero rows dearer**. It was found by reading a token column and
asking why a consolidation cost more.

**Its saving *is* chunk-overlap dedup.** Serving two adjacent chunks with their overlap duplicated
was always waste; the expansion framing concealed a plain defect. It is therefore quotable **only
with `chunk_overlap = 100` attached**. At overlap 0 it is worth nothing, and **at any other overlap
it is unknown** — not small, unknown.

**Backfill has direction without magnitude and ships off.** Its effect rests on a single discordant
row, and three sizing methods for the set that would settle it disagree by two orders of magnitude
for exactly that reason. The honest statement is *this data cannot size the set that would settle
it* — not a row count.

---

## 8. CP-7h: the chunking ablation, closed as a priced deferral

The checkpoint's own rule: *"we did not run it" is not a finding; "we did not run it, it would have
cost X, and here is why the expected result did not justify X" is.*

Running it is cheap: three arms is about an hour of local CPU embedding and a hundred megabytes of
collections ([VO11](EVIDENCE-OPTIMIZER.md#VO11)). If that were the whole price it would have run.

**The price is in the success branch, and it is the half nobody wrote down.** A chunking change is
an **index** change: promoting one re-freezes the corpus stamp, and numbers do not compare across a
stamp. Every substantive result in the phase is denominated in the current index — the saving prices
the prompt in chunks, merge-only's saving *is* the overlap, the composition result scored answers
built from this index, the blindness rate is a rate over retrievals against it, and the published
retrieval headline was measured on it. **The null branch buys a null for an hour of CPU; the success
branch bills the entire phase's evidence base.** The expense is re-measurement, not embedding.

An operational constraint sharpens it: local-mode Qdrant takes an exclusive file lock on the vector
directory, so the arms serialize and cannot overlap a running server or a gate run — collection
isolation solves the data problem, not the concurrency one. The move to server mode has been called
"not a blocker" four times; **this is the first shape that actually needs it.**

**And the deferral does not validate the current setting.** v1 recorded `500/100` as read from
source, never as a measured optimum. **No chunking number exists in this project. 500/100 is
untested, not validated** — and the domain prior that window width probably does not matter much
over continuous prose is recorded as the owner's judgement, consistent with the pre-registered null
and **evidence for nothing**.

Re-entry conditions are recorded so this is a drawer and not a silence: the corpus is re-frozen for
an unrelated reason and the ablation rides along; the domain stops being novels; the manifest learns
to distinguish the arms; or Qdrant moves to server mode.

---

## 9. Three blind spots, named separately

They are listed apart because one summary sentence lets two of them hide behind the third
([VO14](EVIDENCE-OPTIMIZER.md#VO14)).

**The blocking quality metric is computed above the rerank line.** It scores the fused list
pre-rerank, so a candidate that changes what the reranker *receives* is scored on a list the reranker
never saw. Hence: rerank depth never reaches the guard, the reranker's own on/off switch is
uninstrumented and excluded from the space, and pricing rerank-off — worth roughly two seconds a
request — is blocked.

**The gate's cost layer is blind to every memory axis, confirmed at exactly 0.000** across all five,
including across a full budget sweep. On three of them it is **worse than blindness**: the caps act
on the **write path only**, while the reads apply a retention window and no cap at all. Lowering a
cap changes nothing until a write happens and never retro-actively shrinks a store, so a candidate
proposing a tighter cap, measured on a pre-existing store, scores identical to production for a
reason unrelated to its merit. The gate cannot see those axes **even in principle** — the effect is
delayed and depends on write traffic a gate run does not generate. All three left the allowlist on
that ground.

**`quality_1b` ships empty**, so the quality guard is retrieval-side only and by A7 never could be
otherwise inside CI. The consequence recurs in every batch above: **cost exact, quality claim
unavailable.**

---

## 10. What actually promoted

`CURRENT_POINTER` moved **once** in the whole phase, and it moved for a **prompt** — composition C,
which is [`09-prompt-security.md`](09-prompt-security.md)'s to argue. Batch H's merge-only landed as
a config default. **Nothing E-run measured was promoted.**

A search that promotes nothing is not a failed search if it retires four axes on measurement, prices
a fifth's saving exactly, and finds the two defects — retrieval blindness and duplicated overlap —
that no config candidate would have surfaced. What it is not is a story about an optimizer that found
a saving. The headline the phase closed on: **a measured 15.6% saving, out of sample, deliberately
not shipped, because the system cannot yet say what it costs in quality.**

The refusals held to the end: no re-run, **no widening the space to make something pass**, and no
quoting a number measured on one arm for another.
