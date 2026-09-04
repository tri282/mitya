# 03 — Telemetry and Config Identity

**Delta from v1.** v1 had no instrumentation. It could say a request took roughly twenty seconds and
it could not say where the time went; the reranker's share was an estimate written into a design
document [E28 (v1)]. v1 also had no notion of *which configuration produced a number* — settings were
module constants and environment variables, and a measurement's provenance was whatever the operator
remembered. CP1 added both: per-stage attribution, and a versioned config artifact that gives every
span, every eval row and every promotion a name for the configuration behind it.

Neither is a feature. Both are preconditions. Everything v2 does afterwards — pick a provider, gate a
promotion, run an optimizer — is a comparison between two configurations, and a comparison needs a
place to attribute a difference to and a name for each side.

---

## I. Why attribution had to come before any tuning

The v1 number that mattered was a total. A total is unactionable: it cannot say whether the fix is a
faster reranker, a smaller prompt, a different provider, or nothing at all. Worse, it invites the
cheap explanation — *the model is slow* — which was, as it turned out, only about half true.

The instrumentation is one `start_as_current_span` per stage boundary, and that single set of calls
feeds both the trace and the metric. That is what settled the design choice against log-derived
telemetry: the operator plane was mandated anyway, and a log-derived path would have meant
instrumenting the same seven boundaries twice, once for the trace and once for the counters.

