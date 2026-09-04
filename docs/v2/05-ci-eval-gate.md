# 05 — The CI Eval Gate

**Delta from v1.** v1 had evaluation scripts and no gate. A change was judged by a human running an
eval and reading the numbers, which works exactly as long as one person is doing one thing at a time.
v2 added a gate: an automated comparison between the promoted configuration and a candidate one, run
on every promotion attempt, that either blocks or does not.

The gate exists to answer one question — **does this system stay good as it changes** — and the honest
version of this document is that it answers a narrower question than that, on purpose, and the
narrowing was measured rather than assumed.

---

## I. The constraint that shapes everything: CI may not call a judge

The charter forbids an LLM judge in CI. The reasons stack:

- **Cost and quota.** The judge is the binding constraint on this project's measurement budget, not the
  generator. A judged leg in CI would consume the same daily quota the optimizer and the eval runs need,
  and it would consume it on every attempt rather than on every promotion.
- **Determinism.** The gate asserts a determinism invariant on its own inputs. A stochastic scorer
  inside the thing that asserts determinism is a category error.
- **Circularity.** A judge is itself an instrument with measured test-retest variance, and its variance
  sets a floor below which no judge-axis effect can be resolved at any sample size.

So the gate runs on **mechanical metrics only**: things computed from retrieval results, token counts
and rate cards. That constraint is the source of both the gate's strength — it costs nothing to run and
it is reproducible from its own inputs — and its central limitation, which section IV is about.

---

## II. Three layers, and nothing in it is a hand-set threshold

| layer | compares against | can block |
|---|---|---|
| **invariants** | zero — route mismatches, determinism spread against the recorded baseline | yes |
| **cost** | production's own computed value | yes |
| **sampled quality** (recall at k) | production minus the minimum detectable effect *that run computed* | yes |
| ranking and coverage metrics | reported with an interval | no |
| mechanical answer-quality metrics | — | **empty by construction**, pending an open decision |

**Every threshold is derived from the run rather than configured, and that is the design decision.** A
constant would go stale as the system improves: a recall floor set today is a ceiling in six months, and
nobody notices the day it stops binding. The sampled layer compares against production's own value
minus the minimum detectable effect **computed from that run's own discordant-pair rate**, so the bar
moves with the instrument.

Two consequences of that follow directly and are enforced as report fields rather than as prose. A
`NOT-DETECTED` verdict **passes, prints the delta beside the interval, and is never recordable as an
improvement** — which is the discipline the optimizer's ranking depends on. And **no component may
collapse quality to a scalar**: the report schema rejects any `overall`, `score` or `composite` key,
because a scalar is a weighting, and the weighting is precisely what is left open.

**Why the ranking metrics report but do not block.** The defect found during construction was that only
the hit-rate metric carried any interval at all; the ranking and coverage metrics were computed, carried
into the report, and never given one — so the gate was blind to any change that moves a hit's *rank*
without moving it across the cutoff, which is a real improvement the hit-rate metric cannot see. The
paired-proportion test does not extend to them, since it conditions on discordant pairs and discordance
only exists for binary outcomes; hence a **paired bootstrap**, with the interval expressed on the same
scale so the two families are commensurable. But they still do not block, and the reason is not
statistical: **making a second quality metric blocking would implicitly weight ranking quality against
hit quality, and that weighting is exactly what is left open.** A gate that blocks on two quality
metrics has picked a composition; one that reports both has not.

The bootstrap is **seeded**, which is load-bearing rather than tidy: an unseeded bootstrap makes two
gate runs on identical inputs disagree, tripping the determinism invariant this same module asserts and
making a report unreproducible from its own inputs.

**The cost layer blocks on any increase, with tolerance zero and no interval.** That asymmetry is
deliberate: cost is *computed* from token counts and a rate card, not sampled, so there is no sampling
error to tolerate. Two guards fire before a comparison is even attempted — an unpriced production side
returns an error rather than a zero, because no cost claim is possible; and a difference in the output
token ceiling returns an error naming it a **measurement break, not a cost delta**, which is how the
ceiling change described in [`04-provider-gateway.md`](04-provider-gateway.md) is enforced in code
rather than in prose.

