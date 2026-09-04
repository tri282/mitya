# v2 Evidence — the optimizer, the objective, and cost (CP7)

**Anchor namespace: `VO`.** This is one of three tier-3 evidence logs. The siblings own
`VP` (`EVIDENCE-PLATFORM.md` — CP0-CP5: measurement contract, telemetry, gateway, CI gate,
harness, auth/tenancy) and `VM` (`EVIDENCE-MEMORY.md` — CP6 memory, prompt injection, the
fence, persona composition). Anchors here are frozen; tier-2 documents cite them.

**Scope.** The search space and its restriction; the objective and the three cost numbers; the
E-runs and the 15.6% saving; Batch G (anaphora / retrieval blindness); Batch H (neighbour
expansion, merge-only); Batch D's **cost half only**; CP-7h (the chunking ablation, closed as a
priced deferral); O7 (distillation); the latency findings; the phase's closing evidence record.
Batch D's block rates, groundedness and false-refusal numbers are `VM`'s. The gateway's pricing
rows and quota tables as infrastructure are `VP`'s; the USD-per-answer analysis built on them is
here.

<a id="VO1"></a>
## VO1 — How to read a row

**Three conventions, applied without exception.**

**1. Every baseline states its origin, inside the row.** A delta whose reference is ambiguous is
not a delta. Tags used here:

| tag | means |
|---|---|
| `[baseline: v1 recorded]` | a figure from `docs/v1/EVIDENCE.md` (anchors `E1`..`E30`, branch `main`) |
| `[baseline: v2 P7 production config]` | **the config the pointer named at that moment** — see the warning below |
| `[baseline: v2 P2]` | a P2-register figure (R44-R81), superseded where P7 re-measured |
| `[baseline: v2 CP0]` | the measurement contract's MDE form and eval-set denominators |

**"Production" moved during P7, and the pointer is part of the baseline.**

| pointer | what it is | when it was production |
|---|---|---|
| `fb5cb5a842e7` | the pre-P7 serving config, corpus manifest `515c707a5d69`, `chunk_tokens=500`, `models/gemini-3.5-flash-lite` | through the parallel track, E-run(a), E-run(c), Batches G/H |
| `52cd8bfa1108` | E-run(c)'s **candidate** config (`memory_budget_tokens=1024`, `top_k_fused=10`) | never — measured, never promoted (D-10) |
| `b6063fd8be31` | composition C (D-24) | from Batch D's close, 2026-09-03, to the end of the phase |

A cost figure tagged `[baseline: v2 P7 production config]` in VO5-VO8 is against `fb5cb5a842e7`.
The only pointer move in P7 was for a **prompt**, not for a cost candidate.

**2. P7 is authoritative.** P7 is v2's final phase and its readings supersede earlier ones. Where
a register disagrees, the superseded value is shown ~~struck~~, not deleted, with one line on why.
The two standing measurement breaks:

- **`max_tokens` 400 → 1024** (`18570f5`). `src/rag/gate.py:251-282`'s own docstring calls this a
  measurement break and `cost_layer()` returns `ERROR` when two arms differ on it
  (`src/rag/gate.py:296-299`). **No cost figure compares across it.** This is why R157 *supersedes*
  R60's $0.000525 rather than contradicting it. `[read from source]`
- **The session corpus was re-frozen twice** — `4155c97b0a2b` → `49b525f2a131` → `be54610a7f78`
  (Batch G, D-19(d), D-20 residual risk). **Numbers do not compare across a stamp.**
  *Precision note, and it matters:* these three stamps are `test/session_corpus.json`'s
  (`src/harness/sessions.py:44-50`), **not** the vector corpus manifest, which stayed
  `515c707a5d69` all phase. R156 cites the session-corpus re-freezes as the precedent for what a
  *vector*-corpus re-freeze would cost. The precedent is sound; the two stamps are different
  objects and a reader must not merge them. `[read from source]`

**3. Every numeric row carries** the number with its unit, the date or phase, how it was produced
(named script or command), and a provenance tag: `[measured]`, `[computed]`, `[read from source]`,
`[reasoned]`, `[estimate]`. **A number that cannot be sourced is omitted, never guessed.**

**Primary sources.** `plans/mitya-v2/research/p7-findings.md` (R134-R157 and the P7 EVIDENCE
RECORD); `plans/mitya-v2/02-decide/decisions-p7.md` (D-1..D-25);
`plans/mitya-v2/04-plan/phase-7-audit.md` §X; `plans/mitya-v2/04b-schedule/schedule-p7.md`;
`status/mitya-v2/STATUS.md`; `src/optimizer.py`, `src/search.py`, `src/p7_objective.py`,
`src/rag/gate.py`, `src/rag/generate.py`, `src/rag/context.py`, `src/config_artifact.py`,
`src/pricing.py`, `src/held_out_split.py`.

---

<a id="VO2"></a>
## VO2 — The search space, and how it was restricted

**The starting surface.** `ServingConfig.model_fields` holds **30 fields**
(`scripts/check_audit_partition.py`, 2026-09-01, `[measured]`). The audit's §X partitioned them
into four lists — in, out, records-not-knobs, calibration — and the partition was **disjoint but
not exhaustive**: 8 in / 12 out / 5 records / 1 calibration = 26, leaving **4 unplaced**
(`phase-7-audit.md` §X.5, P-23, 2026-09-01, `[measured]`). `prefetch_limit` was placed IN on
evidence the same day (4 → 3), and D-6 placed the remaining three OUT, after which the check
reports `fields=30 unplaced=none in_two_lists=none` and exits 0. `[read from source]`

**The admission rule (D-5).** Default-deny explicit allowlist, one field at a time, admitted by a
human, each entry recording three properties as **data** (`src/optimizer.py`, `SpaceEntry`):

| property | test |
|---|---|
| **P1 proposable** | lives on `ServingConfig`; churns `config_version`, never `corpus_version` |
| **P2 behavior, not belief** | changes what the serving path *does or spends*, not what it *believes* |
| **P3 an instrument with power** | a named instrument that can score the objective **and** hold the guard |

Enforced by two tests in the proposer's own suite: **partition completeness** (a field added to the
artifact later is born OUT and breaks the build until a human places it) and **instrument
integrity** (no allowlisted name may be read by the pricing, limits, gate or metrics machinery —
`model_rates`, `model_limits`, `unknown_model_limits`, `no_tpm_limit` fail by construction).

**The constraint that disqualified every softer form**, stated at `phase-7-audit.md` §X.3 and
carried into D-5: *an optimizer permitted to edit `model_rates` drives its cost objective to zero
without touching a token.* The multiplier in the objective function must be unreachable **by
construction**, not by classification. `[reasoned]`

**The value domain (D-8(b), owner, 2026-09-02).** The strategy stayed evolutionary; what was
restricted is the **domain**. `FIELD_DOMAIN` in `src/search.py` holds an explicit tuple per field,
production's own value included so the search can walk an axis back.

| axis | production | legal values, E-run(a) |
|---|---|---|
| `memory_budget_tokens` | 3072 | 1024, 2048, 3072 |
| `top_k_fused` | 25 | 5, 13, 25 |
| `top_k_rerank` | 3 | 2, 3, 4 |
| `prefetch_limit` | 50 | 50, 100, 200 |
| `allocation_policy` | `passages_first` | **excluded** — D-8(a) |

**Reachable space: 3⁴ = 81 configs, against 19.5M over the full ranges**
(`src/search.py:60-68`, `[read from source]`). Widening a tuple is a human's edit.
`_check_domain_covers_allowed()` makes an allowlisted field with no domain — and a domain entry
with no allowlist row — a loud build break.

**`allocation_policy` was excluded before the run, on a dry-run result (D-8(a)).** Its cheapest
candidate was `passages_only` at **objective $0.002917 against a $0.003983 baseline — 24% under**
`[baseline: v2 P7 production config]`, 2026-09-02, `[measured]`. `PassagesOnlyPolicy`'s own
docstring (`src/memory.py:534`) calls it *"the TEST of the seam, not a candidate"* — it does not
allocate, it deletes memory. Left in, E-run(a)'s headline would have been *"the optimizer found a
24% saving"* where the saving is *delete the memory system*, **and no gate would have blocked it,
because `computed_cost_usd` cannot see memory at all** (VO4).

**The space after the run (D-9).** Four of five axes left on measurement, not assumption. The
allowlist ends the phase at **2 IN / 28 OUT = 30**, partition exhaustive. Per-axis verdicts are in
VO5; the exclusion reasons are recorded in `src/optimizer.py`'s `EXCLUDED` dict, one paragraph per
field, each with a re-entry condition. `[read from source]`

---

<a id="VO3"></a>
## VO3 — Three cost numbers: one ranks, one blocks, one records nothing

**The phase's central divergence, by decision (D-1) and not by accident.**
`phase-7-optimizer.md` said two cost numbers exist. The step-0 audit walkthrough said three.
R134 (P-20, 2026-09-01, `scripts/probe_cost_instruments.py`) settled it: **three**.

| | `computed_cost_usd` | `_estimated_cost_usd` | `mitya_answer_cost_usd` |
|---|---|---|---|
| **call site** | `src/rag/gate.py:251-282`, consumed by `cost_layer()` at `:294` | `src/rag/generate.py:323-339`, passed to `guarded_generate` at `:450` | `src/metrics.py:380`, called from `src/api/app.py:1181` and `src/summarizer.py:183` |
| **prompt side** | **reconstructed, never read**: `passages × manifest.chunk_tokens + len(persona_head)//4`, `passages = top_k_rerank if rerank_enabled else top_k_fused`. Excludes the memory block, composition-A framing, the question, STRUCTURAL's chapter summary | **the real assembled messages**, `sum(estimate_tokens(m["content"]))` — char/4, not a tokenizer. Includes memory, framing, question | **provider-reported** `usage["prompt_tokens"]` |
| **completion side** | `config.max_tokens` — the ceiling | `Config.MAX_TOKENS` — the same ceiling | realised `usage["completion_tokens"]`, with `reasoning_tokens` inside it (R55/R56) |
| **requires** | a `ServingConfig` + a `CorpusManifest`. No request, no retrieval, no provider | an **assembled** messages list ⇒ retrieval and the memory read must have run. Still no provider | a completed provider response with a usage chunk, `OTEL_ENABLED=true`, **and** a `model_rates` row with `metered: true` |
| **role in P7** | **the gate BLOCKS on it** | **P7 RANKS on it** (D-1) | **records nothing** |

All three re-derived independently inside the probe and asserted against the function's output
before any table printed — the run's known-answer check, run first and passing. `[measured]`

**Finding 1 — the "ceiling" is a ceiling on completion and a floor on prompt.**
`computed_cost_usd`'s docstring calls it a CEILING and justifies over-stating as *"the correct
direction for a budget"*. On a memory-bearing request it **under-states the real prompt by 1,074
tokens — 41%** (2026-09-01, `probe_cost_instruments.py`, `[measured]`), because the reconstruction
never sees the memory block or the framing. **Nothing in the report says so**, and the false
docstring survives in `src/rag/gate.py` — a P3-class fix outside P7's write surface (D-1 residual
risk). `[read from source]`

