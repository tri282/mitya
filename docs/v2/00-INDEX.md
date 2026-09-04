# Mitya v2 — Documentation Set

**v1 built the system. v2 operated and measured it.** v1 answered a hardware question — can a single
6GB consumer GPU serve a grounded, persona-styled LLM (yes, and the quality ceiling turned out to be
the fine-tune, not the retrieval pipeline). **v2 answers a delivery question:** at a fixed quality
bar, what is the cheapest and most reliable way to serve grounded answers, and can the system *prove*
it stays that way as it changes. Serving left the GPU for hosted model APIs behind a two-family
provider gateway; the pipeline gained telemetry, versioned config identity, a CI eval gate, a traffic
harness, authentication and per-tenant isolation, conversation memory, and an automated
config-optimization loop. The retrieval stack itself barely moved, and that is a result rather than
an omission — see [`14-unchanged-from-v1.md`](14-unchanged-from-v1.md).

**This set is built on v1, not standalone.** The tier-1 documents describe the whole system as it
stands after v2 and can be read alone. The tier-2 documents cover **only what v2 changed or added**,
and open with a delta map. The v1 set lives on branch `main` at `docs/v1/`.

---

## Reading order

| # | Document | What it answers |
|---|---|---|
| — | `DESIGN.md` | **Start here.** The whole system after v2: components, and one request traced end to end |
| — | `RAG.md` | The substance layer as v2 left it |
| **01** | `01-overview.md` | v1 → v2, the delivery question, and **the delta map**: every v1 subsystem marked unchanged / changed / new |
| **02** | `02-measurement-contract.md` | Eval power, the MDE, known-answer checks, the split — v2's through-line |
| **03** | `03-telemetry-and-config.md` | Per-stage attribution, versioned config identity, the operator plane |
| **04** | `04-provider-gateway.md` | Two provider families, retries, pricing rows, routing rejected before it was built |
| **05** | `05-ci-eval-gate.md` | The gate, mechanical metrics only, and the measured proof it cannot see grounding |
| **06** | `06-traffic-harness.md` | Concurrency, the knee, fault injection, the degradation ladder |
| **07** | `07-auth-and-tenancy.md` | argon2id, opaque sessions, rate limits, and fairness as a non-result |
| **08** | `08-memory.md` | Three tiers in one budget, the allocator seam, retention and purge |
| **09** | `09-prompt-security.md` | Injection, the fence that does not hold, composition C |
| **10** | `10-optimizer.md` | The search space, the objective, the E-runs, what promoted |
| **11** | `11-cost-and-performance.md` | The three cost numbers, latency, and O7's distillation refusal |
| **12** | `12-instruments.md` | Every instrument defect, and why a clean run never caught one |
| **13** | `13-decisions-and-open.md` | What v2 refused, what stays open, the honest headline |
| **14** | `14-unchanged-from-v1.md` | What v2 deliberately did not touch, and why |
| — | `EVIDENCE-PLATFORM.md` | **VP1–VP38** — CP0 measurement contract · CP1 telemetry · CP2 gateway · CP3 gate · CP4 harness · CP5 auth |
| — | `EVIDENCE-MEMORY.md` | **VM1–VM25** — CP6 memory, and the prompt/injection/persona security work |
| — | `EVIDENCE-OPTIMIZER.md` | **VO1–VO16** — CP7: search, E-runs, Batches G/H, chunking, O7, the cost numbers |

**Three tiers.** `DESIGN.md` / `RAG.md` show **what the system is**. `01`–`14` argue **why it is that
way**, and cover only v2's delta. The three `EVIDENCE-*.md` logs hold **every number** and are reached
by anchor from both tiers above.

---

## Two conventions this set enforces

**1. Every baseline states its origin, in the row.** A delta whose reference is ambiguous is not a
delta. Each evidence row is tagged `[baseline: v1 recorded]` or `[baseline: v2 CP0]` /
`[baseline: v2 CP4]` / `[baseline: v2 CP6 no-memory arm]`. This is load-bearing: memory's 1.328x
per-session cost is against **P6's own no-memory arm**, not against v1; auth's latency is against
**P4's pre-auth figure**, not v1's. And the concurrency knee has **no baseline at all** — v1 recorded
its `Semaphore(1)` behaviour qualitatively and published no number, so CP4's knee is a first
measurement, not a delta.

**2. P7 is authoritative, and superseded numbers are struck rather than deleted.** P7 was the final
phase; where an earlier register disagrees, P7's reading wins and the old value stays visible with
the reason. Standing examples: `max_tokens` 400 → 1024 (`18570f5`) is a **measurement break** and no
cost figure compares across it; the **session** corpus was re-frozen twice
(`4155c97b0a2b` → `49b525f2a131` → `be54610a7f78`) while the **document** corpus `515c707a5d69`
never changed — two objects under one rule, and earlier drafts conflated them; the false-refusal
guard's `FAILED 0.2069` was amended to `NOT DEMONSTRATED` after a human read found 0 of 6 were
over-refusals.