**The bound the gate carries onto every promotion** is written in one fixed shape and never as a claim
of equal quality: *cost changed by X percent (computed, at the token ceiling), quality not detectably
worse at the stated interval and sample size*. The residual is stated with it: a promoted configuration
may be **materially worse on retrieval without detection**, bounded by the invariant layer and by
printing the number, and **not removable at any reachable sample size**
[VP20](EVIDENCE-PLATFORM.md#VP20).

**Two bypasses are recorded as a discipline claim rather than as an invariant.** The commit hook can be
skipped and the promotion pointer can be hand-edited. Detection is after the fact, through the promotion
log, not prevention. Claiming otherwise would be the more comfortable sentence and the false one.

---

## III. Calibration: five synthesized defects, and which layer did the work

The gate was calibrated against five deliberately injected defects, all run locally at zero API cost
[VP22](EVIDENCE-PLATFORM.md#VP22). All five matched a prediction written before the run.

**Why synthesized rather than replayed.** The obvious move was to replay v1's ablation arms as labelled
history. That was refused: the power analysis had already withdrawn those arms as noise readings, so
they carry **no true labels**, and calibrating against them would be calibrating against random
assignment. The only ground-truth regressions this project has ever actually suffered were
invariant-class — a cross-process nondeterminism bug and a route-table bug — and both were replicated in
shape.

The two nondeterminism- and routing-shaped defects blocked on the invariant layer. A degraded-retrieval
defect blocked on the sampled layer. A byte-identical candidate — the null — did not block, and nothing
fired. And the fifth, an ungrounded arm, **did not block on anything**, which is section IV.

**Three things the outcome table does not say on its face**, and they are the reason the calibration was
worth running:

1. **The sampled layer had to be shown a brutal defect before it noticed.** The first attempt at the
   retrieval defect cut the prefetch pool moderately and moved a single row, *in the positive
   direction*. Only starving both retrieval legs to a single candidate each produced a block, and it
   cleared its own threshold by a hair [VP22](EVIDENCE-PLATFORM.md#VP22). Destroying a fifth of the
   evaluation set barely registers. **The sampled layer is not a quality gate; it is a catastrophe
   detector**, and that is the power analysis felt from the other side.
2. **The nondeterminism defect was caught by the baseline comparison, not by the within-run check.** The
   within-run determinism check compares the two arms of one run, and both were perturbed identically,
   so their hit vectors agreed and it saw nothing. What fired was the comparison against production's own
   *recorded clean run*. That makes the baseline store load-bearing — and nothing prunes it and it has no
   retention policy, which is why that gap is a real one rather than housekeeping.
3. **The first calibration run produced a false negative that looked exactly like a real pass.** The
   injection mechanism failed to apply, Python reported it as one swallowed warning line, and both
   defects reported *invariants hold*. Nothing errored; the gate simply was not shown the defect. It was
   caught only by asking whether the patch had applied rather than by trusting the disposition. **A
   defect fixture that fails to inject is indistinguishable from a gate that misses it.**

Defect fixtures live outside the source tree and are applied only through the interpreter path for one
subprocess, never by editing the source. A defect introduced into the tree and forgotten is worse than an
uncalibrated gate.

**No single gate-accuracy number is computed.** Five defects and five verdicts is a demonstration that
each layer fires, not an error-rate estimate, and a scalar would merge classes whose detection stories
are entirely different.

---

## IV. The gate cannot see grounding, and that was measured

This is the finding the phase exists to have produced, and the outcome was **predicted in writing before
the run** [VP21](EVIDENCE-PLATFORM.md#VP21).

The experiment: run the pipeline against itself with every retrieved passage stripped from the prompt —
a certain, total regression — and see what the gate's mechanical metrics do. The arm was the metered
production path rather than the local server, because a result on a path the project stopped serving
would be a finding about a different system. The theme was chosen as the one scoring **highest** on
groundedness in the frozen run, on the argument that it carries the most headroom for an ungrounding
effect: if the contrast is invisible there, it is invisible anywhere.

**Stripping every passage moves nothing the gate can measure.** The retrieval layer cannot see it by
construction — retrieval still runs, only the prompt changes. The two mechanical answer-quality metrics
**do not move at all**. The judge detects it overwhelmingly, with groundedness falling to the floor at
several times its own detectable threshold [VP21](EVIDENCE-PLATFORM.md#VP21).

**This is not a power problem, and that distinction is the whole finding.** The mechanical metrics are
not *nearly* equal across the arms; they are **identical zeros**. No sample size fixes a metric that does
not respond. Those regexes measure persona *register*; grounding is a different construct, and the
overlap was assumed rather than checked until this run.

Persona is likewise independent of grounding — identical in both arms. **An answer invented wholesale
from parametric memory is indistinguishable from a grounded one on every axis available to CI, and
indistinguishable on persona even to the judge.**

**And the incentive points the wrong way.** Sending no passages is **32% cheaper** and passes the gate
clean [VP21](EVIDENCE-PLATFORM.md#VP21). The charter's warning that an optimizer minimizing cost under an
insensitive guard is structurally incentivized toward invisible degradation was an argument when it was
written. It is now a measurement, with a named degenerate solution sitting inside the optimizer's
declared search space.

**What that licenses, under the rule fixed before the evidence:** groundedness is recorded as an
offline, non-blocking metric and never gate machinery; and the gate's caption **permanently excludes
grounding** — it may say invariants hold, cost did not grow, and retrieval is not detectably worse. It
may never imply answers were checked for grounding.

**What it forbids:** any gate caption, promotion rationale or optimizer claim implying grounding was
checked. **The gate is a retrieval-and-cost regression tripwire. It is not, and after this measurement
cannot be described as, an answer-quality gate.** That disclaimer is enforced by the report validator,
not by convention: a report lacking it is rejected.

**Why the mechanical metrics were not simply promoted to blocking**, since they are deterministic and
judge-free and therefore the obvious candidates. They were recomputed over v1's full A/B transcript, and
their detectable thresholds are enormous relative to the rates they actually observe — one of them could
**double** without clearing its own threshold [VP21](EVIDENCE-PLATFORM.md#VP21). They stay non-blocking,
now for a measured reason rather than a cautious one.

That analysis produced one reusable diagnostic and one process finding. The diagnostic: **only one
quality axis pairs usefully across arms.** Query difficulty is not a shared property for the others, so
pairing cancels nothing and more rows buy far less than the naive sample-size calculation suggests — a
fact now printed on every run rather than rediscovered. The process finding: every judge-axis threshold
this project had ever published had been computed **by hand, outside any script**. The paired data was
always there and the summary threw it away. **The analysis lived in prose, so no run reproduced it.**
Closing it cost zero API calls — arithmetic over data already paid for.

**The dry run over-alarmed, and it is recorded because every other small sample in this project
under-alarmed.** A two-row dry run scored the grounded arm badly enough to prompt a precondition being
added to the decision rule before the sized run; at full size the grounded arm scored well. The
precondition cost nothing and stays in the rule — **a rule written before the evidence cannot be
retrofitted, which is the entire point of writing it first.**

---

## V. What the gate's own retrieval leg does not exercise

Two findings, both discovered by accident, and both about the gap between what the gate runs and what
production serves [VP24](EVIDENCE-PLATFORM.md#VP24).

**The gate's retrieval arm never loads the reranker.** A candidate configuration that differed *only* in
disabling reranking moved the cost figure by more than double and **not a single rank**: zero flips,
zero movement on any ranking metric. The arm the gate runs scores the fused list directly and the ranker
is never even constructed, while production reranks on every non-structural request. The consequence is
the worst possible combination: **any knob on the rerank stage is invisible to the gate's quality leg
while being visible to its cost leg** — it can be blocked for costing more while its quality effect is
unmeasurable. The rerank knobs join the fused-pool size and the retrieval knobs on the list of things
this instrument cannot score. The searchable-and-measurable space keeps shrinking, which is an argument
*for* reframing the objective around cost rather than against it.

**The blocking metric hides two different failures with opposite remedies.** The evaluation scored only
the top of the fused list and discarded the rest of the pool — but the reranker is handed the whole
pool, so the blocking metric is neither an upper nor a lower bound on what the served answer contains. A
target sitting deep in the pool is a miss for the metric that the reranker can still serve. Separating
them gives two classes:

- a **pool miss** — the target is absent from the fused pool. A retrieval failure. No reranker, no
  fusion weight and no expansion rule recovers it. This is the true ceiling.
- a **pool hit below the cutoff** — an ordering failure, and precisely the headroom a reranker exists to
  convert.

Two things came out of measuring them separately. The pool is **not saturated**, contrary to the working
assumption that it would be — a handful of rows are structurally unreachable at the current prefetch
depth in *every* arm, which makes them a prefetch or indexing question rather than a ranking one. And
one row is displaced out of the pool entirely by the sparse leg, which is a fusion finding nobody was
looking for.

**Pool recall is reported, never blocking**, for three reasons in weight order: it **saturates by
construction**, so a paired test on it produces almost no discordant pairs — a gate that structurally
cannot fire; it sees pool *membership* and nothing else, so every defect that reorders without changing
the set goes undetected at every layer; and swapping the blocking metric would itself be a measurement
break of the token-ceiling class, invalidating the baseline and every ablation number recorded against
it. It is computed by the eval harness and **not yet read by the gate at all**, which is an open gap
rather than a decision — and it is precisely the guard the optimizer's audit later specified for the
fused-pool knob.

---

## VI. What a gate run costs, and where the scarce resource is

A gate run costs **exactly zero dollars** — local ONNX models, local-mode vector store, no API calls
[VP23](EVIDENCE-PLATFORM.md#VP23). The interesting number is time, and the plan's estimate was wrong by
an order of magnitude.

The decomposition is entirely one bucket: the cross-encoder scoring the fused candidates for every row,
on CPU, once per arm. That bucket was **87% of the gate's runtime and it is non-blocking** — it exists
to make the serving path visible, not to decide anything. The individual per-query figure was consistent
with what v1 had recorded [E28 (v1)]; nobody had multiplied it by the row count and two arms.

**Decision: the rerank bucket moves to promotion time only**, which is the same tier logic the mechanical
answer-quality metrics already follow. A routine gate run dropped to roughly a thirtieth of a minute
against four minutes before — a **7.5× throughput gain on the leg the optimizer pays per attempt**
[VP23](EVIDENCE-PLATFORM.md#VP23). The deferral is not free, and section V prices it in recall rather
than in seconds: the band of retrieval movement CI is now blind to is exactly the pool-hit-below-cutoff
headroom.

**The consequence for the optimizer is the reframe.** The gate is paid on every promotion attempt. A
hundred candidate evaluations is hours of wall clock before a single API call is made. The project had
been budgeting measurement in dollars since the gateway phase; **on this leg dollars are exactly zero
and time is the whole constraint.**

The mechanical answer-quality leg is **unmeasured, not free**: it ships empty pending the open
composition decision, so there is no promotion-time leg to time. Priced against an already-spent run as
an estimate, a themed pass is cents and a full pass is a fraction of a dollar, judge calls additional
and of the same order — *what such a leg would cost once its composition is chosen, not a measurement of
one*.

And one number that reframes the whole project's economics: **for a single real user, measurement spend
exceeds serving spend by a wide margin.** One promotion check costs the equivalent of dozens of served
answers. That is an argument for the free leg carrying as much of the gate's work as it can, and against
adding further metered checks to the routine path.

---

## VII. What this leaves open

- **The mechanical answer-quality guard ships empty** and cannot block. The weighting between grounding
  and persona is deliberately undecided, and the measured options are all bad: the mechanical candidates
  cannot gate, the judge is the only thing that sees grounding, and the charter bars the judge from CI.
  **The gate's quality half is inert by construction until someone chooses it** — not a defect, a decision
  nobody has taken.
- **Ceiling versus realised cost is undecided.** The cost layer prices at the token ceiling and says so;
  which of the two the optimizer should minimize has not been settled.
- **The determinism baseline store has no retention policy**, while nondeterminism detection depends on
  it.
- **Pool recall is computed and never read by the gate.**
- **The eval-set stamp hashes the file**, so it cannot detect a split: two reports over different halves
  of the set carry the same provenance stamp.
- **One declared scored family has zero rows on disk**, and the vacuous-denominator check never fires for
  it, because a family with no rows never forms a bucket to check.

Full detail, every number with unit, date and provenance:
[VP20](EVIDENCE-PLATFORM.md#VP20)–[VP24](EVIDENCE-PLATFORM.md#VP24), with the never-measured list at
[VP38](EVIDENCE-PLATFORM.md#VP38).
