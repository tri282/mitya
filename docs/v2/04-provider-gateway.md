# 04 — The Provider Gateway

**Delta from v1.** v1 served generation from a quantized model on a 6GB consumer GPU through a local
OpenAI-compatible server. v2 moved serving off the GPU to hosted APIs behind a gateway that speaks to
**two provider families**. The local arm survives as code — it subclasses the OpenAI-dialect adapter,
because that is a statement about the wire format rather than about the vendor — but it is not the
default, and selecting it logs a warning, since a silently-local arm would make every hosted cost row
read $0.00.

Everything below is CP2. The retrieval stack is untouched by it.

---

## I. Why two provider families, and why they were chosen on divergence

One provider is an integration. Two are an abstraction — but only if the second is genuinely
different, otherwise the abstraction is never tested and its seams are wherever the first vendor
happened to put them.

So the pair was selected **on divergence, not on price**. Three axes were named in advance — streaming
protocol, error taxonomy, rate-limit surface — and the pair was accepted only if it diverged on all
three [VP12](EVIDENCE-PLATFORM.md#VP12). It does: one side streams a named event machine with an
in-band error frame and publishes no rate-limit headers at all; the other streams flat OpenAI chunks,
has one error shape, and returns a full header set with a retry hint. The survey was desk work over
vendor documentation, with no spend and no key upgrade.

**The condition the choice depends on is worth stating, because it is easy to void.** One of the two
providers also ships an OpenAI-compatible endpoint. Adapting it through that endpoint collapses all
three axes to near-identity with the other provider, and the whole point of the exercise — *does this
abstraction survive a genuinely different second provider* — becomes unfalsifiable. The adapter
therefore targets the **native** API. Reaching for the compatibility endpoint would be the cheap path
and would silently cancel the phase's own test.

The divergence bit immediately, and in the place least covered by a status code: **one provider
returns HTTP 200 carrying a refusal.** Content blocks live in a separate code space reached through
response metadata, not through transport status, and the other provider has no equivalent state at all
(its moderation is a separate model call). An adapter that classifies on transport status alone reports
a blocked generation as a **successful empty answer**. So *refused* is a third readiness input,
distinct from both *succeeded* and *transport-failed*, and it exists in the type system rather than
being inferred.

One consequence was accepted rather than fixed. The charter required at least one candidate to expose
configurable per-category safety thresholds, and one does. **The gate passes by its letter and
comparability fails in substance:** one arm's refusal rate is a tunable policy output and the other's
is a model-intrinsic property. Refusal rate is reportable per arm and **not comparable across the
pair** — and turning the configurable side fully off does not fix it, because that makes its floor its
own model-intrinsic behaviour, which is a different experiment.

---

## II. The gateway contract, and what it normalizes

The seam is deliberately small. **One function maps a model id onto a vendor**, and it is the only such
place; adding a provider is an adapter module plus a branch there. The API layer stays
provider-neutral: it calls one function, iterates strings, reads a dictionary. The streaming signature
— an async iterator of strings plus a caller-owned usage dictionary — survived the entire cutover
unchanged, which is what made the cutover a rewrite of one function rather than surgery across the
request path.

What the gateway normalizes is **failure vocabulary**, not response shape. Provider exceptions map to
HTTP status by *meaning*:

- quota exhausted → 429; request too large → 413; transport failure → 502;
- **a tripped spend guard → 503**, because a budget breaker is an availability statement, not a bill to
  settle or a rate to back off from;
- **a content refusal → 422**, because 502 would call a policy decision an upstream failure and a
  200-with-empty-body would report a block as an answer.

The refusal case is also caught **mid-stream**: one provider can emit a refusal after already producing
text, by which point the 200 is committed and the refusal has to become a terminal SSE error rather
than a status code.

**What is deliberately not normalized:** the free server-side latency decomposition one provider
returns in its usage object is recorded where it exists and not dropped for symmetry. Normalizing away
a signal the other side lacks is a loss disguised as tidiness.

**Multi-turn is not solved on one arm and the adapter says so loudly.** The native API refuses the turn
list format the adapter would need, and the alternative was not worked out; the adapter **raises** on
an assistant turn rather than dropping history, because a dropped turn is invisible in the answer.
Single-turn was all the request path needed at this phase; the memory work later had to live with the
constraint.

---

## III. Six wire truths the documentation got wrong

Every row below cost a live probe and would otherwise have cost a debugging session or a published
number [VP13](EVIDENCE-PLATFORM.md#VP13). This is the section to read if you take one thing from the
phase.

**1. The documented streaming query parameter does not, by itself, stream.** Passing it without also
setting the stream flag in the request **body** returns a valid 200 with a complete answer and no
streaming at all. Nothing errors and nothing warns. An adapter written from the documentation gets a
working arm whose time-to-first-token equals its total latency, whose SSE endpoint stalls until
generation completes, which passes a correctness test and silently invalidates every latency number
that arm produces. The check that catches it is asserting the **content type**, not the status code.

**2. The request schema was wrong in four places, and the dangerous one failed silently.** Three of the
four field-name errors returned 400 and were loud. The fourth — reading the usage object from the wrong
nesting level — produced a **correct streamed answer with every token count null**. No exception, no
warning: the cost layer was handed nulls and returned *unpriced*, which is the honest thing to do with
no counts, so the whole failure surfaced as a quiet empty cost column. Under the rule that an unpriced
stage makes its aggregate unpublishable, the cost run would have produced no figure for that arm at
all, or worse, one computed over only the rows that happened to parse. **All five offline fixture tests
passed against the broken adapter, before and after the fix**, because they exercised the inbound parse
and never asserted the outbound body. *An offline fixture suite for a provider adapter must assert the
outbound payload, not only the inbound parse — otherwise it verifies the half that was never in
doubt.*

**3. Reasoning is the dominant cost on both expensive arms, and it cannot be switched off.** One
provider folds reasoning into its completion token count; the other reports it as a separate field. On
the first, the flag that reads as "reasoning off" suppresses reasoning from the *response* and not
from the *invoice* — the only knob that moves the token count is the effort level, whose minimum is
`low`, not `none` [VP13](EVIDENCE-PLATFORM.md#VP13). On the second, two models in the same family sit
at opposite extremes: the small one spends zero thought tokens and the larger ones spend nearly all of
their budget on thought. This corrected an earlier register entry that had called the effect
two-sided: the structural claim was right, the token claim was wrong in **both** directions.

**4. The output-token ceiling includes thinking, and it truncates the answer.** A thinking model given
a modest ceiling returned an eight-token answer with the rest consumed by thought. Two consequences.
That arm would measure as catastrophically bad on persona and groundedness **for a reason that is not
the model**. And it is a *new* empty-completion generator, distinct from the weak-quantization cause
v1 had: **retrying it is actively harmful**, because every retry pays the full thinking cost again and
truncates identically. The truncation state in the response is what separates *empty because truncated
by thinking* (raise the budget, never retry) from *empty because the model returned nothing* (a retry
may help), and both providers report it.

**5. "Completion tokens" is not one quantity across the pair.** One folds reasoning in, the other
reports it beside. A cost table therefore needs **three** normalized columns — answer tokens, thought
tokens, and their sum — or the cross-arm comparison is not reportable at all.

**6. One provider meters per model, and the vendor's own prose said otherwise.** The limiter was first
keyed on the provider alone, citing a documentation line about limits applying at the organization
level. That phrase answers *who shares a limit*, not *whether models share one pool* — and the same
page lists different limits per model, which would be meaningless under one bucket. The remaining-quota
header settled it in seconds: calls to one model did not decrement the other's counter, so the quota
scope is the **(provider, model)** pair. That is the second time in this phase that vendor prose
disagreed with the wire, and **one header read settles what a prose argument does not.** The
corresponding claim for the other provider is **inferred, never measured**: two windows opened
separately in our own limiter and a full two-arm run produced no rejections, but that provider publishes
no rate-limit headers, so there is nothing on the wire to read. If it in fact shares one bucket, the
failure is mostly silent — rejections absorbed by transport retry into latency and burned quota.

---

## IV. Retries: two classes, one shared budget

Two failure classes, and they justify opposite defaults
[VP15](EVIDENCE-PLATFORM.md#VP15):

- **Transport** — no stream at all: a rejection, a 5xx, a connect failure, a timeout, an in-band
  provider error. Always retried. The failure is ours or the network's, and a second attempt is the
  whole remedy.
- **Quality** — the stream succeeded and produced zero tokens. Retried **only where opted in**, because
  a re-draw is a duplicated generation at full price.

**The attempt budget is shared between the classes, and that is the design decision.** Per-class
ceilings multiply: three transport attempts plus three quality attempts is nine generations for one
answer. One budget is consumed by either class, pinned by two tests — one that interleaves both failure
kinds and asserts the ceiling holds, and a converse test pinning that a budget only one class can spend
is two budgets wearing one name. Both classes retry only while **nothing has been emitted**: once a
token is out, a retry would duplicate text mid-answer, so the window closes at the first token and a
normal stream's latency is unaffected.

**The quality retry ships off by default, and the decision rule is the interesting part.** The rule was
fixed in advance and asked an **existence** question — does a zero-token completion ever occur on a
hosted arm? A single occurrence keeps the mechanism; no minimum detectable effect applies. Across every
transcript in the phase, zero-token completions were essentially absent on hosted arms
[VP15](EVIDENCE-PLATFORM.md#VP15), and the one arm that lost draws to transport errors can only
**under**-count them, never over-count — so the observed count is a lower bound, and a lower bound is
all the rule reads. **Re-running the incomplete arm could not have changed the answer.** Decision: keep
the mechanism, default it off per provider, ship the opt-in list empty. On thinking models this is not
merely an economy but a correctness argument — see wire truth 4 above.

*The decision rule determines the precision a run needs, and it was written first. Reading it before
sizing the run would have saved a day's quota.*

---

## V. Rate limits, quota scoping, and the budgets that failed

**Reservations must count what the provider counts.** The limiter reserved input tokens; the provider
meters input plus output, a ~24% overshoot that produced hundreds of real rejections in one live run
[VP15](EVIDENCE-PLATFORM.md#VP15). Every one was absorbed by a transport retry added hours earlier, so
the run completed and the miss converted into latency and wasted daily quota rather than into an error
— the second fix silently rescued the first one's absence, which is exactly how a defect of this class
survives. The reservation now counts input plus the maximum output. The request-size check stays
input-only on purpose: *does this prompt fit at all* is a different question from *how much quota does
this round trip consume*.

**Consulting one limit is not a budget.** The same error occurred at three scales — a per-minute
request count read while the token-per-minute limit bound nearly eight times harder; a daily request
count read while the daily *token* limit was the real ceiling; and a multi-leg run where each leg was
budgeted and the day was never summed. The rule: the limits object returns four ceilings — requests and
tokens, per minute and per day — **compute the ceiling from every one of them and take the minimum**.
On a long-prompt RAG workload the binding limit is usually a *token* limit, not a request count, and
the arm where the request count genuinely does bind is the arm that ran at its estimated pace and hid
the defect on the other one.

**Two mechanisms disagree about *when*, not *how much*.** One provider meters with a continuously
refilling bucket; our limiter is a sixty-second sliding window. The window admits a burst that fits the
per-minute total and the bucket refuses it because the tokens are not present at that instant. No
token-count correction fixes that; honouring the provider's own retry hint does — but honouring a
correct instruction cannot create capacity that does not exist, and with the daily token budget
exhausted the correct instruction is *wait five minutes*, which is not a retry policy. That exposed a
measurement defect worth carrying: minutes of backoff plus a second of generation was being recorded as
a multi-minute time-to-first-token, because the start timestamp sat before the retry loop. Provider
latency and user-experienced latency are different metrics, both legitimate; reporting one as the other
is not. The figure is now split three ways — first token from the attempt that produced it, time lost
to backoff, and total wait.

**Three facts stay open and are recorded rather than fixed.** The daily *token* limit — one of the two
that bind on one provider — is accepted in the config and **not enforced anywhere in code**, so there
the limit that actually ended a run is guarded by discipline alone. The windows live in one process, so
N server workers hold N sets and the effective quota is N×; the deployment runs one worker, which is
what makes a single in-process window correct today, and nothing enforces that it stays one. And
**retries consume the daily request budget** at a ratio no per-request check would surface — at this
prompt size a long run can exhaust the day through retries alone.

---

## VI. Pricing rows: what the gateway is allowed to say

The price card is infrastructure and lives here; the USD-per-answer analysis and the cost objective
built on it belong to [`11-cost-and-performance.md`](11-cost-and-performance.md) and
[VO3](EVIDENCE-OPTIMIZER.md#VO3).

A free-tier hosted call fits none of the obvious states: it has real token counts, a real published
list price, and **no bill**. Landing it as unmetered discards the list price and leaves the whole cost
argument with no number behind it; landing it as spend is a lie. The decision: **price free-tier calls
at published list rates, flag the result unmetered**, so every aggregate containing one is provisional
and every document renders it as a *list-rate estimate at the free tier*
[VP14](EVIDENCE-PLATFORM.md#VP14). The claim this licenses is stated exactly, and the difference is not
cosmetic — *"at published list rates this arm prices at X per thousand answers, measured on free-tier
quota"* is checkable by any reader against a public pricing page; *"this system costs X in production"*
is not checkable at all.

What that knowingly costs was written down when the rows were filled in: they had been commented out
on purpose, on the argument that *an unverified price in a table gets read as a measurement sooner or
later, and a wrong one is worse than a missing one*. Filling them turns that risk on deliberately,
acceptable only because the provisional flag is enforced end to end and **each row cites its pricing
page and the date it was read**. One citation was corrected in place when the vendor console proved
unreachable and the figures were confirmed from secondary trackers: the numbers are right, the citation
was not, and the row says so.

**Local compute is priced against a reference instance or not at all.** *The GPU is already paid for*
is a deployment accident, not a cost argument. A local-compute figure is reportable only as an estimate
naming a reference instance with a published hourly rate, the measured wall-clock of the stage, and the
utilization assumption — all three on the face of the number, or it is not a cost figure.

**The spend breaker is a backstop, not a working limit.** At the cheap arm's list rate its default
ceiling permits several times the request rate the quota limiter allows, so **the quota limiter binds
first and the breaker never fires under normal operation**. It exists for what the limiter cannot see:
a costly model where the request rate is within quota but the spend rate is not. It is disabled on the
live free-tier path, because there is no USD ceiling to breach and an invented one would make the guard
look like it enforced something it does not. It prices the **worst case** — estimated prompt plus a
full completion ceiling — because it must decide before calling, and over-rejecting is the correct
direction for a spend guard.

---

## VII. Routing was rejected on cost, before it was built

Routing — a cheap tier for easy queries, an expensive tier for hard ones — was the phase's most
expected feature. It was closed **by simulating the families offline over the recorded cost matrix**
rather than by implementing three routers, with the classifier's own call cost and the cascade's wasted
cheap-tier calls charged to their own totals [VP16](EVIDENCE-PLATFORM.md#VP16).

The escalation labels were **pre-registered before any arm was measured**, and every signal is
computable from the query alone, because that is all a router sees. That fixed routing's addressable
slice at well under half the evaluation set — a router cannot save anything on a row it must escalate,
by construction.

**Every routing family costs more than always sending traffic to the cheapest arm**, and not by a
margin that needed the phase's 10% separation rule: the best routed option is 74% more expensive than
the trivial policy [VP16](EVIDENCE-PLATFORM.md#VP16).

Two reasons, one economic and one structural.

**The hierarchy is inverted.** The assumed shape was a cost/capability ladder with a small fast model
at the bottom. **The cheaper arm is the larger model** — a 120-billion-parameter model on the
throughput-oriented provider prices well below a small model on the other, because that vendor's
completion-side rate dominates a long-form answer. There is no ladder to climb here.

**And routing pays only if the expensive arm buys something.** Under a non-inferiority framing, an arm
that is not distinguishably better is not worth paying more for, and this project's instrument cannot
show that it is — so routing's premium buys nothing measurable, by construction. That was reinforced by
a structural finding about online quality signals: **the fields that vary with the model are not quality
signals, and the fields that look like quality signals do not vary with the model.** Two candidate
signals exist in the retrieval path and are discarded before the stream, but retrieval happens *before*
the model is chosen, so they have zero between-arm variance and cannot arbitrate between arms at any
sample size. It is not a weak quality floor; it is structurally incapable of being one.

**What the closure does not establish, stated so it is not over-read.** The genuinely expensive tier
was never priced — its daily request quota implies more than a week for one evaluation pass — so the
conclusion is *routing is not adopted among the arms measurable on a free tier*, not *routing never
pays*. Quality did not arbitrate, by design and by circumstance. Both cost figures were taken at the
old token ceiling on truncated answers; raising it widens the gap and **strengthens** the conclusion,
but the absolute figures must be re-quoted. Downstream, routing **leaves the optimizer's search space**
and the tier set that would have been its input domain becomes moot.

---

## VIII. Prompt caching cannot engage at this prompt shape

Provider prefix caching was closed on a measurement of the prompt's own structure rather than on an
opinion [VP17](EVIDENCE-PLATFORM.md#VP17). The published 80–90% hit rates from coding agents are
**workload shape, not matching cleverness**: an agent loop's prefix is large, static and append-only, so
each turn shares essentially all of the previous turn's prefix.

Single-shot RAG is the inverse shape and close to worst case. The persona head is short and the
retrieved passages begin immediately after it in the same system message, so **the stable prefix ends
after a few dozen tokens**, against provider minimum cacheable prefixes one to two orders of magnitude
larger. The cache never engages at all. Moving the persona into its own message does not help — persona
alone is still the whole stable prefix — and raising the completion ceiling does not help, because the
ceiling governs the completion and not the prefix. An on/off pass would have measured run-to-run noise,
and the checkpoint closed without one. A correction found along the way widened the margin: an earlier
register cited a third vendor's minimum, and **neither chosen provider uses that number**.

The denominator discipline for any figure ever published here: a "90% cache hit" is 90% of **input
tokens** read at a reduced rate — not 90% of requests served free, and not a latency claim. Providers
charge a premium to write the cache and a discount to read it, so **a prefix that misses more often
than it hits costs more than not caching.** One arm exempts cached tokens from rate limits, which would
make caching a *quota* saving there — and quota, not dollars, is what this project's measurement budget
is denominated in.

The two levers that could change the answer both leave this phase: growing the static prefix past the
minimum is a quality change (it costs those tokens on every uncached call and moves the prompt away
from the fine-tune's distribution), and pinning retrieved context across turns is a memory-allocation
decision.

---

## IX. The token ceiling: a measurement break, taken deliberately

The output ceiling was raised from 400 to 1024 tokens
[VP18](EVIDENCE-PLATFORM.md#VP18), and the number was recorded as a **measurement break** rather than
as a tuning change.

Where 400 came from: it was sized so that the measured prompt plus the completion fit the local model's
context window on a 6GB GPU. **It was never a product decision about answer length**, and carrying it
into hosted serving imports the constraint the phase exists to escape — structurally the same error as
*the GPU is already paid for*.

It is an API parameter, not an instruction: **the model is never told about it**, and nothing in the
prompt asks for brevity — the persona asks for the opposite. So the request is expansive prose and the
response is cut mid-token. Truncation was not a risk under that configuration; it was the designed
outcome. The measured symptom: the overwhelming majority of answers on both arms did not end on
terminal punctuation, and on the arm that bills reasoning inside the completion count some rows spent
nearly the whole budget thinking.

What the break does and does not invalidate. **Arm-versus-arm comparisons survive** — both arms ran
the same ceiling, so truncation is a shared condition rather than a confound. **Comparisons against the
frozen local baseline do not**: the local arm rarely reached the ceiling and the hosted arms reach it
almost always, so the apparent persona and groundedness improvements compare a mostly-complete answer
set against a mostly-truncated one and must not be published as measured model improvement. **Cost
figures are unaffected in kind** — truncated tokens are billed tokens — but must be labelled with the
ceiling they were taken at.

The run was **not re-taken**, by decision: several hundred generations plus judge calls against a daily
judge quota already near its ceiling, for a comparison already known to be broken. The break is instead
**enforced in code** — the CI gate refuses a cost comparison across differing ceilings and returns the
verdict *a measurement break, not a cost delta* — so the discipline does not depend on anyone
remembering the prose.

The process fix matters more than the number. Both arms *were* smoke-tested first, and the smokes
verified that the run **executed** — exit code, cell count, provenance block — and never looked at what
it **produced**. Printing the last sixty characters of one answer would have shown a sentence stopping
mid-clause before three hundred calls were spent. **A dry run must inspect output, not exit status. A
run that completes cleanly and measures the wrong thing looks identical to success from the outside.**

---

## X. What this phase leaves open

- **Quality was never compared across provider arms.** One arm scored barely half its cells after a
  rate-limit storm, and the instrument could not have arbitrated at that sample size anyway.
- **The expensive tier was never priced**, which bounds the routing conclusion to the arms a free tier
  can deliver.
- **The daily token limit is unenforced in code** on the one provider where it binds.
- **The multi-worker limiter split is unfixed**, and correctness today rests on running one worker.
- **One provider's per-model quota separation is inferred, not measured**, because that provider
  publishes no rate-limit headers.

Full detail, every number with unit, date and provenance:
[VP12](EVIDENCE-PLATFORM.md#VP12)–[VP19](EVIDENCE-PLATFORM.md#VP19), with the never-measured list at
[VP38](EVIDENCE-PLATFORM.md#VP38).
