# 07 — Auth and tenancy: identity, admission, and fairness as a non-result

**Delta:** new in v2. v1 shipped unauthenticated and single-user by decision — no session, no token,
no tenancy, no rate limit, both processes bound to loopback `[E19 (v1)]`, `[E30 (v1)]`. The one
tenancy-shaped input v1's successor had, an inbound `X-Tenant-ID` header, was **removed** by this
phase rather than supplemented.

---

## 1. Why authenticate a system with one user

The same question the harness had to answer, with a different answer. D3 makes the deployment
multi-tenant **by design** — the operator plane, the metric label budget, the quota shape and the
memory work all assume more than one account exists — so identity could not stay a convention. CP1
had already added a tenant dimension threaded through the ContextVar, the logging filter and the
root span, and had documented in three separate places that its input was an unauthenticated
client-controlled header, bounded to *"until P5 makes identity real"*
([VP33](EVIDENCE-PLATFORM.md#VP33)). That was a deliberate deferral coming due, not a defect.

**The shape of coming due was not "add a source".** An authenticated tenant that an unauthenticated
header can still override is not authenticated, so the header had to be retired. Supplementing it
would have left an impersonation primitive in place behind a login wall — the worst of both, since
the login wall makes the system *look* protected. The cost of retiring it was three edits in the
observability module, one in the app, and two existing tests flipped from asserting the header wins
to asserting it is ignored.

That also settles a premise the phase plan asserted. Read strictly — *"nothing should need
re-instrumenting"* — it is **false**, because the intake path did. Read as *"no downstream consumer
of the tenant dimension needs re-instrumenting"* it is **true**: zero call sites below the binding
point were touched. The distinction matters because only the strict reading catches an intake path
nobody re-examined.

---

## 2. The posture, and why each clause is the one it is

Every clause of the charter's auth requirement was implemented without relaxation, covered by unit
tests and by live probes against a running server ([VP33](EVIDENCE-PLATFORM.md#VP33)).

| clause | the choice |
|---|---|
| password hashing | **argon2id**, via the OpenSSL binding in an already-installed transitive dependency |
| session tokens | **opaque, server-side** — not JWT |
| cookie | `HttpOnly` + `Secure` + `SameSite=Lax`, **rotated on login** |
| login rate limits | per-account **and** per-IP, and they **reject rather than sleep** |
| enumeration | unknown account and wrong password indistinguishable by **status, body and timing** |
| registration | closed; accounts seeded by a script — no admin UI, no invite tokens |
| harness credentials | **real logins**, because a test-only credential path is an auth bypass by another name |

**argon2id needed no new dependency.** `cryptography` was already installed and exports an
`Argon2id` KDF; no password-hashing library was added. One thing is asserted from documentation and
is **unverified on this machine**: the binding requires a recent OpenSSL and raises on first use
rather than at import. The startup path probes it and **refuses to boot** if it is unavailable, and
the hasher test fails loudly. A fallback KDF was deliberately not written — the charter names
argon2id, and a silent fallback to a weaker KDF would satisfy the code and violate the charter.

**Opaque server-side sessions rather than JWT** is the choice with the most downstream consequence.
Authorization is re-derived per request from a server-side row, so revocation is immediate and no
signed claim in the client's hands stays valid until it expires. At this scale that costs one lookup
in a stdlib `sqlite3` file — no ORM, no migration tool, single writer, later phases add tables the
same way. **The rate limits reject rather than sleep**, the same property the admission gate is
built on: a limiter that waits is an unbounded queue wearing a limiter's name. **Enumeration is
closed on three axes**; matching status and body is the easy half, and the timing half is why the
probe exists — and why the probe itself was the thing that broke (§6).

Two operational facts that are not footnotes:

- **`Secure` cookies and plain-HTTP local runs are incompatible.** TLS terminates in front, so
  `Secure` is correct in deployment — but every run this project performs speaks plain
  `http://127.0.0.1`, where a client accepts the cookie and never sends it back. The harness would
  log in successfully and 401 on every subsequent request **with no error naming the cause**. It is
  a config flag defaulting to on, warned about at startup, with the client logging a specific
  warning on the exact silent failure. The consequence travels with every CP5 number: **every figure
  was produced with `Secure` off**, and the probe list says so rather than reporting a pass from a
  run where the flag was disabled.
- **Session expiry is two cases and only one is a 401.** Before the answer opens, the request 401s
  and the login wall drops in without a page reload. After the first token, **nothing** — the 200 is
  committed and the status line is long gone. That is HTTP, not an oversight, and any design
  promising a mid-answer bounce to the login wall is promising something the transport does not
  offer. What is guaranteed is that the *next* request fails closed, which it does because
  authorization is re-derived per request from a server-side row.

One confused-deputy path was found by reading rather than by a failure: the anonymous tenant has a
reserved name, and an account permitted to take that name would authenticate successfully and then
be treated as anonymous on every subsequent request, with no error anywhere. Account creation now
refuses it.

---

## 3. Auth costs nothing measurable

Per-request p50 with auth on the fake arm with the cache disabled came in at
**2,045 ms against a pre-auth 2,194 ms** on the same arm and level
([VP33](EVIDENCE-PLATFORM.md#VP33)) — a middleware cost below the noise of retrieval.

**The baseline is P4's pre-auth figure, not v1's.** v1 was a different serving stack on different
hardware with generation on the local GPU; comparing auth overhead across that boundary would be
comparing two unrelated things and getting a large, meaningless number. Zero metered draws was the
right instrument rather than a concession: auth, admission and fairness are provider-independent,
and the fake arm removes the provider variance that would otherwise swamp a millisecond-scale
measurement.

**The comparison is valid only at the two lowest concurrency levels**, and that is a consequence of
the admission gate rather than a caveat about the clock: above the per-tenant cap a single-tenant
ramp is no longer a latency curve at all, it is an admission curve. CP4's higher ramp levels are
therefore **not reproducible post-CP5 for one tenant**.

---

## 4. Multi-tenancy plumbing: a label budget, and a test that is vacuous on purpose

`tenant` becomes a metric label at CP5 and not before, on a rule CP1 wrote: it is admissible once
identity is authenticated and the tenant set is therefore bounded and knowable
([VP36](EVIDENCE-PLATFORM.md#VP36)). Both halves now hold — the value comes from an authenticated
session, and the set is a table only a seeding script can add to.

**The budget is not enforced by trusting that table.** A single key function is the only path a
label value can take: a valid in-budget id passes, anything failing the shape collapses to
`unknown`, and a valid id arriving past the configured distinct-label ceiling collapses to `other`,
logged once. The two sinks are deliberately distinct — collapsing them would hide a cardinality
breach behind what looks like malformed input. The test moved rather than relaxed: `tenant` must
appear on **exactly** the allowlisted instruments and nowhere else, a malformed value must collapse,
and an over-budget value must collapse **differently**. A test asserting only that the label exists
would pass on the unbounded implementation the rule exists to prevent.

**The cross-tenant cache-leak test is vacuous by construction, and it was written as a tripwire.**
Two documents gave opposite instructions for the same line of code — the charter says the answer
cache is deliberately not tenant-keyed while the corpus is shared, a later decision file said
tenant-key it. The later date does not settle it, because the later document was not written against
the earlier one, and its stated rationale reasoned about diff size rather than about the
shared-corpus premise — and is self-defeating, since tenant-keying destroys the cost saving it cites
protecting. **It was retracted, and the cache is unmodified across two batches.**

So the deliverable is a *record*, and the ordering of its four assertions is the argument: the key
function takes no tenant parameter, so a caller cannot key on tenant even by accident; two tenants
demonstrably share one entry, **the leak attempt succeeds and that is the intended behaviour**;
nothing in a cached value is per-tenant, which is what makes the sharing safe rather than merely
true; and then the tripwire — **if per-tenant memory can ever reach the prompt, the cache key must
take a tenant.** Vacuous today, a hard failure the moment memory lands without re-keying. Its
predicate is detected **from the code** rather than from a version marker, because the failure being
guarded is somebody adding memory and forgetting the cache — and a guard keyed on anything that
person would have to remember to update is not a guard.

**CP5 does not get to report an isolation pass here.** It fired at CP6 — see
[`08-memory.md`](08-memory.md).

---

## 5. The queue is bounded, and it moved rather than vanished

CP4 measured an unbounded queue ([VP28](EVIDENCE-PLATFORM.md#VP28)), so CP5 had a number to design a
bound against rather than an assumption. The two caps — a total in-flight limit and a lower
per-tenant limit — were derived rather than defaulted, and the **deliberate inequality is that the
total binds before every tenant can reach its own cap**. If the total were at least the tenant count
times the per-tenant cap, no tenant could ever lose a slot to another and **the starvation number
would be zero by construction** — the same vacuity trap as the cache test, arrived at for a
different reason ([VP34](EVIDENCE-PLATFORM.md#VP34)).

Two properties were tested rather than argued, both being where this class of code fails. The bound
**rejects rather than waits**, because a bound that waits is an unbounded queue with extra steps.
And the slot is released on every exit — normal, streamed, exception, client disconnect — which is
why it is pure-ASGI middleware rather than a context manager in the handler: the handler returns as
soon as the streaming response is constructed, so a slot taken there would be released *before the
tokens were sent*. One `try/finally` covers all six exits.

It does **not** replace the retrieval semaphore, and conflating the two would be a real error. That
lock is mutual exclusion over non-re-entrant retrieval; this is admission control over whole
requests. Different scope, different question, and CP4's decisions were not reopened.

**The bound works, and rejections appeared where there had been none.** At the same arm and level,
CP4's p99 of **64,405 ms with zero rejections** became **4,429 ms with thirteen**
([VP34](EVIDENCE-PLATFORM.md#VP34)). That is the intended trade, delivered.

**And then the uncomfortable half.** Admitted requests still show p95 tails in the tens of seconds
against a two-second idle baseline, and system throughput sits at the CP4 knee rather than at the
gate's capacity. The cap admits far more than the pipeline can serve, because **retrieval is still
mutually exclusive: the lock was narrowed, it was not parallelized.** Most admitted requests are
queued *inside the box*, waiting on that lock.

> **The bound caps how many wait, not how long.** The cap sits above the knee, so the queue moved
> from outside the gate to inside it. Backpressure was relocated, not achieved.

Sizing the gate to the measured knee is a throughput decision, and it is the prerequisite for the
next section.

---

## 6. Fairness is a NON-RESULT, and G-P10 is open

**An absence here would read as a pass, so it is stated first: CP5 delivered admission control. It
did not deliver per-tenant fairness, in either direction.** Two mechanisms were built. One could
never fire and was retired; the other fires and could not be evaluated
([VP35](EVIDENCE-PLATFORM.md#VP35)).

**Mechanism 1 — the gateway's token-pressure fair-share guard. Retired: its precondition is
unreachable.** The guard fires only above **80% of the limiter's token-per-minute budget** with at
least two active tenants. Admission control caps in-flight requests low enough that sustained
pressure sits at **roughly 3% of that threshold**
([VP35](EVIDENCE-PLATFORM.md#VP35)). Across two multi-tenant runs its rejection counter moved by
zero while the admission counter carried every rejection.

> **No offered load can raise the pressure, because the offered load is exactly what admission
> control is refusing. The guard sits behind a gate that makes its own precondition unreachable.**

This is the phase's sharpest lesson and it is not about auth. **Both mechanisms are individually
correct and individually tested** — nine in-process fairness tests pass and the arithmetic is right.
What no in-process test could see is that another correct component, added in the same batch for a
different reason, removes its input. **A control that has never executed is indistinguishable from
an absent one, and here the control is unreachable by construction while its suite is green.**

Two related traps were caught before they shipped, both by arithmetic rather than by a run. Taking
the charter's USD quota unit as the *enforcement* primitive would have been **inert**: every hosted
arm estimates zero cost and the free tier has no dollar ceiling, so the rule compares zero against
zero forever — it would pass review, ship, appear in the docs, and reject nothing. Fairness is
therefore enforced on the pool's token meter and only *reported* in USD, where a zero column is a
true statement about a free tier rather than a missing measurement. And the rule as first written
was degenerate at exactly the deployment's normal state: with one tenant the fair share is the whole
pool, so a lone tenant that fills it trips **its own** share and is told it is over its share when
the fact is that the pool is empty.

**Mechanism 2 — the admission gate's least-served rule. Fires, and cannot be evaluated.**

The first fairness reading was an artifact of the load driver, not a property of the server. The
driver fired a fixed number of attempts per tenant and **never retried a rejection**. Rejection is
instant, so a tenant arriving during a saturated window burned its entire offered load in
milliseconds and finished with zero served, while whichever tenant already held a slot re-fired on
completion and kept it. **It measured arrival luck.** Every starvation magnitude from before that
correction is withdrawn, and none should be quoted.

What survives the correction is directional and worth keeping: **per-tenant fairness against a noisy
neighbour does work** — the heavy tenant is correctly throttled by the per-tenant cap, which is what
that cap is for. What has no fairness is the contest between everyone else for the *global* slots,
which under contention is distributed by arrival time. The charter asks for a global pool with
per-tenant fairness on top; half of that is real.

**And the fixed driver had no statistical power.** Two consecutive identical runs disagreed by a
factor of three, and the reason is visible in the counts: the whole system served a couple of dozen
requests in ninety seconds, so per-tenant served counts were single digits. **A ratio of small
integers is not a number**, and two runs disagreeing threefold is what that looks like.

**G-P10's disposal, stated exactly.** The least-served rule fires — its label appears once the
metric's allowlist was fixed — but whether it *improves* share distribution is unmeasurable at this
throughput. **G-P10 stays open, in both directions**, blocked on sizing the gate to the measured
knee, which is §5's throughput decision. It was inherited unchanged by CP6 and is still open.

Two rules were earned here and bind the rest of the project:

> **An instrument that has not been checked against a known-answer case is not evidence.**
>
> **A number computed from single-digit counts is not a number.**

---

## 7. CP5's six instrument defects

**Six times the instrument was wrong and produced a confident, plausible, false statement about the
product** ([VP37](EVIDENCE-PLATFORM.md#VP37)). None was a defect in the product. This is the phase's
most transferable finding and it has nothing to do with auth;
[`12-instruments.md`](12-instruments.md) aggregates across phases.

1. **A rate limiter that had never counted anything.** The suite was green and the control was
   documented and reviewed, and it would have shipped as *"per-account and per-IP login rate limits:
   PASS"*. The database helper commits on clean exit and **rolls back on any exception**; the login
   path recorded its failed-attempt rows and then raised, unwinding through that same context
   manager. **Both rows were rolled back on the way out**, so the failure count was zero forever and
   neither cap could ever be reached — in production exactly as in the test. Caught by the suite's
   **first execution**.
2. **An assertion failing on an absent series.** The verdict was *"a security-relevant counter with
   no tenant label"*. The label was wired; the **emission** was missing, so the series was absent
   from the scrape entirely and the assertion failed on absence rather than on cardinality. One call
   added to the test's emitter, no source change. **This is the more alarming reading, stated with
   total confidence** — and it is defect 1's lesson from the opposite side: a control that had never
   executed, and a control that was present and looked absent. The only thing separating them is
   executing it.
3. **A timing-oracle probe that blew its own budget.** It reported a near-doubling of login time across
   mixed statuses and would have published *"user enumeration is possible by timing"* — a live auth
   vulnerability. In fact it fired more logins than the login rate limit allows, and **the resulting
   429s skip the argon2id verify entirely and return in roughly half the time of a real credential
   check**. An inert comparison wearing the shape of a timing oracle. Re-sized, and a saturation
   check now **refuses to score the probe at all** if a 429 appears. Caught by reading the evidence
   rather than the verdict.
4. **An enumeration assertion that could only ever fail.** It compared the set of status *codes* to
   the set of response *bodies* — never equal — and reported a failed security requirement on a run
   whose underlying measurement was a clean pass. Caught by re-reading after a clean measurement.
5. **A metric reason silently bucketed to `unknown`.** The least-served rule's label was missing from
   the metric's closed label set, so its rejections were relabelled and those admission decisions were
   attributed to nothing in particular. Caught by the test that asserts label sets are closed and
   small.
6. **A load driver measuring arrival luck rather than fairness** — §6, caught by a repeat run
   disagreeing threefold.

**Two of the six pointed at a scarier conclusion than the truth** (2 and 3), which is the direction
that wastes the most time: a false alarm about a security control gets investigated at full
priority. Two process rules were written down rather than remembered:

> **A probe must record what it saw, not only its verdict**, or a wrong verdict is unfalsifiable
> after the run.
>
> **The probe list must exercise a control against a running server, not only in-process.** The rate
> limiter's bug was invisible to inspection, and would have been invisible to a probe asserting on
> the code rather than on behaviour.

Six premises in CP5's own plan were also false, and are recorded beside the defects: that nothing
needed re-instrumenting (§1), that the quota unit could be USD as an enforcement primitive (§6),
that the cache should be re-keyed by tenant (§4), that cross-tenant leak attempts fail (§4, vacuous
by construction), that the evidence shape is per-tenant cap overspend (§6, a shape the charter
rejects), and the implicit premise that fairness is measurable at this throughput at all (§6).

---

## 8. What this leaves open

- **G-P10 — per-tenant fairness, unresolved in both directions.** The largest open item the platform
  phases carry out ([VP38](EVIDENCE-PLATFORM.md#VP38)).
- **Backpressure was relocated, not achieved** — from an unbounded queue to the limiter's sleep and
  the retrieval lock (§5).
- **There is still no gauge**, so nothing publishes in-flight count or queue depth — the
  bounded-concurrency work needs exactly the instrument
  [`06-traffic-harness.md`](06-traffic-harness.md) §8 shows does not exist.
- **The OpenSSL argon2id precondition is unverified on this machine**; the system refuses to start
  rather than degrading, which converts an unverified assumption into a loud failure rather than
  closing it.
