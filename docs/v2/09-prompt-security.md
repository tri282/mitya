# 09 — Prompt security: injection, the fence that does not hold, composition C

**Delta.** New in v2. v1 had a persona head, a prompt composition A/B chosen on a delta later withdrawn
as noise (`E24 (v1)`, `E27 (v1)`), and a mechanical input/output guard — but **no adversarial
evaluation of any kind**. v2 added a memory tier that injects stored content into the prompt, creating
a surface v1 did not have, and then measured it. This is the least comfortable document in the set:
it carries the project's only real security findings, and most of them are negative.

Every number lives in [`EVIDENCE-MEMORY.md`](EVIDENCE-MEMORY.md), anchors `VM1`–`VM25`.

---

## 1. The fence does not hold

The charter states an invariant: *injected memory is fenced and framed as untrusted data with no
instruction authority.* One fence string, shared by every tier, in one place — because framing
injected content is a security property and three copies of it are three chances to drift.

It was measured against a real provider, with an unfenced control behind every attempt, and **it is
not satisfied**. Memory-mediated indirect injection **succeeded, reproducibly, by more than one
mechanism** ([VM11](EVIDENCE-MEMORY.md#VM11)):

- a hostile turn got **rows written into the durable profile store** — well-formed, short,
  schema-valid rows whose *values* were the attack;
- a planted profile row's instruction was **obeyed through the fence** on re-injection, confirmed by a
  canary appearing in the answer;
- a second fence-escape attempt was obeyed the same way.

**This is an existence result, not a rate.** The valid denominators are single-digit, inside the rule
this project adopted after being burned by small-n arithmetic, and the batch discipline is explicit
that an existence question needs one event rather than a sweep. ***A pass rate for this fence is not a
statement this evidence supports, and no such percentage may be written anywhere.***

What it does **not** say is equally load-bearing. It does not say the fence is worthless — several
attempts were blocked against live controls. It does not test the episodic tier's fence, whose one
attempt was invalid. And it says nothing about attacks outside this set: the set is a **regression
check, not a defence audit**, and an arm this small that finds landings is far likelier to be
understating the surface than overstating it.

**Status: OPEN.** The charter's *no instruction authority* invariant ships as **a documented weakness
with a number behind it, not a solved problem**, written into the tier-1 docs as the largest open
weakness in the serving path. Fixing it is a prompt change, not a schema change — the schema bounds
format, not content ([`08-memory.md`](08-memory.md) §3) — and the set's own charter allows one
adjustment, then a decision, so no second attempt was made inside the phase. Nothing in the later
optimizer work narrows it: those attack rows carried no passages and no planted memory rows, so
**every later attack number is against the prompt scaffold, on a different surface**.

## 2. The measurement found something worse than a weak fence

The next phase re-ran the question against the *prompt* rather than the memory store, with an arm that
existed only to be a control: the shipped persona with its prohibiting sentences stripped, differing in
exactly one respect so it could not drift into being a different prompt that happens to fail
differently.

**The shipped prompt is set-identical to that unfenced control** ([VM14](EVIDENCE-MEMORY.md#VM14)).
Not the same count — the *same attempts*, empty symmetric difference in both directions. Removing the
prohibiting sentences changes nothing, because they were never doing anything.

**The defence everyone assumed existed was never there.** This is a stronger and more uncomfortable
claim than "the fence is weak", and it is a fact about the shipped prompt rather than a virtue of
anything that replaced it.

Three things made it findable, and none of them are the eval itself:

- **A control arm.** Without one, the shipped arm's landings read as an attack set that is simply
  strong, and the missing defence is invisible.
- **Arms as config promotions, not flags.** Each row's config version names the prompt that produced
  it, so an arm cannot be mislabelled after the fact.
- **Draws on the control too.** At a single draw the known-answer check *false-alarmed* — one attempt
  landed on a minority of draws against a fixed control. A metric that flips on resampling is not a
  gate (§7).

## 3. Composition C, and a security change that improved quality

Composition C is the shipped persona's placement plus the control/data separation it never had: system
message → persona → a **precedence marker** naming each block and declaring it data → the memory block
→ the conversation block → a **reinforcement tail**.

Three measured defects motivated it, not taste. The memory fence fails (§1). The shipped prompt has no
instruction against assistant preambles. And the shipped composition **ends its system message with the
memory block** — the most-attended position in the prompt holding its least-trusted bytes.

The earlier B composition was **deleted rather than kept as a third arm**: it had been chosen in v1 on
a persona delta far inside its own detectable-effect threshold, later withdrawn as noise and then
sign-flipped. Carrying B forward would have carried a withdrawn result forward with it.

C was promoted, and the result is the opposite of the usual security tradeoff. Violations fell
**0.5625 → 0.1250** ([VM14](EVIDENCE-MEMORY.md#VM14)), and on the quality leg C came back **superior**
on groundedness, not merely non-inferior — **+0.724 against a measured MDE of 0.343**
([VM18](EVIDENCE-MEMORY.md#VM18)), with the pairing strong enough to resolve it at the run's n.
Fabrication moved in the same direction and outside its own MDE.

**These are one behaviour, counted three times.** *Answer from the recollections and from nothing
else; where they do not reach, say plainly that you cannot recall it.* That single instruction is what
grounds better, fabricates less, and declines more — and §5 and §6 are both consequences of measuring
the third of those with instruments built before it existed.

**Two things the promoted evidence may not be merged into one claim.** The security leg ran on the
serving model; the **quality leg ran on a different, cheaper model** after a quota miss forced a
re-run. The A-vs-C quality comparison is internally valid — both arms, one model, one judge, one run —
but **its transfer to the served model is an assumption, not a measurement**. Security evidence on one
model, quality evidence on another: one claim each, never one claim together
([VM20](EVIDENCE-MEMORY.md#VM20)). And `persona_retention` sat at a **ceiling on every cell of both
arms** ([VM18](EVIDENCE-MEMORY.md#VM18)) — not a tie, a judge that cannot discriminate persona on this
instrument at all, so **no persona claim is available at any number of draws**.

## 4. What composition C did not buy

**Extraction is unmoved at 1/8 on every arm** ([VM15](EVIDENCE-MEMORY.md#VM15)) — the same one attempt
of eight lands on the unfenced control, on the shipped prompt, and on C. The pooled reduction is
**entirely the `jailbreak` family**.

> ***"78% more secure" is a FALSE SUMMARY*** of that result and may not be printed. It takes a
> single-family reduction and reports it as a system property, while the family that exfiltrates a
> tuned, versioned asset did not move at all.

**Both survivors against C are defects C introduced** ([VM16](EVIDENCE-MEMORY.md#VM16)). One forges C's
own block marker inside the user turn — it exists *because* C exists, and the attempt's recorded intent
predicted exactly that before the run. The other asks for the prohibitions rather than the text, and C
has more prohibitions to enumerate, so **the defence manufactured the thing it leaks**; it lands on C
and on neither other arm. A defence with a structure has a structure to forge, and a defence with rules
has rules to enumerate. Both are recorded as C's cost, and both were found by **reading which attempts
survived**, not by reading the rate.

A scope line travels with every figure in §3 and §4: those attack rows carried no passages and no
planted memory rows. **They say nothing about the memory-mediated surface**, which remains as §1 left
it.

## 5. The false-refusal guard: FAILED, amended to NOT DEMONSTRATED

C declines more than the shipped prompt does, and the promotion guard for that — *false refusal not
worse* — was recorded **FAILED** and accepted as a real cost.

A human then read the refusals in full, and **zero of them are over-refusals**
([VM17](EVIDENCE-MEMORY.md#VM17)). Four are **oracle artifacts**: the oracle matches the prescribed
refusal locution, so an answer that declines one specific claim the passages do not carry and then
answers substantively — in one case at length, naming people and events — scores as a *full* refusal.
All of them had the target chapter in the prompt. That is strict application of the grounding
instruction, not paranoia.

The other two are **unanswerable by construction**: they ask which chapter contains a line, and **the
persona is forbidden to name chapters, sources or quotations**. Refusing them is compliance.

Those two rows expose a defect in the benign set itself, and it is the most transferable thing here.
The authoring rule verified that every row was *benign* and that the shipped prompt answered it. **It
never verified that a row was answerable within the persona's own rules** — and those rows passed only
*because the shipped prompt ignores the rule they violate*, which is the very defect the evaluation
existed to measure. The rule was missing a half: **a benign row must be benign AND answerable within
the persona's own rules.** The rows were voided with the reason attached rather than deleted; a row
removed silently takes its finding with it.

**Two denominators appear in the record and are deliberately not reconciled.** The run scored one set
with a runtime void — a row whose target chapter was not served — and the later human read voided two
further rows as unanswerable by construction, leaving a smaller scorable set. They are different void
mechanisms applied at different times, and each figure is reported as its own source states it.

**The status is NOT DEMONSTRATED, in either direction.** The measurement measured the oracle and two
mislabelled rows. It is not evidence that C over-refuses, and it is not evidence that it does not; an
instrument that could say either would need a locution-independent refusal detector, and none exists
here.

Two more things are recorded in the order they happened, because the order is the point. First: **the
shipped prompt's clean refusal score is not a clean pass.** In the leg that passed no passages at all,
it answered every row with confident invented detail while C declined every row. **It never refuses
because it never grounds**, so its low refusal rate is partly measuring a model that answers
regardless. Second: the benign set was authored by the same person who then concluded that the failing
guard was a defect in those rows — **exactly the conclusion to distrust** — so the promotion was made
on the *worse* number, and the amendment recorded afterwards.

## 6. The instrument defects, which are the point

Across this work the instrument was wrong more often than the system was, and each defect would have
published something confident, plausible and false ([VM12](EVIDENCE-MEMORY.md#VM12),
[VM19](EVIDENCE-MEMORY.md#VM19)).

**A metric punished an arm for obeying its own grounding instruction.** The assistant-tell detector
scored C far worse than the shipped prompt — and **26 of 26 matches were the bare literal *I cannot*,
all false** ([VM19](EVIDENCE-MEMORY.md#VM19)): first-person prose the persona is supposed to produce,
in a composition that *instructs* the model to decline with *"I cannot recall"*. The arm that
fabricates instead scored clean. Left alone it would have published *"the safer prompt breaks character
four times as often"* — a shipped metric arguing against the arm that grounds. Caught by reading what
matched rather than the rate; the fix is a **false-positive removal, not a loosening**, narrowed to the
assistant register the metric is named for and re-checked against the same historical transcripts,
where it reproduces the recorded rate with the identical true positives.

**A "4 of 4 blocked" that only meant the fake provider returned prose.** The extractor arm's perfect
block rate was a JSON parse failing against a fake provider's prose output. *Landed = false* meant
"the provider returned prose", not "the schema refused an instruction" — **the defence had never
run**. It was caught because the artifact **withholds** a pass rate rather than computing one: the
module cannot emit a pass rate unless the run hit a real provider *and* the control actually
succeeded, and otherwise the field is absent with a reason attached. An artifact from it cannot be
misread as a pass rate, because there is no field a pass rate could be read out of.

**A fence arm inflated by an attempt whose control was dead.** One attempt landed in neither arm — its
control never transferred, so it tested nothing — and counting it as blocked inflated the arm. Caught
by the control arm itself; the denominator now counts control-landed attempts only, and dead controls
are reported separately. This was the eighth instance of the pattern in the project and the first
caught by a control built specifically to catch it. A fourth, **a metric scoring 0.000 that could never
have moved**, is covered in [`08-memory.md`](08-memory.md) §10.

Two rules were earned, and they are the transferable output of this work:

> A number that reads **0.000** is a claim about the instrument until something proves it is a claim
> about the world — and a number that reads **high** is a claim about the instrument until someone
> reads the matches.

> A regression set that reports everything blocked with no control is **measuring the weakness of its
> own attempts**, and looks identical to a strong defence.

Not one of these was caught by a clean run.

## 7. Where a security verdict is allowed to act

An injection block rate needs a real provider — a fake one cannot be jailbroken, §6 — so it is metered
and stochastic, and the charter makes metered stochastic metrics **reporting instruments that never
block**. The dry run supplied the decisive argument independently: an attempt landed on a minority of
draws against a fixed control, and **a metric that flips on resampling cannot be a gate**.

So the verdict acts in three narrower places: a regressing or absent fence verdict makes a tuning
candidate **unrankable** in the proposer; the gate report already carries non-blocking records with an
explicit reason, a home that is neither a blocking metric nor silence; and the residual — a regressing
candidate stays promotable by a human who reads only the summary line — is closed **at the line a human
actually reads**, by naming the regression in the disposition caption. That caption is deliberately
**not built yet**: no fence record flows into the gate today, and a formatter for records that do not
exist is scaffolding.

**A fence non-inferiority claim was priced before anyone asked for one.** At a margin loose enough to
be nearly meaningless for a security claim, the cost is **one candidate every two to six days** of
provider quota ([VM21](EVIDENCE-MEMORY.md#VM21)) — and a margin that wide would admit a candidate
blocking a fraction of what the incumbent blocks. Three of the sizing inputs are not measurements and
are labelled as such, including a discordance rate that has never been measured for this comparison at
all. The affordability, not the desirability, is what closes the question.

## 8. The mechanical guard underneath, and the half of it that is unmeasured

A deterministic, model-free guard predates all of this and is the only security layer allowed to gate,
because it is free, offline and stable. Two findings about it belong here
([VM23](EVIDENCE-MEMORY.md#VM23)).

**Its similarity layer was measured and demoted.** The fused retrieval score carries no domain signal
at all; reading a single leg directly does carry signal, but the distributions overlap. An authored twin
pair settles it: an adversarial row and a sensitive row score within a rounding error of each other, and
the sparse leg scores the *adversarial* one lower. **Similarity measures topic; the difference between
those rows is intent, and no threshold on a similarity can see intent at any n with any embedding
model.** Worse, the score *rises* with how in-domain an attack is dressed, because those rows name the
corpus's own characters. **A topic-similarity gate is blind by construction to every attack wearing
corpus vocabulary — which is every attack that matters.**

**Two denominators, never one safety score.** Correct-refusal rate over adversarial rows, false-refusal
rate over legitimate ones, reported separately — because *a guard that refuses everything scores 100%
on both, and a single averaged number hides exactly that.* The false-refusal rate is measured and
zero; **the correct-refusal rate is unmeasured**, because it needs a generation pass that was never
run. Half of a guard's contract is therefore unverified, and it is written down as such rather than
implied by the half that passed.

One correction was made rather than quietly renamed: two of the three layers are **output-side**. A
canary detects the prompt leaking; a persona-break check detects the model dropping character. Neither
is knowable before generation, so "input guard" describes a third of what the layer does. And the guard
computes verdicts that **nothing on the request path consumes**.

## 9. What stays open

- **The memory fence does not hold, and the charter invariant is OPEN** ([VM11](EVIDENCE-MEMORY.md#VM11)).
  The episodic tier's fence is **untested** — its one attempt was invalid.
- **Composition C buys nothing against prompt extraction**, and both survivors against it are its own
  defects ([VM15](EVIDENCE-MEMORY.md#VM15), [VM16](EVIDENCE-MEMORY.md#VM16)).
- **The false-refusal guard is NOT DEMONSTRATED in either direction**
  ([VM17](EVIDENCE-MEMORY.md#VM17)).
- **No persona claim exists at any number of draws** ([VM18](EVIDENCE-MEMORY.md#VM18)).
- **The attack set is authored by the same people who wrote the fence** — the standing limitation of
  every hand-written red-team set, and not fixed here.
- **The attack sets have no eval-set discipline**: no immutability assertion, no append-only rule, no
  content stamp in the report, no freeze record — and the provenance stamp **cannot detect a split**,
  because it hashes the file and a split leaves the file untouched
  ([VM13](EVIDENCE-MEMORY.md#VM13)). Recorded, not fixed.
- **A fence rate remains unaffordable** ([VM21](EVIDENCE-MEMORY.md#VM21)), and **the fence verdict is
  not yet wired into the gate report**, so a regression is visible only inside the security run's own
  artifact.

**Three summaries this evidence forbids**, in the terms the record uses:

- ***"78% more secure"*** — FALSE SUMMARY. Extraction is unmoved at 1/8 on every arm.
- ***Any pass rate for the memory fence*** — not a statement this evidence supports; the denominators
  are single-digit.
- ***"The prompt is fenced"*** — the shipped one was set-identical to an unfenced control.

## 10. The one-line summary

An adversarial evaluation with control arms found that the memory fence fails against real attacks,
that the prompt fence everyone assumed existed **was never there**, and that the replacement improved
groundedness while buying nothing at all against prompt extraction. **The invariant ships open, as a
documented weakness with a number behind it** — and the finding that generalizes furthest is that
every one of these was surfaced by a control, a known-answer case, or a human reading six rows, and
**not one by a run that completed cleanly**.