**Finding 2 — the third number is not merely uncomputable offline; it is not being recorded.**
Every row on the shipped price card is `metered: false`
(`src/config_artifact.py:196-243`, `_DEFAULT_MODEL_RATES`; R48's rule — price at list rate so a
USD/answer figure exists at all, report it everywhere as a **list-rate estimate at the free tier**,
never as spend). `src/api/app.py:1173-1181` is a three-way branch on `cost.metered`, so on this
config every answer routes to `record_unmetered_answer()` and **`mitya_answer_cost_usd` has taken
zero observations.** Asserted in the probe's known-answer check. *An objective built on this metric
would be built on an empty histogram, and the failure mode is silence, not an error.*
`[measured]` / `[read from source]`

**Therefore: actual spend across P7 is $0.00.** Every USD figure in this document is arithmetic on
a price card, not a bill. "Beating $0.00" is not possible; the list-rate framing is what makes a
comparison exist at all (R157 §6).

**Why (c) won, in the priority order (D-1).**
*Security:* (b) opens a metered faucet inside an unattended loop and puts pressure on the one flag
the objective must never author; (a)'s blindness is itself an honesty failure — a candidate that
bloats memory shows **no cost increase**, invisible degradation in the objective's own dimension.
*Usability:* (c) sees the axes the phase exists to search; (a) forfeits five of nine; (b) caps the
search at **13 points/day** at n=38 (R138, `[computed]`).
*Enterprise realism:* ranking on a pre-flight estimate cross-checked against realised usage is how
a budget-governed system behaves.

**Four mandatory conditions attached to D-1**, all held through the phase: the divergence is
printed on every trace row and in the headline, never silent; one realised cross-check before any
published saving; **the `metered` flag is not P7's to flip** (flipping it would promote an estimate
to a measurement — the register's forbidden move); and the three write-path memory caps leave the
searchable space on P3 (VO4).

**Known limits of the ranking number, stated by D-1 itself.** char/4 is not a tokenizer (−4.5% vs
provider on one request, `[measured]` at n=1 — *an indication, not a number*); the completion half
is a **constant** at the ceiling, so the objective **cannot see a candidate that shortens answers**;
and the gate's cost layer blocks at **tolerance zero** (`src/rag/gate.py:312`: `BREAK if cand_usd >
prod_usd`, no MDE), so a candidate can rank first and be blocked.

---

<a id="VO4"></a>
## VO4 — What each number reads, and the five memory axes at exactly 0.000

**The three numbers on one deterministic memory-bearing request**, active artifact
`fb5cb5a842e7`, corpus `515c707a5d69`, 2026-09-01, `scripts/probe_cost_instruments.py`
`[baseline: v2 P7 production config]`:

| number | prompt tok | completion tok | USD | tag |
|---|---|---|---|---|
| `computed_cost_usd` | 1,552 | 1,024 (ceiling) | **$0.0030256** | `[measured]` |
| `_estimated_cost_usd` | 2,626 | 1,024 (ceiling) | **$0.0033478** | `[measured]` |
| `mitya_answer_cost_usd` | — | — | **uncomputable offline, and unrecorded** | `[read from source]` |
| realised, R132-derived | 2,751 | ~462 `[inferred]` | $0.0019803 | `[computed]` |

*These are the short-fixture figures and are not comparable to VO7's 4,739.7-token production
prompt — see VO13, R149.* `probe_cost_instruments._turns()` was deliberately left unchanged after
the recalibration, because R134/R135 are published against the short fixture and silently editing
it would restate findings made under conditions their readers were never told about.

**The nine-field × three-number matrix (R135, P-21, 2026-09-01, `[measured]`).** Each axis
perturbed on a `model_copy` of the active artifact; both computable numbers recomputed.

| axis | perturbation | Δ `computed_cost_usd` | Δ `_estimated_cost_usd` |
|---|---|---|---|
| `top_k_rerank` | 3 → 2 | −0.000150 (−500 tok) | −0.000150 (−501 tok) |
| `top_k_fused` | 25 → 5 | 0.000 | 0.000 |
| `prefetch_limit` | 50 → 200 | 0.000 | 0.000 |
| `memory_budget_tokens` | 3072 → 1024 | **0.000** | 0.000 *(fixture artifact — see VO13)* |
| `memory_profile_max_facts` | 10 → 5 | **0.000** | **0.000** |
| `memory_profile_max_prefs` | 20 → 10 | **0.000** | **0.000** |
| `memory_episodic_max_per_tenant` | 20 → 5 | **0.000** | **0.000** |
| `allocation_policy` | `passages_first` → `passages_only` | **0.000** | −0.000310 (−1,032 tok) |
| `persona_head` | 208 → 416 chars | +0.0000156 (+52 tok) | +0.0000156 (+52 tok) |

`mitya_answer_cost_usd` is `UNCOMPUTABLE OFFLINE` on every row.

**The desk read is CONFIRMED, on the strongest version of the test.** All five named memory axes
report `computed_cost_usd` delta **exactly 0.000**, and `memory_budget_tokens` holds it across a
full sweep — 3072 → 128, memory block 1,031 → 175 tokens, `computed_cost_usd` **constant at
$0.0030256 at every point**. The gate's only cost number cannot see the axis R132 measured at
**44% of all context tokens** and **1.328× per session** — A13's largest identified prize.

**And it is worse than blindness on three of the five.** `memory_profile_max_facts`, `_prefs` and
`memory_episodic_max_per_tenant` read 0.000 in **both** computable numbers, because the caps are
enforced **only on the write path**: `src/memory_store.py:185-192` (`_cap_for` → `_evict`, called
from `remember()`) and `:341` (episodic cap inside `remember_episode()`), while the reads —
`profile()` at `:348`, `episodes()` at `:376` — apply the retention window and **no cap at all**.
*Lowering a cap changes nothing until a write happens, and it never retro-actively shrinks a store.*
A candidate proposing `memory_profile_max_facts: 5`, measured on a pre-existing store, would score
identical to production for a reason that has nothing to do with its merit. **All three left the
allowlist on D-5's P3** (D-1 item 4, 2026-09-02) — and `src/search.py`'s
`_check_domain_covers_allowed()` was tightened in the same pass because the covering-only version
of the check *would have kept proposing them*. `[read from source]`

**`allocation_policy` is the one memory axis with a measurable offline delta**, and only on the
assembled number, precisely because it is the axis that turns memory off wholesale: −1,032 tokens,
−$0.00031/request at list rate, −9.3% of the assembled prompt.

**Count correction, recorded.** The schedule says "8 axes"; §X.1 has **8 rows** but **9 existing
fields** (`memory_profile_max_facts` and `_prefs` share a row), plus two proposed rows
(`prefetch_limit_dense`/`_sparse`, `neighbor_expansion`) whose fields did not exist and could not be
perturbed at all. The matrix is 9 fields.

---

<a id="VO5"></a>
## VO5 — E-run(a): four of five search axes did not survive measurement

**What ran.** Evolutionary search over D-8's restricted domains, seed 3, 2026-09-02.
**25 mutations tried, 4 reached the gate, all 4 `does_not_block`**, terminated on the
duplicate-candidate stall after **~9 minutes**. `CURRENT_POINTER` unchanged (`fb5cb5a842e7`)
throughout; **nothing promoted**. Full trace checkpointed per point at
`test/search/20260902T202312Z-evolutionary-seed3.jsonl` (D-8(c): the trace is written as the run
goes — *evidence that only exists if the process exits cleanly is evidence with a failure mode*).
`[measured]`

| # | change | from → to | `objective_usd` | gate ceiling |
|---|---|---|---|---|
| 1 | `top_k_fused` | 25 → 13 | 0.003980 | 0.0030256 |
| 2 | `memory_budget_tokens` | 3072 → 1024 | 0.003360 | 0.0030256 |
| 3 | `top_k_rerank` | 3 → 2 | 0.003238 | 0.0028756 |
| 4 | `top_k_fused` | 13 → 5 | 0.003236 | 0.0028756 |

**The gate's cost ceiling is identical for points 1-2 and again for 3-4**, while the ranking
objectives differ by $0.00062 and $0.000002. **Two of four accepted improvements were invisible to
the number the gate blocks on** — D-1's divergence measured rather than argued (R150, `[measured]`).

**The four measured verdicts that emptied the space (D-9(a)).**

| axis | verdict | evidence |
|---|---|---|
| `allocation_policy` | **degenerate** — one real value plus a test arm | D-8(a); `src/memory.py:534` |
| `prefetch_limit` | **no-op in local mode** — zero movement in pool recall, served recall and latency across 50/100/200 at two `top_k_fused` settings, on all five families | R151 |
| `top_k_rerank` | **pinned at 3** — 3→2 buys $0.000162/answer against −3/10 on `book_named_no_chapter`, at flat latency | R151 |
| `top_k_fused` | **latency axis, not cost** — 2.3× at 10, ~0.04% of cost | R150, VO6 |
| `memory_budget_tokens` | **the only cost axis** — and the only one with no instrument that can see its downside | R149, R127, R148 |

**`prefetch_limit` is free because it is INERT.** Local-mode Qdrant scores every one of the 3,284
points and ignores `SearchParams` entirely (`src/rag/retrieve.py:105-110`), so RRF receives the same
ordering whatever the limit says. It costs nothing **and buys nothing** — and it refutes a
pre-run hypothesis: deeper prefetch does **not** buy back the pool recall that shrinking
`top_k_fused` costs (`f10 p50` and `f10 p200` are identical at 0.8158). Scope: this is a
**local-mode** finding; under Qdrant server mode with HNSW (A1) prefetch depth would matter.
2026-09-02, `[measured]`.

**`top_k_rerank` 3 → 2, the trade in full** (R151, isolated sweep, `top_k_fused`=25,
`prefetch_limit`=50, per family, all `targets`-bearing rows, `[measured]`):

| family | n | served k=3 | served k=2 | delta |
|---|---|---|---|---|
| `book_named_no_chapter` | 10 | 0.3000 | **0.0000** | **−0.3000** |
| `primary` | 38 | 0.6053 | 0.5789 | −0.0264 |
| `quotation` | 15 | 0.9333 | 0.9333 | 0.000 |
| `structural` | 32 | 1.0000 | 1.0000 | 0.000 |
| `negative_control` | 2 | 0.0000 | 0.0000 | 0.000 |

**All 3 of `book_named_no_chapter`'s 10 hits sit at reranked position 3** — every one lives in the
passage `k=2` drops, and the mechanism is plausible rather than merely numerical: when a query names
a book but no chapter the correct chapter is a weaker match and lands lower. 3 rows of 10 and 1 of
38 are **below every MDE in the phase and not detectable** by this project's own rule; the pin rests
on **asymmetry**, not significance — if the effect is real a third of one family's grounding is
gone; if it is noise the saving is $0.16 per thousand answers at list rate on a free tier
(`src/optimizer.py`, `EXCLUDED["top_k_rerank"]`, `[read from source]`).

**The blocking rule is not "outside the MDE"**, recorded because it is easy to assume it is
(`src/rag/gate.py:86-92`): `BREAK` — the only blocking verdict — requires an exact binomial test to
reach significance **AND `b > c`**. A delta *outside* the MDE is `DETECTED`, which is reported and
does not block. The **cost** layer is the opposite: `BREAK` on any increase, tolerance zero. **The
gate is deliberately asymmetric.** `[read from source]`

**E-run(a)'s substantive result is the collapse, not the number.** §VIII pre-authorised a null
branch; this is sharper than null — one axis inert, one degenerate, one a bad trade, one on the
wrong currency. E-run(a) is closed: *one axis with three values is a sweep, not a search*, and R149
already contains it (D-9(d)).

---

<a id="VO6"></a>
## VO6 — The latency findings (CP-7i, and the objective it is not on)

**Latency is not the ranking objective.** D-2 refused to widen A13: **a cost-flat axis sits outside
the optimizer loop entirely.** In-loop the persona axis is its cost projection (`persona_head`
length, +52 tokens per 208 chars) and nothing else. Latency-only knobs are measured by a
standalone, pre-registered instrument with its own stated objective — CP-7i's option (ii) shape:
$0.00, deterministic, no faucet, no CP-7e gate. The sweeps below **are** that instrument.

**The `top_k_fused` sweep — three metrics, isolated, per family**, every other field at production
`[baseline: v2 P7 production config]`, 2026-09-02, `[measured]`:

```
family                     n |  pool 25     10      5 |  rr@3 25     10      5 |   ms 25    10     5
book_named_no_chapter     10 |   0.4000 0.2000 0.2000 |   0.3000 0.1000 0.1000 |    2078   944   623
negative_control           2 |   1.0000 0.5000 0.5000 |   0.0000 0.0000 0.0000 |    2049   970   609
primary                   38 |   0.9211 0.8158 0.7105 |   0.6053 0.6579 0.6579 |    2096   982   625
quotation                 15 |   0.9333 0.8667 0.8667 |   0.9333 0.8667 0.8667 |    2084   912   596
structural                32 |   1.0000 1.0000 1.0000 |   1.0000 1.0000 1.0000 |     130   126   138
```

Timing decomposition on `primary` (n=38), `top_k_rerank` held at 3:

| `top_k_fused` | retr ms | rerank ms | total ms | rerank % | pool recall | recall@5 | post-rr@3 |
|---|---|---|---|---|---|---|---|
| 25 | 273 | 1,848 | **2,121** | **87.1%** | 0.9211 | 0.7105 | 0.6053 |
| 13 | 267 | 902 | 1,170 | 77.2% | 0.8158 | 0.7105 | 0.6579 |
| 10 | 256 | 661 | **917** | 72.1% | 0.8158 | 0.7105 | 0.6579 |
| 5 | 255 | 325 | **579** | 56.0% | 0.7105 | 0.7105 | 0.6579 |

**Four things this establishes.**

1. **`top_k_fused` is a LATENCY axis, not a cost axis.** Retrieval proper is flat at ~260 ms; every
   millisecond saved is reranker work not done. **25 → 10 is 2.3×** (2,121 → 917 ms); 25 → 5 is
   3.7×. The rerank share at production reproduces **87.1%** here.
   `[baseline: v1 recorded]` — v1 E28 measured rerank at **~1.96 s of ~2.25 s retrieval, 87%**, on
   the GTX 1060. `[baseline: v2 P2]` — R34 measured **84.2%** (2,238.5 ms of 2,659.1 ms). **All
   three agree within 3 points across two years, two machines and three instruments.**
2. **`recall@5` is flat at 0.7105 across every value** — the invariance the audit asserted by
   construction, now demonstrated. `[baseline: v1 recorded, E23]`, where 0.7105 = 27/38 is itself
   the corrected denominator (v1's own headline was 0.720 = 18/25; the set grew).
3. **`structural` is immune and 16× faster** (~130 ms): it routes through
   `retrieve_structural_excerpts()`, hard-filtered to a resolved chapter, and never sees the fused
   pool. 32 rows unaffected.
4. **The served-quality picture is MIXED per family and pooling hides it.** `primary` gains 2 rows;
   `book_named_no_chapter` loses 2 of 10; `quotation` loses 1 of 15. **Net across DEFAULT-routed
   families: −1 row.** A primary-only reading of this sweep — taken first, and wrong — showed a
   clean gain. Every `rr@3` difference here is 1-2 rows against an MDE of 0.144 at n=38, so by this
   project's own rule they are **noise readings, not findings**. The deterministic and trustworthy
   columns are **time** and **pool recall**.

**`top_k_fused = 10` is the recorded choice; 5 remains equally admissible on today's evidence**
(owner, 2026-09-02, D-9(b)). They are quality-identical on every family and differ only on `primary`
pool recall and ~350 ms. Pool recall is **not a served metric** — only the reranked top-3 reaches the
prompt, and both settings produce the same top-3. What it measures is **headroom**:

| | primary pool | primary rr@3 | headroom | converted |
|---|---|---|---|---|
| k=25 | 0.9211 | 0.6053 | 0.316 | 66% |
| k=10 | 0.8158 | 0.6579 | 0.158 | 81% |
| k=5 | 0.7105 | 0.6579 | 0.053 | **93%** |

At k=5 the reranker sits at **93% of its ceiling**: any future gain recovers at most 5 points on
primary, because the target is not in the pool to find. **k=10 keeps 3× that room, and 350 ms is
the stated price.** The argument is a **prediction about future work, not a measurement**, and D-9's
residual risk says so.

**What CP-7i does and does not close.** The retrieval-latency half of G-P13 is measured here, out of
band and at $0.00, and the largest measured inefficiency in the system is named with its size.
**No `rerank_enabled` decision was taken** — R75 blocks pricing it, because the gated metric is
computed *above* the rerank line (VO14). And **G-P13's in-band write-latency delta against CP5's
2.1 s baseline remains unproduced** (`status/mitya-v2/STATUS.md:465`); that half is not CP7's and is
not closed here. No `D-` entry exists for CP-7i; the P7 EVIDENCE RECORD lists it among the closed
checkpoints and these sweeps are its evidence.

---

<a id="VO7"></a>
## VO7 — E-run(c): the 15.6% saving, held out of sample, and not promoted

**The accepted candidate, priced on the full search half**, four configs, one pass each, 2026-09-02,
`src/p7_objective.py` `[baseline: v2 P7 production config `fb5cb5a842e7`]`, `[measured]`:

| config | `objective_usd` | prompt tok | saving |
|---|---|---|---|
| production (3072, fused 25, rerank 3) | 0.003982 | 4,739.7 | 0.00% |
| **ACCEPTED** (1024, fused 10, rerank 3) | **0.003360** | **2,667.7** | **15.61%** (−2,072.1 tok) |
| budget only (1024, fused 25, rerank 3) | 0.003362 | 2,672.7 | 15.57% (−2,067.0 tok) |
| fused only (3072, fused 10, rerank 3) | 0.003980 | 4,734.7 | 0.04% (−5.1 tok) |

**15.57 of the 15.61 points are `memory_budget_tokens` alone. `top_k_fused = 10` contributes 0.04%
— five tokens. The cost story is one knob.**

**Supersession, inside P7.** E-run(a) reported ~~18.7%~~ (R150, best found: `top_k_fused=5,
top_k_rerank=2, memory_budget_tokens=1024`, $0.003236). **That config also carried
`top_k_rerank=2` and `top_k_fused=5`, both since reverted** (the pin, D-9(a); the headroom ruling,
D-9(b)). Quoting 18.7% for the accepted config would be the R51 error this project keeps catching —
*a number measured on one arm, quoted for another*. **The phase's figure is 15.6%** (D-9(c),
D-10(d)); 18.7% is retained above only as the measured cost of a config that does not ship.

**The held-out test (E-run(c), 2026-09-02, 349 s, $0.00, nothing promoted).** `CURRENT_POINTER` =
`fb5cb5a842e7` before and after. The held-out half was read **once**, as Batch B intended. The
instrument is a difference-in-differences, so that the two halves holding *different queries*
cancels out:

```
delta_half = objective(production, half) - objective(candidate, half)
gap        = delta_search - delta_heldout

