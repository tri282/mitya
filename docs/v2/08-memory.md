# 08 — Memory: three tiers in one budget

**Delta.** New in v2 (CP6). v1 had no conversation state of any kind: every request was independent,
context assembly was retrieved passages plus a persona head (`E16 (v1)`, `E17 (v1)`), and the API
carried a `session_id` that nothing read. This document covers what CP6 added, why it is shaped the
way it is, and the two honest halves of the result — an exact cost, and no claim about what that
cost bought.

Every number lives in [`EVIDENCE-MEMORY.md`](EVIDENCE-MEMORY.md), anchors `VM1`–`VM25`.

---

## 1. Why memory, and what "memory" was narrowed to

The frontend had always been a chat surface, and a chat surface where turn 2 cannot see turn 1 is a
demo. But "memory" as usually meant — persist the conversation, feed it back — collides head-on with
the charter's §IV constraint that the system does not persist verbatim user text, and with the harder
constraint that the prompt has a fixed size.

So the feature was narrowed before it was built. Memory here means **three named tiers with different
lifetimes and different shapes**, not a transcript store:

- **Recent turns** — verbatim, in **process memory only**, dies with the session. Never a table: a
  verbatim transcript in a database is persisted verbatim user text whatever TTL is written beside it.
  Whole turns only, because a half-turn is a transcript that lies about what was said.
- **Episodic** — one **typed** digest per finished session: normalized topic slugs, structured
  `{book, part, chapter}` references, turn count, end time.
- **Semantic profile** — one row per `(tenant, kind, key)`, `kind` ∈ `{fact, preference}`, bounded
  normalized value, coarse enums.

The narrowing is the design decision. Everything downstream — the schema, the allocator, the cache
verdict — follows from refusing to store a transcript.

## 2. One budget, and the competition that is the point

The three tiers do not get slots. They compete for **one** token budget against the retrieved
passages, and passages win by policy: the shipped allocator admits retrieval **whole** and gives
memory what is left. Passages are never truncated to make room for memory, because cutting a passage
mid-sentence corrupts the grounding the system exists to provide.

That competition is the architecture, not a side effect. It is also why memory is not a free feature:
every token a tier takes is a token of substance the answer does not have.

## 3. No free-text column anywhere in the schema

The input design proposed "a typed profile plus a rolling episodic digest" and did not notice that a
session summary produced by a language model **is free text, and free text can quote the user
verbatim**. An episodic tier with a text column is the rejected option wearing the accepted option's
name.

Decided: **no free-text column anywhere in the memory schema**. Not a policy against quoting — a
schema with nowhere for a paragraph to go. Two tables, no migrations, no text field, and a test that
fails if a table for recent turns ever appears.

