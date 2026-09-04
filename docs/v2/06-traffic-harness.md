# 06 — The traffic harness: concurrency, the knee, faults, and the degradation ladder

**Delta:** new in v2. v1 had no load harness, no fault injection and no ladder walk. What v1 did
carry was an `asyncio.Semaphore(1)` and a **qualitative** statement that concurrent submits queue
rather than fail — `[E20 (v1)]`, on branch `main`. v1 published no concurrency number at all, so
every figure here is a first measurement, not an improvement over a recorded one.

---

## 1. Why build a harness for a system with one user

The deployment has exactly one real user and no real traffic, at any point, ever. That is D3, a
scope decision, not a gap. It is also the reason the harness had to exist: **you cannot claim
reliability you have never loaded.** v1's reliability claims were all structural — a semaphore of
one, a 503 on an unreachable backend — and structural claims are exactly the kind that survive
review and fail under traffic. v2's delivery question ("can the system *prove* it stays this way")
is unanswerable without a load shape someone can re-run.

The harness speaks only the public API, enforced mechanically rather than by discipline: a grep for
application imports under `src/harness/` returns empty ([VP32](EVIDENCE-PLATFORM.md#VP32)). Anything
the API does not expose, the harness cannot see — which turned out to produce findings rather than
limitations (§8).

---

## 2. The session corpus: pre-registered, and unvalidated **by choice**

The corpus is 40 hand-authored multi-turn sessions across five families, with per-family counts
fixed **before** generation and a freeze script that refuses to freeze a composition drifting from
the pre-registration ([VP26](EVIDENCE-PLATFORM.md#VP26)). The pre-registration is enforced, not
trusted.

**Its resemblance to real usage is unproven, and was not tested.** This is the load-bearing
sentence. Four validation options were on the table; the one taken was *explicitly unvalidated,
every dependent claim labelled*. The reason is not laziness — it is that there is no real traffic to
compare against, so every available check compares synthetic to synthetic. Manufacturing a validity
number there produces something that **looks like** evidence and is not. The label travels inside
the artifact (`corpus["label"]`), not only in a findings file, because a label a downstream consumer
never sees is not a label.

The composition is a **measurability** choice, not a realism claim. Half the sessions carry a
literal span precisely because that is what makes the lexical retrieval leg observable at all; the
zero-literal thematic family exists as the control without which the literal families measure
nothing. Nobody knows what fraction of real traffic is pasted passages, and the chosen fraction is
very likely too high. What makes that admissible rather than a thumb on the scale is that **every
figure is reported per family and never pooled** — a later reweighting is arithmetic on numbers
already in hand rather than a regeneration. A pooled headline computed across these five families
means the safeguard has been lost.

The cache repeat rate is a **replay-time dial** rather than baked into the artifact — baking either
point in would mean two corpora and two version stamps. Five defects were caught by dry runs before
generation, every one of which would have been invisible in a run that completed successfully:
literal spans that all began mid-sentence, three families with zero sessions from the longest novel,
a hardcoded turn count, and a template that would have misgendered half the characters in its own
query text.

**The session corpus was re-frozen twice** (`4155c97b0a2b` → `49b525f2a131` → `be54610a7f78`), with
the optimizer's anaphora work. Numbers are not comparable across a stamp, and every CP4 figure is
stamped at the first. The **document** corpus `515c707a5d69` never changed; conflating the two
objects is a mistake earlier drafts made.

---

## 3. The knee was 1, and the cause was the lock rather than the GPU

Ramping concurrency produced flat throughput and linearly scaling latency on **both** arms — a
metered provider arm and a fake arm where generation is a no-op. Adding concurrency bought zero
throughput and cost proportional latency; the inferred semaphore wait was almost all of the added
time ([VP27](EVIDENCE-PLATFORM.md#VP27)).

The fake arm is what makes this diagnostic. **The lock is the wall even on a server that is not
generating**, so the wall cannot be the model, the provider, or the GPU that serving had already
left. v1's `Semaphore(1)` was justified by `--parallel 1` on llama-server and a non-re-entrant CPU
embedder; generation left the box in CP2, and the measurement shows that justification is now gone.
The two arms agreeing on knee, shape and zero-rejection behaviour is also the cross-check the
fake-arm argument needed — a fake ramp does not distort the conclusion, so future ramps can run at
zero spend without caveat.

**Then the premise under the whole decision broke.** A source census found that `/chat` is `async`
but every retrieval call inside it is plain sync, with no thread offload anywhere in `src/`, on a
single-worker uvicorn. Two retrievals could never interleave in the first place: **the mutual
exclusion comes from the event loop, not from the semaphore.** The semaphore only ever stopped
*generation* overlapping. That reframed the decision from "is the lock too coarse" to "the lock is
guarding something the runtime already guards, and is also holding generation for no reason".

---

## 4. Narrowing the lock, and the control that starved

The disposal was to keep the lock and **scope it to route → cache → retrieval → context, released
before the messages are built**, so generation and the SSE stream run outside it. A higher semaphore
count was rejected on hardware fit: the global lock is an artifact of a constraint that has left the
request path, and a larger N would have been a number with no evidence behind it. Removing the lock
outright was measurably identical *today* given the event-loop finding, and lost the tie on cost
asymmetry — keeping a redundant guard costs one semaphore, removing it costs a thread-safety audit
nobody has done.

**The knee moved from 1 to 4** ([VP27](EVIDENCE-PLATFORM.md#VP27)). The prediction was written
before the run as a conjunction of a throughput floor and a latency ceiling; throughput passed
comfortably and **latency missed by 5%**. That is recorded rather than softened. What did not happen
is the disposal condition — the curve did not come back flat — so the decision stands with a partial
miss on its record. Concurrency 1 was unchanged, which is the confirmation that the mechanism is
what it was claimed to be: with one request in flight there is nothing to overlap, so nothing should
move, and nothing did.

**The control did not behave as framed, and that is what makes the gain attributable.** The fake arm
was expected to be unchanged. It was not — its distribution went bimodal, with several requests
starved for a minute — **but it gained no throughput**. Had both arms improved equally, the
mechanism claim would have been unsupported; the effect is *overlapping generation*, which the fake
arm structurally does not have. A fake-only run would have looked identical to a failed decision.

The starvation is carried as a real limitation rather than explained away: under any workload where
retrieval dominates the request, this change can produce an unfair latency distribution. CP5
inherits it as a property to check rather than assume — see
[`07-auth-and-tenancy.md`](07-auth-and-tenancy.md).

---

## 5. The system never rejects

At every concurrency level, on both arms: zero local 429s, zero provider 502s, and p99 degrading
without a single request erroring ([VP28](EVIDENCE-PLATFORM.md#VP28)). **The system queues silently
and unboundedly, reaching a 62-second p99 at a 100% success rate.**

This is the number CP5 designs its bound against, which means CP5 had a measurement rather than an
assumption to size to. It is also the worst shape a reliability failure can take: every request
succeeds, no error is emitted, and the only symptom is a tail nobody is watching. Queue depth itself
is unobservable (§8) and is emitted as an explicit unobservable marker rather than guessed.

---

## 6. Fault injection, and the honest denominator of 1

Before building an injector, a census asked the only question that matters for fidelity: **has this
project ever seen a genuine occurrence of this fault class, and what did the client see?**
([VP29](EVIDENCE-PLATFORM.md#VP29)) The answer for provider timeouts, in-band refusals and app-side
budget exhaustion is no. The answer for provider 429s is yes, in bulk — but every one was absorbed
below the API boundary by transport retry, so **no 429 has ever traversed `/chat`**.

The injector wraps the gateway rather than the client, because the two providers disagree on the
wire — one sends `Retry-After` on every 429, the other no rate-limit headers at all — so a
client-derived taxonomy would not survive a model swap. It sits behind one deployment-scoped env
var: a fault toggle changes what the system does under failure, not what a correct answer is, so it
is not a versioned field and costs no config-version churn.

**Applying the faithfulness bar honestly forces a denominator of 1.** Of eight injectable rows,
exactly one has a genuine recorded client signature; the other seven are fidelity-unverified and
excluded from both halves of any rate. So the taxonomy ships as a per-class table under a fixed
headline and **never as a percentage** — `1/1` is not a publishable rate. The bar was not weakened
to avoid this, and the small denominator is the finding rather than a failure of the checkpoint.

The one verified row paid for itself: an injected 429 with a two-second `Retry-After` produced two
HTTP 200s whose time-to-first-token landed inside a band predicted before the run
([VP29](EVIDENCE-PLATFORM.md#VP29)). **That is the first evidence anywhere in the repo, at any layer,
that the provider's stated wait is honoured on the real serving path.** The injector also logs at
every activation that this run's answers are not real — a fault flag left set during a measurement
run is the same failure as a quality number computed on answers that were never produced.

---

## 7. The degradation ladder: three of five rungs, and it descends on the wrong thing

The ladder existed in code and was **not wired into the request path**: its decision function had
zero call sites outside its own definition, the package re-export and nine test assertions
([VP30](EVIDENCE-PLATFORM.md#VP30)). A pattern rather than an incident — the gateway's retry policy
had the same shape, and the two retry implementations had diverged on the one field added as a bug
fix, so promoting the gateway loop into the request path would have silently reverted it. Deleted
rather than reconciled.

Wiring required fixing the readiness probe first, and the reason is the interesting part.
`generation_available` was derived from an unconditional health query against **llama-server**,
whichever provider was actually serving — a fact about a process that no longer generates anything.
Wiring the ladder against that input would have made it descend on every request while the provider
was fine, turning a dormant defect into a serving one.

The fix narrowed what the rung *means*, and nobody predicted it. Readiness is now config-only and
deliberately not a live probe, because the SPA polls it every few seconds against a provider whose
free tier allows a handful of requests per minute — a liveness probe would spend the quota the
deployment exists to serve with. The consequence:

> `generation_available` means "configured and credentialed", not "the provider is reachable".

**On a provider outage the ladder does not descend at all.** The request fails the way it always
did. The rungs below primary are entered essentially when the deployment is *misconfigured*. The
stated goal is met and the checkpoint is simultaneously answering a narrower question than the
charter assumed: *"is retrieval-only an acceptable product floor?"* reads differently when the
condition that enters it is **the operator forgot the key** rather than **the provider is down**.
Compounding it, the degraded flag is read in several places and assigned `True` nowhere, so the
spend breaker never reaches the ladder either.

**Coverage is 3 of 5 rungs walked, against an attainable ceiling of 4/5**
([VP30](EVIDENCE-PLATFORM.md#VP30)), and the fraction never appears without its caption: the cheaper
rung is unreachable by construction because readiness folds primary and cheaper into one flag and
routing was closed as not-adopted ([VP16](EVIDENCE-PLATFORM.md#VP16)). The unavailable rung was not
walked because entering it means breaking the vector store on purpose.

The walk disproved the claim that the cached rung was forceable over HTTP — **wrong in a register
and in a shipped docstring**, since the decision function returns primary whenever generation is
available, regardless of a cache hit. Only the top rung is harness-forceable, so the ladder walk is
partly a documented manual procedure rather than an automated sweep.

It also showed that the retrieval-only rung serves **an HTTP 200 with an empty answer body and three
source ids**.
Retrieval worked; there is no prose at all. A client rendering only the answer text shows a blank
successful response, which is arguably worse than an error: a 503 is legible as failure, an empty
200 is not.

> **Decided, not built.** The disposal is that the rung stays internal and users get a 503. **Shipped
> behaviour is still the empty 200.** Implementing it is a real change to `/chat` and carries two
> unsettled riders: how the rung stays observable to operators, and that a fourth producer of 503
> arrives with no discriminator, since 503 is already overloaded three ways.

The decision was also taken on **two of the three observations its own rule required** — the SPA
render was never observed. That is recorded as such rather than presented as a satisfied rule.

---

## 8. Three numbers the harness cannot see, and all three are findings about the API

The rule: a number the harness cannot see from outside, plus the operator plane, is **a finding about
the API**, not a licence to reach inside. All three were produced by reading source, at zero
marginal cost, before any run ([VP31](EVIDENCE-PLATFORM.md#VP31)).

**Token counts never cross the SSE boundary.** The meta event carries cost as a scalar with no
denominator. An external client cannot compute cost per token, cannot verify a price against the
rate card, and — most practically — cannot size a run against the token limits that are the binding
ones on one of the two providers.

**Five distinct failure causes collapse into one opaque 502.** A rate limit, a connect failure, a
read timeout, a provider 5xx and an in-band error frame all arrive as the identical string under the
identical status. This is a hard ceiling on the error taxonomy that no injector design can raise:
which fault produced a 502 is not recoverable from outside. It is also why the fault work loses
*diagnostic* value rather than denominator — the one fidelity-verified row expects a 200, so the
collapse constrains exactly the rows already excluded.

**There is no gauge.** Every instrument is a histogram or a counter; histograms record what already
completed, and nothing reports live state. Queue depth and in-flight count are published nowhere,
which means waiting requests are invisible until they finish and **a pile-up is indistinguishable
from a slow provider** from the operator plane. This is precisely the quantity the saturation work
needed, and precisely the one nothing emits. It lands on CP5's bounded-concurrency work too, which
needs a gauge to show its bound is working.

A fourth asymmetry, found by the dry-run rule rather than by reading: **the default serving path
emits no terminal SSE event**, where the cached, structural and error paths all do. The path that
serves most traffic is the one on which a client cannot distinguish a completed answer from a
truncated one. Errors stay detectable; what is missing is the positive confirmation of success —
which is exactly what the "p99 degrades without anything erroring" observation blinds. Open.

---

## 9. What CP4's own instruments got wrong

Aggregated in [`12-instruments.md`](12-instruments.md); the phase's own share is worth stating here
because four of the six premises that broke were **in documents that gated work**
([VP32](EVIDENCE-PLATFORM.md#VP32)) — a design gap register, a research register, an approval
document, an implementation plan, and one shipped docstring.

Two were expensive and were avoided rather than paid: a plan premised on a provider seam that did
not exist at the serving layer, caught at preflight before a metered run was spent; and a
lock-narrowing design that assumed every abort site sat inside the critical section, when five of
eight sat after the release point — shipping it would have turned **every provider error into a
500**, in exactly the region the fault taxonomy was about to be measured in.

The dry-run rule paid for itself three times in one batch: a ramp that measured the **cache** rather
than the system (the first pass warms it; the ramp cycles its query list by design, so every later
level replays warmed queries and the curve degenerates into cache-lookup latency), a client timeout
silently truncating two levels, and a result object that retained no answer text so a dry run could
not show what came back. **All three produced clean HTTP 200s and looked exactly like success.** The
first is now a permanent run condition: a ramp runs with the cache disabled, and a cache-enabled
curve is a legitimate measurement *of the cache* that must never be pooled with one of the system.

One result had to be read backwards. Identical concurrent queries returned identical source ids
across hundreds of requests, which reads as a thread-safety pass. It is not: the retrieval it
exercised was **already serialized by the runtime**, so it is a determinism detector — worth keeping,
and silent on whether the lock could be removed. Both the run's own closing paragraph and the
checkpoint rule as written read it the other way round. Flagged open: it becomes a false safety
claim the moment retrieval is offloaded to a thread or a second worker is added.

---

## 10. What this leaves open

- **Sustained throughput was never measured.** The headline post-change rate is a *burst* figure from
  five requests over eighteen seconds, which never fills the limiter's sliding window; a longer run
  would clamp lower ([VP27](EVIDENCE-PLATFORM.md#VP27), [VP38](EVIDENCE-PLATFORM.md#VP38)).
- **Thread-safety was never established**, and nothing in the harness establishes it
  ([VP38](EVIDENCE-PLATFORM.md#VP38)).
- **Backpressure was never achieved, only relocated** — see
  [`07-auth-and-tenancy.md`](07-auth-and-tenancy.md) §5.
- The retrieval-only 503, the terminal-event gap, the ladder's misconfiguration-only descent, and the
  mislabelled corruption check are all carried forward in
  [`13-decisions-and-open.md`](13-decisions-and-open.md).