mask_id=4501c54788b45d89   search=71   held_out=70   overlap=0
production   search    usd=0.003982  tok=4739.7
production held_out    usd=0.003985  tok=4748.4

candidate                          d_search    d_held   GAP tok    GAP usd   gap %
production (KA1)                        0.0       0.0       0.0   0.000000    0.0%
ACCEPTED (budget 1024, fused 10)     2072.1    2076.4      -4.3  -0.000001   -0.2%
  saving on each half                15.61%    15.63%
```

**Known-answer 1 passes exactly:** production scored against itself gives 0.000 on both halves.
**The saving generalizes: 15.61% seen, 15.63% unseen, gap −4.3 tokens / −0.2%, and negative** —
marginally better out of sample. `[measured]`

**Why the gap is small, and the sentence that must travel with the number.** *Not because the
optimizer generalized well. Because the search had no choice to make.* `top_k_fused` was settled by
D-9 on **latency and reranker headroom, not on cost** (it is 0.04% of cost). That leaves exactly
**one axis ranked on cost — `memory_budget_tokens` — and it is monotone**: lower is cheaper at
every value, on every query. Search-half argmax 1024; held-out argmax 1024. **A monotone single-axis
search has no interior optimum, so it cannot overfit.** The honest report is *"overfit on the cost
objective is exactly measurable, and it is structurally near-zero because the searched axis admits
no interior optimum"* — **never** *"the optimizer generalized well"*. Those two claims produce the
same number and are not the same claim (R152, D-10(c)). The −4.3 tokens **is** the magnitude of
query-to-query variation in how the memory budget binds — the only mechanism available to produce a
non-zero gap here.

**Known-answer 2 was unconstructable in-space, and that is recorded rather than skipped.** The
schedule required *"a deliberately overfit candidate must give a positive gap"*, because without it
a measured 0.000 is indistinguishable from an instrument that cannot see a gap. **No in-space
candidate can be search-half-special**, for the same monotonicity reason. What was established
instead: the instrument reads **exactly 0.000** when nothing changed, and **non-zero** when things
differ (−65.7 and −94.0 on 3-row dry-run subsets, differing in magnitude, consistent in sign).
**Recorded as a substituted check, never as the case the schedule asked for.**

**The quality half of the overfit number was not produced, by D-4, and it is not a gap in this run.**
The held-out half leaves `primary` 19, `structural` 16, `adversarial` 13; only three of eight
families clear R119's single-digit line. D-4 established that **no quality family can carry an
overfit gap at any split shape** — even the unsplit n=38 (MDE 0.144) cannot see effects ≤0.050.
The run reports *"overfit not detected at n=19 (MDE 0.203)"*, printed in full because the
near-emptiness of that sentence is the honest content. `[computed]`

**Split arithmetic, for the record** (R137, P-22, 2026-09-01, `scripts/probe_split_arithmetic.py`,
`[computed]`, at `p_disc = 0.100`; the gate's own form `MDE = 2.8·√(p_disc/n)`,
`src/rag/gate.py:41`) `[baseline: v2 CP0]`:

| family | n | 50/50 | 70/30 | full set |
|---|---|---|---|---|
| primary | 38 | 0.203 | 0.267 | **0.144** |
| quotation | 15 | 0.335 | 0.443 | 0.229 |
| structural | 12 | 0.361 | 0.511 | 0.256 |
| book_named_no_chapter | 10 | 0.396 | 0.511 | 0.280 |
| negative_control | 2 | 0.885 | ∞ | 0.626 |
| entity_pair | **0** | ∞ | ∞ | ∞ |

A 70/30 split raises primary's MDE by ~85%; `entity_pair` is a declared scored family with **zero
rows on disk**, so `gate.py:224`'s vacuous-denominator check never fires because a family with no
rows never forms a bucket. The split E-run(c) actually ran is ~50/50 (71/70), so the 70/30 column
is sizing, not what happened.

**A split is a reporting break with no automatic detector.** `_eval_set_stamp()` hashes the
*file*, and a split leaves the file untouched — two reports over different halves carry the **same**
provenance stamp and are not detectably incomparable. **No half-set fraction is quotable against the
published 0.7105 without its `mask_id` and n.** `[read from source]`

**Nothing E-run measured was promoted (D-10).** `CURRENT_POINTER` moved only for composition C.
The reason is not caution: **no quality verdict exists for the saving.** E-run(a)'s guard returned
`delta = 0.0000, b=0, c=0, mde=null` on every family — **zero by construction, not by measurement** —
and every accepted change is invisible to it:

| change | why `quality_1a` is blind |
|---|---|
| `top_k_fused` 25→13→5 | `recall@5` scores `fused[:5]`; the top 5 of a 25-list **is** a 5-list under stable RRF. Invariant by construction (audit §X.1) |
| `top_k_rerank` 3→2 | `quality_1a` is the **pre-rerank** leg (report caption: *"serving-path (rerank) bucket not evaluated — promotion-time only, R79"*) |
| `memory_budget_tokens` 3072→1024 | does not touch retrieval at all |
| `prefetch_limit` | never moved in this run |

**And the guard the audit specified for `top_k_fused` is not in the gate.** §X.1 requires *"pool
recall, per family. NOT `recall@5`"*; `scripts/eval_retrieval.py:166` computes pool recall and grep
finds **no reference in `src/rag/gate.py` or `scripts/gate.py`**. **The metric exists and the gate
never reads it.** Wiring it is a P3 change and D9's business, not P7's. `[read from source]`

**This is §VIII's named failure mode on the first real run:** an optimizer minimizing cost under an
insensitive guard is structurally incentivized toward invisible degradation, and it found the
cheapest thing the gate cannot see.

**The honest sentence that must travel with the saving (D-9(c)):** *"15.6% cheaper per answer,
exact on the objective; the quality cost of a smaller memory budget is unmeasured and unmeasurable
with v2's instruments."* R127 (the multi-turn metric is arm-invariant by construction — 68 memory
blocks and 8,547 tokens withheld, every scored family moved by 0.000), R148 (both eval sets are
single-turn, so no row can exhibit *"the model lost context it needed"*), A7/G-B (`quality_1b` ships
empty). **A judge would not settle it either.**

**Related, and different in kind (R143, 2026-09-01, `[measured]`):** expansion's *"exact wash"* is a
`recall@5` fact only. On **pool recall** — the metric `phase-7-candidate-fusion.md` guards on —
`full` is 35/38 = 0.9211 against `no-expansion` 33/38 = 0.8684, delta **−0.0526**, still inside the
MDE and **not detected**, but `n_required = 2.8²·0.1053/0.0526² ≈ **298 rows**` — *finite*, where the
`recall@5` comparison's `n_required` is **infinite**. A reader carrying the `recall@5` wash into a
`top_k_fused` or `prefetch_limit` decision would be carrying a number measured on the wrong list.

---

<a id="VO8"></a>
## VO8 — Batch G: retrieval is blind to the conversation

**Origin: a pre-batch check that refuted its own hypothesis.** The check asked whether
`max_recent_turns` is a no-op — whether `memory_budget_tokens` binds first. **It does not, and what
the check found is larger than the question.** Method: allocator arithmetic on
`plans/mitya-v2/05-impl/quality_step13_A_k3.json` (real served answers, 12 sessions of 4 turns),
then `scripts/run_multiturn.py` in process over `httpx.ASGITransport`, fake generation and real
Qdrant, `CACHE_ENABLED=false`, **one config promotion per arm — never an env var**. 2026-09-03,
`$0.00`, zero provider calls. `[measured]`

**1. `max_recent_turns` binds; `memory_budget_tokens` is the no-op.**

```
turn cost (q+\n+a): mean=296 median=288 p90=394 max=476
passages_block:     mean=1564 median=1593 max=1796
room = min(3072, 5120 - 1564) = 3072

 cap  served  rendered  delta      mem_budget  cap  served  rendered
   4    4.00      1251     +0            3072    4    4.00      1251
   3    3.00       968   -282            2048    4    4.00      1251   <- inert
   2    2.00       653   -598            1024    4    3.00       936