**The honest bound.** This buys paragraph-scale non-recovery structurally: values are bounded, at 80
characters ([VM2](EVIDENCE-MEMORY.md#VM2)), lowercased, whitespace-collapsed and punctuation-stripped
at write. It does **not** buy clause-scale non-recovery — 80 characters is ample for a verbatim
clause. The last write-path check is a contiguous-span echo test against the turn in hand; it blocks a
verbatim echo, not a paraphrase, a reordering or a short span, and says nothing about any other turn.

**The schema stops a paragraph. It does not stop a sentence.** That correction was forced by
measurement, not by review: the injection set wrote well-formed, short, schema-valid rows whose
*values* were hostile ([VM11](EVIDENCE-MEMORY.md#VM11)). The typed schema bounds format, not content.
Claiming "structural" without that qualifier would be an invariant defended by a test that cannot
fail.

## 4. The allocator as a seam, deliberately without a winner

Allocation is a policy object: same inputs (tenant, session, query, budget), same output — a plan of
blocks carrying tokens and provenance per block. Adding a policy is a subclass plus a registry entry;
selecting one is a single versioned config field.

Two policies ship. `passages_first` is the shipped behaviour above; `passages_only` exists as the
starved control that makes the seam testable. **CP6 built the seam and deliberately did not pick a
winner**, because picking one requires a metric that can tell allocations apart and no such metric
exists here (§10). The extension point is real, the extension is not made, and the reason is written
down rather than deferred silently.

## 5. Sizing: the raise that made a tier real

The memory budget was never chosen. It fell out of one config field that meant two unrelated things —
the local GPU's context fit and the hosted path's self-imposed input cap — leaving **306 tokens**
after the measured prompt draw ([VM3](EVIDENCE-MEMORY.md#VM3)).

At 306 tokens, with a whole-turns-only admission rule, the recent-turns tier contributed **zero
blocks on essentially every turn against a live provider**. It had appeared to work in development
only because the fake provider replays a 67-character fixture. The budget was raised, and the raise
is what made the tier real.

Three consequences were recorded rather than absorbed: the raise makes CP6's serving cost and latency
rows a **new arm** not comparable to earlier phases', it demotes cross-phase cost comparison to
indicative, and it does **not** change the local rule — a llama-server run still promotes an artifact
at the smaller context. The raise is enabling, not a quality bet: it makes a shipped tier exercised at
all, which is a statement about the instrument, not about answers.

## 6. The lazy episodic trigger

Session *k−1* is summarized on the first turn of session *k* for the same tenant.

There is no session-end signal. HTTP does not have one, SSE does not have one, and inventing one means
a new endpoint, a client contract, and a class of bugs where the client never calls it. The lazy
trigger needs none of that: the next session's first turn is a reliable, already-authenticated event
proving the previous one ended. The stated cost, accepted rather than engineered around: **a tenant's
last session is never summarized**, because it has no successor
([VM7](EVIDENCE-MEMORY.md#VM7)).

The write itself is an ordinary gateway consumer on a cheaper model, in band, post-answer, in the same
admission slot — so its cost lands in cost attribution rather than hiding. A failed write never fails
the turn; the answer is already delivered, and the failure is logged and counted.

## 7. Retention, caps, purge

**Retention** is one time-boxed staleness window over both persisted tiers, enforced lazily on the
write path and at read, never by a job — following the existing precedent of one `DELETE` on a path
that already holds the connection. The 90-day default is an `[estimate]` and is never promoted: it is
a user-data posture, not a finding.

It ships **implemented and unexercised**. The corpus carries no timestamps at all, and under replay a
time-boxed window is crossed only if the harness's own wall clock exceeds it — which would measure
harness pacing, not retention. A turn-count window tight enough to fire would have rested on
single-digit counts ([VM4](EVIDENCE-MEMORY.md#VM4)), below the threshold at which this project agrees
to call something a number. Validated by a unit test with an injected clock, never by a run, and said
so.

**Caps** are separate from the budget and the distinction is load-bearing: profile and episodic caps
are *store* caps, the memory budget is the *injection* cap. To a tuner they look interchangeable, and
they are separated by name and docstring only — mitigated by naming, not by anything structural. The
episodic cap is the first cap in this project to fire against real data
([VM7](EVIDENCE-MEMORY.md#VM7)), and its eviction was observed to be clean recency rather than
asserted to be.

**Purge is a `DELETE`.** The load-bearing property is not "rows are per-tenant" — it is that **no
persisted row mixes two tenants' content**. Stated that way it is assertable, and it is asserted: purge
tenant A, read zero for A and unchanged for B, on both tables and the process store. Cascade from the
accounts table carries onto both.

One honest limit: purge is complete only under single-process operation. The process-memory tier is
emptied for this worker; a second worker would hold a second copy no `DELETE` reaches. Recorded as a
consequence of an assumption the system already made, not as a new one.

## 8. What memory costs, and what the phase cannot say it bought

Memory costs **1.328x per session** ([VM5](EVIDENCE-MEMORY.md#VM5)).

Read that baseline carefully: it is against **CP6's own no-memory arm** — the same corpus, the same
context ceiling, the same models, differing in one config field. It is not against v1 and not against
CP5. That arm's known-answer check passed before its numbers were read: zero summarizer draws, zero
summarizer cost, an unchanged store.

The split matters more than the multiple. Roughly three fifths of the delta is **serving** — the
larger prompt — and two fifths is the **write**. A memory design that counted only its write cost
would understate the bill by nearly two thirds.

Two qualifiers travel with the multiple: it is specific to this corpus's turn lengths and to the
episodic cap, and CP6's serving rows are a new arm (§5), so cross-phase comparison is indicative only.

**And the phase cannot say what that 32.8% bought.** Cost is exact; quality is not measurable here at
all. That is not a hedge, it is the finding, and the open question **O4 is not retired**.

**Utilization is not value.** Memory took a large minority of all context tokens
([VM6](EVIDENCE-MEMORY.md#VM6)), with the two persisted tiers behaving differently — recent turns flat
per turn, episodic accumulating as sessions complete, bounded only by its cap. That says which tiers
*consumed* budget. It says nothing about which was *worth* it, and the two must not be conflated.

**Storage is not a concern.** Retained bytes per session per tenant are negligible against the
telemetry the same request already writes ([VM7](EVIDENCE-MEMORY.md#VM7)).

## 9. The cache question closed on a measurement, not a principle

The intuition was that memory forces a per-tenant cache and costs some hit rate. The measurement says
something sharper: **putting memory state in the key does not reduce the hit rate, it eliminates it**
([VM9](EVIDENCE-MEMORY.md#VM9)).

The mechanism is visible once measured. The cache's entire value on this corpus lives in *duplicate
follow-ups across different sessions* — and any session-scoped key component separates exactly those.
A per-session fingerprint takes the hit rate to zero with collisions structurally available and none
observed.

The middle ground is a leak, not a compromise. A coarse bucketed fingerprint recovers some hit rate by
mapping differing memory states onto one key, which is precisely the cross-tenant serve the threat
model names. **Its recovered hit rate and its cross-tenant collision rate are the same number**, and
the curve has no knee.

So: **memory-bearing requests bypass the cache entirely, the tenant never enters the key, and the
per-tenant cache is never built.** The tripwire CP5 wrote — designed to start failing the moment
per-tenant memory reached the prompt — fired, and was amended *with* the decision to assert the bypass
rather than the key component, keeping its own can-this-test-fail companion.

Two things this closes honestly. The charter row claiming cache keys include tenant and memory state
was **corrected in place**: the tenant does not enter the key and never will. And the surviving cache
may be worth close to nothing — its population is memory-free first turns, all distinct on this
corpus. It is retained on an estimate-grade benefit for real traffic, and its hit rate is henceforth
reported over the memory-free population only, never pooled.

Related, and closed the same way: the memory-ordering idea for provider prefix caching — pin the
profile block ahead of the per-turn passages so more of the prompt is stable — **does not fire**. What
varies per turn is the passages block, and it sits ahead of anything a memory tier could pin
([VM10](EVIDENCE-MEMORY.md#VM10)).

## 10. Why there is no multi-turn success metric, and why O4 stays open

A metric was built, and it was **rejected by its own pre-registered test**: an instrument that cannot
separate a deliberately starved allocation from the full one is rejected whatever else it recommends.

It moved **0.000** on every scored family ([VM8](EVIDENCE-MEMORY.md#VM8)) while the arms provably
differed — dozens of memory blocks and thousands of plan tokens withheld, served passages byte-identical
across the two. The reason is structural, and it was written into the module *before* either arm ran:
retrieval is issued on the query alone and reads no transcript, so an allocation policy that changes
only which *memory* tiers reach the prompt cannot move a metric scored on *retrieved chapters*. The
metric is arm-invariant by construction.

The other two candidate metric families were checked against the corpus exhaustively and neither
exists: no per-turn gold for task success or answer quality, so both need authored gold or a metered
judge on a corpus already declared unvalidated. And a judge could not have retired O4 anyway — the
charter makes judges reporting instruments that never gate. Closed on: **ship the feature, drop the
success rows.**

What the rejected metric becomes is worth keeping: the same invariance that disqualifies it as a value
metric makes it a sound **retrieval-damage guard**. It cannot show memory helping; it can show an
allocation crowding out the passages block, and any future policy must run it and assert
non-inferiority against the recorded baseline.

**Residual risk, stated:** the phase ships a memory system whose value to answer quality is
**unmeasured, not demonstrated**. If memory adds nothing, nothing in CP6 or CP7 would detect it.

## 11. The `profile` tier is UNEXERCISED, never "unused"

The profile tier wrote **zero rows** across the entire corpus run
([VM6](EVIDENCE-MEMORY.md#VM6)) — and the correct reading of that zero is a property of the corpus,
not of the tier.

The known-answer check establishes it directly. Fed a turn stating user facts and a preference, the
extractor returns typed rows in exactly the designed shape — typed keys, bounded values, coarse enums,
no free text. Fed a corpus turn, it returns the empty list. The corpus is literary questions about
Dostoevsky, authored for retrieval evaluation; it carries no user preferences and no user facts. **An
empty extraction is the correct output on every one of those turns.**

Profile *storage*, eviction, retention, purge and *injection* are covered — injection by the security
set, which plants rows directly. **Extraction from realistic conversational traffic is covered by
nothing in this work**, and no artifact here may be read as covering it.

A shape worth carrying beyond this project: **a failed write and an empty extraction are the same
value in the artifact.** The extractor catches everything and returns zero rows, correctly, since a
failed write must not fail the turn — so the only things separating "wrote nothing" from "found
nothing" are a log line and a failure counter. Both exist, and the run's artifact carries the counter
rather than the row count alone. This was found because the probe's own first pass reported zero rows
with valid extracted rows behind it.

## 12. What memory never measured

Beyond §8 and §10:

- **No in-band write latency delta exists.** The obligation was live — report the turn latency delta,
  timestamped at the answer-done event — and the run produced no such number
  ([VM25](EVIDENCE-MEMORY.md#VM25)). The write is in band, so slot occupancy grew by the write's
  duration and that growth is **unmeasured, not zero**. The pre-memory worst-case admitted-wait
  arithmetic no longer holds on turns where a write fires and must not be quoted stale. **Memory's
  latency cost cannot be stated at all.**
- **The memory budget is not enforced.** The allocator charges raw turn text against the budget but
  emits the *fenced* rendered text, so the fence is never charged, and the error propagates into the
  tiers budgeted after it ([VM22](EVIDENCE-MEMORY.md#VM22)). The overshoot is largest as a fraction
  exactly where a cost search would push. Recorded, unfixed, and load-bearing: the fix churns the
  config version, so it is a pre-batch defect rather than a drive-by edit. What was served remains
  correctly measured; what it is *not* is a measurement of "memory at the configured budget".
- **The gate's cost layer is blind to every memory axis but one** — it reports exactly zero for the
  budget and all three caps ([VM22](EVIDENCE-MEMORY.md#VM22)), on the axis that consumes a large
  minority of context. Three of those are worse than blind: the caps are enforced on the write path
  only, so lowering one has a *delayed* effect that a gate run generates no write traffic to reveal.
- **No cross-session memory number.** Persistence is asserted at the store and at one in-process
  request; no measured cross-session claim exists ([VM4](EVIDENCE-MEMORY.md#VM4)).
- **Retrieval remains blind to the conversation**, and memory does not close it: memory reaches the
  prompt for *generation* and never reaches the retriever
  ([VM25](EVIDENCE-MEMORY.md#VM25)).

## 13. The one-line summary

Memory ships as three typed tiers competing for one budget behind a swappable allocator seam, with a
schema that has nowhere to put a paragraph, retention and per-tenant purge implemented, and a cache
question closed by measuring it rather than assuming it. It costs a third more per session, its
security fence does not hold ([`09-prompt-security.md`](09-prompt-security.md)), and **the project
cannot say what it bought.** All three of those are the result.
