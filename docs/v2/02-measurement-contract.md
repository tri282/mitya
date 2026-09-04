# 02 — The measurement contract

v2's first checkpoint built no feature. **CP0 exists to establish what this project is able to
detect**, before anything is optimized against an instrument nobody has characterized. Everything
downstream — the gate's thresholds, the optimizer's admission rule, every "no effect found" verdict —
inherits from it. This is the document to read if you read only one.

**v1 anchors** are cited as `[E23 (v1)]` and live on branch `main`
(`git show main:docs/v1/EVIDENCE.md`).

---

## 1. Why the contract came first

The tempting order is: build the improvement, measure it, publish the delta. That order has a silent
failure mode. If the instrument cannot resolve the effect, a measured improvement and a measured
regression look the same — both land inside the noise — and whichever sign the run happened to
produce becomes the finding. v1 shipped exactly that failure: a prompt-composition A/B selected on
deltas that were a fraction of their own detection floor, and the selection was **withdrawn** at CP0
once the floor was computed ([VP5](EVIDENCE-PLATFORM.md#VP5)).

So CP0 inverts the order. Characterize the instrument first, publish its floor, and let the floor
decide which questions are askable at all. The uncomfortable output — that most of them are not — is
the deliverable, not a setback.

## 2. Two identities: what an eval-set version pins, what a config version pins

These are separate objects with separate rules, and conflating them is how a comparison quietly
becomes meaningless.

**An eval-set version is the set of row ids present.** Sets are append-only with immutable ids, so
any two versions are comparable **on their intersection**. That mechanism is what made an
unrecorded mid-phase churn recoverable rather than fatal: rows were added between two freezes and the
addition was never written down, yet every id in the earlier version was still present and unedited
in the later one, so no number was lost ([VP1](EVIDENCE-PLATFORM.md#VP1)). Edits, by contrast, are
forbidden — an edited row keeps its id and silently changes what a stored score meant.

Two consequences are enforced rather than advised. **Denominators are per-family and never pooled**,
because pooling denominators is precisely how v1 published a wrong recall figure. And **growth
follows the coverage axis, not the n axis** — adding rows to a family that is already saturated buys
statistical power the analysis in §3 shows cannot be reached; adding a family the set never had buys
a finding, which is how the one callable retrieval effect this project has ever produced was found.

**A config version is a content hash of the frozen, normalized config.** Two identical configs are
the same version by construction, so a candidate config gets an identity for free with no counter to
coordinate, and every span, metric and eval row carries it. **An environment variable cannot switch
arms; promoting an artifact does.** This is the rule that makes "which arm produced this row" a fact
rather than a recollection ([VP9](EVIDENCE-PLATFORM.md#VP9)).

**What a config version pins is exactly what is in it, and CP0's successor found three absences the
hard way** — the generation model, the persona text, and which embedder built the index. The first is
the instructive one: because the model was not in the hash, the version did not churn between two
provider arms, and **arm B was served arm A's cached answers**. A caching layer, a versioning
scheme, and an A/B harness were each individually correct and together produced contaminated quality,
latency and cost rows ([VP9](EVIDENCE-PLATFORM.md#VP9)).

**The corpus stamp is a third identity, and the two objects under it must not be merged.** The
document corpus `515c707a5d69` never changed across the whole of v2. The synthetic **session** corpus
was re-frozen twice, and numbers do not compare across a stamp
([VP26](EVIDENCE-PLATFORM.md#VP26)). One rule, two objects; earlier drafts conflated them.

## 3. Detection power, and the curve that never crosses

The rule is fixed in advance: **MDE = 2.8 × SE_diff** at 80% power, α 0.05, two-sided. For paired
hit/miss retrieval outcomes under McNemar, the standard error comes from the discordant-pair rate —
the rows where two arms actually disagree.

Measured across four retrieval arms, **every observed delta is 3–7× smaller than the MDE that would
make it callable**, and the sample sizes required run from several hundred rows to a few thousand
([VP2](EVIDENCE-PLATFORM.md#VP2)). Increasing n to the original target does not fix it; the target
was chosen before anyone computed what it would buy.

**The phrasing matters and is the finding.** A detection curve normally flattens somewhere useful —
you pick an n, you accept an MDE, you proceed. Here the curve **never crosses** the effects being
chased. There is no reachable sample size at which RRF prefetch limits, leg selection, expansion
on/off or chapter dedupe become distinguishable on this set.

Two facts make this stronger than a one-off. The analysis was **re-run on a much larger set and came
back bit-identical in every cell** — same recalls, same discordant counts, same MDEs — because the
new rows all went to families outside the headline denominator. And the instrument is thinner than
its n suggests: most primary rows return an identical hit/miss in all four arms, so a small handful
of rows carry every ablation conclusion this project has published or withdrawn
([VP2](EVIDENCE-PLATFORM.md#VP2)).

**The corollary binds every later phase.** No configuration knob may be selected on this set unless
its effect clears the floor. When CP7's optimizer reports that four of five candidate axes did not
survive, and when the gate reports `NOT-DETECTED`, that is this rule executing — not an absence of
effort. And `NOT-DETECTED` is enforced as never recordable as an improvement, in the report schema,
because prose is not a control ([VP20](EVIDENCE-PLATFORM.md#VP20)).

One deliberate subtlety: the MDE the gate reports is an **observed-effect** MDE, estimated from the
run being judged, not a pre-registered design MDE. With zero discordant rows there is no variance
estimate at all, and the gate reports null rather than `0.0` — a zero there would read as infinite
sensitivity ([VP2](EVIDENCE-PLATFORM.md#VP2)).

## 4. The judge instrument, and where its variance lives

Answer quality is scored by an LLM judge, which raises the obvious objection: is the instrument
measuring the system or itself? CP0 answered it by **measuring the attribution before authoring a
single rubric row**, because the two candidate explanations demanded opposite fixes — an unstable
judge needs rubric work, an unstable generator needs k draws averaged.

Held-fixed test-retest showed the split cleanly. On persona retention **the generator carries 93% of
the variance**, so no amount of rubric work helps and averaging draws is the only available lever
([VP3](EVIDENCE-PLATFORM.md#VP3)). On groundedness the balance is the other way and the questions
genuinely separate. **k = 3 draws per cell** was locked on that arithmetic: it removes a fixed
fraction of per-cell noise, k = 5 buys little more for substantially more cost, and a cell with fewer
than two judged draws is recorded failed rather than averaged. The minimum is hard-coded rather than
exposed as a flag — a knob there would let a run redefine "failed cell" between baseline and
candidate.

**The sizing was later measured to be optimistic**, because it had been estimated on a single theme
and the restriction deflated the within-cell variance. The published mode means survive (they are
built from between-cell variance); what the revision bites is per-cell claims, so worst-cell rankings
are indicative only ([VP3](EVIDENCE-PLATFORM.md#VP3)). Recording the revision rather than silently
restating the number is the pattern this whole set follows.

**Two metrics were cut, both on measurement, neither on taste.** A fabricated-events *count* was
refuted — the judge disagrees with itself about whether anything was invented at all, and the obvious
fix made every measure worse; the boolean survives. Graded relevance was cut before implementation
on a closed form: with binary single-target labels, NDCG and MRR induce identical orderings, so NDCG
carries zero additional information ([VP3](EVIDENCE-PLATFORM.md#VP3)). A metric that cannot be shown
to add information is maintenance cost with no return.

**One rubric fact worth carrying:** editing one axis's instructions moved another axis's scores. The
rubric is one artifact, not three, and it cannot be revised piecewise.

## 5. Known-answer checks as a discipline

A known-answer check is a case whose correct output is known **before** the run, whose only job is to
fail when the instrument is broken. The discipline is cheap and it is the single highest-yield
practice v2 produced.

Concretely, it looks like: a dry run whose printed call budget disagrees with the transcript's own
cell count; a baseline arm asserted to make zero summarizer calls before its cost numbers are read;
an ablation arm asserted to be identical to baseline **row by row**, not merely in aggregate, so a
divergence proves the payload guard leaked and the run is void; a rate-row reconstruction that must
reproduce a previously recorded objective to six decimals before any new number derived from it is
believed.

The rule earned across four phases, and it is the one to carry out of this project: **a number that
reads 0.000 is a claim about the instrument until something proves it is a claim about the world.**
The standing counterexamples are ugly — a fairness guard whose rejection counter had never counted
anything, a mechanical security metric at an identical zero on both arms of a total regression, a
memory axis reading zero across its entire searchable domain because a fixture was too small. Every
one of them was caught by a known-answer case, by reading what actually matched, or by a human
reading six rows. **Not one was caught by a clean run.** See
[`12-instruments.md`](12-instruments.md).

## 6. The held-out split

The optimizer searches over configs, which means the search half of the eval set is looked at
repeatedly and its numbers are, by construction, optimistically biased. So the set is split, and
**the held-out half is read once**, after a candidate is chosen.

The instrument is a difference-in-differences: the production/candidate delta is computed within each
half, and the two deltas are compared. That construction is deliberate — the halves hold *different*
queries, so their absolute difficulty differs, and only the differencing cancels it. When the
optimizer's accepted candidate was tested this way the out-of-sample gap was small and the saving
held ([VO7](EVIDENCE-OPTIMIZER.md#VO7)). The candidate was nonetheless **not promoted**, because a
cost saving with no quality verdict is not a decision — see [`10-optimizer.md`](10-optimizer.md).

## 7. The frozen v1 baseline, and the two defects that had to be fixed to produce it

v2 needed one number to measure against. Producing it took two harness repairs first, and **both were
defects in the measurement apparatus, not in the retriever.**

**Cross-process nondeterminism.** Recall was bimodal across repeated invocations on one unchanged
commit. The cause was an entity map returning a `set`, so a multi-entity query's alias blocks
reordered under the hash seed, changing the expanded query *text*, its embedding and the dense
ranking. Three consequences were kept, and only the first is about evaluation: it was a **live
serving defect** (two users, one question, different chunks); **every v1 retrieval number was a draw
from a distribution**; and a repeat-run check must be **cross-process**, because a single process
pins its own hash seed ([VP4](EVIDENCE-PLATFORM.md#VP4)).

**A harness that computed a route and then ignored it.** The retrieval script called the router,
asserted the route against expectation, logged that it matched, and then ran the default hybrid path
for every row — the structural retrieval function was imported by production and by the quality
harness and by nothing in the retrieval harness. Structural queries are nearly pure metadata, so the
default path missed rows production finds at rank 1 ([VP4](EVIDENCE-PLATFORM.md#VP4)). **A passing
assertion next to an unused result is not a control**, and the log line saying `route ok` was true
and irrelevant.

**The headline moved because the denominator moved, and that is the tell.** v1 published two figures
whose denominators were 26 then 25; the corrected baseline stands at **0.7105 (27/38)**
([VP4](EVIDENCE-PLATFORM.md#VP4), `[E23 (v1)]`). The old figure was never the retriever's recall on
those rows — it was the recall of a pipeline production does not run.

The frozen quality row carries its own caveat, and it is recorded rather than repaired: the artifact
holds **no config version, no git SHA, no timestamp and no persona text**, so it is reproducible from
git history and not from anything the artifact records — and the charter permits exactly one
re-measure, which this run spent ([VP4](EVIDENCE-PLATFORM.md#VP4)). That defect is what provenance
stamping on every later run exists to prevent.

## 8. CP0 found four numbers that were wrong before they were published

This is the point of the phase, not an aside. A checkpoint whose job is to build instruments found
that **its own instruments were the first thing to fail** ([VP5](EVIDENCE-PLATFORM.md#VP5)):

- Ten of seventy-two quality cells were **empty answers scored as answers** — a zero-token stream
  became the string `"(empty response)"`, which is judgeable. `generation_errors: 0` was reported
  alongside. Caught by a dry run whose only job was to print a call budget and whose cell count
  disagreed with the transcript.
- A **rate limiter that was over its configured rate** with nothing running concurrently: its window
  is per-process, so sequential invocations each got a fresh one. Caught by looking at the provider's
  own console.
- An **empty-completion retry guard set an order of magnitude too low**, hiding a residual failure
  rate that is per-query and permanent. Caught by running the same arm at a different setting.
- A **time-to-first-token published for a stream in which no token ever arrived** — the timestamp was
  set in the give-up branch too, so the metric recorded the moment the server stopped trying. Caught
  by offline reproduction against stub generators.

The correction cascade is worth following. Fixing the first defect and restricting the analysis to
cells where both arms produced real answers **flipped the sign** of v1's persona delta, which is what
withdrew v1's prompt-composition selection. The shipped composition became *unjustified* — a
different and weaker claim than *wrong*, and the distinction is kept
([VP5](EVIDENCE-PLATFORM.md#VP5)).

**One disagreement is recorded and left unresolved.** The groundedness MDE at the original n is
published three ways. Two differ only by estimation method and agree to within a couple of percent;
the third differs because its **denominator** changed when the empty cells were removed. That is a
real disagreement, not a methodological footnote, and no phase adjudicated it
([VP5](EVIDENCE-PLATFORM.md#VP5)). It is stated here rather than quietly resolved in favour of
whichever number reads better.

---

## 9. What the contract is worth

Two structural absences follow from it and no work inside v2 would have closed either.

**No statistical quality comparison this system can produce is detectable at any reachable n.** The
gate's real detection power therefore lives in its **invariant** and **cost** layers — and both
defects this project has actually suffered were invariant-class, with no statistics involved
([VP38](EVIDENCE-PLATFORM.md#VP38)).

**And the mechanical metrics cannot see grounding**, which was measured rather than assumed: strip
every retrieved passage from the prompt and the gate's mechanical metrics do not move at all, while
the judge detects the regression overwhelmingly ([VP21](EVIDENCE-PLATFORM.md#VP21)). Not a power
problem — identical zeros, and no sample size fixes a metric that does not respond.

A measurement contract that concludes *"we can measure less than we hoped"* is doing its job. The
alternative is not measuring more; it is publishing the same numbers without knowing.