```

Four turns cost ~1,183 accounted; **even the largest turn observed (476) leaves four turns under the
3072 budget.** The candidate document's *"~370 tokens off every request"* for 4→2 is **−598
measured, understated by 60%** `[baseline: v2 P7 candidate doc `phase-7-candidate-memory.md` §III.1]`.
`memory_budget_tokens` 3072→2048 changes nothing served; 1024 binds only by admitting three turns —
which is `max_recent_turns = 3` under a worse label. **M1 and M2 are not independent axes.**
R151's no-op shape, on the axis nobody suspected. **R136 reproduced to the token: +68 overshoot at
four turns.**

**2. Retrieval never sees the conversation.** `src/api/app.py:844-847` resolves `body.query` against
a client-supplied `filters` and nothing else. Recent turns reach the prompt and never the retriever.
**68 of 108 turns** in `test/session_corpus.json` are contentless follow-ups — *"why does that matter
later on?"*, *"what happens right after this?"* — so **63% of multi-turn requests retrieve on a
string with nothing to retrieve on.** `[measured]`

*This is why `max_recent_turns` has no instrument.* `src/memory_metrics.py` family (c) scores
retrieval recall, so with memory unable to reach retrieval it is **structurally invariant** to the
knob — the same shape as STRUCTURAL's `recall@5` tautology. **Batch G had no guard because of a
layer nobody had traced, and the trace is the finding.**

**3. The repair fires and does not carry.** `n_prev_queries` prepends previous **questions** (never
answers) to the retrieval query: **~15 tokens of embedding input, zero prompt tokens.** Metric (c),
per family, never pooled `[measured]`:

| family | n | n=0 | n=1 | n=2 |
|---|---|---|---|---|
| `locate` | 22 | 8/22 0.364 | 8/22 0.364 | 10/22 0.455 |
| `passage_paste` | 30 | 12/30 0.400 | 17/30 0.567 | **19/30 0.633** |
| `structural` | 10 | 5/10 0.500 | 5/10 0.500 | 5/10 0.500 |
| `thematic` | 33 | 2/33 0.061 | 2/33 0.061 | 4/33 0.121 |
| `refusal` | 0 | — | — | — |

`structural` is flat and **must be** — its hard filter pins retrieval to the named chapter.
Passage tokens move +1.8% (162,267 → 165,199) and are **flat between n=1 and n=2** (165,176), which
is different chunks retrieved, not the feature's cost. `refusal` has no gold and is reported as
`--`, **never as 0.0**.

**On the purpose-built `anaphora` family — 20 three-turn sessions, last turn only scored — `n=0`
reads 0/20 and `n=2` reads 3/20, against a promotion threshold of 10/20 fixed before the run**
(`phase-7-corpus-anaphora-family.md` §8). §8's band for 1-9 is *"the repair FIRES and does not
promote"*. **Verdict: `n_prev_queries` ships at 0** (D-20). The field stays **versioned** — a knob
that changes what reaches retrieval must churn `config_version` whoever moves it, and defaulting it
off is not the same as deleting it.

**4. The authored instrument is weaker than the organic one, and that is the finding.**

| | predecessors at `n=2` | effect |
|---|---|---|
| `passage_paste` (organic, 2-turn) | **one** — `n=2` is effectively `n=1` | **+7 / 30** |
| `anaphora` (authored, 3-turn) | **two**, by construction | **+3 / 20** |

**Hypothesis `[reasoned]`, not a result: concatenation dilutes.** One prior query sharpens a
contentless probe; two lengthen the embedded string and blur it — at `n=2` the anaphora retrieval
query is the probe plus a ≤600-char span plus a ~180-char turn 2, ~800 characters embedded as one
vector. **Untestable here** — the arm that would settle it is `n=1`, dropped from the final design.
**Carried to v3 as a named hypothesis, not resolved** (D-20(e)).

**5. Two defects this produced, both fixed.** The live question was **last** in the concatenation and
nothing caps query length, so the encoder truncates silently from the tail and a long prior turn
cuts the user's question out of its own retrieval. Measured distribution: `passage_paste` mean 177
chars, max 547, p90 451 — **3× every other family**, and it is named for users pasting text. Fixed:
question **first** (free — BM25's query side is a bag of words at flat weight 1.0), each prepended
turn capped. **The cap was itself mis-sized at 400**, set on the corpus-wide mean of 177 while the
spans it must not clip run to 547; 5 of 20 authored spans were truncated. Now **600**. `[measured]`

**A third defect, in the instrument's denominator (D-19(c)).** Metric (c) scores **every** turn,
including turn 1, which has no predecessors and cannot respond to this axis. **35 of 95 scored turns
are turn 1.** For `passage_paste`: 12 immovable, 18 movable, so the reported 0.400 → 0.633 is
**+7 hits on 18 movable turns**. *The instrument halved its own effect, and the arithmetic was in the
artifact the whole time.* `[computed]`

**A process failure, recorded not smoothed.** The first run's decision rule arrived **after** its
numbers (D-19(a)); the second run's pre-registered threshold was set against a design that could not
reach it — the family's turn 2 was deliberately written without a literal quote so `n=1` and `n=2`
would separate, while the same document required `n=1` to clear 5/20, seeing only that weakened turn
2. **Both were set in one pass and never checked against each other.** The third was registered
first and the result honoured it.

**Cost (O8).** `$0.00`. Three run sets, eight arms, ~2 hours of wall clock, **no provider call**;
~9.4 min per arm, against an estimate of ~12 min total — **an undershoot of 2.3×**, because the dry
run's 4 turns barely exercise the per-turn summarizer path. The session corpus was **re-frozen
twice** and numbers are not comparable across a stamp (VO1). `[measured]`

**Disposition: the blindness ships unrepaired, by decision** (D-20). It is the largest user-visible
defect P7 found, and it is a deliberate call on the measured magnitude, not an oversight. **Whether
appending prior turns helps the model *answer* is untested and stays untested** — that is
groundedness, judge territory, `quality_1b` empty. And D-18(e)'s objection stands: if the model
answers from the transcript while the programmatic Sources list cites what the *current* query
retrieved, **the citation lies** — the same failure class the payload-match guard prevents in VO9.

**One correction the build made before any code was written (D-18(f)), recorded rather than
deleted.** The cache key was expected to break under `n_prev_queries > 0` — the corpus repeats
*"why does that matter later on?"* verbatim across sessions, so two sessions would key to one entry
having retrieved different passages. It does not: `cached = None if memory_bearing else
state.cache.get(...)` (`src/api/app.py:711`), and `n_prev_queries` can only fire when `recent_turns`
is non-empty, so the concatenation fires **exactly on the requests that already bypass the cache**.
**No key change was needed and none was made.** *The reasoning was right and only the premise was
missing; the guard that saves it is one someone else built for another reason.* `[read from source]`

---

<a id="VO9"></a>
## VO9 — Batch H: the axis measured out at zero, and an arm nobody proposed promoted

**What D-13 admitted, and on what claim.** `neighbor_expansion` entered on an arithmetic case from
`phase-7-candidate-granularity.md` §II.2: baseline `3 × 500 = 1,500` against A1's
`1 hit × (500 + 2 × 400) = 1,300` — *"the rare candidate where the cost objective and the quality
hypothesis point the same way"*. D-13(a) had already recorded the condition: **the entire 200-token
difference is `2 × chunk_overlap` of dropped duplicate**, so the saving is a function of a knob
another checkpoint decides.

**The arms were illegal before they were run.** A1/A2/A3 all move `top_k_rerank` to 1 or 2, and it
is **pinned at 3** (owner, R151). **The 13% saving was never expansion's — it was trading k down and
spending the proceeds on expansion.** With k fixed the arithmetic inverts:
`3 × (500 + 2 × 400) = 3,900` against `3 × 500 = 1,500`. `[computed]`

**The five arms.** 97 target-bearing rows of `test/retrieval_eval.json`; retrieve → rerank(full) →
`expand_spans()` → score; **one config promotion per arm**, `$0.00`, no provider call, no server;
2026-09-03. Scores what the **served spans** contain, not the fused pool. `[measured]`

| arm | radius | policy | recall@k | coverage | multi(9) | mean tok | vs baseline |
|---|---|---|---|---|---|---|---|
| baseline | 0 | `off` | 0.4227 | 0.4089 | 0.2963 | **1,535** | — |
| **merge-only** | 0 | `align_only` | 0.4227 | 0.4089 | 0.2963 | **1,502** | **−2.19%** |
| merge+backfill | 0 | `backfill` | 0.4330 | 0.4192 | 0.2963 | 1,589 | +3.5% |
| r1-align | 1 | `align_only` | 0.4227 | 0.4089 | 0.2963 | 3,351 | **+118%** |
| r1-backfill | 1 | `backfill` | 0.4433 | 0.4399 | 0.4074 | 3,849 | **+151%** |

*Note the denominator: 1,535 mean tokens is **served span** tokens on 97 retrieval rows, not the
4,739.7-token assembled prompt of VO7. The two are different instruments over different populations
and do not compose.*

**Known-answer check, registered before the run and passed:** both `align_only` arms are identical
to baseline **row by row**, not merely in aggregate. Rule 1 confines every admitted neighbour to its
hit's chapter, so expansion **cannot add a chapter** — only BACKFILL, which admits a new **rank**,
can. A divergence would have meant the payload guard was leaking and the run was void.

**So `r1-align` bought +118% for a gain that is zero by construction**, and narrowed the prompt from
3.00 to 2.59 spans while doing it — **14% of served material disappearing**, the cost D-13(c) named
in advance and this measured.

**The finding is an arm nobody proposed.** `expand_spans()` returned early on `radius <= 0`, so
**merge-without-expansion was unreachable**: the radius and the merging were one switch. Separated
(`src/rag/context.py:137-152`, `policy == "off"` is the disabled state, **not** radius 0):

```
merge-only vs baseline: recall and coverage IDENTICAL on all 97 rows
  -33.7 tok/row (-2.19%)   28 rows cheaper, 0 dearer   -116.7 tok on the changed rows
