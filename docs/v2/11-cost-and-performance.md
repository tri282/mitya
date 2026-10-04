# 11 — Cost and performance: three numbers, and why distillation cannot help

**New in v2.** v1 had one cost question — does it fit in 6GB — and answered it in VRAM. v2 serves
from hosted APIs, so cost became a currency question, and the phase's job was to say *which* number
is being optimised and what it is allowed to claim.

Every figure below lives in [`EVIDENCE-OPTIMIZER.md`](EVIDENCE-OPTIMIZER.md) (`VO1`–`VO16`) or
[`EVIDENCE-PLATFORM.md`](EVIDENCE-PLATFORM.md) (`VP1`–`VP38`) and is cited by anchor.

---

## 1. Three cost numbers: one ranks, one blocks, one records nothing

The plan said two cost numbers existed. A probe found **three**
([VO3](EVIDENCE-OPTIMIZER.md#VO3)). They were not duplicates of each other; they answer different
questions and require different things to exist.

**The reconstructed number.** Passage count times chunk size, plus the persona head, priced against
the rate card. It needs a config and a corpus manifest and nothing else — no request, no retrieval,
no provider. That is exactly why the CI gate blocks on it: it is computable at candidate-proposal
time, before anything has run. The price of that convenience is that it **never sees** the memory
block, the framing, or the question. Its docstring calls it a ceiling; on a memory-bearing request it
under-states the real prompt by about **41%** ([VO3](EVIDENCE-OPTIMIZER.md#VO3)). It is a ceiling on
completion and a **floor** on prompt, and nothing in the report said so.

**The assembled number.** The real messages summed with a `char/4` estimator, with completion held at
the configured ceiling. It requires retrieval and the memory read to have actually run, so it cannot
be computed at proposal time — but it sees every axis the optimizer searches. **P7 ranks on this
one.**

**The realised number.** Provider-reported usage priced through the rate card. It is the only one of
the three that describes something that happened. It **records nothing at all**: recording requires a
rate row flagged `metered: true`, every shipped row is `metered: false`, and the API's cost path
therefore routes every answer to the unmetered branch. The metric has taken **zero observations**,
by design. An objective built on it would be built on an empty histogram, and *the failure mode is
silence, not an error*.

**They diverge because they are answering different questions, and the divergence is a decision.**
Blocking needs a number available before a candidate runs; ranking needs a number that can see what
the candidate changed. No single number has both properties, so the phase took both and printed the
gap on every trace row rather than picking one and hiding the loss. The measured consequence is that
a candidate can rank first and still be blocked, which E-run(a) observed on two of four accepted
points ([VO5](EVIDENCE-OPTIMIZER.md#VO5)).

Two limits of the ranking number are stated by the decision that chose it. `char/4` is not a
tokenizer. And the completion half is a **constant at the ceiling**, so the objective is structurally
incapable of seeing a candidate that shortens answers — a cost search blind to half the bill on the
expensive side, since completion tokens price several times higher than prompt tokens on every row of
the card ([VO10](EVIDENCE-OPTIMIZER.md#VO10)).

---

## 2. Actual spend is $0.00, and that is what makes the comparison possible

Every rate row the system ships is `metered=False` ([VP14](EVIDENCE-PLATFORM.md#VP14)). Serving ran
on free-tier quota throughout. **Actual spend across the optimizer phase is $0.00.**

That is not a rhetorical flourish, it is a constraint on what may be claimed, and the project wrote
the rule down before it had a number to apply it to: price free-tier calls at published list rates,
report the result as an estimate, and never as spend. The licensed claim is *"at published list
rates, this arm prices at $X per thousand answers, measured on free-tier quota"* — checkable by any
reader against a public pricing page. The unlicensed one is *"this system costs $X in production"*,
which is checkable by nobody.

**"Beating $0.00" is not possible.** The list-rate framing is the only thing that makes any cost
comparison exist at all. Strip it and there is no cost axis, no objective, no optimizer and no O7.

The corollary is uncomfortable and is stated rather than avoided: **no realised per-answer cost
exists, and none can on this deployment.** Flipping `metered` would promote an estimate to a
measurement, which is the register's forbidden move, and the phase was explicitly not permitted to do
it. Every USD figure in this document is arithmetic on a price card, not a bill.

**A USD figure whose provenance is unnamed is not a figure.** One rate row's citation was corrected
in place when the cited pricing page turned out to have been unreachable and the numbers confirmed
from secondary trackers instead — the numbers were right and the citation was not
([VP14](EVIDENCE-PLATFORM.md#VP14)). That is the standard: a rate row carries the page and the date
it was read, or it does not get to price anything.

---

## 3. What measurement costs, and the currency it is actually denominated in

The gate does not spend money. It runs local ONNX models and a local-mode vector store, and a run
costs **$0.00 exactly** ([VP23](EVIDENCE-PLATFORM.md#VP23)). What it spends is **wall clock**, and
the plan's estimate for that was off by an order of magnitude — the cost was the reranker bucket,
scoring candidates on CPU once per arm across the whole eval set, which nobody had multiplied out.
Moving that bucket to promotion time only bought most of it back.

This inverts the project's habit. Since the gateway phase, measurement had been budgeted in dollars.
**On the gate leg USD is exactly zero and time is the entire constraint**, and the optimizer pays
that time on every promotion attempt. That is why the search's cap was denominated in seconds and why
a USD cap would have capped nothing.

Three separate mechanisms produce that zero and it is worth keeping them apart, because only one of
them is why the search is free: the rate limiter prevents quota exhaustion; `metered: false` means
USD is an estimate and never a bill; and **the objective makes no provider call at all**. Only the
third is load-bearing. *If the objective ever moved to realised cost, the limiter would be unchanged
and the search would stop being free.*

The one metered piece of work in the phase was the prompt-security batch, and it is the case study
for the rule. Each leg was budgeted separately, the day was never summed, and the run crossed the
daily request ceiling partway through an arm — leaving most rows empty and the survivors selected by
whichever ran before the trip ([VO10](EVIDENCE-OPTIMIZER.md#VO10)). It was **discarded, not
analysed**. The rule this earned is that a budget consulting one rate limit is not a budget; the
binding limit is whichever of requests-per-minute, tokens-per-minute, requests-per-day and
tokens-per-day comes first, and on at least one provider family the binding one is a token limit that
**nothing in the codebase enforces** ([VP14](EVIDENCE-PLATFORM.md#VP14)).

---

## 4. Latency, and the fact that it is not on the objective

Latency was deliberately kept **off** the optimizer's objective. A cost-flat axis sits outside the
loop entirely; measuring latency inside a search that ranks on dollars would have meant either
inventing a weighting between seconds and cents or letting a latency win masquerade as a cost win.
Instead the latency work is a standalone, pre-registered instrument with its own stated objective,
run at $0.00 with no gate involved ([VO6](EVIDENCE-OPTIMIZER.md#VO6)).

What it established:

**The reranker is the request path.** At the fused depth of 25 that was production when the sweep
ran, it is **87.1% of retrieval wall clock** ([VO6](EVIDENCE-OPTIMIZER.md#VO6)), and that figure
agrees within three points with v1's GPU-era measurement [E28 (v1)] and with a v2 measurement taken
by a different instrument on a different machine. Three instruments, two years, two machines, one
answer. Retrieval proper is flat at ~260 ms; every millisecond saved on the fused-list depth is
reranker work not done, which is why cutting that depth is a **2.3×** latency win for four
hundredths of a percent of cost.

**That cut has since shipped, so the 87.1% is a historical reading and the current one is lower.**
The serving artifact carries the fused depth at **10**, where the same sweep measures the retrieval
path at **917 ms with the reranker at 72.1%** of it ([VO6](EVIDENCE-OPTIMIZER.md#VO6),
[VO9](EVIDENCE-OPTIMIZER.md#VO9)). Rerank is still the majority of the path and still the largest
measured inefficiency; the share is smaller because the pool it scores is smaller. Quoting 87.1% as
a present-tense property of the shipped config would be quoting a number measured on one arm for
another, which is the error this project keeps catching.

**Generation stopped being the bottleneck and retrieval became it.** That is the v1→v2 delta in one
sentence, and it is measured rather than asserted. It is also the strongest argument for a decision
the phase could *not* take: pricing the reranker off entirely is worth roughly two seconds a request,
and it is blocked, because the gate's quality metric is computed **above** the rerank line and
therefore cannot score a change to reranking at all ([VO14](EVIDENCE-OPTIMIZER.md#VO14)).

**One family is immune.** Structural queries route through a hard-filtered path that never sees the
fused pool, and run more than an order of magnitude faster. A pooled latency number would have
averaged that in and reported an improvement that a third of traffic already had.

**The headroom argument is a prediction, not a measurement.** The chosen fused depth (**10**, the
value now shipped) is not the cheapest admissible one; **5** is quality-identical on every family
and differs only in how much room the reranker has left to find anything a future improvement might
recover (81% of its ceiling at 10 against 93% at 5). That is an argument about future work, priced at
about **350 ms**, and it is labelled as such ([VO6](EVIDENCE-OPTIMIZER.md#VO6)).

**One half of the latency picture was never produced.** The in-band write-latency delta for the
memory path, against the auth phase's baseline, does not exist ([VO15](EVIDENCE-OPTIMIZER.md#VO15)).
Stated as an absence, because an absence that is not printed reads as a pass.

---

## 5. The `max_tokens` break, and why P7 supersedes P2 rather than contradicting it

The output ceiling moved from 400 to 1024 mid-project. **This is a measurement break, and no cost
figure compares across it** ([VP18](EVIDENCE-PLATFORM.md#VP18)). The gate enforces that in code
rather than trusting prose: a cost comparison between two arms that differ on the ceiling returns an
error verdict, not a delta.

The reason the original value existed is itself the argument. 400 was sized so that prompt plus
completion fit the local context window on the 6GB GPU — a hardware constraint, never a product
decision about answer length. Carrying it into hosted serving imports the exact constraint v2 exists
to escape, and the measured result was that most answers were guillotined mid-sentence.

So every earlier per-answer price is a **400-token figure**. The P7 reading **supersedes** the P2 one
and does not contradict it; the superseded number stays visible, struck, with the reason. The same
discipline applies inside the phase: the prompt shape itself grew about 2.5× between the two
readings, and there is a second break inside P7 where the same config was priced under two different
memory fixtures ([VO13](EVIDENCE-OPTIMIZER.md#VO13)). Numbers from either side of either break do not
compose.

---

## 6. O7: distillation cannot move the cost number at all

The question inherited from P2 was whether distilling the model would make local serving cheaper than
hosted. P2 answered "not yet" directionally. P7 closed it on arithmetic, at a desk, with no run and
no provider call.

The local cost model is a rented instance billed by the hour, priced as
`instance_rate × seconds_per_answer ÷ utilization`. Distillation moves `seconds_per_answer`.
Utilization is set by traffic. **Substituting the definition of utilization is what closes the
question:**

```
util        = (answers_per_day x seconds_per_answer) / 86400
USD/answer  = rate_per_hour x (seconds_per_answer / 3600) / util
            = rate_per_hour / answers_per_hour
```

**Seconds-per-answer cancels.** At a 24/7 rented instance, USD per answer is the hourly rate divided
by answers per hour, and **the model is not in the result** ([VO12](EVIDENCE-OPTIMIZER.md#VO12)). The
long way round — decode rate, tokens, utilization percentage — agrees to the cent.

This is a stronger statement than P2 could make. It is not that distillation moves the number by too
little to matter; **it cannot move it at all.** A faster GPU has the same property: a rented T4
shortens seconds-per-answer and changes USD-per-answer by exactly nothing.

**Break-even is therefore a traffic number and only a traffic number.** Against the cheapest hosted
arm on the card it is **1,630 answers per day** ([VO12](EVIDENCE-OPTIMIZER.md#VO12)), roughly sixteen
times this deployment's traffic. The margin has narrowed since P2 — longer completions raise hosted
cost and local utilization together — and would keep narrowing under further ceiling increases. The
direction is worth carrying forward even though it changes nothing today.

**What distillation actually buys is the saturation ceiling.** A faster model does not change what an
answer costs; it changes how many answers one GPU can produce in a day before it is fully busy — in
this case roughly a threefold increase. **That makes distillation a capacity decision, not a cost
decision, and this deployment has no capacity problem.** Nothing in the traffic harness or the
serving record shows the system near saturation.

**The falsifier, named because one assumption is doing all the work.** Twenty-four-hour rental is a
*choice*, and it is the choice that creates the cancellation. Under **per-second billing** — a
serverless GPU where idle time is unbilled — the cancellation does not happen, cost per answer
becomes rate times seconds per answer, and on this project's own measured decode rate that
**undercuts the cheapest hosted arm immediately, at any traffic, with no distillation required**
([VO12](EVIDENCE-OPTIMIZER.md#VO12)). That regime was not entered, for a reason that is itself
unmeasured: a quantized 7B must be resident to answer in seconds rather than minutes, and **per-request
cold-load cost is priced nowhere in this project.** The one number that would decide the question is
the one nobody has.

---

## 7. Gaps, stated as gaps

- **No realised per-answer cost, and none can exist on this deployment.** The metric has zero
  observations by design and the flag that would change that is not the optimizer's to flip
  ([VO3](EVIDENCE-OPTIMIZER.md#VO3)).
- **No cold-load price**, which is the term that decides the per-second-billing falsifier
  ([VO12](EVIDENCE-OPTIMIZER.md#VO12)).
- **No quality verdict for the measured saving**, so no cost-quality frontier exists — only a cost
  axis with an unpriced other side ([VO15](EVIDENCE-OPTIMIZER.md#VO15)).
- **No in-band write-latency delta** for the memory path against the auth-phase baseline
  ([VO15](EVIDENCE-OPTIMIZER.md#VO15)).
- **No token-limit enforcement on the provider family where a token limit binds**
  ([VP14](EVIDENCE-PLATFORM.md#VP14)) — an observation about the limiter, recorded, not repaired.

The honest summary of v2's cost work is that it produced an **exact, reproducible, list-rate cost
model with a searchable axis and a proven-generalizing saving on it, on a system that has never spent
a cent** — and that the reason no cost decision shipped is not the cost side, which is exact, but the
quality side, which is unavailable.
