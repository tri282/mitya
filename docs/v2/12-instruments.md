# 12 — Instruments: every defect, and why a clean run never caught one

**Delta.** New in v2, and it is not a subsystem. v1 had instruments and v1's instruments were wrong
too — the `0.720` recall figure and the prompt-composition A/B were both withdrawn in v2 — but v1
never counted them, so nothing accumulated. This document is the accumulation.

**The thesis, in one sentence: across four phases the instrument was wrong more often than the system
was, and not one defect was caught by a clean run.** Every one was caught by a known-answer case, by
a control arm, by reading what actually matched instead of the rate, or by a human reading six rows.

Numbers live in the three evidence logs; this document argues the mechanisms and cites them.

---

## 1. Why this document is organised by failure mode

The obvious organisation is by phase, and it is the wrong one. **The modes repeat across phases that
share no code, no author's attention and no subject matter**, and that repetition is the finding. A
rate limiter whose rows were rolled back by an exception, and a fairness guard behind a gate that makes
its own precondition unreachable, are one defect in two vocabularies: a control that never executed,
with a green test suite in front of it. Each entry below gives **the mechanism**, **what it would have
published**, and **what actually caught it** — the third being the argument.

---

## 2. Mode one — the instrument never ran

**The login rate limiter had never counted anything.** `connect()` commits on clean exit and rolls
back on any exception; `login()` wrote the two failed-attempt rows and then raised, unwinding through
that same context manager, so both rows were rolled back on the way out and the failure counter
returned zero forever — **in production exactly as in the test**
([VP37](EVIDENCE-PLATFORM.md#VP37)). It would have published *"per-account and per-IP login rate
limits: PASS"* — a shipped, documented, reviewed security control that could not fire. Caught by the
**first execution** of the suite against a running server. Twenty-seven unit tests were green
throughout, because none of them had ever asked the database what it held after the raise.

**The error-ratio panel read 0.000 during a total generation outage.** The live run's first request
was a 503 from an unreachable generator; it recorded all six pre-generation stage durations and **no
request at all**, because the 503 path raised before the recording line was ever reached
([VP10](EVIDENCE-PLATFORM.md#VP10)). Both RED panels are driven by the request counter, so the metric
added to expose an outage was structurally incapable of expressing one. It would have published an
operator plane that stays green while nothing is served. Caught by watching the dashboard during a
real outage — four pre-generation exits shared the shape, and the fix was one `_abort()` helper, not
four patches. *A counter incremented only on the success path is not a counter, and the shape that
produces it is an early raise that skips the recording line.*

**Two ratio panels could not display zero.** With no cache hits the hit series does not exist, the
division returns no result, and the panel renders "No data" rather than 0. The guard everyone reaches
for — clamping the denominator — does nothing, because **the missing term is the numerator**
([VP10](EVIDENCE-PLATFORM.md#VP10)). Neither defect is reachable offline: a unit test that records one
hit and one miss has by construction made both series exist.

**The fairness guard sat behind a gate that made its own precondition unreachable.** The gateway's
token-pressure fair-share guard fires only above a fraction of the provider's per-minute token pool;
admission control, added in the same batch for a different reason, caps in-flight requests low enough
that sustained pressure sits at roughly **3% of the threshold**
([VP35](EVIDENCE-PLATFORM.md#VP35)). No offered load can raise it, because the offered load is exactly
what admission control is refusing. Nine in-process fairness tests pass and the arithmetic is right.
It would have published *"per-tenant fairness: delivered"*. Caught by reading a counter that had moved
by zero across two multi-tenant runs while a sibling counter carried every rejection. **A control that
has never executed is indistinguishable from an absent one**, and here the control is unreachable by
construction with a green suite in front of it.

**The opposite side of the same coin, and it is why "never ran" is the right frame rather than "was
absent".** A probe reported a security-relevant counter as carrying no tenant label. The more alarming
reading, stated with total confidence — and false. **The label was wired; the emission was missing**,
so the series was absent from the scrape entirely and the assertion failed on a *missing* series
rather than an unlabelled one ([VP37](EVIDENCE-PLATFORM.md#VP37)). One call added to the test's own
emitter; no source change. A control that is present and looks absent, and a control that is absent
and looks present, are separated by exactly one thing: executing it.

---

## 3. Mode two — the instrument measured the wrong world

**A memory fixture roughly 20× too small made an axis read 0.000 across its entire searchable
domain.** The cost objective's fixture built its recent turns from a 144-character stand-in; a single
real prior turn renders around **700 tokens** ([VO13](EVIDENCE-OPTIMIZER.md#VO13)). The whole rendered
memory block came in below the domain's own floor, so **no legal value of the memory budget could ever
truncate it** and the axis reported zero movement at every point the proposer was allowed to emit. It
would have published a phase headline saying the largest identified prize in the system — memory, at a
large minority of all context tokens — does not matter, *not because the objective was blind but
because the fixture was too small to make the budget bind*. Caught not by an audit but by an unrelated
piece of work that wanted a "retrieval-irrelevant axis that still moves the objective" and could not
find one. The fix is calibrated to a measured turn size, **never to whatever made the knob move** —
the difference between those two motivations is the entire integrity of the number.

**A load driver measured arrival luck rather than fairness.** It fired a fixed count of attempts per
tenant and never retried a rejection; rejection is instant, so a tenant arriving during a saturated
window burned its whole offered load in milliseconds and finished with zero served, while whichever
tenant already held a slot re-fired on completion and kept it ([VP37](EVIDENCE-PLATFORM.md#VP37),
[VP35](EVIDENCE-PLATFORM.md#VP35)). It would have published a starvation ratio as the phase's headline
fairness claim. Caught by a repeat run disagreeing by **3×** — and the corrected driver then had no
statistical power at all, because the whole system serves 22-26 requests in a 90-second window and a
ratio of small integers is noise. The magnitudes were withdrawn; the directional reading survived.

**A load ramp measured the cache instead of the system.** Its first pass populated the cache and the
ramp cycles its query list by design, so every later level replayed warmed queries and the curve
degenerated into cache-lookup latency ([VP31](EVIDENCE-PLATFORM.md#VP31)) — a throughput figure two
orders of magnitude above the truth. Caught by a two-request dry pass whose output was read. The fix
is a permanent run condition, not a code change: a cache-enabled curve measures *the cache*, and the
two curves must never be pooled.

**The retrieval harness computed a route and then ignored it.** It called the router, asserted the
result, logged `route ok`, and ran the default hybrid path for every row — the structural retrieval
function was imported by the app and by nothing in the harness
([VP4](EVIDENCE-PLATFORM.md#VP4)). Structural queries are nearly pure metadata, so the harness missed
all of them while production finds them at rank 1. It had already published v1's headline recall
figure: **that number was never the retriever's recall on those rows, it was the recall of a pipeline
production does not run**. Caught by reading the harness against the serving path.

**The gate's own retrieval arm never loads the reranker.** It scores the top of the fused list
directly, so a candidate differing only in whether reranking is enabled moved cost substantially and
**not a single rank** ([VP24](EVIDENCE-PLATFORM.md#VP24)) — a reading that any rerank knob is
quality-neutral. Caught by accident, while gating a config chosen to exercise a different path. The
consequence is the worst combination: rerank knobs are **invisible to the gate's quality leg and
visible to its cost leg**, so they can be blocked for costing more while their quality effect is
unmeasurable.

**A histogram could not see its two most expensive stages.** They reported an identical value at p50,
p95 *and* p99 on both routes — not a latency but the top edge of a bucket set covering eight stages
across five orders of magnitude ([VP8](EVIDENCE-PLATFORM.md#VP8)). **The symptom of failure is an
identical quantile at every percentile, which reads as a suspiciously stable measurement rather than
as an error.**

---

## 4. Mode three — the instrument's success meant something else

**"4 of 4 blocked" only meant the fake provider returned prose.** The injection set's extractor arm
scored a perfect block rate because the fake provider's output failed to parse as JSON, so zero rows
were written. `landed=False` meant *the provider returned prose*, not *the schema refused an
instruction* — **the defence was never exercised**
([VM12](EVIDENCE-MEMORY.md#VM12)). It would have published a perfect block rate for a defence that had
not run. Caught by the artifact refusing to emit a pass rate at all: the module cannot produce that
field unless the run was against a real provider **and** the control actually succeeded, and it
carries a withheld-reason string instead. *An artifact from that module cannot be misread as a pass
rate, because it contains no field a pass rate could be read out of.*

**A fence arm was inflated by an attempt whose control was dead.** One attempt landed in neither the
attack arm nor its unfenced control, so it tested nothing — and counting it as blocked moved the arm
from one-in-three to two-in-four ([VM12](EVIDENCE-MEMORY.md#VM12)). It would have published a fence
twice as effective as the measurement supports, on the phase's headline security number. Caught by
**the control arm itself**; the denominator now counts only control-landed attempts and dead controls
are reported separately. This was the eighth instance of the pattern in the project and the first
caught by a control arm built specifically to catch it.

**A defect fixture that fails to inject is indistinguishable from a gate that misses.** The gate
calibration's first run injected two synthesized defects through a path that raised on import; Python
reported it as a single swallowed warning line and **both defects reported `invariants hold`**
([VP22](EVIDENCE-PLATFORM.md#VP22)). Nothing errored. The gate simply was not shown the defect. Caught
by asking whether the patch had applied rather than trusting the disposition.

**A whole family of tests that passed because their subject was never exercised** — a property test
whose generated phrasings made every position agree; a hostile-input set covering newline as an
*interior* character while the defect was *positional*; a mock transport that bypasses the very method
the instrumentation wraps, so zero spans existed and *"no secret in any span"* passed because there
was nothing to search ([VP11](EVIDENCE-PLATFORM.md#VP11)). The defence that worked every time:
**assert non-vacuity beside the property, and run a deliberate-leak drill**. *A redaction test that
has never been observed to fail is not evidence.*

**Empty answers scored as answers.** Ten of seventy-two quality cells were empty strings the judge
scored, because the collector returned an empty answer and the judge substituted a judgeable
placeholder; the run reported zero generation errors ([VP5](EVIDENCE-PLATFORM.md#VP5)). Restricted to
the pairs where both arms produced real answers, the persona delta **flips sign** — it had already
published v1's prompt-composition selection, now withdrawn. Caught by a dry run whose only job was to
print a call budget and whose cell count disagreed with the transcript's own summary.

**And one vacuous test kept deliberately.** The cross-tenant cache-leak test is recorded as *vacuous
by construction* and prints that phrase rather than skipping
([VP36](EVIDENCE-PLATFORM.md#VP36)): under a shared corpus with no per-tenant content the leak attempt
succeeds and that is correct. Its value is a **tripwire** that fails the moment per-tenant memory can
reach the prompt — detected from the code, never from a plan file, because a guard keyed on something
the forgetful person must remember to update is not a guard. It fired at CP6, and the question it
forced was closed on a measurement ([VM9](EVIDENCE-MEMORY.md#VM9)).

---

## 5. Mode four — the metric punished the right behaviour

**An assistant-tell regex scored the safer prompt four times worse than the prompt that fabricates.**
The metric exists to catch a model dropping character into assistant register. Composition C
*instructs* the model to decline with *"I cannot recall"* where the passages do not reach, so the
regex's bare literal `I cannot` fired on first-person prose the persona is supposed to produce.
**All 26 matches were verified false** ([VM19](EVIDENCE-MEMORY.md#VM19)) — every one the bare literal
inside in-character narration:

> *"…whether he is right or whether he is merely burying himself in the dark, **I cannot** say, for
> even he, I suspect, does not fully know…"*

It would have published *"the safer prompt breaks character four times as often"* — a shipped metric
arguing against the arm that grounds, while the arm that invents scored a clean zero. Caught by
**reading what matched, not the rate**; the third false positive in this pattern and the third found
the same way. The fix narrowed the pattern to the assistant register the metric is named for and was
re-checked against the same v1 transcripts as the two earlier tightenings: **the same true positives
survive, and the bare literal caught none of them**. That is what makes it a false-positive removal
rather than a loosening — and stating which it is, out loud, is the whole discipline.

---

## 6. Mode five — the rate was real and the interpretation was not

**A false-refusal rate survived a human read at 0 of 6.** Composition C was recorded as failing its
false-refusal guard on six of twenty-nine benign rows. The human read the six answers in full: **four
are oracle artifacts** — the refusal oracle matches a prescribed locution, so a partial decline that
then answers substantively at length scores as a full refusal — and **two are defects in the benign
set**, rows the persona is forbidden to answer at all, which had passed authoring review only because
the shipped prompt ignored the rule ([VM17](EVIDENCE-MEMORY.md#VM17)). The status was amended from
FAILED to **NOT DEMONSTRATED**: not evidence that the arm over-refuses, and not evidence that it does
not. The instrument would need a locution-independent refusal detector to say either.

Two things make this entry the sharpest in the set. The comparison arm's **0.0000 is not a clean
pass** — in the first benign leg, which passed no passages at all, that arm answered every row with
confident invented detail while the other declined every row. **It never refuses because it never
grounds.** And the correction runs against its author's own work: *the benign set is mine, and "the
failed guard was actually a defect in my rows" is exactly the conclusion to distrust*. The acceptance
was taken on the **worse** number and amended afterwards, deliberately in that order.

**A timing oracle that was an inert comparison.** An enumeration probe reported a ratio consistent
with a live user-enumeration vulnerability. **The probe had blown its own budget**: it fired more
logins than the account and host caps allow, and the rejections that followed skip the password hash
entirely and return in roughly half the time of a real credential check
([VP37](EVIDENCE-PLATFORM.md#VP37)). It would have published a live auth vulnerability. Caught by
**reading the evidence rather than the verdict**; the probe now refuses to score itself at all when a
rejection appears in its sample. Its sibling defect is the mirror image: an assertion comparing the
set of status *codes* to the set of response *bodies* — **an assertion that could only ever fail** —
reported a failed security requirement on a run whose measurement was a clean pass.

**Zero divergence that was not a thread-safety result.** Identical queries issued concurrently
returned identical chunk ids at every level on both arms. Read naively that is a concurrency-safety
finding. It is not: the retrieval it exercised was **already serialized by the runtime**, because
retrieval is plain sync on a single-worker event loop, so the check is a *determinism* detector and is
**silent** on the question it was cited for ([VP32](EVIDENCE-PLATFORM.md#VP32)). Both the run's own
closing paragraph and the checkpoint rule as written read it the other way round. It is carried as an
open gap precisely because it becomes a false safety claim the moment retrieval is offloaded to a
thread or a second worker is added.

---

## 7. Mode six — the budget consulted one limit

The same error at three scales, all in one phase family
([VP15](EVIDENCE-PLATFORM.md#VP15)): a run sized against requests-per-minute while a token-per-minute
limit bound nearly eight times harder; a run called comfortable at a fraction of its daily *request*
budget while one pass exceeded the daily *token* budget outright; and, in the final phase, three legs
each budgeted separately with the day never summed, which exhausted the daily quota at row six of the
second arm and left **52 of 58 rows empty**, the survivors being whichever ran before the trip
([VM24](EVIDENCE-MEMORY.md#VM24), [VO10](EVIDENCE-OPTIMIZER.md#VO10)).

The last one is the worst shape, because the survivors are a **selection effect** — the same defect as
the empty-cells case in §4, which is known in this project to flip a sign. The run was **discarded,
not analysed**, and its lasting consequence is a merge prohibition: the re-run moved both arms onto a
different serving model from the one the promoted config serves, so the security evidence and the
quality evidence are one claim each and never one claim together
([VM20](EVIDENCE-MEMORY.md#VM20)).

The rule was already written down, verbatim, in the project's own instructions — *budget against every
rate limit and take the minimum; a budget that consults one limit is not a budget* — **and it is
written there because it had been violated once before.** A rule surviving contact is not the same as
a rule being followed, and this is the clearest case in the project of the second failing while the
first held.

---

## 8. What was never caught by a clean run — and why that is structural

Every entry above was caught by a known-answer case, a control arm, reading what matched instead of
the rate, or a human reading a handful of rows in full. **None was caught by a run completing
successfully**, and the reason is not diligence: a run that completes cleanly and measures the wrong
thing is *identical to success from the outside* — same exit code, same row count, same well-formed
artifact, usually a plausible number. The signals that separate the two exist only if someone decided
in advance what the instrument must show if it is working.

That is why the dry-run rule here is not *"do a small run first"* but **"do a small run first and read
what it produced"**. Smoke tests at two rows verified that a run *executed* and never looked at what it
*produced*; printing the last sixty characters of one answer would have shown the truncation that
invalidated a baseline comparison before three hundred calls were spent
([VP18](EVIDENCE-PLATFORM.md#VP18)).

---

## 9. The rules the project actually earned

Stated as rules, because each one is a defect that has already been paid for.

- **An instrument that has not been checked against a known-answer case is not evidence.**
- **A number computed from single-digit counts is not a number.**
- **A number that reads 0.000 is a claim about the instrument until something proves it is a claim
  about the world** — and its converse: a number that reads high is a claim about the instrument until
  someone reads the matches.
- **Read what matched, not the rate.**
- **A probe must record what it saw, not only its verdict**, or a wrong verdict is unfalsifiable after
  the run.
- **Exercise a control against a running system, not only in-process.** The rolled-back rate limiter
  was invisible to inspection and would have been invisible to any probe asserting on the code rather
  than on behaviour.
- **Assert non-vacuity beside the property.** A test that passes because its subject was never
  exercised looks exactly like a test that passes because the property holds.
- **A dry run must inspect output, not exit status.**
- **Budget against every limit and take the minimum.**
- **State the absence.** An unstated absence reads as a pass — which is why the vacuous cache test
  prints *vacuous by construction* instead of skipping, and why each evidence log ends with a section
  listing what it never measured.

**The count, for scale: four instrument defects at CP0, two on the operator plane at CP1, six at CP5,
three at CP6, four at CP7, plus the harness and gate defects in between.** Against them, the product
defects the project actually suffered are few enough to name — cross-process nondeterminism in entity
resolution, and a harness that ran the wrong path — and **both were invariant-class, caught with no
statistics involved** ([VP22](EVIDENCE-PLATFORM.md#VP22)). **That asymmetry is the single most
transferable thing v2 produced.**