```

**Zero rows dearer — monotone, not a mean hiding a tail.** Cost is exact and counted, so **no MDE
applies** (A13/D-1) and `src/rag/gate.py:312` reads `DETECTED`, an improvement. Adjacent hits are
common, not exceptional: **41%** of rows on a 29-row probe and **29%** of the 97. *Serving two
adjacent chunks with their overlap duplicated was always waste; the expansion framing concealed a
defect.* The promoted arm was found **by reading a token column and asking why a consolidation cost
more.**

**The condition that must travel with the number (D-25(b)):** the −2.19% **is** `chunk_overlap`
dedup, `chunk_overlap = 100` (`src/config.py:84`, `[read from source]`). **Batch F prints the saving
with that condition attached or does not print it.** At `chunk_overlap = 0` it is worth **nothing**.
CP-7h closed without changing overlap, so the repricing-to-zero clause never fired.

**Backfill: direction without magnitude, ships OFF.** `+0.0103` recall and coverage on **one**
discordant row, MDE 0.0289 → BELOW; +3.5% tokens (17 rows dearer, 11 cheaper). **0 quality
regressions in any arm at any radius; 3 gains and 0 losses across both backfill arms.** Under R20's
standing rule no knob is selected on an effect below its MDE. Sizing what would settle it:

| method | n |
|---|---|
| McNemar, ≥5 same-direction discordants (`0.5⁵ = 0.031`) at a 1.03% rate | ~485 rows |
| R20-style, n where MDE falls to the observed +0.0103 | ~760 rows |
| discordant rate 1/97 = 1.03%, 95% CI [0.026%, 5.61%] → n for 5 discordants | point **485**, CI **[89, 19159]** |

**Two orders of magnitude, because the estimate rests on one event.** The honest statement is
*"this data cannot size the set that would settle it"*, **not** *"485 rows would."* `[computed]`

**The trap in the obvious re-entry argument (D-21(f)).** *"Backfill is potentially right given more
multi-target rows, and the cost is negligible"* is true of **two different arms and neither has both
properties.** Coverage-discordant rate, stratified `[computed, post hoc]`:

| arm | single-target (n=88) | multi-target (n=9) |
|---|---|---|
| merge+backfill (r0) | 1.1% | **0.0%** |
| r1-backfill | 2.3% | **22.2%** |

A ten-fold concentration — **but only with the radius on**, and mechanistically so. At 30%
multi-target r1-backfill would need **~61 rows** (27 authored) — but r1 costs +151%, and
`max_context_tokens` cannot clamp it, because the base expansion happens **before** any refill.
So the re-entry condition is **not** "more multi-target rows"; it is *27 multi-target rows authored
**AND** a decision to accept radius 1's +151%*, which reopens the `top_k_rerank` pin the phase
settled on measurement. **22.2% is 2 events out of 9, computed after seeing the pooled result:
hypothesis-generating, and no part of it is a power calculation anyone pre-registered.**

**Two defects the build closed before any arm ran.** (i) The chapter guard had a hole: Rule 1 pruned
neighbours across a boundary but not two **hits** that are themselves adjacent ids in different
chapters — ids run continuously across boundaries (`ingest.py:334`). Those merged into one span
rendering two chapters while its payload claimed one: **the citation lie the guard exists to
prevent, through the one door it did not cover.** (ii) BACKFILL's refill pulled the next rank
unconditionally, so a candidate adjacent to an existing span merely extended it and filled no slot;
a refill now counts only if it raises the span count. `[read from source]`

**The runaway D-14 bounded, and why it is a versioned field.** Hits at 13/14/15 with r=1 close to
`12-16`; hits at 16/17 close to `15-18`; the two touch and the transitive closure merges them to
`12-18` — `500 + 6 × 400 = 2,900` against 1,500, **+93%**, and under BACKFILL it iterates.
`Config.CTX_SIZE = 6144` is enforced as `max_input_tokens` at the gateway
(`src/rag/generate.py:451`), a truncation that would eat the persona and the memory block rather
than the span that caused it. Five rules in enforcement order, the cap binding **both** arms
(capping only BACKFILL would confound the policy under test with the cap), and **token accounting
measured on the rendered block, never computed from window arithmetic** — *the overlap dedup **is**
the cost mechanism, so computing it would be estimating the result under test.* That is R136's
mis-enforced ceiling verbatim, and the phase declined to build a second one. `[reasoned]`

**What ships (D-21(g)):**

| field | value | why |
|---|---|---|
| `neighbor_collision_policy` | **`align_only`** | merge-only, promoted on measurement |
| `neighbor_expansion` | **0** | the radius measures out at nothing |
| `max_span_tokens` | 2000 | guard, inert at radius 0, kept for the day radius returns |
| `max_context_tokens` | 2000 | guard, backfill's stop condition |

**Cost (O8).** `$0.00`, five arms, ~8 minutes of wall clock, no provider call.

**Baseline provenance caveat.** R154 and D-21 record *"one config promotion per arm"* but do **not**
name the pointer the baseline arm ran under. Batch H closed on the same day as Batch D; whether its
baseline is `fb5cb5a842e7` or the post-D-24 `b6063fd8be31` is **not stated in any source read for
this document.** The arms are internally comparable to each other, which is what the claim needs;
the absolute 1,535-token figure is not anchored to a named pointer.

---

<a id="VO10"></a>
## VO10 — Batch D, the cost half only

*Block rates, groundedness, false-refusal rates and the fence itself are `VM`'s. What follows is
the batch's cost, quota and provenance, which is the half CP7 owns.*

**Batch D is the only metered work in P7.** Everything else on the phase — the parallel track,
E-run(a), E-run(c), Batch G, Batch H, R141's ingest pricing, R157's arithmetic — ran at **$0.00 with
zero provider calls**. `[read from source]` across the register's per-entry cost notes.

**The draws.** `scripts/eval_fence.py`, 16 attack attempts at k=5 draws (verdict = max-over-draws)
plus 30 benign rows at k=1; then `scripts/eval_quality.py`, 58 paired cells at k=1. 2026-09-03.

| leg | draws | source | tag |
|---|---|---|---|
| security run incl. dry runs | **~360** metered calls | R155 cost note (O8) | `[measured]` |
| security, as counted in the day's sum | **~300** | R155 §9 | `[measured]` |
| quality generation | **~232** | R155 §9 | `[measured]` |
| dry runs | **~40** | R155 §9 | `[measured]` |
| **day total** | **~570 against RPD 500** | R155 §9 | `[computed]` |

**The two security figures (~360 and ~300) are both approximations recorded in one document and are
not reconciled at source.** Neither is load-bearing for the finding, which is the day total.

**The quota miss, and it is the phase's second instrument failure.** The first quality run exhausted
the `3.5-flash-lite` free-tier **RPD 500** at row 6 of arm C: **52 of 58 rows empty with
`quota_exceeded`**, and the six survivors were whichever ran before the trip — **a selection
effect**, R24's defect exactly (empty rows folded into means flip a sign). **Discarded, not
analysed.** Cause: *each leg was budgeted separately and the day was never summed.* The judge
survived only because `JUDGE_MODEL` is a different model id and therefore a different quota window
(`quota_scope()` is `(provider, model)`, G-P11). `CLAUDE.md` states the rule verbatim — *"Budget
against every rate limit, and take the minimum. A budget that consults one limit is not a budget"* —
**and it is recorded there because it was violated once before.** `[measured]`

**The consequence that outlives the miss, and it is a merge prohibition.** The re-run put both arms
on `models/gemini-3.1-flash-lite`, so **Batch D's quality evidence is on 3.1-flash-lite while the
promoted config serves 3.5-flash-lite.** Each comparison is internally valid on its own model;
**the two runs are one claim each, never one claim together.**

**Rate rows, for the arithmetic in VO12** (`src/config_artifact.py:196-243`, `[read from source]`;
these are `VP`'s infrastructure, quoted here because every USD figure in this document is derived
from them):

| model id | prompt $/Mtok | completion $/Mtok | `metered` |
|---|---|---|---|
| `models/gemini-3.5-flash-lite` | 0.30 | 2.50 | **false** |
| `models/gemini-3.1-flash-lite` | 0.25 | 1.50 | **false** |
| `openai/gpt-oss-120b` (Groq) | 0.15 | 0.60 | **false** |
| `local-q4km` | 0.0 | 0.0 | **false** |

**Budget arithmetic the phase never had to spend (R138, P-24, 2026-09-01,
`scripts/probe_p7_budgets.py`, `[computed]`).** Known-answer check reproduced R121's two published
binding-limit blocks exactly before any new number was read. On the serving row, at 2,751 prompt +
1,024 reserved output = **3,775 tokens per draw**:

```
rpm  15       BINDS pace     (tpm would allow 250,000/3,775 = 66.2)
tpm  250,000  does not bind
rpd  500      BINDS volume
tpd  ABSENT on this row -- and read by no line of src/ anyway (R121)
```

| rows/point | draws/point | points/day | USD/point (realised, list rate) |
|---|---|---|---|
| 1 | 1 | 500.0 | 0.0020 |
| 4 | 4 | 125.0 | 0.0079 |
| 10 | 10 | 50.0 | 0.0198 |
| **38** | **38** | **13.2** | **0.0753** |
| 50 | 50 | 10.0 | 0.0991 |

**At the audit's own headline denominator (primary n=38) a realised-cost objective supports 13
points per day.** That row is one of D-1's reasons for refusing (b). `tpd` binds on no faucet here,
so R121's enforcement gap does not bite — **but it would bite instantly on any Groq arm, where `tpd`
is both the binding limit and the one nothing in `src/` enforces.**

**The wall-clock currency, which is what the search was actually capped in (D-7(c)).**

| run | cost | source |
|---|---|---|
| non-promoting gate run | **29.4 s** = 2 × 14.1 s (full arm) + 1.25 s model load | R138 `[computed]` from `[measured]` parts |
| promoting gate run | **238.8 s** = + 2 × 104.7 s (rerank arm) | R138 |
| ~~R79's headline~~ | ~~241.6 s per point~~ | **superseded**: R79's run included the rerank bucket on every arm; P3 then took R79's own recommendation and gated rerank behind `if promote:` (`scripts/gate.py:464-466`). **An 8.1× difference in the number P7's throughput budget was built on**, and R79's text still reads *"100 candidate evaluations is ~6.7 hours"* — right for the *promoting* run (6.63 h), wrong for the routine one |
| Batch A, measured per point | **38.9 s** | D-7 `[measured]` — the number D-7's cap was actually set from, *"no cap from a cost that was never measured"* |

Corroboration measured 2026-09-01: the two `--arm full` / `--arm no-expansion` runs took **14.9 s**
and **13.4 s** on the grown 121-row set, against R79's 14.1 s on 106 rows. `[measured]`

**Why the cap is denominated in time and not USD (D-7(c)), with a correction to the reasoning
offered at the checkpoint.** Zero cost is **not** produced by the built-in rate limiter. Three
separate mechanisms: the limiter prevents **quota** exhaustion; `metered: false` means USD is a
**list-rate estimate and never a bill**; and the search is free because **D-1's objective makes no
provider call at all.** Only the third is why a USD cap caps nothing. *If the objective ever moved
to realised cost, the limiter would be unchanged and the search would stop being free.*

---

<a id="VO11"></a>
## VO11 — CP-7h: the chunking ablation, closed as a priced deferral

**Outcome (c) of three** — (a) run it, (b) refuse and price the refusal, (c) defer past v2 with the
price attached. **Taken: (c)** (D-25, owner, 2026-09-03). The checkpoint's rule: *"'We did not run
it' is not a finding; 'we did not run it, it would have cost X, and here is why the expected result
did not justify X' is."*

**X, half one — running it** (R141, P-27, 2026-09-01, `scripts/probe_ingest_price.py`).
Timed the two embedders the ingest path uses — `fastembed` `TextEmbedding(nomic-ai/nomic-embed-text-v1.5)`
and `SparseTextEmbedding(Qdrant/bm25)`, both local CPU — over 64 chunks of real corpus prose, then
extrapolated. **It never opens `data/vector_db/`**; disk is extrapolated from the existing
collection *precisely so that pricing the ablation does not become the thing that blocks a gate run.*
**Known-answer check, run first and passing:** the timed vectors are 768-dim, matching the active
manifest's `dense_dim`, **and** the embedder's `max_length` is 8192, which exceeds the largest arm's
`chunk_tokens` of 1500 — so no arm is silently truncated and the ablation would measure granularity
rather than truncation.

```
corpus 515c707a5d69, dense nomic-embed-text-v1.5 (768d), chunk_tokens 500
model load                     0.8 s warm  (69.9 s cold -- first run pulled 18 BM25 files from HF)
64 chunks: dense 26.3 s, sparse 0.02 s  ->  823.3 us/token  (411.7 ms per 500-token point)
existing collection on disk    38.2 MB over 3,284 points = 11.9 KB/point
```

**The sparse leg is free — 0.02 s against dense's 26.3 s. BM25 is 0.08% of embedding cost.**
`[measured]`

Priced **per token, not per point** — the arms are iso-token by construction (`points ×
chunk_tokens` ≈ 1.64 M in all three), so a per-point rate measured at 500 tokens would report B2 as
three times cheaper than it is `[computed]`:

| arm | `chunk_tokens`/overlap | points | chunks/chapter | wall | new collection |
|---|---|---|---|---|---|
| baseline | 500 / 100 | 3,284 | 14.2 | **22.5 min** | 38.2 MB |
| B1 | 750 / 150 | 2,190 | 9.5 | **22.6 min** | 35.0 MB |
| B2 | 1500 / 300 | 1,095 | 4.7 | **22.6 min** | 31.8 MB |

**Three arms: ~68 min of embedding, ~105 MB of new collections.**
**Excluded and named rather than absorbed:** parse + chunk, the Qdrant upsert, and — the term that
probably dominates — **one retrieval-eval scoring pass per arm**, ~14 s per arm *if* it can run
against an alternate collection without a code change, which is unverified. Also unpriced: **D2** is
unresolved, so three arms would pool under one corpus label and be indistinguishable from their
manifests.

**The constraint that makes it awkward.** Local-mode Qdrant takes an **exclusive process-level file
lock** on `data/vector_db/` (`docs/RUNBOOK-P6.md:16`, `scripts/ingest_corpus.py:8-12`), so building
three collections **cannot overlap a running `uvicorn`, a gate run, or another ingest** — the arms
serialize, and the failure mode when they do not *"looks like a hang, not an error"*. Collection
isolation solves the **data** problem and **not** the concurrency one: separate collections in one
local-mode directory share the one lock. That is **G-P23**'s shape (Qdrant server mode, deferred four
times as "not a blocker") and **this is the first shape that actually needs it.** `[read from source]`

**X, half two — succeeding** (R156, `[computed]`). *This is the half the schedule never wrote down,
and it is the one that decides.* A chunking arm is an **index** change
(`src/config_artifact.py:617` — `CHUNK_TOKENS` / `CHUNK_OVERLAP` / `EMBED_MODEL` change answers only
through an index). **Promoting one re-freezes the corpus stamp, and numbers are not comparable
across a stamp.**

| finding | what a third re-freeze does to it |
|---|---|
| **R154** — merge-only, **−2.19% tokens** | **doubly.** The saving *is* `chunk_overlap` dedup (`src/rag/context.py:145`) — not merely re-stamped, **re-priced**, to zero at overlap 0 and to an unknown value at any other overlap |
| **R150 / R151 / R152** — the 15.6% saving, gap −0.2% | the objective reconstructs the prompt as `passages × chunk_tokens`; the saving is denominated in the old chunk size |
| **R155 / D-24** — composition C, groundedness +0.724 | passages served are chunks; the judge scored answers built from this index |
| **R153 / D-20** — 63% retrieval blindness | a rate over retrievals against this index |
| **0.7105 (27/38)** | the published headline, re-measured against a corpus it was never measured on `[baseline: v1 recorded, E23]` |

**Cost to restore, from the record, floor not estimate:** Batch D alone was **~570 metered draws
against RPD 500 — ≥2 days of quota** before a single new number exists; E-run(c) 349 s; E-run(a)
~9 min over 25 mutations; every gate point 29.4 s non-promoting / 238.8 s promoting. **The embedding
is the small term. The re-measurement cascade is the expense, and it is paid only in the branch
where the ablation succeeds.**

**Why the expected result does not justify X.** The expectation was **pre-registered as null**
(audit §XI.8: the deltas land inside the MDE), and the checkpoint's own framing is that a null is
*"a success of the method, not a wasted budget line"*. **That framing holds when the budget line is
the only cost.** It does not hold here: *the null branch buys a null for ~68 minutes, and the
success branch bills the entire phase's evidence base.* **Time and money are the rejection reason,
and the money is denominated in re-measurement, not in embedding.** `[reasoned]`

**The domain prior, recorded as the owner's and explicitly not as a measurement (R156 §3).**
Internal-docs RAG has structure to chunk against — sections, headings, tables — and a
structure-aware chunker earns its keep there. Novels are continuous prose windowed *within* an
already-parsed chapter unit (`src/rag/ingest.py:357-397`), so what remains is a window width over
homogeneous text, and 500/100 is an accepted default for that shape. **Consistent with the
pre-registered null; evidence for nothing.**

**What CP-7h does NOT do (D-25(b)).** It does not re-open membership — chunking was out of the
search space under every outcome (audit §X.2, `[CORPUS]`, re-ingest per point). It does not reprice
Batch H — R154's −2.19% stands, conditional on `chunk_overlap = 100`. It does not resolve G-P23.

**And it does not validate 500/100.** `[baseline: v1 recorded, E6]` — v1 recorded the values
(`Config.CHUNK_TOKENS = 500`, `Config.CHUNK_OVERLAP = 100`, counted with the embedder's own
tokenizer) as **read from source**, never as a measured optimum. **500/100 is untested, not
validated. No chunking number exists in this project**, and R156 does not create one.

**Re-entry conditions, so the deferral is a drawer and not a silence (D-25(c)):** the corpus is
re-frozen for an unrelated reason (the cascade is then already paid and the ablation should ride
along); the domain stops being novels; **D2** lands and makes the arms distinguishable by manifest;
or **G-P23** takes Qdrant to server mode and the arms stop serializing.

---

<a id="VO12"></a>
## VO12 — O7: distillation closes on arithmetic, and the USD-per-answer curve

**Batch F step 9, R157, 2026-09-03. Desk arithmetic, no run, no provider call**, the form CP-ζ
specified. Extends R60/CP-ζ (P2's first answer) to P7's measured prompt shape and promoted arm.

**Which number, named first.** `_estimated_cost_usd` (`src/rag/generate.py:323-339`) — **D-1's
choice**, the assembled prompt summed over real messages with completion **held at the
`Config.MAX_TOKENS` ceiling**. **The local side is put on the same basis rather than left on a
realised one, which is the only reason the two halves are comparable at all.**

**Known-answer check, run before any new number was read:** pricing R152's measured production
prompt (4,739.7 tok × $0.30/Mtok = $0.001422) plus the ceiling completion
(1,024 × $2.50/Mtok = $0.002560) reproduces R152's recorded objective **$0.003982 to six decimals**.
That check pins the **rate rows and the completion ceiling**; it does **not** discriminate the two
cost numbers. The ranking number is identified by a different tell: **4,739.7 prompt tokens is
fractional** — a mean over rows whose assembled prompts differ — where the gate's reconstruction is
an **integer identical on every row of a config**. `[computed]`

**Two measurement breaks against R60, stated rather than absorbed.**

1. **`max_tokens` moved 400 → 1024** (`18570f5`). ~~R60's $0.000525/answer~~ is a **400-token
   figure and is not comparable to anything below.** **Superseded here, not contradicted.**
2. **Prefill grew ~1,900 → 4,739.7 tokens (2.5×)**, the P6/P7 prompt shape. R60's per-answer prices
   were computed at the old shape.

**The hosted curve at today's shape** `[computed]`, all at the ceiling:

| arm | prompt $ | completion $ | **USD/answer** |
|---|---|---|---|
| `models/gemini-3.5-flash-lite` (**the promoted serving arm**) | 0.001422 | 0.002560 | **$0.003982** |
| `openai/gpt-oss-120b` (Groq, cheapest arm CP-α selected) | 0.000711 | 0.000614 | **$0.001325** |
| gemini + E-run(c)'s **accepted** candidate (−15.61%) | — | — | **$0.003360** |
| gemini + E-run(a)'s **best found** (−18.7%) | — | — | **$0.003237** |

**Neither E-run candidate is promoted** (D-10; `CURRENT_POINTER` = `b6063fd8be31`). They are on the
curve as *measured reachable* costs, not as what serves.

**The arithmetic that closes the question.** R60 priced local as
`instance_rate × seconds_per_answer / utilization` and observed that distillation moves the numerator
while utilization is set by traffic. **Substituting the definition of utilization makes the point
exact rather than directional:**

```
util             = (answers/day x seconds/answer) / 86400
USD/answer       = rate_per_hour x (seconds/answer / 3600) / util
                 = rate_per_hour / answers_per_hour            <-- seconds/answer CANCELS