---

## Headline numbers

| | Value | Evidence |
|---|---|---|
| Serving | Moved **off** the 6GB GPU to hosted APIs, two provider families | [VP12](EVIDENCE-PLATFORM.md#VP12) |
| Retrieval baseline, re-derived | **0.7105 (27/38)** — denominator 26 → 25 → 38 across three readings | [VP4](EVIDENCE-PLATFORM.md#VP4) |
| Per-stage attribution | The first time the project knew where the time went | [VP7](EVIDENCE-PLATFORM.md#VP7) |
| CI gate | Three layers, and **nothing in it is a hand-set threshold** | [VP20](EVIDENCE-PLATFORM.md#VP20) |
| What the gate cannot do | **It cannot see grounding** — measured, not assumed | [VP21](EVIDENCE-PLATFORM.md#VP21) |
| Concurrency knee | **1 → 4** after narrowing the lock to retrieval | [VP27](EVIDENCE-PLATFORM.md#VP27) |
| Auth | argon2id, opaque sessions, **no measurable latency cost** | [VP33](EVIDENCE-PLATFORM.md#VP33) |
| Fairness | **NON-RESULT.** Two mechanisms, one retired, one unevaluable. **G-P10 open** | [VP35](EVIDENCE-PLATFORM.md#VP35) |
| Memory | Three tiers, one budget, **1.328x per session** — and no claim about what it bought | [VM5](EVIDENCE-MEMORY.md#VM5) |
| The fence | **Does not hold.** An existence result on single-digit denominators across two arms, never a rate. §IV invariant **OPEN** | [VM11](EVIDENCE-MEMORY.md#VM11) |
| The shipped prompt | Had **no fence at all** — set-identical to an unfenced control | [VM14](EVIDENCE-MEMORY.md#VM14) |
| Composition C | Violations **0.5625 → 0.1250**, groundedness **+0.724** vs MDE 0.343 — promoted | [VM18](EVIDENCE-MEMORY.md#VM18), [VM20](EVIDENCE-MEMORY.md#VM20) |
| *"78% more secure"* | **FALSE SUMMARY.** Extraction is unmoved at 1/8 on every arm | [VM15](EVIDENCE-MEMORY.md#VM15) |
| Cost objective | Ranks on one number, **gates on another**, and a third records nothing | [VO3](EVIDENCE-OPTIMIZER.md#VO3) |
| The saving | **15.6%**, holds out of sample (gap −0.2%) — and **not promoted**, no quality verdict exists | [VO7](EVIDENCE-OPTIMIZER.md#VO7) |
| Retrieval blindness | **63%** of multi-turn requests retrieve on a contentless string — ships unrepaired | [VO8](EVIDENCE-OPTIMIZER.md#VO8) |
| Chunking | **Untested, not validated.** CP-7h closed as a priced deferral | [VO11](EVIDENCE-OPTIMIZER.md#VO11) |
| Distillation | Closed on arithmetic: model speed **cancels**; break-even is **1,630 answers/day** against the cheapest hosted arm | [VO12](EVIDENCE-OPTIMIZER.md#VO12) |
| Actual spend | **$0.00** — every rate row is `metered=False`, list-rate at the free tier | [VO3](EVIDENCE-OPTIMIZER.md#VO3) |
| Instrument defects | **Thirteen+**, across CP0, CP5, CP6 and CP7 | [VP5](EVIDENCE-PLATFORM.md#VP5), [VP37](EVIDENCE-PLATFORM.md#VP37), [VM19](EVIDENCE-MEMORY.md#VM19), [VO13](EVIDENCE-OPTIMIZER.md#VO13) |
| What was never measured | The absences, listed plainly | [VP38](EVIDENCE-PLATFORM.md#VP38), [VM25](EVIDENCE-MEMORY.md#VM25), [VO15](EVIDENCE-OPTIMIZER.md#VO15) |

---

## The finding that outlived every phase

**The instrument was wrong more often than the system was.** Across CP0, CP5, CP6 and CP7, more than
a dozen instruments produced confident, plausible, **false** statements: a rate limiter that had never
counted anything, a metric that scored 0.000 and could never have moved, a memory fixture 20x too
small that made an axis read zero across its entire searchable domain, a metric that punished an arm
**for obeying its own grounding instruction**, and a day's API quota spent on a run that measured the
wrong thing. **Every one was caught by a known-answer case, by reading what actually matched, or by a
human reading six rows — and not one was caught by a clean run.**

The rule earned across four phases: **a number that reads 0.000 is a claim about the instrument until
something proves it is a claim about the world.** [`12-instruments.md`](12-instruments.md) collects
them.

---

**Every number in this set lives in one of the three `EVIDENCE-*.md` logs.** Documents `01`–`14` and
the tier-1 pair reference them by anchor and state no figure of their own. Anchors `VP1`–`VP38`,
`VM1`–`VM25` and `VO1`–`VO16` are frozen and are never renumbered.