The first trace paid for itself immediately. **Rerank is roughly 84% of the retrieval path**
[VP7](EVIDENCE-PLATFORM.md#VP7) — measured three times on three separate occasions — against a v1
design document that justified the reranker at a fifth of that. The correction is a lever, not a
footnote: the narrowing from the fused pool to the served chunks is a slice *after* scoring and costs
nothing, so the size of the **fused pool** is the latency knob and the size of the **reranked** set is
not. A year of tuning the wrong parameter was avoided by one trace.

Attribution also converted a standing mystery into arithmetic. The gap between the structural route
and the default route had been a bare percentage; the spans split it into a retrieval component (no
sparse leg, no rerank) and a prefill component (fewer, shorter excerpts). The binding consequence is
methodological: **any later latency or cost comparison must hold the route mix fixed**, or it measures
the mix rather than the arm.

The spans are trustworthy because they were cross-checked against the hand-rolled timer they replaced,
and the two agreed to within milliseconds. An instrument that replaces another instrument should be
made to reproduce it once before the old one is deleted.

---

## II. What instrumentation cost, and what it could not see

**Overhead is below the detectable floor.** Two cold arms, instrumented and not, produced point
estimates that were *negative* [VP8](EVIDENCE-PLATFORM.md#VP8) — which instrumentation cannot cause,
and which is the tell that the whole quantity is noise. The correct report is "below the detectable
floor at this n", never "zero" and never a negative overhead. Two details generalize: total wall-clock
is the wrong instrument for a millisecond-scale effect, because answer-length variance dwarfs it,
while time-to-first-token excludes decode and has a small enough spread to make the comparison
possible at all; and a warm prompt prefix on the local server improved one route by more than an order
of magnitude, which invalidates any sequential A/B that does not restart the server between arms. That
was the third instance of one pattern in this project — a store that outlives a single arm — and the
standing rule became: before any A/B, enumerate every such store.

**The expensive part of the pipeline was the part the histogram could not measure.** The first metrics
pass reported the two generation stages at exactly the same value for p50, p95 *and* p99, on both
routes. That is not a latency; it is the top edge of the bucket list
[VP8](EVIDENCE-PLATFORM.md#VP8). One histogram covered eight stages spanning five orders of magnitude,
from a sub-millisecond route lookup to a multi-second token stream, and the two stages that dominate
the request were the two that fell off the end. The symptom is the dangerous part: an identical
quantile at every percentile reads as a *suspiciously stable measurement*, not as an error.

Two smaller instrumentation defects are worth carrying because they generalize past this project. The
first real trace was dominated by hundreds of zero-duration ASGI send spans — one per streamed token —
which buried the meaningful spans and would have made the storage projection scale with **answer
length** rather than with pipeline complexity. And the metrics endpoint traced itself, filling a
fixed-size in-memory trace buffer with scrape noise, because the URL exclusion list had been written
when only a health endpoint existed and nothing prompts you to revisit it when you add an endpoint of
exactly the same character.

---

## III. Config identity: what a version pins

A configuration version is the content hash of a frozen, validated model:
`sha256(canonical_json)[:12]`. Two identical configurations are the same version by construction, and
a candidate configuration gets a version for free with no counter to coordinate and no registry to
keep consistent. The hash is taken over **normalized, parsed values, never input text**, so `"3"` and
`3` land on the same version; the canonicalization is pinned by a test, because an unstable hash means
version churn on every restart, which poisons the telemetry dimension silently.

Adoption was made a facade rather than a refactor. A metaclass resolves the versioned knobs through
the active artifact, so roughly two hundred call sites across three dozen modules keep working
unmodified [VP9](EVIDENCE-PLATFORM.md#VP9). The costs are real and were written down at the metaclass:
type checkers cannot see through it, and it is action at a distance. The alternative — threading a
config object through every call site — was a larger diff with the same failure modes and a worse
chance of landing.

**The three-way field split is the load-bearing design decision.** A field is *versioned* if changing
it changes what a correct answer **is**; *deployment* if it changes where the system runs, how fast, or
how much it costs, but not what the answer says; *secret* if it authenticates. The ambiguous cases were
called explicitly rather than left to instinct: the answer cache and the empty-completion retry count
are deployment; model rate rows and quota limits are **versioned**, because a price or limit change
should be a visible config version rather than a silent edit.

**Fail closed, four ways.** Refuse to serve on a bad pointer or an invalid artifact — a fallback would
silently serve an unversioned configuration while still stamping *something* on every span, which is
worse than not starting. Refuse an artifact whose contents no longer hash to its filename, because
content addressing that is never checked is decoration and a hand-edited artifact stamps a version
name on a configuration nobody ran. Assert at build time that no secret can enter the artifact, since
the artifact is written to disk, diffed, and stamped onto spans that leave the process.

### A module constant defeats the versioning that is the point

The rule earned twice in this phase: **a knob the optimizer will later search cannot live as a module
constant, and the exposed form must be a function, not a constant.** A constant snapshots at import
time, so a process that loaded before a promotion keeps serving the old value while stamping the new
version onto its spans — the exact failure the versioning exists to prevent, made invisible. The
persona head moved from a module constant to an artifact field exposed through an accessor for that
reason; the summarizer's intent string moved out of the router module for the same one. The training
system prompt deliberately stayed separate, because it records what the model was *trained* on, which
is a different fact from what serving sends.

### The three things a config version did not pin

Each was discovered by a measurement going wrong, not by review
[VP9](EVIDENCE-PLATFORM.md#VP9):

1. **The generation model.** The version did not churn between two provider arms, so the answer cache
   never invalidated and **arm B was served arm A's cached answers**. That contaminates quality
   (arm B's cells contain arm A's generations), then latency (a cache hit returns immediately), then
   cost — in that order of severity, and only the third was on the decision table when the field was
   omitted. Closed by putting the serving model on the artifact; the existing invalidate-on-version-
   change then dropped the cache with no new cache code.
2. **The persona text.** The artifact carried the prompt composition and no persona field, so the
   mechanism built to invalidate on configuration change was blind to the single largest determinant of
   an answer's style. The sync between the serving persona and the training persona was enforced *by a
   comment*, and no test imported both, so an edit to one was a silent green-suite change.
3. **Which model built the index.** The vector store records dimension and distance and not model
   identity, and the installed embedding library ships fifteen models at the serving dimension. So an
   embedder swap is loud **only** when the dimension differs — and a dimension mismatch fails at
   ingest, not at query, which means **the dangerous direction, ingest with one model and serve with
   another, is exactly the silent one**. Closed with a corpus manifest and a startup assertion. A
   second silent path was found alongside it: the query and document prefixes are specific to the
   embedding model and unconditional, so under a different embedder they stop being instructions and
   become literal noise — and unlike a vector mismatch, that survives a clean re-ingest, because both
   sides stay consistently wrong.

The manifest carries one further honesty device. It was **adopted** from a live index rather than
written at ingest, so some fields were *read* from the store and others were *asserted* from today's
configuration — and the index records none of the asserted ones, which is the bug the manifest exists
to close. An adopted manifest therefore catches every future swap and cannot detect a mismatch already
present at adoption time. The manifest records which provenance it has. **General rule: when a check
must bootstrap from unverifiable state, record which fields were read and which were asserted, in the
artifact itself.**

### The version is the arm, and an environment variable is not

This is the part that reads as pedantry until it costs a run. `Config` resolves through the **active
artifact**, not the environment; the environment is read only when *building* an artifact. So launching
an eval with an environment override does not switch arms — it runs the promoted configuration and
labels nothing. That happened, in production use, and the provenance block caught it: both runs carried
the same config version [VP19](EVIDENCE-PLATFORM.md#VP19), the artifact stating plainly that they were
the same configuration, while the scores were plausible enough to have been believed. **To measure an
arm, promote a configuration for it.** Adding an environment override that bypassed the artifact would
reintroduce the defect the artifact closed.

**The one override that exists is deliberately process-scoped.** `MITYA_CONFIG_VERSION` pins a single
process to a named version, which is what makes a side-by-side comparison possible at all without
moving the pointer everyone else reads. It overrides the pointer, never the artifact — the artifact is
still the thing that resolves, so the provenance block still records which version actually ran.

**Trace export is opt-in the same way.** With no `OTEL_EXPORTER_OTLP_ENDPOINT` the SDK builds spans
and drops them, so a development run needs no collector and no docker; `OTEL_ENABLED=false` is the
stronger switch that installs no provider and no instrumentation at all. Two levels, because "I do not
want to run a collector" and "I do not want the overhead" are different requests.

Provenance stamping on every eval run follows from the same argument: config version, corpus version,
git SHA, **whether the tree was dirty**, timestamp, serving model, composition. The dirty flag matters
more than it looks — a measurement taken on an uncommitted tree is not reproducible from its SHA, and
recording the SHA anyway is precisely how that becomes invisible.

---

## IV. The operator plane, and two metrics that could not express their condition

The operator plane is Prometheus and Grafana in Compose, driven by the same instrumentation as the
spans, with the dashboard checked in as a file. The panel set is RED plus spend: latency percentiles,
error rate by provider, cost per answer, quota rejections, cache hit rate.

It shipped with two defects, and both are more interesting than the panels.

**A counter incremented only on the success path is not a counter.** The first live run's opening
request was a 503 from an unreachable generation server. It recorded all six pre-generation stage
durations and **no request at all**, because the error path raised before the recording line was ever
reached — so during a total generation outage the error-ratio panel read zero
[VP10](EVIDENCE-PLATFORM.md#VP10). Not a wrong number: a metric structurally incapable of expressing
the condition it was added to expose. Four early-exit paths shared the shape. The fix went in **one**
place — a local abort helper that releases the semaphore *and* records the request as an error, with
all four exits routed through it — rather than four. The transferable form: **grep the exits, not the
happy path.**

**Two ratio panels could not display zero.** With no cache hits the hit series does not exist, the
division returns no result, and the panel renders "No data" rather than 0. The guard everyone reaches
for — clamping the denominator — does nothing here, because **the missing term was the numerator**.
The error-ratio panel had the identical defect and was hidden during the run only because errors
happened to occur. Neither defect is reachable offline: both need a real scrape producing a genuinely
*absent* label set, and a unit test that records one hit and one miss has by construction made both
series exist.

**Cardinality: a validator is not a cardinality guard.** The tenant header had a shape validator, and
an earlier register recorded that the validator was therefore also the cardinality guard. That is true
for logs and false for metrics. The pattern stops a newline forging a log line; it admits an
astronomically large set of distinct valid values, and the header was unauthenticated until the auth
phase. A caller sending well-formed distinct values mints one time series per value per metric,
unboundedly — a memory-exhaustion path into the operator plane, reached through the front door by
valid input. Tenant therefore stays on spans and logs and appears in **no** metric label at this phase.
**A validator bounds the shape of a value; a metric label needs a bounded set of them.**

**Spend is three-way, not two-way.** Nothing billing us (local inference, free tier) is *not* a cost of
zero; something billing us with a missing rate row is *unpriced*, not free; only the third case is a
cost. Folding the middle case into `0.0` is indistinguishable from a real metered-but-free call, which
is exactly the false claim the charter forbids. The unpriced case got its own counter, and any
aggregate containing an unmetered or unpriced stage is provisional and cannot be published as spend.
This is the deliberate inverse of the quota side, which guesses the *stricter* tier when it does not
know: unknown price fails toward "unpublishable", unknown quota fails toward "slower".

---

## V. Infrastructure security: four drills, three found something

The components CP1 added to Compose and the request path were probed rather than reviewed. None of
what follows was found by reading a passing test suite
[VP11](EVIDENCE-PLATFORM.md#VP11).

**The trace UI was LAN-exposed and unauthenticated.** A Compose port mapping written without a host
prefix binds all interfaces. Two things made it worse than an ordinary misconfiguration: the trace
image has no authentication of any kind and renders full request contents — queries, retrieved
passages, prompt text; and published container ports bypass the host firewall, because the container
runtime's NAT rules are evaluated before the firewall's zones. The firewall looked closed while the
port was reachable. **A Compose port mapping with no host prefix is a LAN publish.**

**The fix for that re-opened a wider hole.** Switching to host networking to sidestep a broken bridge
path removed the mapping layer entirely — and the image opens **nine** listeners, of which only three
had been pinned. The one that matters is the query API behind the UI: same data, same absence of
authentication. Pinning the UI while leaving the query API open pins the front door and leaves the
window. **Host networking publishes every default-bound listener the image opens, not only the ones
you thought about — enumerate the sockets after starting any new service.**

The method note is deliberately conservative: connecting from the host to its own LAN address proves
the socket *accepts on that interface*, not that a remote machine could reach it. That is the point.
The binding was one firewall rule away from exposure, and firewall state is exactly what an unrelated
container or VPN change alters without notice. Bind narrowly *and* firewall, in that order.

**The third finding was a one-character regex bug with a trust-boundary consequence.** The tenant
validator anchored with `$`, which in Python matches at end-of-string **or immediately before a
trailing newline**. A trailing newline therefore passed the shape check and was interpolated verbatim
into the log format — one forged log line per request, from an unauthenticated header. The transferable
part is why the tests missed it: the hostile-input set *did* cover newlines, as **interior** characters,
which reject correctly. **The test's blind spot mirrored the regex's exactly** — both treated "contains
a newline" as the property, when the property that mattered was "is anchored at the true end of the
string". The fix rejects rather than sanitizes: stripping the newline would invent a tenant nobody
sent.

A fourth item was found while extending tracing rather than by a drill: the tracing library's built-in
URL redaction list is vendor-shaped and carries no generic key parameter, so it was dormant only
because the current provider sends its key as a header. It stops being dormant the moment a
key-in-URL provider is wired, which the two-provider requirement guarantees will eventually happen.

The drill on the two monitoring images found nothing beyond a pre-existing host service, and the
negative result is still worth its cost — because the image metadata *declaring* one port each is a
declaration, not proof, and that was the previous defect exactly. Two probing habits came out of it:
**probe the body, not the status** (an admin API refused with a 500 and a message, where a 404-shaped
expectation would have read "endpoint missing"), and make the compose file **refuse to start** without
an admin password rather than fall back to a default one.

**The recurring test pathology, four instances and one defence.** A test that passes because its
subject was never exercised: a property test whose generated inputs all agreed so it discriminated
nothing; the interior-newline case above; a mock transport that bypassed the very method the
instrumentation wraps, so *zero* spans were produced and "no secret in any span" passed because there
was nothing to search. The defence that worked every time is **assert non-vacuity beside the property**
— that at least one span exists, that the sentinel is present in the input — and run a deliberate-leak
drill against the control with the control removed.

---

## VI. What this phase leaves open

- **The determinism baseline store has no retention policy** and nothing prunes it, while the CI gate's
  ability to catch nondeterminism regressions depends on it (G-G, see
  [`05-ci-eval-gate.md`](05-ci-eval-gate.md)).
- **Traces are in-memory by choice**, so they are lost on restart; the storage figure measures RAM
  rather than disk, and the buffer's configured ceiling implies a multi-gigabyte steady state it will
  never reach at this deployment's traffic — safe, but not self-evidently so.
- **The magnitude of an embedder-mismatch's quality damage is deliberately unmeasured.** It needs a
  second model download and a full re-ingest, and the assertion's value rests on the silence, not on
  the delta.
- **The evidence trail itself is untracked.** The planning, status and notes directories are excluded
  from version control by deliberate choice, so the reasoning behind every number lives on one machine
  until it is published into a tracked document. The eval sets *are* tracked, so every cited hash stays
  independently checkable — that half of reproducibility holds.

Full detail, every number with unit, date and provenance:
[VP7](EVIDENCE-PLATFORM.md#VP7)–[VP11](EVIDENCE-PLATFORM.md#VP11), with the never-measured list at
[VP38](EVIDENCE-PLATFORM.md#VP38).