```

**USD/answer at a 24/7 rented instance is `rate ÷ answers-per-hour` and does not contain the model.**
At $0.09/hr and 100 answers/day: **$0.0216/answer** — and the long way round agrees:
decode-only 44.5 s/answer at R30's ~23 tok/s over 1,024 tokens, 5.15% utilization, $0.001113 at 100%
util, $0.001113 / 0.0515 = **$0.0216**. `[computed]`

**So the break-even is a traffic number, and only a traffic number:**

| to beat | required volume | vs today's 100/day |
|---|---|---|
| gemini $0.003982 | 542 answers/day | 5.4× |
| the accepted candidate $0.003360 | 643 answers/day | 6.4× |
| **groq $0.001325** | **1,630 answers/day** | **16.3×** |

**O7's answer, in CP-ζ's required form:**

> **Still not yet, and now for a stronger reason than P2 could state.** Serving cost is
> **$0.003982/answer** on the promoted arm and **$0.001325** on the cheapest available one, both at
> the `max_tokens` ceiling. A local instance at $0.09/hr costs **$0.0216/answer** at this project's
> traffic, and **a distilled model does not change that figure by one cent** — at a rented instance
> billed for idle time, USD/answer is `rate ÷ answers-per-hour`, and model speed cancels out of it
> exactly. Distillation's only lever is the **saturation ceiling**: 44.5 s/answer saturates one GPU
> at **1,941 answers/day**, and a 3× distilled model raises that to **5,822**. **Distillation is a
> capacity decision, not a cost decision, and this project has no capacity problem.**

**The margin narrowed and the conclusion did not.** ~~R60 read the gap at 41× against groq~~; at
today's longer completions it is **16.3×**, because a longer answer raises hosted cost and local
utilization together. Two more `max_tokens` doublings would keep narrowing it. **The direction is
worth carrying forward even though it changes nothing today.**

**The falsifier, named because it is the only assumption that would flip this.** **24/7 rental is
R60's choice and it is doing all the work.** Under **per-second billing** (serverless GPU, idle
unbilled) the cancellation does not happen and cost/answer is `rate × seconds/answer` =
**$0.001113** — which **beats groq's $0.001325 immediately, at any traffic, with no distillation
required.** That regime is not entered here for a reason that is itself unmeasured: a 7B q4 model
must be resident to answer in 44.5 s, and **per-request cold load is not priced anywhere in this
project.** Recorded as the falsifier, not resolved. R60's five assumptions all still stand; this is
a sixth R60 did not name. `[reasoned]`

**What is NOT claimed.** No realised per-answer cost is produced. Every figure is a **ceiling**, and
`metered=False` on every row means **actual spend remains $0.00 against free-tier quota**. R30's
17.380 s and ~23 tok/s are **GTX 1060** measurements `[baseline: v1 recorded, E28 — ~23.6 tok/s,
~21.3 s total at ≤400 decode tokens]`; a rented T4/A4000 is faster, which shortens seconds/answer and
therefore **does not move the USD/answer result at all** — it moves only the saturation ceiling.

---

<a id="VO13"></a>
## VO13 — The instrument defects, and what each would have published

*Three entries follow: **two are CP7's own defects**, and the third is an adjacent one recorded but not fixed. Both of CP7's own were caught by reading output, never by an
exit code.*

**1. The memory fixture understated a real turn by ~20×, and an axis read 0.000 across its entire
searchable domain (R149, 2026-09-02, `[measured]`).**

**How it surfaced: not from an audit.** Batch C's retrieval-cache work tried to use
`memory_budget_tokens` as its *"retrieval-irrelevant axis that still moves the objective"* example
and **could not** — no in-domain value changed the number. *The cache was correct; the axis was
dead.* `FIELD_DOMAIN`'s legal range for the field is **(1024, 2048, 3072)**.

| `memory_budget_tokens` | mean prompt tok, **before** | **after** recalibration |
|---|---|---|
| 3072 | 2727.6 | 4832.6 |
| 2560 | — | 4317.2 (−515.4) |
| 2048 | 2727.6 (**Δ 0.000**) | 3801.5 (−1031.1) |
| 1536 | — | 3283.8 (−1548.9) |
| 1024 | 2727.6 (**Δ 0.000**) | 2765.6 (−2067.0) |
| 512 *(out of domain)* | 2274.8 | — |

**Before: exactly 0.000 at 3072, 2048 and 1024 — the whole legal range.** It moved only at 512, a
value the proposer **can never emit**. After: monotone across the domain, and 3072 → 1024 is worth
**−2,067 prompt tokens / −$0.00062 per answer**, the largest single lever in the space
(`top_k_rerank` 3→2 is −541).

**Root cause, a defect P6 had already named in another form.** The fixture's recent turns came from
`scripts/probe_cost_instruments._turns()`, whose answer is `"She waits, as the poor always wait." × 4`
— **144 characters, ~36 tokens.** **R131 `[measured]`, against a live provider: a single prior turn
renders 738 and 708 tokens.** The fixture understates a real turn by ~20×, and the whole memory
block rendered **1,031 tokens — below the domain floor of 1024**, so *no in-domain budget could ever
truncate it.*

**What it would have published.** A search in which A13's largest measured prize — memory at 44% of
context tokens, 1.328×/session — was **structurally unreachable**, and a phase headline reporting
that `memory_budget_tokens` does not matter. **Not because the objective was blind, but because the
fixture was too small to make the budget bind.** Everything in VO7 — the 15.6%, the held-out gap,
the monotonicity argument — exists only because this was caught.

**The fix, and the discipline in it.** `FIXTURE_TURN_TOKENS = 723` in `src/p7_objective.py:142`, the
**midpoint of R131's two measurements**, with `_calibrated_turns()` reproducing the probe's turn
shape at that size. **Calibrated to a measurement, never to whatever made the knob move** — the
comment in the source says so, *because the difference between those two motivations is the entire
integrity of the number.* `[read from source]`

**What it supersedes.** ~~Batch C's "the objective sees the memory axes" demonstration~~ used
3072 → 512. The claim was true and the arithmetic right, but 512 is **below the domain floor**, so
the proof was made with a value no candidate can propose. **Any reading of Batch C's memory-axis
evidence should cite R149 instead.** It also creates a break *inside* P7: the 2,727.6-token prompt
of the pre-recalibration runs and the 4,739.7-token production prompt of VO7 are the **same config
under two fixtures**, and no cost figure compares across them.

**What it does NOT license.** No change to the serving path, no change to production's
`memory_budget_tokens`, no claim that 1024 is better than 3072. **The fixture remains synthetic**:
O5/O10's representativeness caveat is unchanged and is now *more* load-bearing, because the fixture
is the thing setting the size of the prize. **A candidate cheaper on this fixture and dearer on real
sessions passes every guard in v2** (D-7 residual risk).

**2. A day's quota went to a run that measured the wrong thing** — Batch D, ~570 draws against RPD
500, each leg budgeted separately and the day never summed. Full record and consequences in **VO10**.
**What it would have published:** arm C's quality means computed over 6 non-random survivors — R24's
defect, which is known in this project to flip a sign. It published nothing, because the run was
**discarded, not analysed.**

**3. A third, adjacent, recorded and not fixed (R136, `[measured]`).** The rendered memory block
**always overshoots `memory_budget_tokens`**, by ~56 tokens plus ~4 per admitted turn:

```
memory_budget_tokens  1024 ->  block 1031   (+7)
                       768 ->  block  831  (+63)
                       512 ->  block  578  (+66)
                       256 ->  block  313  (+57)
                       128 ->  block  175  (+47)
```

Cause `[read from source]`: `PassagesFirstPolicy.allocate()` (`src/memory.py:452-531`) admits recent
turns against the **raw** turn text but emits a `ContextBlock` whose `tokens` counts the **fenced**
text — `render_turns()` wraps the transcript in `_TRANSCRIPT_OPEN` + `_FENCE_RULES` +
`_TRANSCRIPT_CLOSE` and prefixes every line. One turn: raw 37 → rendered 93 (**+56**); three turns:
raw 111 → rendered 175 (**+64**). The fence is never charged to `room`, and the later tiers budget
against the **under-counted** `used`, so the error propagates rather than being absorbed. Reproduced
independently in Batch G at **+68 on four turns**, from different data down a different path.

**Why it matters to P7:** the constant is largest as a *fraction* exactly where the search would push
— 37% over at 128, 12% over at 512, 0.7% over at 1024 — so **a candidate proposing a small budget
would be credited with a saving it does not deliver.** One genuine point in D-1's favour: the
ranking objective prices the **rendered** block, so the overshoot is *inside* the objective rather
than hidden from it. **Recorded, not fixed** — `src/` was not the parallel track's write surface.

**The phase's transferable form of the rule.** P5 learned it on six instruments, P6 on three, P7 on
four more, **and P7's are the ones where the instrument was measuring a world that did not exist.**
Every one was caught by reading what matched, by a known-answer case, or by a human reading six
rows. **Not one was caught by a clean run.** The fourth form:
**a number that reads 0.000 is a claim about the instrument until something proves it is a claim
about the world.**

---

<a id="VO14"></a>
## VO14 — Three named blind spots

*Named separately because one summary sentence lets two of them hide behind the third.*

**1. The blocking quality metric is computed above the rerank line (R75).** `quality_1a` scores the
fused output pre-rerank, so **a candidate that changes what rerank *receives* is scored on a list
rerank never saw.** Consequences carried through the phase: `top_k_rerank` never reaches the guard
(VO7); `rerank_enabled` and `rerank_model` are `[UNINSTRUMENTED]` and excluded from the space
(audit §X.2); and pricing rerank-off — *a decision worth ~2.2 s per request* — is blocked on it
(schedule §8b(e)).

**2. The gate's cost layer is blind to every memory axis — CONFIRMED at exactly 0.000** across all
five (R135, VO4), including across a full `memory_budget_tokens` sweep from 3072 to 128. **And it is
worse than blindness on three of them: the caps act on the write path only**
(`src/memory_store.py:185-192, :341` against uncapped reads at `:348, :376`), so the gate cannot see
them **even in principle** — the axis has a *delayed* effect whose measurement depends on write
traffic a gate run does not generate. Those three left the allowlist on D-5's P3.
**The ranking objective is a separate story with a separate defect** — its memory fixture
understated a real turn by ~20×, which made `memory_budget_tokens` read 0.000 across its entire
searchable domain until R149 recalibrated it, after which the axis moved and E-run(a) took it
(VO13). **A number that read zero for a reason that was not the world.**

**3. `quality_1b` ships empty (G-B), so the quality guard is retrieval-side only.**
`scripts/gate.py:277` hard-codes `quality_1b: []` pending G-B, and the Batch A report confirms it
empirically: `quality_1b` 0 rows, `quality_1a` 15 rows of `recall_at_5` / `mrr` /
`distinct_target_chapters_hit_at_5`. **No composed quality gate blocks anything, and by A7 it never
could — CI must never call a judge.** Two downstream consequences the phase kept hitting: a `k=1`
screen has no surface to act on, because the blocking layers are deterministic and there is no `k`
in the blocking path to reduce (D-7(d)); and every *groundedness* hypothesis in P7 — the memory
budget's downside, neighbour expansion's contiguity claim, whether more prompt-side turns help —
lands in the same place: **cost exact, quality claim unavailable.** `[read from source]`

---

<a id="VO15"></a>
## VO15 — What CP7 never measured

*Absences, stated as absences. An absence that is not printed reads as a pass.*

- **No fairness number. G-P10 stays open**, inherited unchanged through P5, P6 and P7.
- **No allocation-quality claim.** R128 — and **P7 was explicitly forbidden to manufacture one.**
  `allocation_policy` left the space as degenerate (VO2), so **O4 remains open**, not newly opened.
- **No chunking number.** CP-7h closed on (c), deferred past v2 and priced (D-25, R141, R156).
  **500/100 is untested, not validated** (VO11).
- **No O4 retirement.** No multi-turn success metric exists.
- **No quality verdict for the 15.6% saving.** Not "non-inferior", not "not detected", not a break —
  **there is no verdict at all**, because the guard is invariant to every axis that moved (VO7).
- **Retrieval blindness ships unrepaired, by decision** — 63% of multi-turn requests retrieve on a
  contentless string; `n_prev_queries` ships at 0 (D-20, R153, VO8).
- **Nothing E-run measured is promoted.** `CURRENT_POINTER` moved only for composition C.
- **No `rerank_enabled` decision**, and **G-P13's in-band write-latency delta** against CP5's 2.1 s
  baseline was never produced (VO6).
- **No realised cost, and none can exist on this deployment.** `mitya_answer_cost_usd` has taken
  zero observations, by design, and flipping `metered` is forbidden (VO3).
- **`n=1` vs `n=2` for `n_prev_queries`** — the dilution hypothesis is recorded with its evidence and
  **carried to v3**, not resolved (D-20(e)).
- **Whether backfill helps** — real in direction, unmeasured in size, and this data cannot size the
  set that would settle it (VO9).
- **Merge-only's saving at any other `chunk_overlap`.** Known at 100, zero at 0, **unknown
  everywhere else.**

**Four rationales the phase found had outlived their premises, recorded as a reading list and not as
a backlog (R146).** They are named because a rationale ages more quietly than a number does —
nothing recomputes it, and no tripwire fires. **SPLADE's exclusion** is recorded in `CLAUDE.md` and
the audit as a 6GB hardware constraint; v1's own design spec cut it as **YAGNI**, in one list with
five other components, and **no measurement of SPLADE's footprint on this box exists** (R144). The
contention half of the hardware reason was retired for the serving path by A3, on the identical
reasoning `CLAUDE.md`'s own amended `ctx_size` rule already followed. **`precision_at_k`** is built,
`TIER_1A`-classified and has **zero callers**; its chunk-density objection is sound across a
re-ingest and **does not bind inside a search**, where every candidate shares one chunking (R145).
**MRR over NDCG/MAP** comes back clean and does not decay: with one relevant item at rank r,
NDCG = 1/log₂(r+1) against MRR = 1/r — different values, **identical ordering** — and Average
Precision with a single relevant item **is** 1/r, so MAP *is* MRR on 88 of 97 target-bearing rows
(R147). **Query rewriting**'s token case dies on arithmetic — a user query is ~20-40 of a ~2,727-token
prompt and stripping filler saves ~10 tokens, **under 0.4%** — while the case that survives is
contextual/coreference rewriting *before* retrieval, which memory does **not** cover and no v2 eval
row can see (R148). **None of the four licenses work.**

---

<a id="VO16"></a>
## VO16 — The closing evidence record

**Closed 2026-09-03.** Registers R134-R157. Decisions D-1..D-25. Batches A/B/C/C-fix, E-run(a),
E-run(c), G, H, D closed; CP-7a..CP-7j closed; Batch F is the record itself. `CURRENT_POINTER` =
**`b6063fd8be31`** (composition C, D-24).

**The headline, in the form CP-7g licensed:**

> **P7 cut the cost of a served answer by a measured 15.6%, out of sample, and did not promote it.**
> The saving generalizes (search 15.61%, held-out 15.63%, gap **−0.2%**, R152) and is **denominated
> in the ranking cost number, not the gate's**. It is unpromoted because **no quality verdict exists
> for it** — `quality_1b` is empty by design and A7 forbids a judge in CI. What *did* promote is a
> prompt: composition C, which cut prompt-injection violations 0.5625 → 0.1250 and came back
> **superior** on groundedness (+0.724 against an MDE of 0.343), after the measurement showed **the
> shipped prompt had no fence at all.**

*(The composition-C half of that sentence is `VM`'s to evidence; it is quoted here because the
headline is one sentence and splitting it would misreport the phase.)*

**§VIII's alternative headline is also earned**, on R150's numbers: **rerank is 87.1% of the
retrieval path at production and `top_k_fused` 25 → 10 buys 2.3× for 0.04% of cost** — *generation
stopped being the bottleneck and retrieval became it*, measured rather than asserted.

**Three things this headline may not be compressed into.**

| compression | why it is false |
|---|---|
| *"78% more secure"* | extraction is unmoved at 1/8 on every arm; the reduction is entirely the `jailbreak` family (`VM`) |
| *"Memory pays for itself"* | unmeasurable here by construction (R128) |
| *"Chunking is fine at 500/100"* | **untested, not validated** (VO11) |

**What may not be merged.**

- **Batch D's quality evidence is on `3.1-flash-lite`; the promoted config serves `3.5`.** Each
  comparison is internally valid; **one claim each, never one claim together** (VO10).
- **The session corpus was re-frozen twice** — `4155c97b0a2b` → `49b525f2a131` → `be54610a7f78`.
  **Numbers do not compare across a stamp**, and these are session-corpus stamps, not the vector
  manifest (VO1).
- **`max_tokens` moved 400 → 1024** (`18570f5`). **No cost figure compares across that break** —
  which is why R157 **supersedes** R60's $0.000525 rather than contradicting it (VO12).
- **Batch H's −2.19% is `chunk_overlap` dedup** and is quotable **only** with `chunk_overlap = 100`
  attached (D-25, VO9).
- **18.7% is not the phase's number.** It is a config carrying `top_k_rerank=2` and `top_k_fused=5`,
  both reverted (VO7).
- **The 2,727.6-token and 4,739.7-token prompt figures are the same config under two fixtures**
  (VO13). They do not compose.

**One correction the record made rather than propagated.** `status/mitya-v2/STATUS.md`'s "Next step"
still read *"Step 0 — the architecture audit — is the next thing to run"*. **Step 0 ran on
2026-09-01** and produced `04-plan/phase-7-audit.md`. **Corrected in place** — the G-P12(a)
precedent: *a documentation defect is fixed where it lives, not annotated where it is found.*

**Refused in advance, and held (D-10).** No re-run, **no widening the space to make something
pass**, and no quoting a number measured on one arm for another.
