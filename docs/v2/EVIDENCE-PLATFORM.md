# v2 Evidence — the platform phases

**Scope.** CP0 (measurement contract and eval power) · CP1 (telemetry, config identity, the operator
plane) · CP2 (the provider gateway) · CP3 (the CI eval gate) · CP4 (the traffic harness) · CP5 (auth
and tenancy). Memory, prompt security and the optimizer are in the sibling logs
(`EVIDENCE-MEMORY.md`, anchors **VM**; `EVIDENCE-OPTIMIZER.md`, anchors **VO**). This log owns
anchors **VP1..VP38** and they are frozen.

**How to read a row.** Every number carries its unit, the phase or date that produced it, the script
or command that produced it, and a provenance tag: `[measured]` (a command ran, output recorded),
`[computed]` (closed form over measured inputs), `[read from source]` (asserted by reading code),
`[reasoned]` (settled by argument, argument stated), `[estimate]` (judgement, never citable as a
measurement). A number that cannot be sourced is omitted, never guessed.

**Two conventions, in every row.**

1. **Every baseline states its origin inside the row** — `[baseline: v1 recorded]`,
   `[baseline: v2 CP0]`, `[baseline: v2 CP4]`, or `none — first datapoint`. A delta whose reference
   is ambiguous is not a delta. This bites constantly here: CP5's auth latency is measured against
   CP4's pre-auth figure, not against v1's; CP4's concurrency knee is measured against v1's
   `Semaphore(1)`.
2. **P7 is authoritative where registers disagree.** A superseded number is shown ~~struck~~, never
   deleted, with one line on why. The standing examples are `max_tokens` moving **400 → 1024** (a
   measurement break — no cost figure compares across it) and the session corpus being re-frozen
   twice (`4155c97b0a2b` → `49b525f2a131` → `be54610a7f78`; numbers do not compare across a stamp).

**v1 cross-references** are cited as `[E28 (v1)]` and resolve to `docs/v1/EVIDENCE.md` on `main`
(`git show main:docs/v1/EVIDENCE.md`).

---

<a id="VP1"></a>

**Citations to `docs/RAG.md`, `docs/DESIGN.md` and `docs/FINE_TUNE.md`** are **provenance, not
navigation**: they name the file a number was read from at the time it was recorded. Those three
documents were superseded by this set and removed from the tree; they remain readable at tag `v1`
and in every commit before the removal. Do not rewrite these citations to point at the new docs —
a provenance record that names its actual source is the point.

## VP1 — The eval sets, their versions, and what a version is

**A9 — append-only sets with immutable row ids.** A version is *the set of ids present*; any two
versions are comparable **on their intersection**. This is the mechanism, and it is what made an
unrecorded churn recoverable rather than fatal. `[reasoned]`

| Version | Frozen | Retrieval rows / sha256 | Quality rows / sha256 | Provenance |
|---|---|---|---|---|
| v1 | 2026-08-16, git SHA `6bf896a25f25` | 27 (25 primary + 2 negative controls) | 36 | `[read from source]` `git show 6bf896a2:test/retrieval_eval.json` |
| **v2.0** | 2026-08-24, commit `ef3c260` | 99 / `f3109937…` | 50 / `ab459c77…` | `[measured]` working tree clean at freeze |
| **v2.1** | **2026-09-01, commit `a683f0c`** | **141** / `1003728ce5ebb945` | **58** / `81e2b464f35d491c` | `[measured]` as `_eval_set_stamp()` computes them (`scripts/gate.py:82-95`), re-read from the clean tree after commit, unchanged from the pre-commit reading |

**The unrecorded middle, recorded now.** Two churns landed between v2.0 and v2.1 and only one was
written down at the time: (1) the `quotation` family, 99 → 121 rows, sha256 `c39e9bac`, never
recorded — which is what made "v2.0" name a set that was not on disk; (2) the structural expansion,
2026-09-01, retrieval `r-0122..r-0141` and quality `q-0051..q-0058`. **A9 holds across the whole
gap** — every id in v2.0 is present and unedited in v2.1 — so **no v2.0 number was lost**, and the
intermediate `c39e9bac` state did not need to have been recorded. `[measured]`
`[baseline: v2 CP0]`

**Structural denominators at v2.1.** Retrieval **12 → 32 of 141** (22.7%); quality **4 → 12 of 58**
(20.7%). Against a chapter-proportional expectation over the five books: **χ² = 3.14** (retrieval,
n=32) and **2.16** (quality, n=12), df=4, critical 9.49 — **indistinguishable from proportional**.
Zero-coverage holes closed: Brothers Karamazov 0 → 9 retrieval rows, Notes from the Underground
0 → 4. `[computed]` from `scripts/validate_eval_set.py` counts. Chapter-count is itself a weak
assumption — real structural traffic follows reading interest, and C&P holds 17.7% of the chapters
while being the most-read book. `[reasoned]`

**Per-family denominators, never pooled.** Mixing denominators produced v1's `0.731` bug
`[E23 (v1)]`; the rule exists so it is not rebuilt at scale. `structural` became its own family at
CP0 because a row whose chapter is regex-resolved and hard-filtered **cannot miss** (VP4).
`[reasoned]`

**Two version-stamp defects, recorded and unfixed.** `_eval_set_stamp()` hashes the file, so it
**cannot detect a split** — two reports over different halves carry the same provenance stamp
(P7 Batch B implication, `decisions-p7.md`). And `entity_pair` is a declared scored family with
**zero rows on disk**, where `gate.py:224`'s vacuous-denominator check never fires, because a family
with no rows never forms a bucket (R137). `[read from source]`

---

<a id="VP2"></a>
## VP2 — Detection power: the MDE curve that never crosses

**The rule.** `MDE = 2.8 × SE_diff` at 80% power / α 0.05 two-sided. For paired hit/miss under
McNemar, `SE_diff = sqrt(p_disc / n)` with `p_disc` the discordant-pair rate.

**Measured at primary n=40, four arms, post-R19 harness** (2026-08-20, `scripts/eval_retrieval.py
--arm {full,dense-only,sparse-only,no-expansion}`) `[measured]` · `[baseline: none — first power
analysis on this set]`:

| comparison | discordant | p_disc | delta | MDE | n required |
|---|---|---|---|---|---|
| full vs dense-only | 3 | 0.075 | −0.025 | 0.121 | ~941 |
| full vs sparse-only | 3 | 0.075 | +0.025 | 0.121 | ~941 |
| full vs no-expansion | 4 | 0.100 | 0.000 | 0.140 | ∞ (no effect) |
| dense-only vs sparse-only | 6 | 0.150 | −0.050 | 0.171 | ~470 |
| dense-only vs no-expansion | 7 | 0.175 | −0.025 | 0.185 | ~2195 |
| sparse-only vs no-expansion | 3 | 0.075 | +0.025 | 0.121 | ~941 |

**Every observed delta is 3-7× smaller than the MDE that would make it callable.** n=150 (the
original target) yields MDE ~0.072 against effects of 0.025-0.050 — short of all of them. **The
curve does not flatten at a useful threshold; it never crosses one.** `[computed]`

**Re-run at 106 rows and identical in every cell** (2026-08-29, R72) — same recalls, same discordant
counts, same MDEs, same n required — because **the headline denominator did not move**: all 61 new
rows went to `adversarial` (25), `sensitive` (19), `book_named_no_chapter` (10) or `structural` (10).
`[measured]` · `[baseline: v2 CP0's own n=40 analysis]`

**The instrument is thinner than n=40 suggests.** 32 of the 40 primary rows return an identical
hit/miss in **all four arms**; eight rows carry every ablation conclusion this project has published
or withdrawn. `[measured]`

**The corollary, binding on every later phase.** No configuration knob may be selected against this
set unless its effect clears ~14 recall points at n=40. RRF prefetch limits, leg selection,
expansion on/off and chapter-dedupe are all inside the noise. **Growth follows the coverage axis,
not the n axis** — and that rule was later demonstrated rather than argued (VP25). `[reasoned]`

**The MDE the gate reports is an observed-effect MDE**, not a pre-registered design MDE: `p_disc`
and the bootstrap SE are estimated from the run being judged. With zero discordant rows there is no
variance estimate and the gate reports `mde: null`, never `0.0` — which would read as infinite
sensitivity. `[read from source]` `src/rag/gate.py`

**One arithmetic correction made to this project's own audit (P7, 2026-09-01).** *"1.96 understates
every MDE by 43%"* → it is **30%** (`1 − 1.96/2.8`); 43% is the reciprocal. `[computed]`

---

<a id="VP3"></a>
## VP3 — The judge instrument: where the variance lives, and the two metrics that were cut

**Design question, settled by measurement rather than by rubric work.** Persona retention was
unusable as an instrument — near-zero A/B correlation, bimodal scores, MDE a quarter of the scale.
The two remaining explanations demanded opposite fixes (unstable judge → rubric work; unstable
generator → k draws averaged), so the attribution was measured before any row was authored.

**Judge test-retest, input held fixed** (2026-08-21, `scripts/probe_judge_variance.py`, 30 fixed
answers, k=3, `models/gemini-3.5-flash-lite`) `[measured]` · `[baseline: none — first datapoint]`:

| axis | within-var (judge alone) | exact agreement | at t=1.0 |
|---|---|---|---|
| persona_retention | 0.089 | 0.733 | noise_share 0.027 |
| groundedness | 0.067 | 0.800 | 0.208 |
| fabricated_events (count) | **0.500** | **0.500** | 0.542 |

**Generation variance, same cells** `[measured]`: persona within-cell **1.333** (gen+judge) →
**1.244** generator alone = **93%**; groundedness 0.303 → 0.236 (78%); fabrication 1.667 → 1.167
(70%). **The generator carries 93% of persona-retention variance**, so averaging k draws is the only
lever on that axis. `[computed]`

**k = 3, locked 2026-08-23.** Per-draw sd 1.155 ⇒ cell-mean SE `1.155/√k`; k=3 cuts per-cell noise
**42%** (fixed by `1 − 1/√3`), k=5 reaches only 55% for 67% more cost. A cell needs **≥ 2 judged
draws** or it is recorded failed and excluded — `min(2, draws)` in code, deliberately **not** a flag,
because a knob there would let a run redefine "failed cell" between baseline and candidate.
`[reasoned]` on `[measured]` inputs

**The k=3 sizing was then measured to be 1.8× optimistic** (2026-08-24, R29, over the frozen 50-row
set rather than one theme) `[measured]` · `[baseline: v2 CP0's own R25 figure]`:

| axis | within-cell (R25, 12 rows) | within-cell (R29, 50 rows) | between-cell | SE of cell mean at k=3 |
|---|---|---|---|---|
| persona_retention | ~~1.333~~ | **2.367** | 1.225 | **0.888** (R25 predicted 0.667) |
| groundedness | ~~0.303~~ | **0.636** | 1.023 | **0.460** |

R25's single-theme restriction inflated `noise_share` — which it flagged — **and deflated the
absolute within-cell variance**, which it did not. The published mode mean is unaffected
(±0.158, built from between-cell variance over 49 cells); what the revision bites is per-cell claims,
so `worst_cells` is indicative only, its top entries separated by less than one cell SE. **Persona
and groundedness have opposite variance structure**: persona within 2.367 > between 1.225 (mostly
the sampler); groundedness within 0.636 < between 1.023 (the questions genuinely separate).

**Two metrics cut, both on measurement.**

- **`fabricated_events` as a count — refuted** (2026-08-21, R27). Bare integer: count exact
  agreement **0.500**, boolean **0.800**. The candidate fix (enumerate, then `len()`) was **worse on
  every measure**: count 0.500 → **0.200**, within-var 0.500 → **1.478**, mean inflated 1.13 → 1.73.
  The stored draws show `q-0026#p3 = [3, 0, 4]` on one unchanged answer — the judge disagrees about
  whether anything was invented at all. Published metric is the **boolean**; the integer is demoted
  to `mean_fabricated_events_UNRELIABLE` rather than deleted. The boolean is **not** redundant with
  groundedness: corr −0.460, and 6 of 30 cells are low-groundedness but clean. `[measured]`
- **NDCG@k and graded relevance — cut before implementation** (R9). With binary single-target labels
  `NDCG@k = 1/log2(r+1)` and `MRR = 1/r` are both strictly decreasing in rank alone, so they induce
  identical orderings and NDCG carries zero additional information. `[reasoned]` — closed form.
- **The fabricated-quote checker — cut** (R3). 9 quoted spans ≥25 chars across v1's 72 answers,
  **0 exact corpus matches**. A metric that reads 0.00 by construction consumes maintenance and
  reports nothing. `[measured]` `[baseline: v1 recorded]`

**One rubric fact worth carrying:** editing one axis's instructions moved another axis's scores —
groundedness `between_var` fell 0.427 → 0.171 under the enumerated prompt with no change to its own
wording. The rubric is one artifact, not three. `[measured]`

---

<a id="VP4"></a>
## VP4 — The frozen v1 baseline, and the two harness defects that had to be fixed to produce it

**Two retrieval numbers were withdrawn before any v2 comparison could be made, and both were harness
defects rather than retrieval defects.**

**R18 — cross-process nondeterminism, a live serving defect** (2026-08-19) `[measured]`.
`recall@5` was bimodal across 23 process invocations on one commit — 0.720 (18/25) nine times,
0.760 (19/25) fourteen times, one row flipping. Root cause: `EntityMap._scan()` returned a
`set[str]`, so a two-entity query's alias blocks reordered under `PYTHONHASHSEED`, changing the
expanded query **text**, its embedding, and the dense ranking. Fixed to an occurrence-ordered list
(`src/rag/entities.py`); verification **12/12 identical** across separate processes, spread exactly
0. Consequences kept: it was a **serving-path** defect (two users, two workers, one question,
different chunks); **every v1 retrieval number was a draw from a distribution**; and the repeat-run
check must be **cross-process**, because one process fixes the hash seed. R17's earlier diagnosis
(Qdrant HNSW approximate search) is ~~superseded~~ — Qdrant local mode has no HNSW at all and its
`SearchParams(exact=True)` was a no-op, so there was nothing to pin. `[measured]`

**R19 — the harness computed a route and then ignored it** (2026-08-20) `[measured]`.
`scripts/eval_retrieval.py` called `route()`, asserted it against `expected_route`, logged
`route ok`, and then ran the DEFAULT hybrid path for every row.
`retrieve_structural_excerpts()` was imported by `src/api/app.py` and `scripts/eval_quality.py` and
by **nothing** in the retrieval harness. Structural queries are nearly pure metadata, so the DEFAULT
path missed all four while production finds them at rank 1.

| | recall@5 | MRR | n |
|---|---|---|---|
| before (broken harness) | 0.6512 | 0.4512 | PRIMARY 43 |
| after R19 | **0.7250** | **0.5287** | PRIMARY 40 |
| STRUCTURAL, after | 1.000 | 1.000 | 3 |

**The headline as it stands after P3's Q4 reclassification** `[measured]`, 2026-08-29:

| bucket | value | note |
|---|---|---|
| PRIMARY n=38 | **recall@5 0.7105 (27/38) / MRR 0.5039** | supersedes ~~0.7250 (29/40) / 0.5287~~ — `r-0024`/`r-0025` moved to `structural` **by derivation**, eval file untouched (A9) |
| STRUCTURAL n=12 | 1.000 / 1.000 | near-tautological by design: the router resolves the chapter, the hard filter finds it |
| NEG-CTRL n=2 | 0.500 / 0.125 | |
| QUOTATION n=15 | 0.8667 | see VP25 |

`[baseline: v1 recorded]` — v1 published ~~`0.731` (19/26)~~ then ~~`0.720` (18/25)~~ `[E23 (v1)]`.
The denominator is the tell: **26 → 25 → 38**. `0.720` was never the retriever's recall on those
rows; it was the recall of a pipeline production does not run.

**The frozen `local-q4km` quality row** (2026-08-24, `scripts/eval_quality.py --draws 3 --modes A`,
50-row set v2.0, `EMPTY_RETRIES=4`, `-c 3072`) `[measured]` · `[baseline: none — this IS the
baseline]`:

> persona_retention **3.626 ± 0.158**, groundedness **2.813 ± 0.144**, fabrication rate 0.616,
> **49/50 cells scored**, 1 failed, 0 judge errors. Per-theme groundedness: Ambiguity 3.211,
> Textual 3.467, Philosophy 2.667, Motivation 2.310.

A3 permits exactly one re-measure of this row and this run spent it. **Two rows were in
circulation** and the tie could not be broken by a third observation, so it broke on provenance
quality (R46): the v1 row ~~persona 3.11/3.22, groundedness 2.44/2.53~~ carries a point value and
nothing else — no method, no interval, no date — and is **superseded, not deleted**. The 49/50
denominator travels with the number. `[reasoned]` on `[measured]` inputs

**The frozen row's latency half** (2026-08-24, R30, n=7, `-c 3072`, TTFT/total read from the `meta`
SSE event, retrieval via `scripts/measure_latency.py --retrieval-only`) `[measured]`:

| metric | median | `[baseline: v1 recorded]` `[E28 (v1)]` |
|---|---|---|
| TTFT, DEFAULT | **7.790 s** | ~7.9 s — **the 4096 → 3072 context drop moved nothing**, as predicted: prefill is ~1900 tokens either way |
| TTFT, STRUCTURAL | **4.260 s** | not isolated in v1 — **45% faster**, first isolation |
| total, all | 17.380 s | ~21.3 s — **not a speedup**; total is decode-length dominated at ~23 tok/s |
| retrieval-only | 2.284 s | ~2.25 s — a standing miss against the ≤0.6 s target, 3.8× over |
| cache hit | **0.002 s** | ~0.008 s |

`q-0029` was excluded and the exclusion is the honest move: its first request exhausted all four
`EMPTY_RETRIES`, so `app.py`'s `if answer:` guard correctly declined to cache it and the second
request was a second miss (20.4 s), not a hit. **An empty completion also defeats the cache** — a
query that fails repeatably pays full generation cost on every request forever. `[measured]`

**The frozen row's reproducibility is weaker than the charter claimed** (R54) `[measured]`.
`quality_step13_A_k3.json` and its `_summary.json` carry **no `config_version`, no git SHA, no
timestamp, no persona text**. The row is reproducible **from git history, not from anything the
artifact records**, and A3 forbids re-measuring it, so this cannot be repaired retroactively — only
prevented for the next frozen row (VP9).

---

<a id="VP5"></a>
## VP5 — CP0's instrument defects: four numbers that were wrong before they were published

Each was caught by a check whose only job was to check, and each would have entered a published
number.

| # | The instrument said | The truth | Caught by | Provenance |
|---|---|---|---|---|
| R24 | 72 quality cells judged; means reported | **10 of 72 cells were empty answers scored 1/1.** `_collect_answer()` returned `("", None)` on a zero-token stream and `judge_answer()` substituted `answer or "(empty response)"`, making the empty string judgeable. `generation_errors: 0` was reported. | a `--dry-run` whose only job was to print a call budget, and whose cell count disagreed with the transcript's own summary by seven | `[measured]` |
| R26 | rate limiter respected 15 RPM | **18/15 RPM observed with nothing concurrent.** `RateLimiter`'s sliding window is per-process; three sequential invocations meant three fresh windows, and the boundary can approach 2× the configured rate. A 20 s sleep is far short of the 60 s needed to drain. | the AI Studio console | `[measured]` + `[reasoned]` mechanism |
| R28 | `EMPTY_RETRIES=1` was sufficient | **19.4% → 16.7% → 8.3%** empty-completion rate at guards none / 1 / 4. One was badly under-set. The residual is **per-query and permanent** (`q-0036` failed 2 of 3 draws in two independent runs at different settings) and **is not prompt size** — failing prompts are *smaller* than passing ones. | running the arm at a different setting | `[measured]` |
| R30/R33 | `ttft_ms=2503`, `total_ms=2503`, answer `''` | **A TTFT published for a stream in which no token ever arrived.** `t_first_token` was set in the `StopAsyncIteration` branch too, so it recorded the moment the server gave up. All `EMPTY_RETRIES+1` attempts are consumed inside one `__anext__()`. | offline reproduction against stub generators | `[measured]` |

**R24's numeric correction, restricted to the 26 pairs where both arms produced non-empty answers**
`[computed]` · `[baseline: v1 recorded]`:

| axis | published (n=36) | corrected (n=26) |
|---|---|---|
| persona delta (B−A) | ~~+0.111~~ | **−0.231** — the sign flips |
| groundedness corr(A,B) | +0.625 | **+0.852** |
| groundedness sd_diff | 1.180 | **0.720** |
| groundedness MDE | ~~0.551~~ | **0.395** |

**v1's prompt-composition A/B result is withdrawn.** Composition B was selected on +0.111 persona
and +0.083 groundedness against MDEs of 1.147 and 0.551. Both deltas are noise; the defect-free
persona number points the other way. The shipped composition is **unjustified**, which is a
different claim from wrong. `[computed]` · `[baseline: v1 recorded]` `[E24 (v1)]`

**A disagreement this log does not resolve.** The groundedness MDE at n=36 is published three ways:
**0.551** (R23, closed form, on the defective 36 cells), **0.395** (R24, closed form, on the clean
26), and **0.5376** (R76, paired bootstrap, on the same 36 cells R23 used). R76 states the 1-2% gap
between 0.551 and 0.5376 is *bootstrap versus normal approximation, not a disagreement*. The gap
between those and 0.395 **is** a disagreement, and it is a denominator change, not a method change.
P7 does not adjudicate; STATUS records "stale MDE 0.551 mapped across documentation" as an open
tidy-up. **Both readings are recorded here and the disagreement is marked.**

**R33's second half is refuted, not fixed.** `ttft_ms=2387 > total_ms=2036` is **not reachable** from
this code — both values derive from one `t_start` in one closure and `total_ms` is read strictly
later. No raw log from the R30 run survives, so the most likely explanation (two log records from
two back-to-back requests) cannot be confirmed. Recorded as **not reproducible, cause
unestablished**. The client/server divergence (20.4 s against a server-reported 2.0 s) is likewise
unexplained and stays open. `[reasoned]`

---

<a id="VP6"></a>
## VP6 — The domain gate does not separate, and the score the charter named carries no signal

**R22**, 2026-08-20, `scripts/probe_gate_scores.py --json probe.json`. Closed R15, the charter's last
open question, and corrected charter A10 in place. Denominators: `adversarial` n=25, `sensitive`
n=19, `primary` n=40 as the must-pass control. `[measured]` · `[baseline: none — first datapoint]`

**Finding 1 — the fused score is not a signal at all.** `retrieve()` returns Qdrant native RRF, and
RRF is `sum(1/(k + rank))` — computed from **ranks**, not similarity. Every query's top-1 fused score
is drawn from the same small set of rank-arithmetic values whether or not anything relevant was
found; all three families sit on a 0.5000 floor. **A10 named the one score that provably cannot
gate.**

**Finding 2 — the legs carry signal and the distributions overlap.**

```
dense_top1   adversarial  min 0.5563  p25 0.6312  med 0.6751  p75 0.7310  max 0.7995
             sensitive    min 0.6579  p25 0.7372  med 0.7712  p75 0.7846  max 0.8033
             primary      min 0.6233  p25 0.7241  med 0.7729  p75 0.7994  max 0.8304

best dense threshold 0.6799 -> catches 56% adversarial, refuses  5% sensitive,  8% primary
best bm25  threshold 13.16  -> catches 36% adversarial, refuses  0% sensitive,  8% primary
```

Neither is viable alone: 44-64% of adversarial traffic passes.

**Finding 3 — the failure is structural, not a tuning problem.** The authored twin pair sits
**0.0038 apart on dense**, and BM25 scores the *adversarial* row **lower**:

```
r-0065 adversarial  dense 0.7995  bm25 25.83
r-0077 sensitive    dense 0.8033  bm25 32.36
```

Retrieval scores measure topical similarity; the difference between these two rows is **intent**. No
threshold on a similarity separates them at any n with any embedding model.

**Finding 4 — score rises with how in-domain the attack is dressed.** Median `dense_top1` by class:
out-of-domain 0.6663 · prompt-extraction 0.6722 · persona-break 0.6939 · jailbreak 0.7057 ·
injection-embedded **0.7361**. `"What's the weather in Saint Petersburg today?"` scores 0.7077 on a
corpus place name alone. A topic-similarity gate is blind by construction to every attack wearing
corpus vocabulary.

**Finding 5 — the false refusals land on the worst rows.** Below the 0.6799 cut sit two minor-entity
rows and one STRUCTURAL row. Metadata-shaped queries have low dense similarity by nature, so a dense
gate refuses precisely the class VP4's R19 repaired. **Any deployed threshold must be route-aware**:
a query the router resolved to a chapter has already proven itself in-domain.

**Decision.** Layer 1 is retained **as an out-of-domain filter only** — the floor of the distribution
is genuinely non-corpus traffic (`What's 47 times 13?` 0.5563; `Write me a Python function…`
0.6132). It is **not** a refusal mechanism for injection, persona-break or jailbreak. **This is
R15's permitted outcome, on the record before it was measured.**

The guard's output-side layers, the injection set, the fence and composition C are the security
log's — see `EVIDENCE-MEMORY.md`. Two numbers belong here because they are CP2 acceptance figures
for the mechanism this log owns: `scripts/eval_guard.py` measured **false refusal 0/54 = 0.0%**
across `sensitive + primary + structural + book_named` `[measured]`, and the **correct-refusal half
is unmeasured** — scanning queries gives 0/25, which is the expected and correct result for
output-side layers, and the real number needs a generation pass over the 25 adversarial rows.
`[measured]` · `[baseline: none — first datapoint]`

---

<a id="VP7"></a>
## VP7 — Per-stage attribution: the first time this project knew where the time went

**CP1 step 2-3.** One `start_as_current_span` per stage boundary feeds the trace *and* the metric,
because OTel emits both signals from one set of instrumentation calls — which is what settled C1
against log-derived telemetry, since step 7 mandated a metrics endpoint under **every** option and
log-derived would have instrumented the same seven boundaries twice. `[reasoned]`

**Measured, DEFAULT route, 227 tokens / 921 chars** (2026-08-24, R37, live generation, console +
Jaeger export) `[measured]` · `[baseline: none — first attribution; v1 estimated but never
instrumented]`:

| span | ms | attributes |
|---|---|---|
| POST /chat (root) | 17802.8 | |
| cache.check | 0.6 | `cache.hit=False` |
| route | 0.1 | `route.name=DEFAULT` |
| entities.resolve | 0.5 | `entities.resolved_count=1` |
| retrieve | 373.4 | `points=25` |
| **rerank** | **1996.6** | `25 -> 3` |
| context.build | 0.1 | `sources=2` |
| generate.first_token | 5301.0 | `generate.empty=False` |
| ↳ POST (httpx child) | 5294.3 | |
| generate.stream | 10123.5 | `tokens=227, answer_chars=921` |

**Rerank is the retrieval budget, entirely.** Measured three times on three occasions:
**~84%** of the retrieval path (R34, n=4), **84.2%** — 2238.5 ms of 2659.1 ms (R38), and reproduced
on the export path at 2416.1 ms against retrieve's 520.8 ms (R35). On its own it is **~4× the whole
0.6 s retrieval target**. v1 estimated *"FlashRank ~1.96s of ~2.25s"* without instrumentation
`[E28 (v1)]`; it is now attributed rather than estimated. `[measured]` · `[baseline: v1 recorded]`

**A superseded design justification, corrected by P7's audit re-read of R34.** v1's `docs/RAG.md`
§III.2 justified the reranker at ~~"~0.3-0.5 s"~~ — **wrong by ~5×** against the measured 2.24 s.
The consequence is a lever, not a footnote: **`top_k_fused`, not `top_k_rerank`, is the latency
lever**, because the 25→3 narrowing is a slice *after* scoring and costs nothing. `[computed]`
· `[baseline: v1 recorded]`

**The 45% STRUCTURAL TTFT gap decomposes** (R37) `[measured]` · `[baseline: v2 CP0's R30 row]`:

| | DEFAULT | STRUCTURAL | delta |
|---|---|---|---|
| retrieve | 373.4 | 132.3 | −241 |
| rerank | 1996.6 | **absent** | −1997 |
| generate.first_token | 5301.0 | 4123.8 | −1177 |
| **implied TTFT** | **7671** | **4256** | **−3415** |

R30 measured 7.790 s / 4.260 s on a different day with a different instrument; **the spans reproduce
it to within 4 ms on the STRUCTURAL side.** The gap is now split rather than asserted: ~2238 ms is
the retrieval path (no BM25 leg, no rerank) and ~1177 ms is prefill (2 excerpts against 3 reranked
chunks). Reproduced a third time at 43.7% (R38) and a fourth at 3.308 s separation against R30's
3.530 s (R45). **Any later latency or cost comparison holds the route mix fixed, or it measures the
mix rather than the arm.** `[measured]`

**The span decomposition and the hand-rolled timer agree to 2 ms** — stage spans summed to ~7630 ms
against a reported `ttft_ms=7628` — which is what makes the spans trustworthy enough to replace the
timer. `[measured]`

**Cost hangs on the same spans, and `metered` is the finding, not the zero** (R38). Local inference
costs $0.00 in fees and that figure is exact, but nobody meters it, so it is **not a price**.
`Rate(0.0, 0.0, metered=False)` is a different claim from `metered=True, usd=0.0`. `total_usd()`
propagates `metered=False` through any sum containing one. **An unknown model is UNPRICED, never
zero** — `rate_for()` returns `None` and warns, and an unpriced stage makes its whole aggregate
unpublishable; it does not raise, because a missing price row must not turn a chat into a 500. This
is the deliberate inverse of `Config.limits_for()`, which guesses the *stricter* quota tier.
`[read from source]` `src/pricing.py`

**One instrumentation defect, and it would have destroyed the deliverable** (R37) `[measured]`. The
first real-generation trace carried **165 spans, of which ~155 were zero-duration
`POST /chat http send`** — one per ASGI send message, and an SSE response sends one per token
(~44 ms apart, the ~23 tok/s decode rate). Two consequences: the eight meaningful spans were buried,
destroying the paste-an-id workflow this phase existed to deliver; and step 10's storage number would
have scaled with **answer length**, not pipeline complexity — ~20× inflation. Fixed with
`exclude_spans=["send","receive"]`: **165 → 10 spans** for a 227-token DEFAULT answer, 9 for a
STRUCTURAL one.

---

<a id="VP8"></a>
## VP8 — Instrumentation overhead, storage, and the histogram that could not see its two most expensive stages

**Overhead is below the detectable floor** (2026-08-27, R45, two cold arms, n=12 each, llama-server
restarted between them, `CACHE_ENABLED=false`) `[measured]` · `[baseline: v2 CP1, same run,
`OTEL_ENABLED=false`]`:

| metric | delta (instrumented − uninstrumented) | MDE at 80% power |
|---|---|---|
| TTFT, DEFAULT, n=8/8 | **−66 ms**, 95% CI [−188, +57] | ~175 ms (2.25% of TTFT) |
| TTFT, STRUCTURAL, n=4/4 | −20 ms | ~135 ms |
| total wall-clock, n=12/12 | −3.04 s | ~4.2 s (22% of mean) |

**The point estimates are negative, which instrumentation cannot cause.** That is the tell that the
whole quantity is noise, and the correct report is *below the detectable floor at this n* — never
"zero", never a negative overhead. **Total wall-clock is the wrong instrument for this**: it carries
answer-length variance (12-24 s spread on identical settings), which dwarfs a millisecond-scale
effect; TTFT excludes decode and has sd ~50 ms against a 7.7 s mean, which is what makes a 175 ms
MDE achievable at n=8. `[computed]`

**A state channel that invalidates any sequential A/B.** Re-running the same query set without
restarting llama-server dropped STRUCTURAL TTFT **4325 ms → 327 ms** — a 13× improvement from nothing
but a warm prompt prefix — and the mean total 17.4 s → 12.3 s. **Every latency A/B must restart
llama-server between arms.** Third instance of one pattern (R18's `PYTHONHASHSEED`, this, and R51's
answer cache); the standing gate was amended to its general form: *before any A/B, enumerate every
store that outlives a single arm.* `[measured]`

**The histogram could not measure its two most expensive stages.** The first pass reported
`generate.first_token` and `generate.stream` at exactly **5.0000 s for p50, p95 *and* p99, on both
routes** — that is not a latency, it is `STAGE_DURATION_BUCKETS`' top edge. TTFT alone is 7.7 s, so
the two stages dominating the pipeline were the two the instrumentation could not see. One histogram
covers eight stages spanning five orders of magnitude (`route` sub-millisecond to `generate.stream`
~19 s); buckets extended to 30 s. **The symptom of failure is an identical quantile at every
percentile**, which reads as a suspiciously stable measurement rather than an error. `[measured]`

**`/metrics` was tracing itself.** Every 15 s Prometheus scrape produced a trace: **240/hour** of
pure noise against Jaeger's in-memory 50k ring buffer, evicting the traces the buffer exists to hold.
`excluded_urls` covered `health` from step 2 and was never extended when step 7 added an endpoint of
exactly the same character. Now `"health,metrics"`; verified after at 8 chat traces and zero
`/metrics` traces with Prometheus scraping throughout. **Adding an endpoint means revisiting the
exclusion list, and nothing prompts you to.** `[measured]`

**Storage, stated with its caveat.** **~110 KiB of Jaeger RSS per traced request** over n=8 ⇒
**~107 MiB per 1k requests** — measuring **RAM, not disk**, because Jaeger runs in-memory by choice
(which keeps G8 deferred; traces are lost on restart). Upper bound: Go does not return freed memory
promptly, so an RSS delta overstates per-trace cost. `MEMORY_MAX_TRACES=50000` at this figure implies
a multi-GB steady state if the buffer ever fills — it will not at this deployment's traffic, but the
number is not self-evidently safe. `[measured]` + `[computed]` · `[baseline: none — first datapoint]`

**The debug workflow, verified end to end** (R35, R45). Take an id off a response header or a log
line, paste it, get the full stage tree, with **no conversion step** — because `request_id` **is**
the trace id in hex. Direct trace-id lookup returns the full tree in **median 4 ms**. `[measured]`

**A correction made and then unmade, worth the line.** Searching Jaeger by `request_id` as a *tag*
returned zero spans; that was recorded as a broken debug workflow and a duplicate root-span attribute
was added to fix it. Wrong — `test/test_span_tree.py:263` already documented that `request_id` is the
trace id in hex. The join was never broken; the lookup was. The attribute was reverted. **A query
returning nothing is evidence about the query at least as much as about the system.** `[measured]`

---

<a id="VP9"></a>
## VP9 — Config identity: what a version pins, and the three things it did not

**The mechanism** (locked 2026-08-26, R43). A frozen pydantic model, content-hashed:
`version = sha256(canonical_json)[:12]`. Two identical configs are the same version by construction,
and a P7 candidate gets a version free with no counter to coordinate. Hashing is over **normalized,
parsed values, never input text** — validate into the typed model first so `"3"` and `3` hash
identically; serialize `sort_keys=True, ensure_ascii=True, separators=(",",":")`, floats through
`repr()`. An unstable hash means version churn on every restart, which poisons the telemetry
dimension silently, so it gets a test. `[read from source]` `src/config_artifact.py`

**Call-site migration is a facade, not parameter passing.** A metaclass `__getattr__` on `Config`
resolves ~18 versioned knobs through the active artifact, so **~200 call sites across 36 modules**
keep working unmodified. Accepted costs written at the metaclass: type checkers cannot see through
`__getattr__`, and it is action-at-a-distance. `[read from source]`

**The three-way field split — versioned / deployment / secret.** *Versioned* if changing it changes
what a correct answer **is**; *deployment* if it changes where the system runs, how fast, or how much
it costs, but not what the answer says; *secret* if it authenticates. **~18 versioned, ~38
deployment, 3 secret.** Ambiguous cases called explicitly: `CACHE_ENABLED` and `EMPTY_RETRIES`
deployment; `MODEL_LIMITS` and `MODEL_RATES` **versioned** (a limit or price change should be a
visible config version); `SUMMARY_INTENT` versioned and **moved out of `src/rag/router.py`**, since a
knob P7 will search cannot live as a module constant. `[reasoned]`

**Fail-closed, four ways.** Refuse to serve on a bad pointer or an invalid artifact — a fallback
silently serves an unversioned config while still stamping *something* on every span, which is worse
than not starting. `load()` additionally refuses an artifact whose contents no longer hash to its
filename: content-addressing that is never checked is decoration, and a hand-edited version file
stamps a version name on every span for a config nobody ran. Secrets cannot enter the artifact,
**asserted at build time** — the artifact is hashed, written to disk, diffed, and stamped onto spans
that ship to Jaeger. `[read from source]`

**What the versioned artifact did NOT pin, in order of discovery:**

| # | Missing field | Consequence, measured | Closed by |
|---|---|---|---|
| 1 | **the generation model** (R51) | `config_version` did not churn between arms, so **arm B was served arm A's cached answers**. Contaminates quality (arm B's cells include arm A's generations), latency (a hit returns in ~0 ms), and cost — in that order of severity; only the third was on the decision table. | a `serving_model` field on `ServingConfig`; CP1's existing invalidate-on-config-change (R42) then drops the cache with **no new cache code** |
| 2 | **the persona text** (R54, G-S3) | `ServingConfig` carried `prompt_composition` and **no persona field**, so the mechanism built to invalidate on config change was blind to the single largest determinant of an answer's style. The fine-tune sync was enforced **by a comment** (`generate.py:36-38`); a repo-wide grep found **no test importing both** constants — byte-identical at 208 chars, and an edit to one was a silent green-suite change. | C5 (R61): ownership **moved**. `ServingConfig.persona_head: str`, exposed as `active_persona_head()` — a **function**, not a constant, because a constant snapshots at import and defeats the versioning. `src/training.py`'s `SYSTEM_PROMPT` deliberately stays separate: it records what the model was *trained* on |
| 3 | **which model built the index** (R43) | `data/vector_db/meta.json` records `size: 768, distance: Cosine, metadata: null`. `ensure_collection` hardcodes `size=768`, and installed fastembed has **15 models at dim 768**. So an embedder swap is loud **only** when the dimension differs — and a dimension mismatch fails at *ingest*, not at query, so **the dangerous direction (ingest with A, serve with B) is exactly the silent one.** | a corpus **manifest**, not a bare hash, plus `assert_embed_model_matches()` at serving startup and in the gate, with a negative test |

**A second silent path the plan did not have** (R43): `_QUERY_PREFIX = "search_query: "` and
`_DOC_PREFIX` are **nomic-specific and unconditional**. Under any other embedder they stop being
instructions and become literal noise on every query and every document — and unlike the vector
mismatch, **that survives a clean re-ingest**, since both sides stay consistently wrong. Covered
today only because model identity implies the prefix pair. `[read from source]`

**The manifest is adopted, not ingested, and the artifact says so.** `bootstrap_config.py
--adopt-corpus` **reads** `qdrant_collection`, `dense_dim` (768) and `point_count` (3284) from
Qdrant and the five-book set from `structure.json`; it **asserts** `dense_model`, `chunk_tokens` and
`chunk_overlap` from today's `Config`. The index records none of the asserted three — **and that
absence is the bug the manifest exists to close.** So an adopted manifest catches every future swap
and **cannot detect a mismatch already present at adoption time**. Hence
`CorpusManifest.provenance`, defaulting to `"ingested"`. **The general rule: when a check must
bootstrap from unverifiable state, record which fields were read and which were asserted, in the
artifact itself.** `[measured]` + `[reasoned]`

**No drift at bootstrap.** `451327aab528` was asserted field-by-field (**18/18**) against `Config`
before the facade landed, then checked against the values `docs/RAG.md:292-293` records as measured:
`PREFETCH_LIMIT=50`, `TOP_K_FUSED=25`, `TOP_K_RERANK=3`, `RERANK_ENABLED=true`, `CTX_SIZE=3072`,
`MAX_TOKENS=400`. All match. `[measured]` · `[baseline: v1 recorded]`

**An env var cannot switch arms — and this was caught in production use** (R64, VP19).

**Provenance stamping, so later runs do not inherit the frozen row's defect** (R61).
`scripts/eval_quality.py` writes `config_version`, `corpus_version`, git SHA, **`git_dirty`**, UTC
timestamp, `serving_model`, `prompt_composition`, `top_k_rerank`. `git_dirty` matters more than it
looks: **a measurement taken on an uncommitted tree is not reproducible from its SHA, and recording
the SHA anyway is how that becomes invisible.** `[read from source]`

**The startup wiring was verified by drill, not by test** (R43). `TestClient(app)` completes lifespan
cleanly against pointer `451327aab528`; an artifact identical but for
`embed_model="BAAI/bge-base-en-v1.5"` **refuses to boot**, naming both values, **before** loading the
embedder or FlashRank — so the gate fails in under a second rather than after ninety. Deliberately
not made a permanent test: booting lifespan loads ONNX, FlashRank and Qdrant, which does not belong
in an offline suite that runs in seconds. **The wiring is verified, not regression-guarded.**
`[measured]`

**Config versions by phase, each churn a deliberate measurement boundary** `[read from source]`:

```
CP1  451327aab528  (bootstrap; == v1 baseline, 18/18 asserted)
CP2  -> 0c57c9877544 (generation-model field)  -> cdad13a756dc (rate rows)
     -> 4c9c75e14ba7 (persona onto the artifact; default arm switched)
     -> 93e354c4067f (Groq arm)                -> f2a6708c8f54 (MAX_TOKENS 400 -> 1024)
CP3  f2a6708c8f54 (gate calibration, R77)
CP6+ 2d1b59e676de -> fb5cb5a842e7 (G-P18 raise)
CP7  -> 8e97e38de285 (Batch G: max_recent_turns versioned at 4, n_prev_queries at 0)
     -> 26f394009884 (Batch H: align_only, radius 0, the two span guards, top_k_fused 25 -> 10)
     -> b6063fd8be31 (composition C promoted)
```

*The CP7 lineage was previously written as one step from `fb5cb5a842e7` to `b6063fd8be31`. It is
four pointers, and the two intermediate ones carry serving values that are live today; see
[VO1](EVIDENCE-OPTIMIZER.md#VO1). `[read from source: config/serving/CURRENT and git history,
2026-09-08]`*

**Amended 2026-08-31 (G-P18):** `Config.CTX_SIZE` is **6144** on the hosted serving path, and the
memory budget rose 306 → 3072. `CLAUDE.md`'s local `-c 3072` rule is **unchanged** and now scoped to
the local arm only — the two numbers had been one value for two different reasons (a 6GB GPU fit and
a self-imposed input cap at `src/rag/generate.py:451`), and A3 froze the local arm out of the request
path. **P6's serving rows are therefore a new arm under R51**: cross-phase latency and cost
comparison with CP4/CP5 is *indicative only*; within-phase deltas are exact.
`[read from source]` `src/config_artifact.py:519`

---

<a id="VP10"></a>
## VP10 — The operator plane, and two metrics that could not express the condition they existed for

**Built** (CP1 step 7, 2026-08-27, R44): Prometheus + Grafana in Compose, metrics from the same OTel
instrumentation as the spans, dashboard as a file. RED-plus-spend: p95 TTFT, p99 total, error rate by
provider, cost per answer, quota rejections, cache hit rate.

**Defect 1 — the metric that could not show the outage it exists for.** `[measured]` The live run's
first request was a 503 from an unreachable llama-server. It recorded **all six pre-generation stage
durations and no request at all**: the 503 path raises `HTTPException` before `event_stream` ever
runs, so `record_request_duration` was never reached. Both RED panels are driven by
`mitya_request_duration_seconds_count`, so **during a total generation outage the error-ratio panel
read 0.000.** Not a wrong number — a metric structurally incapable of expressing the condition it was
added to expose. Four pre-generation exits shared the shape (400, a bare 500, 503, 504), each calling
`state.sem.release()` directly. Fixed in **one** place, not four: a local `_abort()` that releases
*and* records the request as an error, with all four exits routed through it. Confirmed after:
`outcome="error"` fires under both `route="DEFAULT"` and `route="STRUCTURAL"`.

> **The pattern: a counter incremented only on the success path is not a counter, and the shape that
> produces it is an early `raise` that skips the recording line. Grep the exits, not the happy path.**

**Defect 2 — two ratio panels that could not show zero.** `[measured]` With zero cache hits,
`rate(mitya_cache_lookups_total{outcome="hit"}[5m])` does **not** evaluate to 0 — the series does not
exist, the division returns no result, and Grafana renders "No data". `clamp_min` on the denominator
is the guard everyone writes and it does nothing here: **the missing term was the numerator.** The
error-ratio panel had the identical defect, hidden during the run only because errors happened to
exist. Both now read `(sum(...) or sum(...) * 0) / clamp_min(sum(...), 1e-9)`. **Neither defect is
reachable offline** — both need a real scrape producing a real *absent* label set, and a unit test
that records one hit and one miss has by construction made both series exist.

**Cardinality: a validator is not a cardinality guard.** `[reasoned]` R41 recorded that tenant
shape-validation "is also the cardinality guard". **True for logs, false for metrics.**
`_TENANT_RE` is `^[a-z0-9][a-z0-9-]{0,31}\Z`: it stops a newline forging a log line, and it admits on
the order of 37³² distinct values. The header was **unauthenticated until CP5**, so a caller sending
distinct valid values mints one Prometheus series per value per metric, unboundedly — a
memory-exhaustion path into the operator plane through the front door, reached by **well-formed**
input. At CP1, `tenant` therefore stays on spans and logs and appears in **no** metric label.

> **A validator bounds the SHAPE of a value; a metric label needs a bounded SET of values. Only the
> second bounds cardinality.**

**Spend is three-way, not two-way** `[read from source]` — and the third case got its own instrument,
`mitya_unpriced_answers_total`:

| case | meaning | enters `mitya_answer_cost_usd`? |
|---|---|---|
| `metered=False` | nothing bills us (local inference, free tier). **Not a cost of 0.** | no |
| metered, `usd=None` | something billed us and the rate row is missing | no |
| metered and priced | | **yes** |

Folding the middle into a `0.0` is indistinguishable from a real metered-but-free call, which is
exactly the false claim G5 forbids. All three make an aggregate provisional; only one is a cost.

**Verified live, closing R43's and R44's open gaps** (R45) `[measured]`: on a real root span,
`config_version = 451327aab528`, `corpus_version = 515c707a5d69`, `route`, `tenant`, in a 9-11 span
tree; `outcome="ok"` fires; `unmetered=12`, `unpriced=0`, cost-histogram samples **0** — the
three-way split behaving exactly as specified, with `local-q4km` recorded as **unmetered** rather
than as a cost of zero.

**A test fragility caught in draft, worth the line.** The tenant non-vacuity test first proved its
predicate could fail by emitting a real tenant-labelled series. `prometheus_client`'s registry is
process-global and a series cannot be un-added, so that left the absence test passing only by
alphabetical luck in the runner's ordering. **The predicate was what was under test, so the predicate
gets the fabricated input** — not the registry getting permanent damage. `[measured]`

---

<a id="VP11"></a>
## VP11 — Infrastructure security: four drills, three of which found something

Every one of these is a component this project added to Compose or to the request path. None was
found by reading a passing test suite.

**R35 — the Jaeger port was LAN-exposed and unauthenticated.** `[measured]` The Compose file as first
written published both ports with a bare `HOST:CONTAINER` mapping, which binds **0.0.0.0**; confirmed
by `ss`: `LISTEN 0 4096 0.0.0.0:16686`. `127.0.0.1` in a browser URL describes where the *client*
connected from and says nothing about what the listener accepts. Two things made it worse than a
normal misconfiguration: **Jaeger all-in-one has no authentication of any kind** and its UI renders
full request contents — queries, retrieved passages, prompt text; and **published container ports
bypass firewalld**, because Docker's DNAT rules live in its own iptables chain evaluated before
firewalld's zones. This host's physical interfaces sit in firewalld's **`drop`** zone, so the
firewall looked closed while the port was reachable from the LAN.

> **Rule, first earned: a Compose port mapping with no host prefix is a LAN publish. Write the
> interface explicitly or do not publish.**

**R36 — the fix for R35 re-opened a wider hole.** `[measured]` `network_mode: host` was adopted to
sidestep a broken bridge path. Jaeger all-in-one opens **nine** listeners; R35 bound the three the
project uses and left six at their defaults, which bind `0.0.0.0`. Under a published-port setup that
was harmless; **under `network_mode: host` there is no mapping layer, so every default-bound socket
lands on every host interface.**

| port | what | bound, before the second fix |
|---|---|---|
| 16686 / 4318 / 4317 | UI, OTLP HTTP, OTLP gRPC | loopback (R35) |
| **16685** | **query gRPC — READS TRACES** | **all interfaces** |
| 14250 / 14268 / 14269 / 5778 | collector gRPC, collector HTTP, admin, agent config | all interfaces |

**16685 is the one that matters** — the query API behind the UI, same data, same absence of
authentication. Pinning the UI while leaving the query gRPC open pins the front door and leaves the
window. All nine now bound to `127.0.0.1` by explicit flag; verified after that no Jaeger port
appears on any non-loopback address.

> **Rule, second earned: `network_mode: host` publishes EVERY default-bound listener the image opens,
> not only the ones you thought about. Enumerate with `ss -ltn` after starting any new service.**

**Method note, so this is not over-read:** connecting from the host to its own LAN address proves the
socket *accepts on that interface*. It does not prove a remote machine could reach it — the physical
interfaces sit in the `drop` zone, which probably blocked it. **That is the point:** the binding was
one firewall rule away from exposure, and firewalld state is exactly what a Docker or VPN change can
alter without notice. Bind narrowly *and* firewall, in that order.

**The drill applied to two new images** (R44, Prometheus + Grafana) `[measured]`. Both pinned to a
registry-confirmed tag (`prom/prometheus:v3.14.0`, `grafana/grafana:13.2.0`), **no `ports:` lines at
all**, every listener pinned to loopback by flag or env var. `docker image inspect` declares one port
each — a declaration, not proof, which was R36's defect — so the drill ran anyway and found only
pre-existing `systemd-resolved` LLMNR on 5355. Surfaces probed rather than inferred:

| probe | result |
|---|---|
| Grafana `GET /` anonymous | 302 to login |
| Grafana `GET /api/datasources` no auth | 401 |
| Prometheus `POST /-/reload` | 403 (lifecycle absent) |
| Prometheus `POST /api/v1/admin/tsdb/delete_series` | **500** `"admin APIs disabled"` |
| Prometheus `GET /api/v1/query` | 200 — open by design, hence loopback-only |

The `delete_series` result is worth keeping: **500, not 404.** The route is registered and refuses at
request time; a 404-shaped expectation reads that as "endpoint missing" and a 500 as "something
broke", and only the body distinguishes them. **Probe the body, not the status.**
`GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?...}` makes Compose refuse to start rather
than fall back to `admin`/`admin` — verified by `docker compose config` exiting 1 with the variable
unset.

**R41 — a log-injection hole from `$` instead of `\Z`.** `[measured]` The tenant validator shipped as
`re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")`. **In Python, `$` matches at end-of-string OR immediately
before a trailing newline.** So `b"acme\n"` passed the shape check and was returned verbatim into
`LOG_FORMAT`'s `[%(tenant)s]` — from an unauthenticated header, **one forged log line per request**:

```
b'acme'     -> 'acme'
b'acme\n'   -> 'acme\n'      <-- accepted, control character intact
b'a\nb'     -> 'default'     <-- interior newline correctly rejected
```

**Why the tests missed it, which is the transferable part.** The hostile-input set *did* cover
newline and carriage return — as **interior** characters. Those reject correctly, because `\n` is not
in the character class. Only the **trailing** position is special-cased by `$`, and that was the one
case absent. **The test's blind spot mirrored the regex's exactly:** both treated "contains a
newline" as the property, when the property that mattered was "is anchored at the true end of the
string". Fixed with `\Z`. **Reject, never sanitize** — stripping the newline would invent a tenant id
nobody sent; rejection falls back to the default and logs at WARNING via `%r`, so control characters
render as escapes rather than being reproduced into the line that reports them.

**R42 — OpenTelemetry's own URL redaction is AWS-shaped.** `[measured]`

```
>>> opentelemetry.util.http.PARAMS_TO_REDACT
['AWSAccessKeyId', 'Signature', 'sig', 'X-Goog-Signature']
```

No generic `key` or `api_key`. `HTTPXClientInstrumentor` calls `redact_url()` before setting
`http.url`, so this was dormant **only** because `google-genai` sends its key as the
`x-goog-api-key` header. It stops being dormant the moment a key-in-URL provider is wired — and D1
requires ≥2 provider families. Closed in `configure_tracing()` by extending the list, which
`redact_query_parameters()` reads from the module global at call time, so the extension covers every
instrumentation and is additive. **Drilled**: with the extension removed the span carries
`http.url=…?key=SENTINEL-SECRET-…`; with it, redacted. The regression test additionally asserts the
param list it exercises still matches `src/observability.py`'s, because drift there would mean
testing a redaction the app does not apply. **A redaction test that has never been observed to fail
is not evidence.**

**SELinux denied a world-readable file, and it had nothing to do with ports** (R44) `[measured]`.
Prometheus failed with `permission denied` on a file that is `-rw-r--r--` and readable by `cat`.

| | label |
|---|---|
| container process | `system_u:system_r:container_t:s0:c75,c906` |
| ordinary repo file | `unconfined_u:object_r:user_home_t:s0` |
| what a container may read | `container_file_t` |

The open passed the Unix permission check and was denied on the **label**. Bind mounts are the only
place this arises. `:Z` (private) over `:z` (shared): `Z` stamps the mounting container's own MCS
categories. Two things to carry: **the relabel is persistent and in-place** — `ops/` is
`container_file_t` on disk after the first run — and **the symptom reads as a wrong path**, so the
first instinct is to check the mount target rather than `ls -Z`.

**The vacuous-test family, four members and one defence.** R39 (a property test generating
ungrammatical phrasings, so all positions agreed and it discriminated nothing), R41 (interior
newlines while the defect was positional), R42 (an `httpx.MockTransport` bypasses the very method
`HTTPXClientInstrumentor` wraps, so **zero** spans were produced and "no sentinel in any span" passed
because there was nothing to search), and R44's registry-damage case. **The shared shape: a test that
passes because its subject was never exercised, not because the property holds.** The defence that
worked every time: **assert non-vacuity beside the property** (`len(spans) >= 1`,
`"127.0.0.1:1" in tree`) and run a deliberate-leak drill. `[reasoned]` on `[measured]` instances

**Delegation, four data points and one failure mode** `[measured]`. Mechanical work came back
correct, idiomatic, with no design substitution, and flagged real gaps accurately. The one
security-shaped detail is where it slipped (R41's `\Z`), and the report claimed hostile inputs were
covered because *by its own tests* they were. Rules earned: **delegate volume, keep seam-sensitive
and trust-boundary work inline**, and **verify a delegated security control by probing it directly,
not by reading its passing test results**; **an agent flagging a security gap in a docstring has done
its job, leaving it there has not**; and — R44's new one — **a delegated unit of work can end in a
partial state that looks finished from the outside**: an agent was terminated mid-edit by an
account-level rate limit having written `src/metrics.py` and the `/metrics` route but **none** of the
recording call sites. The endpoint would have served zero `mitya_*` series, and unused imports would
have been the only visible symptom. `src/metrics.py` existed, was well written, and was inert.

---

<a id="VP12"></a>
## VP12 — The provider pair, chosen on divergence rather than on price

**CP-α closed 2026-08-28 (R53).** Pair = **Google (Gemini) + Groq**, from a desk survey of vendor
documentation only — no spend, no key upgrade. The rule written in advance: choose for divergence on
the three axes the abstraction absorbs, and the pair is accepted only on 3/3. `[reasoned]` from
`p2-provider-survey.md` · `[baseline: none — first datapoint]`

| axis | Google | Groq | diverges |
|---|---|---|---|
| streaming | named event machine (`step.delta`, `interaction.completed`), usage in a terminal named event, **in-band `error` frame** | flat OpenAI `chat.completion.chunk`, usage in an opt-in trailing chunk, no in-band error | yes |
| errors | string `code` names, **three distinct 429 sub-types**, refusals in a separate non-HTTP code space | OpenAI `{message, type}`, one 429, custom 498/499 | yes |
| rate limits | **no headers at all**, limits unpublished, RPD resets midnight Pacific, rolling 10-min spend cap | full `x-ratelimit-*` + `retry-after`, published per model, org-scoped | yes |

**3/3. Accepted.** Neither the rejection nor the inconclusive branch fires.

**The condition the ruling depends on.** Google also ships an **OpenAI-compatible endpoint**.
Adapting Google through it collapses all three axes to near-identity with Groq, and the phase's own
test — *whether the abstraction survives a genuinely different second provider* — becomes
unfalsifiable. **The Google adapter targets the native Interactions API.** Reaching for the compat
endpoint voids CP-α and forces a re-take. `[reasoned]`

**The finding that bit the implementation.** Google returns **HTTP 200 carrying a refusal**. Content
blocks live in a separate code space (`safety`, `recitation`, `prohibited_content`, `spii`,
`blocklist`, `content_blocked`) reached through `promptFeedback` / finish reasons, **not** through a
status code. Groq has no equivalent state — its moderation is a separate Llama Guard model call. An
adapter that classifies on transport status alone reports a blocked generation as a **successful
empty answer**, so *refused* is a third readiness input distinct from both *succeeded* and
*transport-failed*. `[reasoned]` from vendor docs, later confirmed structurally by R57

**A10 passes by its letter and fails in substance.** The gate requires *at least one* candidate to
expose configurable per-category safety thresholds. Google does — 4 categories, 5 thresholds
including `OFF`. Groq exposes no threshold surface on the chat endpoint at all. The gate passes;
**comparability does not.** One arm's refusal rate is a tunable policy output and the other's is a
model-intrinsic property. **Refusal rate is per-arm reportable and not comparable across this pair**,
and setting Google to `BLOCK_NONE`/`OFF` does not fix it — that makes Google's floor its
model-intrinsic behaviour too, which is a **different experiment**. `[reasoned]`

**Where a model id becomes a vendor.** `_provider_for()` in `src/rag/generate.py` is the **only**
such place; adding a provider is an adapter under `src/gateway/` plus a branch there, and the request
path does not change. `src/api/app.py` stays provider-neutral: it calls one function, iterates
strings, reads a dict. `stream_answer()` kept its exact signature — `AsyncIterator[str]` plus a
caller-owned usage dict — through the entire cutover, so the cutover is a rewrite of one function
rather than surgery across the request path. `[read from source]`

**Error mapping speaks the gateway's vocabulary rather than collapsing** `[read from source]`
`src/api/app.py`:

| exception | status | why |
|---|---|---|
| `QuotaExhausted` | 429 | |
| `RequestTooLarge` | 413 | |
| `BudgetExceeded` / `Degraded` | 503 | a tripped spend guard is **degraded mode**, an availability statement — not a bill to settle or a rate to back off from |
| `ContentRefused` | **422** | 502 would call a policy decision an upstream failure; 200-with-empty would report a block as an answer |
| `TransportError` | 502 | |

`ContentRefused` is also caught **mid-stream**: Google can emit a refusal partway through a stream
that already produced text, by which point the 200 is committed and it must become a terminal SSE
error.

**`LocalProvider` survives, subclassing `GroqProvider`** — a statement about the wire format, not the
vendor, since llama-server speaks the same OpenAI-compatible dialect and this avoids a third copy of
that parser. It is **not** the default, and selecting it logs a warning: a silently-local arm would
make every hosted cost row read $0.00. `_headers()` returns `{}` rather than inheriting, so
`GROQ_API_KEY` is never sent to a plaintext localhost process. `[read from source]`

---

<a id="VP13"></a>
## VP13 — Six wire truths the documentation got wrong

Every row cost a live probe and would otherwise have cost a debugging session or a published number.

**1. `?alt=sse` alone does not stream Google** (R55) `[measured]`, six live calls.

```
POST /v1beta/interactions?alt=sse   (no "stream" in body)
  -> 200, content-type: application/json
  -> ONE line: {"id":…,"status":"completed","usage":{…}}
```

A valid 200 with a complete answer and **no streaming at all**. Nothing errors, nothing warns. Add
`"stream": true` to the **body** and the documented event machine appears. **An adapter written
against the docs alone gets a working Google arm whose TTFT equals its total latency**, whose `/chat`
SSE stalls until generation completes, which passes a correctness test and silently invalidates every
Google TTFT number. **Assert `content-type: text/event-stream`, not just the status code.**

**2. The Google adapter's request schema was wrong in four places, and the dangerous one failed
silently** (R57) `[measured]`, live end-to-end calls after the implementing agent reported the happy
path unverified:

| # | wrong | right | how it failed |
|---|---|---|---|
| 1 | `generationConfig` | `generation_config` | 400, loud |
| 2 | `systemInstruction: {"parts":[…]}` | `system_instruction: "<str>"` | 400, loud |
| 3 | `contents: [{role, parts}]` | `input: "<str>"` | 400, loud |
| 4 | `payload["usage"]` | `payload["interaction"]["usage"]`, key `total_input_tokens` | **200, silent** |

Defect 4 produced a **correct streamed answer with every token count `None`**. No exception, no
warning — `cost_usd()` was handed `None` and returned `UNPRICED`, which is the honest thing to do
with no counts. The whole failure surfaced as a quiet empty cost row, and under R48 an unpriced span
makes its aggregate unpublishable, so the cost run would have produced **no Google figure at all**
or, worse, one computed over only the rows that happened to parse. **All five offline fixture tests
passed against the broken adapter, before and after the fix** — they exercised SSE parsing and never
asserted the **request body**, the surface where all four defects lived.

> **An offline fixture suite for a provider adapter must assert the outbound payload, not only the
> inbound parse. Otherwise it verifies the half that was never in doubt.**

**3. Reasoning is the dominant cost on both expensive arms, and it cannot be switched off** (R56)
`[measured]`, 7 live non-streaming calls, one prompt, sequential.

Groq `openai/gpt-oss-120b`, `max_tokens=300` — Groq **folds reasoning into `completion_tokens`**:

| setting | completion | reasoning | share | answer chars | finish |
|---|---|---|---|---|---|
| baseline | 200 | 101 | **50%** | 505 | stop |
| `include_reasoning: false` | 133 | **55** | 41% | 392 | stop |
| `reasoning_effort: "low"` | 102 | **22** | 22% | 396 | stop |

Google, `max_output_tokens=300` — thought reported **separately**:

| model | output | thought | share | finish |
|---|---|---|---|---|
| `gemini-3.5-flash-lite` | 50 | **0** | 0% | complete |
| `gemini-3.5-flash-lite`, `thinking_level: "minimal"` | 63 | **0** | 0% | complete |
| `gemini-3.5-flash` | **8** | **288** | **97%** | truncated |
| `gemini-3.6-flash` | **12** | **284** | **96%** | truncated |

**`include_reasoning: false` does not stop the billing** — 55 reasoning tokens with reasoning "off".
It suppresses reasoning from the *response*, not from the *invoice*. Only `reasoning_effort` moves
it, and GPT-OSS's minimum is `low`, not `none`: **there is no setting that takes GPT-OSS reasoning to
zero.** `low` is nonetheless worth having (101 → 22 tokens, a **78% cut**, answer length essentially
unchanged) — whether it costs answer *quality* is a judge question, not a token question.

This **corrects R55**, which said *"Gemini also emits thought steps, so this is not one-sided."* The
structural half was right; the token half was wrong in **both** directions — flash-lite spends
**zero** and flash spends **288**. The conclusion survives, the reason is the opposite of the one
given. `[measured]`

**4. `max_output_tokens` includes thinking, and it truncates the answer.** `gemini-3.5-flash` at 300
returned an **8-token answer**; thinking consumed 288. With `Config.MAX_TOKENS` at 400, that arm gets
roughly 100 tokens to answer in, and would measure as catastrophically bad on persona and
groundedness **for a reason that is not the model**. This is also a **new empty-completion generator**
distinct from the weak-q4 EOS cause (VP5/R28): **retrying it is actively harmful**, because every
retry pays the full thinking cost again and truncates identically — at `EMPTY_RETRIES=4` that is five
full-price thinking passes for one empty answer. `finish_reason`/truncation state is what separates
*empty because truncated by thinking* (raise the budget, never retry) from *empty because the model
returned nothing* (retry may help), and both providers report it. `[measured]`

**5. "Completion tokens" is not one quantity across the pair.** Groq folds reasoning into
`completion_tokens`; Google reports `total_thought_tokens` as its own field beside
`total_output_tokens`. A cost table therefore needs **three** columns — answer tokens,
reasoning/thought tokens, and their sum — normalized across providers, or the cross-arm comparison is
not reportable. Measured share on one 60-token Groq completion: **45 of 60 = 75% reasoning**;
measured on the full run: **154.1 of 400 = 39%**. `[measured]`

**6. Groq meters per model, and the vendor's own prose said otherwise** (R61) `[measured]`. The
limiter was first keyed on the *provider* alone, citing Groq's page: *"rate limits apply at the
organization level, not individual users."* That phrase answers **who shares a limit**, not
**whether models share one pool** — and the same page lists different limits per model, which would
be meaningless under one bucket. Settled on the wire with `x-ratelimit-remaining-requests`:

| call | model | remaining |
|---|---|---|
| 1 | `gpt-oss-120b` | 999 / 1000 |
| 2-4 | `gpt-oss-20b` | 999, 998, 997 (its own counter) |
| 5 | `gpt-oss-120b` | **998**, not 996 |

**Three calls to one Groq model did not decrement the other's bucket.** `quota_scope()` is
`(provider, model)` for every provider. **Second time in CP2 that vendor docs disagreed with the
wire** (R55 was the first), and **one header read settles in seconds what prose argument does not.**

**Free, and taken:** Groq's usage object carries `queue_time` / `prompt_time` / `completion_time` — a
server-side latency decomposition at no cost, separating *their* queue from *our* network. Google
offers no equivalent; recorded where it exists rather than dropped for symmetry. Google `ListModels`
is a **free GET** that verifies a key without spending — the cheapest liveness check available, and
all six `models/gemini-*` rows in the limits table appear in the live response, so there are no dead
rows. `[measured]`

**Multi-turn to Google is not solved, and the adapter says so loudly.** `input` as a list of turns is
refused — *"When using the steps-based API version, use step_list input format instead of turn_list."*
That format was not worked out; `_to_google_payload()` **raises** on an assistant turn rather than
dropping history, because a dropped turn would be invisible in the answer. Single-turn is all the
request path needed at CP2. `[measured]`

---

<a id="VP14"></a>
## VP14 — Pricing rows and rate-limit tables: what the gateway knows

This section documents the **mechanism** — what the gateway knows about price and quota, and how the
numbers are permitted to be stated. The cost objective and the USD-per-answer analysis built on it
are the optimizer log's (`EVIDENCE-OPTIMIZER.md`).

**Free-tier calls are priced at published list rates and reported as an estimate, never as spend**
(R48) `[reasoned]`. `src/pricing.py` drew three states and a free-tier hosted call fit none of them:
it has real token counts from `usageMetadata`, a real published list price, and **no bill**. It had
been landing as `metered=False`, which discarded the list price and left the cost-optimization claim
with no number behind it.

**Decision.** Price free-tier calls at list rates; report the result as **`metered=False`**, so every
aggregate containing it is provisional and every document renders it as *list-rate estimate at the
free tier*, never as spend. **What this knowingly costs:** `config_artifact.py:150` had the rate rows
commented out on purpose — *"an unverified price in a table gets read as a measurement sooner or
later, and a wrong one is worse than a missing one."* Filling them turns that risk on deliberately,
acceptable only because the provisional flag already exists and is enforced end to end. **Each rate
row cites the pricing page and the date it was read.**

> **The claim this licenses, stated exactly.** Not *"this system costs $X per 1k answers in
> production."* Instead: *"at published list rates, this arm prices at $X per 1k answers, measured on
> free-tier quota."* The second sentence is checkable by any reader against a public pricing page;
> the first is not checkable at all.

**One rate row's provenance was corrected in place** (R57): the Groq row cited
*"console.groq.com pricing, read 2026-08-28"*; that page was unreachable that session and
$0.15 / $0.60 per Mtok was confirmed from **secondary pricing trackers** instead. **The numbers are
right, the citation was not** — the comment now says so and flags a console re-read before any
published USD figure leans on it. `[measured]` · marked as **weaker provenance than R48 requires**

**Local compute is priced against a reference instance or not at all** (R49) `[reasoned]`. *"The 1060
is already paid for"* is a deployment accident, not a cost argument. **Rule:** local-compute cost is
reportable only as an `[estimate]` naming three things **on the face of the number** — a **reference
instance** with a published hourly rate, the **measured wall-clock** of the stage being priced, and
the **utilization assumption**. Absent any of the three it is not a cost figure and does not appear
in a cost table. This is strictly more work than R48's free-tier shadow price, which reads one
pricing page; here the hardware profile is itself a design claim being made.

**The rate-limit table, and the rule that consulting one limit is not a budget** (R85, R96)
`[computed]` from `Config.MODEL_LIMITS` · `[baseline: none — first tabulation]`. A draw at
`MAX_TOKENS=1024` is **~2766 tokens** (prompt 1742 + completion 1024), verified against
`config_artifact.py:320-326`:

| model | per-minute ceiling | per-day ceiling | which limit binds |
|---|---|---|---|
| `models/gemini-3.5-flash` | 5 (rpm) | **20** (rpd) | rpm / rpd |
| `models/gemini-3.5-flash-lite` | 15 (rpm) | **500** (rpd) | rpm / rpd — `tpm` 250,000 would allow 90/min; there is **no `tpd`** |
| `openai/gpt-oss-120b` (Groq) | **2.89** (tpm 8000 / 2766) | **72** (tpd 200k / 2766) | **tpm / tpd — both token limits** |

**Switching the serving arm changes the budget by more than an order of magnitude**: the per-minute
ceiling falls 15 → 2 and the per-day ceiling 500 → 72.

**`tpd` — one of the two limits that bind on Groq — is not enforced anywhere in code.** `[measured]`
`src/limits.py`'s `RateLimiter` accepts `rpm`, `tpm`, `rpd` and **no `tpd`** (`limits.py:67-73`);
`Config.limits_for()` returns `tpd` in the dict, so a budget *can* consult it, but nothing at runtime
stops a run that breaches it. **On the one provider where `tpd` binds, the limit that actually ended
a run is guarded by discipline alone.** Recorded as an observation about the limiter, not a proposal
to change it.

**The verified budget for CP4's runs, as the worked example** (R96) `[computed]` · `[baseline: v2
CP2's four-limit rule]`:

| run | draws | list-rate USD |
|---|---|---|
| fake ramp, concurrency 1/2/4/8/16 × 20 | **0** (FakeProvider) | $0.00000 |
| real ramp, concurrency 1/2/4 × 5 | 15 | $0.04624 |
| fault injection, `429:1` `retry_after=2.0` | 1 | $0.00308 |
| ladder walk, `force_primary` + `force_cached` | 3 | $0.00925 |
| saturation observation / corruption check | 0 — read the ramp's own records | — |
| **TOTAL METERED** | **19** | **$0.05857** |

19 draws is **3.8% of `rpd`**; at rpm 15 that implies a **1.3-minute** wall-clock floor; `tpm` and
`tpd` do not bind on this arm. **The binding constraint was none of them** — it was the
`data/vector_db/` exclusive file lock, since every shape needs `uvicorn` up and no offline eval may
run concurrently.

**A correction inside R96, caught at preflight before anything was spent** `[measured]`. R96's
central premise — *the zero-spend fake ramp "needs no code change" because `src/gateway/fake.py`
already satisfies the `Provider` protocol* — is **false**. The protocol claim is true; the
**selectability** claim is not. `_provider_for()` calls `_resolve_adapter(model)`, which maps a model
id onto the google, groq or local adapter, and a grep for `FakeProvider` across
`src/rag/generate.py`, `src/config.py` and `src/api/app.py` returns **nothing**. The premise was
inherited unverified from the schedule. **Third asserted-and-unchecked premise in the project, and
the second to survive into a document that gated a decision.** The arithmetic survives; the
"approvable" verdict narrowed to *approvable for the metered portion only*.

**A correction to the prefix-cache minimums that widened the conclusion** (R53) `[read from source]`.
R52 cited ~~Anthropic's 1,024-token minimum cacheable prefix~~; **neither chosen provider uses that
number** — Gemini Flash requires **4,096**, Groq's GPT-OSS models **128-1,024**, varying by model.
The conclusion stands and its margin is *wider* than stated for Google. One new lever appears:
**Groq's cached tokens do not count toward rate limits**, which would make prefix caching a *quota*
saving on that arm, not only a cost one — and quota is what the measurement budget is denominated in.

**Spend rate is a backstop, not a working limit.** `Config.SPEND_RATE_MAX_USD_PER_MINUTE` defaults to
**$0.10**; at the cheap arm's list rate that is ~63 answers/min against an rpm of 15, so **the quota
limiter binds first and the breaker never fires under normal operation.** It exists for what the
limiter cannot see: a costly model where 15 rpm is within quota but the spend rate is not.
`budget=None` on the live path, because at the free tier there is no USD ceiling to breach — Google's
rolling cap is literally $0 — so an invented USD ceiling would make the guard look like it enforced
something it does not. `estimated_cost_usd` prices the **worst case** (estimated prompt plus a full
`MAX_TOKENS` completion) because the guard must decide before calling and over-rejecting is the
correct direction for a spend guard; `MAX_TOKENS` is a genuine upper bound on both arms precisely
because thinking counts against it (VP13). Computed ceiling for the default arm: **$0.00157**.
`[read from source]` + `[computed]`

---

<a id="VP15"></a>
## VP15 — Rate limiting and retries: three budget failures at three scales

**The reservation counted the wrong direction** (R65) `[measured]`, from **242 real HTTP 429s** from
Groq absorbed by 84 transport retries during a live run. `guarded_generate()` reserved
`_input_tokens(messages)`; **provider TPM counts input plus output**:

| reserved | tokens | requests/min permitted |
|---|---|---|
| what the limiter counted (input only) | 1712 | **4.7** |
| what Groq counts (input + completion) | 2112 | **3.8** |

A **24% overshoot**. Every excess request came back 429, and a transport retry added the same day
absorbed all of them — so the run completed and the miss converted into latency and wasted `rpd`
instead of an error. **Without that retry this arm would have died the way the first one did**; the
two fixes were written hours apart and the second silently rescued the first one's absence. Fixed:
the reservation is now `input_tokens + max_output_tokens`. The request-size check stays input-only on
purpose — *does this prompt fit at all* is a different question from *how much quota does this round
trip consume*.

**The three budget misses, same error at three scales** `[measured]` · `[baseline: v2 CP2's own
pre-registration]`:

| # | read | ignored | consequence |
|---|---|---|---|
| R62 → R65 | `rpm 30` | `tpm 8000` | the Groq arm was sized at **~5.0 minutes**; **TPM binds 7.9× harder**, so 150 draws is **~40 minutes** |
| R62 → R68 | `rpd 1000` | `tpd 200,000` | the arm was called comfortable at 15% of `rpd`; the real ceiling is **93 draws/day**, and one k=3 pass is **319,950 tokens = 160% of `tpd`**. Even k=1 is 53% |
| P7 Batch D | each leg budgeted | the day never summed | **~570 draws against RPD 500**, voiding 52 of 58 rows in one quality leg |

> **`Config.limits_for()` returns four limits — rpm, tpm, rpd and tpd. A budget that consults one of
> them is not a budget. Compute the ceiling from every limit and take the minimum.** On a long-prompt
> RAG workload the binding one is usually a *token* limit, not a request count. Google's flash-lite
> has tpm 250,000 against the same ~2100-token request, so there rpm genuinely does bind — **which is
> why the Google arm ran at its estimated pace and hid the defect.**

**Why the leaky bucket made a quota exhaustion look like congestion** (R68) `[measured]`. Groq meters
with a bucket that refills continuously (`x-ratelimit-reset-tokens: 547ms`) while
`src.limits.RateLimiter` is a 60-second sliding window. The window admits a burst that fits the
per-minute total; the bucket refuses it because the tokens are not present at that instant. **The two
disagree about *when*, not *how much*, so no token-count correction fixes it.** Fixed by honouring
the provider's own `retry-after` (Groq sends it on every 429; Google sends no rate-limit headers at
all). **But honouring a correct instruction cannot create capacity that does not exist** — with `tpd`
exhausted, the correct instruction is *"wait five minutes."*

**A measurement defect that exposure produced** (R68) `[measured]`. `ttft_s` was recorded as **316
seconds** on one dry run: five minutes of `retry-after` plus about **1.5 s** of actual generation,
because `t_start` sat *before* the retry loop. Provider latency and user-experienced latency are
different metrics and both are legitimate; reporting one as the other is not. Now split three ways:
**`ttft_s`** (first token from the attempt that produced it — provider latency), **`retry_wait_s`**
(time lost to backoff), **`total_s`** (what the caller actually waited).

**Two retry classes, one shared budget** (R71, closing CP-γ) `[reasoned]` + `[measured]`:

| class | trigger | retried? | why |
|---|---|---|---|
| **transport** | no stream at all — 429, 5xx, connect, timeout, in-band provider error | always | the failure is ours or the network's, and a second attempt is the whole remedy |
| **quality** | stream succeeded, produced zero tokens | **only where opted in** | a re-draw is a duplicated generation at full price |

`Config.MAX_GENERATION_ATTEMPTS` (default 3) is consumed by **either** class, because per-class
ceilings multiply: three transport attempts plus three quality attempts is **nine generations for one
answer**. Pinned by two tests — one drives three transport failures interleaved with three empty
completions with both classes enabled and a budget of 3, asserting at most three generations; the
converse test pins that a budget only one class can spend is two budgets wearing one name. Both retry
only while **nothing has been emitted**: once a token is out, a retry would duplicate text
mid-answer, so the window closes at the first token and TTFT is unaffected for a normal stream.
`[read from source]` + `[measured]` 161 checks across 22 modules

**CP-γ's decision, and why the incomplete arm could not change it** (R70) `[measured]`. Failure
classification over every CP2 transcript, separating *zero-token completions* (the stream succeeded
and produced nothing) from *transport failures* (no stream at all):

| arm | draws | ok | zero-token | transport |
|---|---|---|---|---|
| `gemini-3.5-flash-lite` quality | 150 | 150 | **0** | 0 |
| `gemini-3.5-flash-lite` cost | 50 | 42 | **0** | 8 |
| `openai/gpt-oss-120b` | 150 | 93 | **1** | 56 |

The rule was fixed in advance and asked an **existence** question: *a single occurrence is enough to
keep the mechanism; no MDE applies.* **Decision: keep the mechanism, default OFF per provider.** An
arm that lost 56 draws to transport errors can only **under**-count zero-token completions, never
over-count them, so the observed count is a lower bound and a lower bound above zero is all the rule
needs. **Re-running Groq could not have changed the answer** — it could only raise a number the rule
does not read. `Config.QUALITY_RETRY_MODELS` ships **empty**.

> **The decision rule determines the precision required, and it was written first.** Reading the rule
> before planning the run would have saved a day's quota — and would have caught, before spending it,
> that the budget never checked `tpd`.

**Retries consume `rpd`.** 84 retries rode on top of 150 real draws. At this prompt size a longer run
could exhaust the daily request budget **through retries alone**, which no per-request check would
catch. `[measured]`

**Still open, carried from v1's `RateLimiter`.** `ScopedRateLimiter` centralizes the windows but does
**not** fix the split: they live in one process, so N uvicorn workers hold N sets and the effective
quota is N×. Needs shared state, not a better key. Deployment runs **one worker**, which is what makes
a single in-process window correct today. `[read from source]`

---

<a id="VP16"></a>
## VP16 — Routing was rejected, on cost, before it was built

**CP-β closed 2026-08-28 (R69).** The routing question was decided by simulating families **offline
over the recorded cost matrix**, not by implementing three routers — the classifier's call cost and
the cascade's wasted cheap-tier calls charged to their own totals. `[measured]` + `[reasoned]`

**Labels were pre-registered before any arm was measured** (R58) `[reasoned]`,
`scripts/label_escalation.py`, artifact `research/p6-escalation-labels.json`. A row escalates if any
of five signals fires, all computable **from the query alone**, because that is all a router sees:

| signal | fired |
|---|---|
| **E1** multi-entity synthesis — the project's own `EntityMap` resolves ≥2 distinct entities | 21 |
| **E2** adjudicated disjunction — a polar question posing alternatives to arbitrate | 11 |
| **E4** contradiction to reconcile — an explicit reversal marker | 8 |
| **E3** unstated inference — asks about intent the text does not state | 4 |
| **E5** compound — ≥2 interrogatives | 2 |

Result: **31 escalate, 19 cheap-sufficient** on the 50-row quality set. **Routing's addressable slice
is 19 of 50 rows — 38%** — a router cannot save anything on the other 31 by construction. Read
against the thinking measurements this cuts both ways and the two effects nearly cancel: the *saving
per routed row* is much larger than the rate card implies (cheap tier spends **zero** thought tokens,
expensive tier 96-97%), but it applies to **38%** of rows. **Denominator is the 50-row quality set,
not the 106-row retrieval set.**

**The arithmetic** (R69, over the same 50 rows, at the measured per-answer figures) `[computed]` ·
`[baseline: v2 CP2's own always-cheapest arm]`:

| strategy | total | USD/1k | vs cheapest |
|---|---|---|---|
| **all-Groq (always cheapest)** | $0.0250 | **0.500** | **1.00×** |
| routed, inverted (cheap→Google, esc→Groq) | $0.0435 | 0.870 | 1.74× |
| routed as designed (cheap→Groq, esc→Google) | $0.0552 | 1.103 | 2.21× |
| all-Google | $0.0736 | 1.473 | 2.95× |

**Every routing family costs more than always sending traffic to the cheapest arm.** Not a close call
requiring CP-β's 10% cost-separation rule: **the best routed option is 74% more expensive than the
trivial policy.**

**Why this was arithmetically inevitable.** Routing pays **only if the expensive arm buys something**,
and G-S2 (below) established that this project's instrument cannot show it does. Under
non-inferiority, an arm that is not distinguishably better is not worth paying more for — so
routing's premium buys nothing measurable **by construction**. `[reasoned]`

**The hierarchy is inverted, and that is the substantive finding.** The assumed shape was a
cost/capability ladder — small fast model for easy queries. **The cheaper arm is the larger model**:
Groq's `gpt-oss-120b` (120B) costs **2.9× less per answer** than `gemini-3.5-flash-lite`, because
Groq's rate card is cheaper per token and Google's completion leg ($2.50/Mtok) dominates a long-form
answer. **There is no ladder to climb here.** `[measured]`

**No online quality floor is derivable, and the reason is structural** (R59) `[reasoned]`, field
inventory of the spine and the retrieval path. Not *"nobody plumbed the field"*: **the fields that
vary with the model are not quality signals, and the fields that look like quality signals do not
vary with the model.** Two candidate signals exist and are discarded before the stream — the FlashRank
cross-encoder score (computed at `src/rag/retrieve.py:181`, dropped at `:188`) and the fused RRF score
— but **retrieval does not vary across model arms**: the router selects a generation model, and
retrieval, fusion and rerank all happen *before* that choice. A signal with zero between-arm variance
cannot arbitrate between arms at any n. **It is not a weak floor; it is structurally incapable of
being one.** The fields that *do* vary — `generate.empty`, token and char length, cost, latency — are
a degenerate floor (total failure only) and a length proxy that is not quality.

**G-S2 — cost decides, within non-inferiority, with Tier 1 teeth** (R63) `[reasoned]`, operator
decision. The headline is **cost**; quality is reported as **non-inferiority within the pre-computed
MDE**, never as equality and never as a ranking, with the MDE travelling with the claim. A small set
of **Tier 1 metrics act as hard disqualifiers** (source-leak rate, assistant-tell rate, both refusal
rates), so the choice is not purely economic. **Buying resolution was rejected on measurement, twice
over**: more rows is refuted by VP2, and a more precise judge prompt was *measured to make it worse*
by VP3.

**What this closure does NOT establish, stated so it is not over-read** `[reasoned]`:

- **The genuinely expensive tier was never priced.** `gemini-3.5-flash` is rpd 20 — **7.5 days** for
  one k=3 pass — so the conclusion is *"routing is not adopted among the arms measurable on the free
  tier"*, not *"routing never pays"*.
- **Quality did not arbitrate**, by design (G-S2) and by circumstance (Groq's arm scored 31/50 cells,
  VP19).
- **Both cost figures were measured at `MAX_TOKENS=400`**, i.e. on truncated answers (VP18). At 1024
  Google roughly doubles ($0.001523 → $0.003083) while Groq moves less, **widening the gap and
  strengthening the conclusion** — but the absolute figures must be re-quoted at the new budget.

**Downstream:** routing **leaves the optimizer's search space**, and the tier set (O1) becomes moot —
a tier set is the input domain of a routing family, and there is no family. Recorded rather than
decided.

**One defect found while labelling, which reaches production** (R58) `[measured]`. `Jesus`'s alias
list in `entities.json` contains **`Word`**, **`Lord`** and **`God`**, so *any* query containing the
common noun "word" resolves `Jesus Christ` — `q-0044` ("without a **word** of reproach") does exactly
that. `EntityMap.resolve()` runs in the **live retrieval path**, where resolved entities drive
filters and query expansion. 6 of 50 rows resolve it; only `q-0044` is unambiguously spurious. **No
P-6 label changed because of it — checked, not assumed.** A sibling defect: `EntityMap.resolve()`
maps **"Marie"** (Shatov's wife) to *Mary, Mother of Jesus*. Both recorded, neither fixed.

---

<a id="VP17"></a>
## VP17 — Three cache layers, two refuted on measurement

| layer | key | stores | saves | staleness risk |
|---|---|---|---|---|
| **L1** `AnswerCache` | normalized exact query + filters + route + `config_version` + `corpus_version` | the answer | whole pipeline including the metered call | **yes** — an old answer is served |
| **L2** semantic (**REFUTED**) | dense cosine over the query | retrieved chunks | retrieval + fusion + rerank | yes — wrong chunks served |
| **L3** provider prefix cache (**cannot engage**) | **byte-identical token prefix** | provider-side attention KV | prefill on the shared prefix only | **none** — output generated fresh every call |

**L2 is refuted, and not for the reason anyone predicted** (R50) `[measured]`,
`scripts/probe_semantic_cache.py` + `_overlap.py`, one afternoon, zero spend, no API calls.

Over **1204 disjoint-target DEFAULT pairs**: `corr(query cosine, mean served_recall) = **+0.192**`,
where `served_recall(A→B) = |topk(A) ∩ topk(B)| / |topk(B)|`, asymmetric because the cache serves A's
chunks to B. **Cosine barely predicts chunk overlap.** Twelve authored paraphrases served from their
source row's reranked top-3:

- highest-cosine pair in the set (**0.9586**) shares **zero** chunks with its source;
- **median served_recall is 0.33** — one chunk of three;
- the must-miss population tops out at cosine 0.8293 with served_recall 0.00 both directions.

**Not because adversarial pairs collide — they separate cleanly — but because correct matches do not
reproduce retrieval.** Mechanism, and it is why this generalizes: retrieval is **not** a function of
the dense query vector. The pipeline is EntityMap expansion → **dense + BM25 hybrid** → RRF →
**FlashRank cross-encoder over the raw query string**; **two of those three stages are lexical.**
`"epileptic seizure"` → `"epileptic fit"` barely moves the dense vector and completely changes the
sparse leg. **A dense-cosine key measures one of three retrieval signals and is blind to the two that
respond most sharply to the wording change it exists to absorb.**

Disposed of without further testing: **caching at the fused layer** (BM25 is *inside* the fusion, so
the lexical sensitivity is upstream of rerank); **a key that captures the lexical signal properly**
(in the limit that is L1's normalized exact-match key — *the honest path to a higher hit rate is
better L1 normalization, not semantics*); and **raising the threshold** (T ≥ 0.83 admits zero
colliding pairs while retaining 12/12 "hits", but those hits have served_recall 0.33 and 0.00 —
**safe and useless**).

**Two defects in the probes themselves, both recorded** `[measured]`: (1) the first verdict was an
artifact — the must-miss population used disjoint eval `targets`, and **44 of 94 DEFAULT rows are
refusal rows with `targets: []`**, so an empty set intersected nothing and every refusal row counted
as disjoint from all 94, including the jailbreak row whose correct behaviour is refusal. Corrected
population 50 rows; `max(must-miss)` fell 0.9172 → 0.8293 and the margin flipped −0.0408 → +0.0472.
**The first REFUTED verdict was wrong**; the refutation rests on the overlap probe, not on it. (2)
The threshold sweep is miscalibrated and **must be read inverted** — its "hits kept" column counts
cosine ≥ T and never checks whether the hit is *good*, so it reports **safety, not usefulness**.

**Limit of the claim, stated.** `TOP_K_RERANK = 3` makes served_recall coarse — one swapped chunk is
a 0.33 swing. k=3 is what this system serves, so it is the right denominator for a decision about
this system. **The claim is about dense-cosine keys over this pipeline, not about semantic caching in
general.** `[reasoned]`

**L1 was a state channel between arms** (R51) — see VP9 row 1. Two of the checkpoint's stated
premises were **already false**: a hit had stopped reporting `route_name: "CACHE"` at CP1 (it emits
the real route with `cache_hit` as an orthogonal dimension, deliberately, so hits stay poolable with
misses per route), and a hit already emits `cost_usd: FREE.usd` with `total_usd()`'s rule making any
aggregate containing an unmetered stage provisional. `[read from source]`

**A claim that is still wrong in a code comment, carried rather than silently fixed.**
`src/api/app.py:352` claims a hit's `cost_usd: FREE` *"makes A5's saving claim measurable as cost
avoided."* **It does not**: cost *avoided* is the counterfactual price of the arm that would have
served the request, which is computable under routing (run the router, skip the provider call) and is
not what `FREE` records. `[reasoned]`

**L3 cannot engage at this prompt shape** (R52, CP-ε closed by R70) `[reasoned]` + `[estimate]`.
Coding agents publish 80-90% prefix-cache hit rates; that number is **neither L1 nor L2**, and it is
**workload shape, not matching cleverness**. An agent loop's prefix is large, static and
append-only — system prompt plus tool schemas, order 10-20k tokens byte-identical every turn — so
turn *N+1* shares 100% of turn *N*'s prefix and the rate approaches `1 − new/total`. **Mitya is the
inverse shape, and single-shot RAG is close to worst case**: `PERSONA_HEAD` is roughly **45 tokens**
`[estimate]`, composition A puts `passages_block` in the **same system message** directly after it,
so the stable prefix ends at about **60 tokens** where the retrieved chunks begin — against provider
minimums of **4096** (Gemini Flash) and **128-1024** (Groq GPT-OSS). **The cache never engages at
all.** Composition B does not help: persona alone is still the whole stable prefix. Raising
`MAX_TOKENS` does not change this — the ceiling governs the completion, not the prefix. An on/off
pass would therefore measure run-to-run noise, and **CP-ε closes without one.**

> **Denominator discipline for any L3 figure ever published:** a "90% cache hit" is 90% of **input
> tokens** read at the reduced rate — not 90% of requests served free, and not a latency claim.
> Provider rates are cache **write 1.25×** base and **read 0.1×**, with output unaffected, so **a
> prefix that misses more often than it hits costs more than not caching.** State input tokens as
> the denominator and the write/read split, or it is not reportable.

The two levers, neither free, both routed out of the platform phases: **grow the static prefix past
the minimum** (costs those tokens on every uncached call and moves composition A away from the
fine-tune distribution — a **quality** change, hence the optimizer's), and **pin retrieved context
ahead of the transcript and stop re-retrieving per turn** (a memory-allocation decision, hence
`EVIDENCE-MEMORY.md`'s). The measured replacement for the `[estimate]` above — 393 bytes shipped /
511 pinned — belongs to the memory log; **it is an estimate replaced by a measurement, not promoted
to one.**

**"Do not cache at all" could not be closed at CP2** (R47) `[reasoned]`. The instrument that settles
it — the harness's repeat rate — is built one phase later. Measuring it on CP2's traffic would have
produced a **right answer to the wrong question**: the eval set is 99 rows of deliberately distinct
queries authored for coverage, whose repeat rate is near zero **by construction**, so the stated rule
would have licensed "do not cache at all" as a property of the eval set masquerading as a property of
the system. And the qualifier does not expire at CP4: **the harness repeat rate is a design
parameter, not a natural fact** — even measured, the number is chosen, not found (VP26).

---

<a id="VP18"></a>
## VP18 — `MAX_TOKENS` 400 → 1024: a measurement break, taken deliberately

**What it is** (R66) `[read from source]`. `MAX_TOKENS` is the **API parameter** (`max_tokens` on
Groq, `max_output_tokens` on Google): a hard server-side cutoff on the token stream. **It is not an
instruction and the model is never told about it.** Nothing in the prompt asks for brevity — the
persona asks for the opposite (*"incorporating your deep literary, existential, and philosophical
tone"*). So the request is expansive prose and the response is guillotined mid-token. **Truncation
was not a risk under this configuration; it was the designed outcome.**

**Where ~~400~~ came from** `[computed]` · `[baseline: v1 recorded]`:

```
CTX_SIZE   = 3072      the 6GB GPU's context budget
prompt    ~= 1742      retrieved passages + persona (measured)
1742 + 400 = 2142      fits inside 3072
```

**400 was sized to fit the local context window on the hardware this phase exists to escape.** It was
never a product decision about answer length. Carrying it into hosted serving imports the constraint
being escaped — structurally the same error as *"the 1060 is already paid for."*

**Measured impact** `[measured]`:

| arm | draws at the 400 ceiling | answers not ending on terminal punctuation |
|---|---|---|
| `gemini-3.5-flash-lite` | 0 of 150 (no telemetry captured on that pass) | **128 / 150** |
| `openai/gpt-oss-120b` | **94 / 141** | 91 / 141 |

Groq's is worse for the same budget because reasoning is billed inside `completion_tokens`: mean 109
reasoning tokens, **max 398** — one row spent 398 of its 400 thinking and had ~2 left to answer with.

**What it does and does not invalidate** `[reasoned]` on `[measured]` inputs:

- **The arm-vs-arm comparison survives** — both arms ran the same budget, so truncation is a shared
  condition rather than a confound between them.
- **The comparison against the frozen baseline does not.** `local-q4km` rarely reached the ceiling;
  the hosted arms reach it almost always. **The persona ~~3.626 → 5.000~~ and groundedness
  ~~2.813 → 3.973~~ deltas must not be published as measured model improvement** — they compare a
  mostly-complete answer set against a mostly-truncated one. `[baseline: v2 CP0's frozen row]`
- **Cost figures are unaffected in kind** — truncated tokens are billed tokens — but must be labelled
  *at `MAX_TOKENS=400`*, because they are not the cost of a complete answer.

**Not re-run, by decision.** ~300 generations plus ~300 judge calls against a judge budget already
near its daily 500. The run stands as measured, with the truncation stated wherever its numbers
appear. `[reasoned]`

**The gate enforces the break rather than trusting prose.** `src/rag/gate.py` refuses a cost
comparison when `max_tokens` differs between production and candidate, with the verdict
`"a measurement break, not a cost delta"`. `[read from source]`

**Process fix, because the smoke tests did not catch it.** Both arms were smoke-tested at 2 rows
first; those smokes verified that the run **executed** — exit code, cell count, provenance block —
and never looked at what it **produced**. **Printing the last 60 characters of one answer would have
shown `"...must assert his will upon the"` before 300 calls were spent.**
`scripts/eval_quality.py` now computes a `truncation` block into every summary and logs it at
WARNING. `[measured]`

> **A dry run must inspect OUTPUT, not just exit status. A run that completes cleanly and measures
> the wrong thing is the expensive failure, and it looks identical to success from the outside.**

---

<a id="VP19"></a>
## VP19 — CP2 as measured: what was complete, what was contaminated, and what it cost

**The matrix** (R67) `[measured]`, three runs across two arms, 2026-08-28 · `[baseline: v2 CP2's own
desk prediction, made independently from rate cards before any call]`:

| | `gemini-3.5-flash-lite` | `openai/gpt-oss-120b` |
|---|---|---|
| config_version | `4c9c75e14ba7` | `93e354c4067f` |
| n (priced rows) | 42 | 93 |
| prompt tokens | 1656.5 | 1732.9 |
| completion tokens | 390.4 | 400.0 (at ceiling) |
| **reasoning tokens** | **0.0** | **154.1** (39% of completion) |
| **USD/answer** | 0.001473 | **0.000500** |
| **USD/1k answers** | 1.473 | **0.500** |
| TTFT p50 / p95 | **1.88 s / 2.57 s** | ~~7.62 s / 33.43 s~~ **withdrawn** |
| total p50 / p95 | 11.71 s / 13.61 s | ~~8.26 s / 34.00 s~~ **withdrawn** |

**Both arms land within 5% of the desk prediction** ($0.001570 and $0.000525), derived from published
rate cards before any call was made. **Two independent routes to the same number is the strongest
form this project's cost figures have had.** Per R48 both are `metered=False` — a **list-rate estimate
at the free tier**, never spend. The per-answer analysis built on these rows is the optimizer log's.

**Google's latency is the one clean latency claim CP2 can make**: TTFT p50 **1.88 s** against the
frozen local row's **7.790 s** — a **4×** improvement. `[measured]` · `[baseline: v2 CP0's frozen
row]`

**Groq's latency is withdrawn, twice over** `[measured]`: a retry storm (242 × HTTP 429 absorbed by
three retry attempts that were not enough), and `ttft_s` that included rate-limit backoff before that
was split out. **It measures a limiter defect, not a provider.** Withdrawn rather than caveated.

**Groq's quality half is unusable.** 31 of 50 cells scored, 19 failed, **57 of 150 draws errored** —
the run was launched before the limiter fix. 31 cells is below what the paired test can use.

| half | Google | Groq |
|---|---|---|
| quality | complete (150 draws, truncated per VP18) | **incomplete** (31/50 cells) |
| cost | complete (42 rows) | complete (93 rows) |
| latency | complete, clean | **contaminated** |

**A provider-availability datapoint, unprompted** `[measured]`. The Google cost pass lost **8 of 50
rows (16%)** to `api_error: "gemini-3.5-flash-lite is currently experiencing high demand"` after
exhausting all three retry attempts — the same error class that killed an earlier arm hours before.
**A 16% transient failure rate on a free tier**, against a retry policy tuned for jitter rather than
multi-second provider overload: 0.25 s base, capped at 4 s, 3 attempts, **under 2 s of total
backoff** — an order of magnitude short. Not changed at CP2: a longer backoff on a user-facing
request is a latency decision, and the eval harness and `/chat` may legitimately want different
policies.

**An env var cannot switch arms, and the provenance block caught it on its first real use** (R64)
`[measured]`. Arm 2 was launched as `SERVING_MODEL=openai/gpt-oss-120b python
scripts/eval_quality.py …` and **ran `models/gemini-3.5-flash-lite`** — the same model as arm 1.
`Config.SERVING_MODEL` resolves through the **versioned artifact facade**, not the environment;
`ServingConfig.from_env()` reads the env var only when *building* an artifact, and the active
artifact is loaded from `config/serving/CURRENT` with its value already baked in. **Both runs carry
`config_version` `4c9c75e14ba7`** — the artifact stating plainly that they are the same
configuration.

**The numbers were plausible, which is what makes this class of error dangerous**: persona 5.000 vs
5.000, groundedness 3.973 vs 3.933 — deltas small enough to read as *"the arms are
indistinguishable"* and be believed. Two independent instruments both said otherwise. **To measure an
arm, promote a config for it** (`SERVING_MODEL=… bootstrap_config.py --promote`); that is the design
working, not a workaround, and an env-var override that bypassed the artifact would reintroduce the
defect it closed.

**Salvage: an unintended replication, and it is the run-to-run variance number nothing else provided**
`[measured]` · `[baseline: v2 CP2's pre-registered MDE]`:

| metric | run 1 | run 2 | spread |
|---|---|---|---|
| persona_retention | 5.000 | 5.000 | **0.000** |
| groundedness | 3.973 | 3.933 | **0.040** |
| fabrication_rate | 0.547 | 0.533 | 0.014 |

Groundedness spread of **0.040** sits well inside the pre-registered MDE of **0.258** — about a
**sixth** of it — which is evidence the MDE is not absurdly conservative.

**A second defect found at the same time:** the transcripts recorded **no tokens, no USD, no TTFT, no
cache_hit and no zero-token flag** — only judge scores. The run that completed was **half** of the
specification. Fixed: `_collect_answer()` now returns a `telemetry` block per draw, with `ttft_s`
`None` when no token ever arrived (recording the give-up time as a TTFT reports the least trustworthy
requests as the fastest).

**The pre-registered MDEs, fixed before the run** (R62) `[computed]` · `[baseline: v2 CP0's R29
variance components]`, n=50 cells, α 0.05 two-sided, power 0.80, using the **independent** (hence
conservative) SE-of-difference form:

| metric | SE(cell mean, k=3) | SE(arm mean, n=50) | SE(difference) | **MDE** |
|---|---|---|---|---|
| persona_retention | 0.888 | 0.1256 | 0.1776 | **0.497** |
| groundedness | 0.460 | 0.0651 | 0.0920 | **0.258** |

A persona delta under **0.497** or a groundedness delta under **0.258** is *indistinguishable at
n=50*, not a win and not a loss. The frozen baseline's own persona mean is 3.626 ± 0.158, and an MDE
of 0.497 is **14% of the scale**.

**The judge is the binding constraint, not generation** `[computed]`: 300 judge calls for two arms is
**60%** of a daily 500-rpd quota, so **two arms is the maximum measurable in one day** — a quota
fact, not a design preference. The expensive Google arm (`gemini-3.5-flash`, rpd 20) needs **7.5
days** for one k=3 pass and was excluded; **the tier the routing question would most want to price is
the one the free tier cannot deliver.**

**CP2 spend (charter O8).** Roughly **1,000 hosted calls** across two providers on 2026-08-28, at
**$0.00 actual spend** (free tier); priced at list rates the same traffic is about **$1.10**. **Two
runs were wasted to defects** — one measured the wrong model, one died on a rate-limit storm — and
**Groq's daily token budget was exceeded 5×** (~998,000 tokens against a 200,000 `tpd`), which is
what `tpd` accounting now exists to prevent. `[measured]` + `[estimate]` on the list-rate column

**Every CP2 checkpoint closed** — CP-α, CP-β, CP-γ, CP-δ, CP-ε, CP-ζ — and all seven design gaps
(G-S1..G-S7) with them.

---

<a id="VP20"></a>
## VP20 — The gate: three layers, and nothing in it is a hand-set threshold

**Shape** `[read from source]` `src/rag/gate.py`, `scripts/gate.py`:

| layer | compares against | can block? |
|---|---|---|
| **invariants** | zero (route mismatches, determinism spread vs the recorded baseline) | **yes** |
| **cost** | production's own computed value | **yes** |
| **sampled quality** (`recall_at_5`) | production minus the MDE **that run computed** | **yes** |
| MRR, `distinct_target_chapters_hit_at_5`, pool recall | reported with an MDE | **no** — `blocking: false`, skipped by `disposition_of()` |
| `quality_1b` | — | **empty by construction**, pending G-B |

**A constant would go stale as the system improves**, which is why every threshold is derived from
the run rather than configured. `[reasoned]`

**`NOT-DETECTED` passes, prints delta beside MDE, and is never recordable as an improvement** —
enforced as a report field the optimizer's ranking reads, not as prose. **No component may collapse
quality to a scalar**: the report schema rejects any `overall`/`score`/`composite` key.
`[read from source]`

**Why MRR and coverage report but do not block** (R74) `[computed]` + `[reasoned]`. The defect found:
only `recall_at_5` carried any test at all — MRR and coverage were computed, carried into the report,
and **never given an interval**, so the gate was blind to any change moving a hit's *rank* without
moving it across k. A row going from rank 4 to rank 2 is a real improvement hit/miss cannot see.
McNemar does not extend to them: it conditions on discordant pairs, and discordance only exists for
binary outcomes. Hence a **paired bootstrap** — resample rows, take both arms' scores for each
resampled row, preserving pairing — with MDE reported as `2.8 ×` the bootstrap SE, so the two MDE
families are commensurable. **Making a second quality metric blocking would implicitly weight ranking
quality against hit quality, and that weighting is exactly what G-B leaves open.** A gate that blocks
on two quality metrics has picked a composition; one that reports both has not.

**The bootstrap is seeded, and that is load-bearing rather than tidy**: an unseeded bootstrap makes
two gate runs on identical inputs disagree, tripping the determinism invariant this same module
asserts and making a report unreproducible from its own inputs. `[read from source]`

**The gate blocks on ANY cost increase — tolerance zero, no MDE.** `src/rag/gate.py:311`:
`verdict = BREAK if cand_usd > prod_usd else (DETECTED if cand_usd < prod_usd else NOT_DETECTED)`.
There is no tolerance band and no MDE on the cost layer, deliberately: cost is `[computed]` from
token counts and a rate card, not sampled. `[read from source]`

**Two guards that fire before a comparison is attempted** `[read from source]`: production being
`UNPRICED` returns `ERROR` — *"no cost claim is possible"* — rather than a zero; and a `max_tokens`
difference returns `ERROR` naming it a measurement break (VP18).

**The gate may never claim grounding was checked.** `validate_report()` rejects a report lacking the
disclaimer, and every report carries **"GROUNDING NOT CHECKED"** verbatim (VP21). `[read from
source]`

**Two bypass facts, recorded as a discipline claim rather than an invariant** (G-F, closed as such):
`--no-verify` skips a hook and `config/serving/CURRENT` can be hand-edited. **Detection after the
fact via the promotion log, not prevention.** `[reasoned]`

**The bound carried on every promotion**, in this exact shape and never as an assertion of equal
quality:

> *"cost −X% [computed, at the `MAX_TOKENS` ceiling], quality not detectably worse (MDE m at n=N)"*

**A promoted config may be up to ~12-18 recall points worse than production without detection**
(residual risk 1). Bounded by the invariant layer and by printing the number; **not removable at any
reachable n**. `[reasoned]` on `[measured]` inputs

**G-E is open**: the cost layer prices at the `MAX_TOKENS` **ceiling** and says so; whether the
optimizer should optimize ceiling or realised cost is undecided. **G-G is open and load-bearing**:
nothing prunes `test/gate/baseline-*.json`, and R78 showed R18-class detection depends on that store
(VP22). `[reasoned]`

---

<a id="VP21"></a>
## VP21 — The gate cannot see grounding, and that was measured rather than assumed

**The checkpoint** (R77, 2026-08-29). 15 rows × 2 arms × 3 draws = **90 generations + 90 judge
calls**, `models/gemini-3.5-flash-lite` on the **metered production path** via the A6 pointer, config
`f2a6708c8f54`, corpus `515c707a5d69`, theme `Ambiguity`. **Branch B — the outcome predicted in
writing before the run.** `[measured]` · `[baseline: the same run's grounded arm]`

**Arm choice, recorded because the rule required it.** The metered production path, not local
llama-server: a $0 result on a path this project stopped serving at CP2 would be a finding about a
different system. Theme `Ambiguity` because the frozen run scored it **highest** on groundedness
(3.211), so it carries the most headroom for an ungrounding effect — **if the contrast is invisible
there, it is invisible anywhere.**

**Measured**, paired on cell id, 15/15 cells scored in both arms, 0 failed, 0 generation errors,
0 judge errors:

| metric | grounded | no-RAG | delta | MDE | verdict | tier |
|---|---|---|---|---|---|---|
| source_leak | 0.000 | 0.000 | **0.0000** | — | NOT-DETECTED | **1b (GATE)** |
| assistant_tell | 0.000 | 0.000 | **0.0000** | — | NOT-DETECTED | **1b (GATE)** |
| groundedness | 4.022 | 1.000 | −3.0221 | 0.5579 | DETECTED | judge |
| persona_retention | 5.000 | 5.000 | 0.0000 | — | NOT-DETECTED | judge |
| fabrication | 0.644 | 1.000 | +0.3555 | 0.3124 | DETECTED | judge |

**Stripping every passage from the prompt — a certain, total regression — moves nothing the CI gate
can measure.** Retrieval Tier 1a cannot see it by construction (retrieval still runs; only the prompt
changes). Tier 1b's two mechanical metrics do not move at all. The judge detects it overwhelmingly:
groundedness falls to the floor at **5.4× its own MDE**.

**This is not a power problem, and that distinction is the finding.** `source_leak` and
`assistant_tell` are not *nearly* equal across arms — they are **identical zeros**. **No sample size
fixes a metric that does not respond.** The regexes measure persona *register*; grounding is a
different construct.

**Persona is independent of grounding.** 5.000 in both arms. **An answer invented wholesale from
parametric memory is indistinguishable from a grounded one on every axis available to CI, and
indistinguishable on persona even to the judge.**

**The measured incentive, and it is the line with the longest reach** `[measured]`:

```
grounded   prompt 1677.1 tok   completion 575.7   shadow $0.001942/answer
no-RAG     prompt   89.9 tok   completion 518.8   shadow $0.001324/answer  (-32%)
```

**Sending no passages is 32% cheaper and passes the gate clean.** The charter's warning that *"an
optimizer minimizing cost under an insensitive guard is structurally incentivized toward invisible
degradation"* was an argument when written. **It is now a measurement with a named degenerate
solution sitting inside the optimizer's declared search space.** Costs are shadow prices computed
from token counts × published rates (prompt 0.3, completion 2.5 per million); `metered: false`, the
free tier billed $0. Both numbers are real and must not be conflated.

**What branch B licenses, per the rule fixed before the evidence:** groundedness is recorded as
**Tier 2 — offline, non-blocking, never gate machinery**; the gate's caption **permanently excludes
grounding** (it may say invariants hold, cost did not grow, retrieval not detectably worse — it may
never imply answers were checked for grounding); and this stands as **residual risk 2**.

**What it forbids:** any gate caption, promotion rationale, or optimizer claim implying grounding was
checked. **The gate is a retrieval-and-cost regression tripwire. It is not, and after this
measurement cannot be described as, an answer-quality gate.**

**The dry run over-alarmed, and that is recorded because every other small sample in this project
under-alarmed.** A 2-row dry run scored the grounded arm at groundedness 1.5 and prompted a
precondition to be added to the decision rule before the sized run; at n=15 the grounded arm scored
**4.022**. The precondition cost nothing and remains in the rule — **a rule written before the
evidence cannot be retrofitted, which is the entire point of writing it first.** `[measured]`

**G-C closed by the same finding one layer down** (R76) `[measured]` + `[computed]`, recomputed over
v1's 72-row A/B transcript by the harness rather than by hand, 36 paired cells:

| axis | n | delta | MDE | r | pairs? | judge-dependent |
|---|---|---|---|---|---|---|
| persona_retention | 36 | +0.1111 | 1.1327 | −0.059 | no | yes |
| groundedness | 36 | +0.0833 | 0.5376 | +0.625 | yes | yes |
| fabrication | 36 | +0.0278 | 0.2987 | +0.167 | no | yes |
| **source_leak** | 36 | 0.0000 | **0.2931** | −0.037 | no | **NO** |
| **assistant_tell** | 36 | +0.0556 | **0.1904** | −0.086 | no | **NO** |

`source_leak` and `assistant_tell` are the only 1b metrics A7 does not touch — deterministic regexes,
no judge — so they were the plausible candidates for a blocking quality metric. **Their MDEs are
0.293 and 0.190: a 19-29 percentage-point move is required before either is callable.** Against the
base rates those regexes actually see (25.0% leak, 8.3% tell after tightening), **the assistant-tell
rate could double without clearing its own MDE.** `quality_1b` stays non-blocking, now for a measured
reason. `[baseline: v1 recorded]` `[E24 (v1)]`

**The pairing diagnostic is the reusable half.** Only groundedness pairs (+0.625). **For four of five
quality axes, n is the wrong lever** — query difficulty is not a shared property, so pairing cancels
nothing and more rows buy far less than the naive calculation suggests. Now printed on every run as
`pairs_usefully` rather than being rediscovered. **Floor, stated so the number is not over-read:** a
judge-axis MDE cannot go below the judge's own test-retest variance (VP3), so where these MDEs are
large the **instrument** is the binding constraint, not the sample size.

**A defect found on the way, and it is the same class as R74's one layer up.** Every judge MDE this
project had published was computed **by hand, outside any script**. `scripts/eval_quality.py`
contained no MDE, no `sd_diff`, no correlation. The paired data was always there; the summary threw
it away. **The analysis lived in prose, so no run reproduced it.** Closing G-C cost **zero API calls
and zero USD** — arithmetic over data already paid for. `[measured]`

---

<a id="VP22"></a>
## VP22 — Calibration on five synthesized defects: the invariant layer does the work

**R78**, 2026-08-29, five gate runs, all local, **$0**, no provider calls. **Provisional at n = 5** —
five defects is a demonstration that each layer fires, not an error-rate estimate.
`[measured]` · `[baseline: none — first calibration]`

**Why synthesized rather than replayed.** The charter named v1's ablation arms as labelled history.
VP2 withdrew them as noise readings, so they carry **no true labels**, and replaying them would
calibrate the gate against random assignment. **The only ground-truth regressions this project has
ever had are R18 and R19, both invariant-class.**

| # | defect | injected as | disposition | what fired |
|---|---|---|---|---|
| D1 | cross-process nondeterminism (R18 shape) | shuffled entity-resolution order via `sitecustomize` on `PYTHONPATH` | **blocks** | `determinism_vs_baseline` |
| D2 | route-table corruption (R19 shape) | every route forced to DEFAULT | **blocks** | `route_match_prod`, `route_match_cand`, `determinism_vs_baseline` |
| D3 | no-RAG arm | VP21's run, not re-run | **does not block** | **nothing in Tier 1** |
| D4 | degraded retrieval, `prefetch_limit` 50 → 1 | config artifact | **blocks** | `recall_at_5` BREAK, 9 flips, delta −0.2105 vs MDE 0.2084 |
| D5 | the null — candidate byte-identical to production | config artifact | **does not block** | nothing; 0 flips, MDE undefined |

**All five matched the prediction written before the run.**

**Three things the table does not say on its face:**

1. **D4 had to be made brutal before the sampled layer noticed.** The first attempt cut
   `prefetch_limit` 50 → 3 and moved **one** row, in the *positive* direction — `does_not_block`.
   Only at `prefetch_limit = 1`, starving both legs to a single candidate each, did it break, and it
   **cleared the threshold by 0.002** (delta −0.2105 against MDE 0.2084). **Destroying 8 net rows of
   38 barely registers.** VP2's finding felt from the other side: **the sampled layer is not a
   quality gate, it is a catastrophe detector.**
2. **D1 was caught by the baseline comparison, not by the within-run check.** `determinism_spread`
   compares the two arms of one run and saw nothing — both subprocesses were perturbed and their hit
   vectors happened to agree. `determinism_vs_baseline`, which compares production against its own
   recorded clean run, is what fired. **R18-class detection therefore depends on the baseline store**
   — the store **G-G** notes has no retention policy and nothing pruning it. G-G is load-bearing, not
   housekeeping.
3. **The first D1/D2 run produced a false negative that looked exactly like a real one.**
   `sitecustomize` runs before the repo root is on `sys.path`, so `import src` raised, Python
   reported it as a single swallowed warning line, and **both defects reported `invariants hold`**.
   Nothing errored; **the gate simply was not shown the defect.** Caught only by asking whether the
   patch had applied (`route.__name__`) rather than trusting the disposition. **A defect fixture that
   fails to inject is indistinguishable from a gate that misses.**

**No single "gate accuracy" number is computed.** Five defects, five verdicts, and a scalar would
merge classes whose detection stories are entirely different. `[reasoned]`

**Fixtures live in `test/gate/defects/`** and are applied **only** via `PYTHONPATH` for one
subprocess. `src/` is never modified: a defect introduced into the tree and forgotten is worse than
an uncalibrated gate. `[read from source]`

---

<a id="VP23"></a>
## VP23 — What a gate run costs, and why the scarce resource is time

**R79**, 2026-08-29, three consecutive runs, warm disk cache, API down. `[measured]` + `[computed]` ·
`[baseline: the plan's ~25 s estimate]`

```
gate run wall clock : 243.9 / 237.9 / 242.9 s   mean 241.6, spread 6.0 (2.5%)
gate run USD        : $0.00000 exactly -- local ONNX + local-mode Qdrant, zero API calls
```

**Decomposition** `[measured]`:

```
scripts/eval_retrieval.py --arm full      14.1 s
scripts/eval_retrieval.py --arm rerank   104.7 s      (7.4x)
dense + sparse model load                  1.25 s     (negligible)

gate = 2 x full + 2 x rerank = 237.6 s   ~= the 241.6 s measured end to end
```

**The plan estimated ~25 s. It is 241.6 s, and the gap is neither model loading (1.25 s) nor eval-set
size.** It is the **rerank bucket**: FlashRank scoring 25 candidates per query across 106 rows, on
CPU, once per arm. That bucket is **87% of the gate's runtime (209.4 s of 241.6 s) and it is
non-blocking** — it exists to make the serving path visible (VP24), not to decide anything.
Consistent with v1's own 0.3-0.5 s per query for 25 CPU pairs `[E28 (v1)]`; **nobody had multiplied
it by 106 rows and two arms.**

**Decided (operator's call, step 11): the rerank bucket moves to promotion time only** — the same
tier logic 1b already follows. A routine gate run went **~~241.6 s~~ → 29.9 s** (3 runs, 2.5%
spread), a **7.5×** throughput gain on the leg the optimizer pays per attempt. `[measured]` ·
`[baseline: v2 CP3's own 241.6 s]`

**The deferral is not free, and VP24 prices it in recall rather than in seconds.**

**Consequence for the optimizer.** The gate is paid on **every promotion attempt**. **The scarce
resource on the free leg is not money — it is throughput**: 100 candidate evaluations is ~6.7 hours
of wall clock before a single API call is made (~0.8 hours after the rerank move). The project had
been budgeting measurement in USD since CP2; **on this leg USD is exactly zero and time is the whole
constraint.** `[computed]`

**1b is unmeasured, not free.** `quality_1b` is an empty list pending G-B, so there is **no
promotion-time leg to time**. Using VP21's already-spent run as a proxy `[estimate]`:
$0.001942/answer shadow ⇒ a 15-row theme × 3 draws is **$0.0874** and the full 50-row set
**$0.2913**, judge calls additional and of the same order. **This is what a 1b leg *would* cost once
its composition is chosen, not a measurement of one.**

**Monthly projection** `[computed]` on a stated `[estimate]` — the rate is written down rather than
smuggled in:

```
[estimate] 10 gate runs/week, 1 promotion/week
[computed] 1a: 43 runs/month x $0                     = $0.00/month, ~0.4 machine-hours (post-move)
[computed] 1b: 4.3 promotions x 45 draws x $0.001942  = $0.38/month shadow, IF 1b were populated
```

**O8's actual question — does measurement spend exceed serving spend?** **For one real user, yes, by
a wide margin.** At CP2's $0.001473/answer, a single 45-draw promotion check costs the equivalent of
**~59 served answers**. **Measurement dominates serving for a solo deploy**, which is an argument for
the free 1a leg carrying as much of the gate's work as it can, and against adding further metered
checks to the routine path. `[computed]`

**Three bounds the gate hands downstream, all measured**: an ungrounded answer is 32% cheaper and
passes clean (VP21); no quality improvement is detectable at any reachable n (VP2); and on the free
leg the scarce resource is **time, not money**.

---

<a id="VP24"></a>
## VP24 — What the gate's own retrieval leg does not exercise

**R75 — the gate never exercises the rerank stage, and it was found by accident** `[measured]`,
2026-08-29. A candidate config differing only in `rerank_enabled: False` was gated to exercise the
continuous-metric path. It moved **cost by +109.1%** and **not a single rank**: `mrr` delta 0.0,
`dchit` delta 0.0, **zero flips**.

**Cause.** `scripts/eval_retrieval.py:121` — `ARMS["full"]` carries `rerank_query: None`, so the arm
the gate runs scores `fused[:5]` and the ranker is **never even loaded**. Production serving *does*
rerank (`src/api/app.py:465`, every non-STRUCTURAL request).

**This is R19's finding in a new place** (VP4). The consequence is narrower — rerank reorders within
an already-retrieved candidate set, so recall@5 over `fused[:5]` is not meaningless — but **any config
knob on the rerank stage (`rerank_enabled`, `rerank_model`, `top_k_rerank`) is invisible to the
gate's quality leg while being visible to its cost leg**, which is the worst combination: it can be
blocked for costing more while its quality effect is unmeasurable. Rerank knobs join `TOP_K_FUSED`
(invisible at k=5) and VP2's retrieval knobs on the list of things this instrument cannot score.
**The searchable-and-measurable space keeps shrinking**, which is evidence for the cost-objective
reframe rather than against it.

**R81 — recall@5 hides two different failures with opposite remedies** `[measured]`, 2026-08-30,
4 arms × 121 rows, local, $0, ~51 s. `scripts/eval_retrieval.py` scored `fused[:5]` and threw the
remaining 20 candidates away — but `rerank()` is handed the whole `TOP_K_FUSED` pool, so **recall@5
is neither an upper nor a lower bound** on what the served top-3 contains. A target at pool rank 12 is
a recall@5 miss the reranker can still serve.

- **pool miss** — target absent from the fused pool. A **retrieval** failure. No reranker, no fusion
  weight, no expansion rule recovers it. The true ceiling.
- **pool hit below k** — an **ordering** failure, and precisely the headroom a reranker exists to
  convert.

| arm | family | n | recall@5 | pool recall | headroom | pool misses |
|---|---|---|---|---|---|---|
| full | primary | 38 | 0.7105 | **0.9211** | **0.2105** | 3 |
| full | quotation | 15 | 0.8667 | 0.9333 | 0.0667 | 1 |
| dense-only | primary | 38 | 0.7368 | 0.8947 | 0.1579 | 4 |
| dense-only | quotation | 15 | 0.4667 | 0.6667 | 0.2000 | 5 |
| sparse-only | primary | 38 | 0.6842 | 0.8684 | 0.1842 | 5 |
| sparse-only | quotation | 15 | 0.8000 | 0.9333 | 0.1333 | 1 |
| no-expansion | primary | 38 | 0.7105 | 0.8684 | 0.1579 | 5 |
| no-expansion | quotation | 15 | 0.9333 | **1.0000** | 0.0667 | 0 |

The recall@5 column reproduces R80 bit-for-bit, which is the provenance check that nothing else
moved. `structural` is vacuous here by construction (pool ≤ 2 under
`TOP_K_STRUCTURAL_EXCERPTS`).

**Finding 1 — the pool is NOT saturated, contrary to the prior.** The working assumption was that
pool recall would come back 1.0, since each row has 1-3 target chunks and the pool holds 25. Primary
pool recall is **0.9211**, and `r-0032`/`r-0044` are pool misses in **every arm** — structurally
unreachable at `PREFETCH_LIMIT=50`. **No downstream stage can serve them**; they are a
prefetch/indexing question, not a ranking one. `r-0018` is a pool miss under `full` and a pool hit at
rank 25 under `dense-only`, so **the BM25 leg displaces it out of the pool entirely.**

**Finding 2 — VP23's deferral now has a price in recall, not only in seconds.** Primary headroom is
**0.2105** — eight rows sitting in the pool below the cut (pool ranks 24, 10, 16, 6, 18, 16, 10, 8).
**That is the band of recall movement CI is blind to while rerank runs only at promotion.** The
decision stands; the argument for it is now quantitative.

**Decision — pool recall is REPORTED, never blocking; `recall@5` keeps the blocking role.** Three
reasons in weight order `[reasoned]`: **power** (pool recall saturates by construction, so a paired
non-inferiority test on it produces almost no discordant pairs — a gate that structurally cannot
fire); **coverage** (pool recall sees prefetch membership and nothing else, so every defect that
reorders without changing the set goes NOT-DETECTED at every layer — D4 was a membership defect and
would still be caught, the ordering half of the defect space would not); and **break** (swapping the
blocking metric is a measurement break of the `MAX_TOKENS` class, invalidating 0.7105 and every
ablation number in the registers).

**Pool recall is still not wired into the gate.** `scripts/eval_retrieval.py:166` computes it;
`src/rag/gate.py` never reads it — and it is precisely the guard the optimizer's audit specified for
`top_k_fused`. Wiring it is a **CP3 change**, routed and not done. `[read from source]`

**The rerank knobs' blindness has a second instance, from P7 and authoritative here.** Entity
expansion's apparent wash is **`recall@5`-only**: on **pool recall** the arms split **35/38 vs
33/38** with `n_required ≈ 300`, not infinite (R143). **The effect exists; the blocking metric cannot
see it** — the same blindness R75 records for the rerank knobs. `[measured]`

---

<a id="VP25"></a>
## VP25 — The first callable retrieval effect this project produced, and why five prior measurements missed it

**R73 — a census, not a sample** `[measured]`, all 40 primary rows:

```
queries containing a quotation mark:      0 / 40
queries containing >=1 capitalised name: 40 / 40
median query length:                     10.5 words
```

**Lexical retrieval's distinct advantage is exact matching of rare literal strings, and the primary
set contains none.** The lexical signal actually available is proper nouns — present in all 40, and
the same tokens `EntityMap.resolve()` already injects and dense retrieval handles without help.
**The BM25 leg was being evaluated on the query distribution least able to distinguish it. That is a
property of the instrument, not of BM25.**

**R82 — restated on the current 121-row set, and still exactly zero** `[measured]`, family assignment
via `family_of()` (the derivation path, not the declared key); literal = a double-quoted span ≥ 8
characters:

| family | n | retrieval-scored | literal-bearing |
|---|---|---|---|
| primary | 38 | yes | **0/38 = 0.0000** |
| quotation | 15 | yes | 15/15 = 1.0000 |
| structural | 12 | yes | 0/12 |
| book_named_no_chapter | 10 | yes | 0/10 |
| negative_control | 2 | yes | 0/2 |
| sensitive | 19 | no | 0/19 |
| adversarial | 25 | no | 0/25 |
| **TOTAL** | **121** | | **15/121 = 0.1240** |

**Every literal in the corpus sits in the one family authored to carry them, and in no other.** The
corpus-wide 0.1240 exists only because R73 predicted the gap and R80 filled it deliberately.

**R80 — the measurement** `[measured]`, 2026-08-29, 4 arms × 121 rows, local, $0, ~60 s. 15 rows
authored as a new `quotation` family (`r-0107..r-0121`, 3 per novel across all five), each query
embedding a **verbatim span extracted from the indexed chunk of its own target chapter**, so the
literal is guaranteed present and the target is exact by construction. Rows carry an explicit
`family` key, so the headline denominator is untouched: `primary` stays at 38.
`[baseline: v2 CP0's own four-arm ablation]`

```
family: quotation (n=15)              family: primary (n=38)
  full          0.8667 (13/15)          full          0.7105 (27/38)
  dense-only    0.4667  (7/15)          dense-only    0.7368 (28/38)
  sparse-only   0.8000 (12/15)          sparse-only   0.6842 (26/38)
  no-expansion  0.9333 (14/15)          no-expansion  0.7105 (27/38)

  full vs dense-only:  b=6  c=0  delta -0.4000  MDE 0.4572  -> BREAK
  all six primary comparisons: NOT-DETECTED, unchanged
```

**Removing the BM25 leg costs 6 of 15 quotation rows and gains none.** The exact binomial on a 6-0
discordant split rejects at **p = 0.0156**. **This is the first ablation arm in this project's
history to produce a statistically callable retrieval effect**; every prior comparison — v1's six
arms, R20, R72 — sat inside its own noise band.

**R20's growth rule is vindicated concretely: 15 authored rows produced a callable effect that 900
more rows of the existing composition provably could not.** `[reasoned]`

**A note on the gate's own verdict ordering, worth carrying.** `full vs dense-only` returned **BREAK
while |delta| (0.400) sits below the MDE (0.4572)**. Not a contradiction: the MDE is a two-sided
design quantity for an effect of unknown sign, while the exact binomial conditions on the discordant
pairs actually observed, and a 6-0 split is lopsided enough to reject regardless. **The gate's
ordering — BREAK evaluated before the MDE comparison — is what produced the right answer.** Had the
MDE alone governed, the first real retrieval effect this project measured would have been reported as
NOT-DETECTED. `[read from source]` + `[measured]`

**R81 splits the effect, and the split strengthens it** `[computed]`:

```
pool recall delta   0.9333 - 0.6667 = 0.2667   retrieval — unrecoverable downstream
remainder                             0.1333   ordering  — a reranker could win it
```

Four rows are **absent from the dense-only pool entirely**. **BM25 is not merely ranking literal
chunks higher; it is reaching chunks the dense leg never retrieves**, so a cross-encoder cannot
substitute for the lexical leg on these queries — it never sees the chunk. **Note the direction of
the correction: the cheaper reading (BM25 as a reranking effect a stronger reranker would absorb) is
the one the data rules out.**

**Two secondary readings, both direction rather than effect** `[measured]`: `sparse-only` (0.8000)
nearly matches `full` (0.8667) — with a literal present, BM25 alone does most of the work; and
`no-expansion` (0.9333, pool recall **1.0000**) is the **best** arm here, above `full` (b=0, c=1, not
detected at n=15). **Entity expansion appears to mildly dilute an exact literal match**, seen at both
the ranking layer and the retrieval layer — but **two independent views of one row is not
significance.**

**The honest limit, stated in the finding itself.** These rows are **synthetic and maximally
favourable to BM25 by construction** — a verbatim quote is the ideal lexical input. The finding is
**"BM25 acts when literals are present"**, NOT **"production traffic needs BM25"**. Whether real
traffic contains literals is unknown and unknowable today: v2 has no real users and its query
distribution is synthetic by construction (D3/O10). **That makes residual risk 4 concrete** — the
harness's synthetic query distribution *decides, in advance and by default, whether this retrieval
component can ever demonstrate value again.** `[reasoned]`

**The unfixed corollary.** The query composition of this set **was never chosen; it accreted.** Real
users quote. If production traffic contains quotation and the eval set never does, **every retrieval
number this project reports is computed off-distribution.** Unresolved, and unresolvable inside v2.

---

<a id="VP26"></a>
## VP26 — The synthetic session corpus: pre-registered, unvalidated by choice, re-frozen twice

**CP-4a closed 2026-08-30 (R91). This is a `[decided]` entry, not a `[measured]` one** — the evidence
it rests on was already measured (VP25's per-family literal counts, the callable BM25 effect, its
retrieval/ordering split); what CP-4a adds is a **choice**, and the register records it as one. **No
number in the decision is an observation about the corpus; the corpus did not exist yet.**

**(a) Provenance and validation — option (iv): explicitly unvalidated, every dependent claim
labelled.** Production method (a different question from validation): sessions are **authored from
the indexed corpus and by hand**, the same construction VP25 used — a verbatim span extracted from
the indexed chunk of its own target chapter, so the literal is guaranteed present and the target is
exact by construction. Multi-turn scaffolding hand-authored. **No model generation pass**, so O8
gains no generation-cost row. **Validation: none, by choice.** There is no real traffic to compare
against — that is D3, not a gap — so every available check compares synthetic to synthetic.
**Manufacturing a validity number here would produce something that looks like evidence and is not.**
`[reasoned]`

**(b) Composition, pre-registered with counts** `[decided]`:

| family | sessions | turns/session | literal-bearing | exists to exercise |
|---|---|---|---|---|
| `passage_paste` | 12 | 2-3 | **yes** | a pasted passage asked about naively — long query (2-5× anything in the existing set), off-distribution for the embedder, dense in entity surface forms |
| `locate` | 8 | 2-3 | **yes** | the `quotation` shape carried to multi-turn: short literal, explicit locate intent |
| `thematic` | 10 | 3-4 | no | **the zero-literal control.** Without it the literal families measure nothing |
| `structural` | 5 | 2 | no | the STRUCTURAL route, which bypasses hybrid retrieval entirely |
| `refusal` | 5 | 2-3 | no | guard behaviour under session context; not retrieval-scored |
| **total** | **40** | ~3 avg | **20/40 = 0.50** | |

**Why few families rather than many.** Each family carries its own denominator, and **a denominator
too small to be callable is a family that cannot produce a finding.** Spreading 40 sessions across
seven or eight families would buy eight underpowered denominators.

**The 50% is a measurability choice and is indefensible as a realism claim.** Nobody knows what
fraction of real traffic is pasted passages, and 50% is very likely too high. **This is admissible
only because (a) is option (iv).** What makes it safe rather than merely labelled is the
**per-family denominator**: every number is reported per family and never pooled, so a later
reweighting is *arithmetic on numbers already in hand*, not a regeneration.

**(c) Repeat rate — two points, both parameters: 0.0 and 0.30.** 0.0 yields the cache's floor value
at no extra cost; 0.30 (~36 of ~120 turns) is high enough that the hit rate is not sampling error,
where 0.10's ~12 repeat turns would be noise. **Not a sweep**, deliberately.

**The label, verbatim, travelling with every dependent claim** — this is the O10 qualifier made
concrete, and it is carried **inside the artifact** (`corpus["label"]`), not only in a findings file,
with `test/test_session_corpus.py` asserting it is present. **A label that lives only in a findings
file is one a downstream consumer never sees.**

> This number is derived from a harness-authored synthetic session corpus (R91). Its resemblance to
> real usage is **unproven and was not tested**: no real traffic exists to compare against (D3), and
> the corpus was declared unvalidated by choice rather than validated by a check. Its composition is
> a pre-registered design parameter — 40 sessions across five families, 50% literal-bearing — chosen
> for measurability, not for realism, and its cache repeat rate is a dial set to {0.0 | 0.30}, not an
> observation. Per-family figures are reported separately so any reweighting is arithmetic rather
> than a regeneration.

**What this corpus cannot support** `[reasoned]`: any claim that the traffic mix resembles real
usage; any cache-hit-rate or cache-USD claim without the repeat-rate qualifier attached verbatim; and
**a statistical quality comparison at n=10-12 per family** — the MDE wall is VP2's and no growth at
this scale clears it.

**Residual risk carried, and it is a thumb on the instrument unless disclosed.** The composition was
chosen partly *because* it makes the lexical leg measurable. **If a future reader sees a pooled
headline computed across these five families, the safeguard has been lost and the number should not
be trusted.**

**Frozen** (R92) `[measured]`, 2026-08-30, three dry-run cycles plus three full drafts, local, $0, no
provider call and no server. `test/session_corpus.json`, version **`4155c97b0a2b`** (content hash),
**40 sessions / 108 turns**, ids `s-0001..s-0040`, committed under `test/` like every other eval
artifact:

```
family           sessions  turns  books covered  literal
passage_paste          12     30            5/5      yes
locate                  8     22            5/5      yes
thematic               10     33            5/5      no
structural              5     10            4/5      no
refusal                 5     13            n/a      no
total                  40    108                  20/40 = 0.50
```

Matches the pre-registration exactly, and `scripts/freeze_sessions.py` **refuses to freeze** a
composition that drifts from it — **the pre-registration is enforced rather than trusted.** Spans
come from real indexed chunks: the drafter imports `src.rag.ingest.chunk_units` at the live
`CHUNK_TOKENS=500` / `CHUNK_OVERLAP=100` and re-derives the same **3284** chunks the retriever sees,
so a literal it emits is provably inside the chunk that will be retrieved for it. No Qdrant, so no
`data/vector_db/` lock was taken.

**Five defects, all caught by dry runs before generation** `[measured]` — each would have been
invisible in a run that "completed successfully":

1. **Thematic subjects were clauses, not subjects.** A `split(",")` heuristic produced *"What is
   going on with The narrator addresses an audience of gentlemen to argue that physical suffering at
   this point in Notes from the Underground?"* Fixed by matching against `entities.json`'s 262
   canonical names.
2. **Every literal span began mid-sentence.** A chunk is a token window with overlap, so its first
   sentence is nearly always a fragment. Fixed by dropping each chunk's leading fragment and
   requiring a capital start.
3. **Three of five families had zero Crime and Punishment sessions**, and Notes from the Underground
   appeared once in 40 — chunks were walked in one global shuffled order, so the two longest novels
   crowded the rest out. Fixed with per-book round-robin; coverage is now 5/5 for the three families
   that carry effects.
4. **Every session had exactly two turns** — 80 against the pre-registered ~120 — because the count
   was hardcoded rather than drawn from the per-family range. Fixed; 108 turns, each family inside
   its band.
5. **A gendered-pronoun bug.** A template read *"Why does {subject} act the way **he** does"*, and
   subjects are drawn from an entity list containing Liza, Sonia, Dunya, Grushenka and Nastasya.
   **The corpus would have misgendered characters in its own query text.**

**The repeat rate is a replay-time dial, not baked in** — baking either point in would mean two
corpora and two version stamps. `expand_with_repeats()` applies it, and the test asserts rate 0.0
yields exactly zero repeats and rate 0.30 yields a mean of **0.304** over 20 seeds. **A parameter
nobody checks is a parameter that drifts.** `[measured]`

**Re-frozen twice, and numbers are NOT comparable across a stamp** `[measured]` ·
**P7-authoritative**:

```
4155c97b0a2b  ->  49b525f2a131  ->  be54610a7f78
```

The re-freezes came with the optimizer's anaphora work. **Every number in this log derived from the
session corpus is stamped `4155c97b0a2b`** and must not be spliced with a later-stamped figure. A
third re-freeze was **priced and refused** — the price and the refusal belong to
`EVIDENCE-OPTIMIZER.md`.

---

<a id="VP27"></a>
## VP27 — The concurrency knee: 1, then 4

**R98, Batch C-run**, 2026-08-30, live against `uvicorn` on this machine. **Spend: 21 draws, $0.0344
list-rate shadow, 4.2% of the 500/day `rpd`** — under the R96 budget, because completions ran
~450-600 tokens against the `MAX_TOKENS=1024` ceiling that budget priced at (G-E, ceiling vs
realised). Artifacts each carry their own `/ready` provenance block captured **before** any traffic,
so no curve can later be mistaken for one measured on a different arm. `[measured]` · O10 applies

```
FAKE arm (CACHE_ENABLED=false), 20 requests per level
 conc     rps    p50 total    wait p50    429   502   census
    1   0.282       2080 ms         0 ms  0.00  0.00   200x20
    2   0.282       4182 ms      2102 ms  0.00  0.00   200x20
    4   0.280       8746 ms      6666 ms  0.00  0.00   200x20
    8   0.203      36191 ms     34111 ms  0.00  0.00   200x20
   16   0.282      51054 ms     48974 ms  0.00  0.00   200x20

METERED arm (models/gemini-3.5-flash-lite, CACHE_ENABLED=false), 5 per level
 conc     rps    p50 total   p50 ttft    wait p50    429   502
    1   0.163       6227 ms    3372 ms         0 ms  0.00  0.00
    2   0.156      12853 ms    9863 ms      6626 ms  0.00  0.00
    4   0.158      19305 ms   15398 ms     13078 ms  0.00  0.00
```

**`knee_concurrency = 1` on both arms.** Throughput is flat across every level while latency scales
linearly (metered 1× / 2.06× / 3.10×). **Adding concurrency buys zero throughput and costs
proportional latency.** The inferred semaphore wait is almost all of it — 13,078 of 19,305 ms at
concurrency 4; 48,974 of 51,054 ms at 16.

**Measured on the fake arm too, where generation is a no-op: the lock is the wall even on a server
that is not generating.** `[baseline: v1 recorded]` `[E20 (v1)]` — v1's `asyncio.Semaphore(1)` was
justified by `llama-server --parallel 1` and non-re-entrant CPU models; **generation has left the box,
so the lock now binds on retrieval alone and that justification is gone, measurably.**

**The two arms agreeing is the cross-check the fake-arm argument needed.** Same knee, same flat-rps
shape, same zero-rejection behaviour; the only difference is service time (2.1 s retrieval vs 6.2 s
retrieval-plus-generation). **A fake ramp does not distort the conclusion**, so future ramps can run
at zero spend without caveat.

**The premise that broke, and it reframed the whole decision** (R100) `[measured]`, source census.
`/chat` is `async def` and **every retrieval call is plain sync** — `retrieve(...)` at `app.py:539`
and `:549`, no `await`. Grep for `to_thread`, `run_in_executor`, `ThreadPoolExecutor` across `src/`
(harness excluded): **no matches.** Between the semaphore acquire and the first generation await the
only `await` is `_dependency_status`, which on the hosted path contains no await at all. uvicorn runs
one worker.

> **Two retrievals can never interleave, and the mutual exclusion comes from the event loop, not from
> `Semaphore(1)`.** The semaphore only ever stopped *generation* overlapping.

**This inverts how the corruption check must be read** — see VP32.

**C8's disposal: option (i), narrowed to its first half.** The lock survives, scoped to
route → cache → retrieval → context, **released before `build_messages`** so generation and the SSE
stream run outside it, and becomes `asyncio.BoundedSemaphore(1)`. **No new generation bound is
built** — per-provider bounding already exists in the limiter (which *sleeps* rather than rejects),
and a per-tenant bound is incoherent before tenants exist. Option (ii) (a higher N) was rejected on
**hardware fit**: the global lock is a 6GB-GPU artifact whose constraint has left the request path,
and a higher N would be a number with no evidence behind it. **(i) versus (iii) was NOT decided by
the priority order** — given the event-loop finding they are *measurably identical today*, and the
tie broke on cost asymmetry: keeping a redundant guard costs one semaphore, while removing it costs a
thread-safety audit nobody has done. `[decided]`

**The acquire site does not move.** A cache hit still acquires the semaphore, but now waits behind at
most one *retrieval* (~2.1 s) instead of a full *generation* (~6.2 s metered, ~21.3 s in v1
`[E28 (v1)]`). Moving the cache lookup above the acquire is recorded as an unbuilt variant with a
named trigger, not adopted.

**R101, Batch D-run**, 2026-08-30. **Spend: 17 metered draws (2 dry + 15), $0.02516**, plus a free
fake-arm control. Cumulative CP4 spend: **38 draws, ~$0.060, 7.6% of `rpd`**. `[measured]` ·
`[baseline: v2 CP4's own pre-change curve]`

```
METERED ARM  (CACHE_ENABLED=false, gemini-3.5-flash-lite, 5 requests per level)
 conc |  rps before   after      x |  p50 before    after    delta
    1 |       0.163   0.156   0.96 |      6227 ms  6456 ms   +3.7%
    2 |       0.156   0.222   1.42 |     12853 ms  8395 ms  -34.7%
    4 |       0.158   0.274   1.74 |     19305 ms 14185 ms  -26.5%

knee_concurrency: 1 -> 4          15/15 status 200      0 divergent chunk_ids
```

**The prediction was written before the run: at concurrency 4, rps ≥ 0.22 AND p50 ≤ 13,500 ms.**

- **rps 0.274 — PASS**, 1.74× against a 1.4× target.
- **p50 14,185 ms — MISS**, by 5%; the improvement was 26.5%, not the predicted 30%.

**The criterion was a conjunction and half of it missed. That is recorded, not softened.** What did
*not* happen is the disposal condition — *"if it comes back flat, take option (ii) with that as its
recorded reason."* **It did not come back flat: the knee moved from 1 to 4**, which is the qualitative
change the decision was about. **Option (i) stands, with the partial miss on the record.**

**Concurrency 1 is unchanged (0.96×, +3.7%)** — the confirmation that the mechanism is what it was
claimed to be: with one request in flight there is nothing to overlap, so nothing should move, and
nothing did.

**A measurement caveat that matters more than the miss.** **0.274 rps exceeds the stated sustainable
ceiling of 0.25** (`rpm 15` = 15/60 s). Not a contradiction: 5 requests in 18.2 s never fills the
limiter's 60-second sliding window, so `RateLimiter` never engaged. **The measured figure is a burst
rate and is not sustainable**; a run long enough to fill the window would clamp to ~0.25. **And
n=5 per level — these are five-sample medians, not distributions.** Both qualifiers travel with the
throughput figure and must not be dropped.

**The control starved, and its not-improving is what makes the gain attributable** `[measured]`:

```
FAKE ARM      conc 4:  rps 0.280 -> 0.227 (0.81x)      p50 8746 -> 4153 ms
              distribution went BIMODAL: median (ex-max) 8724 -> 2377 ms,
              while several requests landed at 55-64 s; mean 12419 -> 12872 ms
              outliers sit MID-RUN (indices 7, 13, 15, 17), so not cold start
METERED ARM   conc 4:  spread 4.3x -> 2.2x, mean 17679 -> 12155, max 25726 -> 14185
```

**The two arms moved in opposite directions on tail behaviour**, and the explanation is structural:
on the fake arm generation is instant, so the lock is held for ~100% of the request and releasing it
before an instant generation buys nothing while adding scheduling churn under burst arrival. On the
metered arm the lock covers ~2.1 s of a ~6.3 s request, so there is genuine overlap and the queue
drains smoothly.

**This inverts the control's expected reading and strengthens the finding.** The control was framed
as *"the fake arm should be unchanged"*. It was not — **but it did not gain throughput**, and that is
precisely what makes the metered gain attributable to overlapping generation rather than to the
refactor in general. **Had both arms improved equally, the mechanism claim would have been
unsupported.** For this run **the fake arm is the control, not the instrument** — the whole effect is
*overlapping generation*, which the fake arm does not have, so a fake-only run would have looked
identical to a failed decision.

**Recorded as a real limitation, not explained away:** under any workload where retrieval dominates
the request, this change **can produce an unfair latency distribution** — most requests fast, a few
starved. The metered arm does not exhibit it. **CP5 inherits it: fairness becomes a property to check
rather than assume.**

---

<a id="VP28"></a>
## VP28 — The system never rejects: an unbounded queue, measured

**R98 §2** `[measured]` · O10 applies · `[baseline: v1 recorded]` `[E20 (v1)]` — v1 recorded
qualitatively that *"concurrent submits queue; they do not fail"*; this is the first time it was
measured.

```
local_429_rate    = 0.00   at EVERY level, on BOTH arms
provider_502_rate = 0.00   at EVERY level, on BOTH arms
p99_degrades_without_error = True  at EVERY level
```

**The system never rejects anything. It queues silently and unboundedly, reaching 62 s p99 at a 100%
success rate.** That is not a defect this phase fixed — the queue mechanism is CP5's — **it is the
number CP5 designs its bound against**, and it means CP5 had a measurement rather than an assumption
to design to.

**Queue depth itself remains unobservable** — see VP31 — and is emitted as an explicit
unobservable marker, **never guessed**.

---

<a id="VP29"></a>
## VP29 — Fault injection: the taxonomy's honest denominator is 1

**The census that set the ceiling** (R87) `[measured]`, over the registers plus a source read of the
gateway and request path. For each fault class the harness must inject: has this project ever seen a
genuine occurrence, and what did the client see?

| fault class | verdict | client-visible signature | `Retry-After` |
|---|---|---|---|
| provider timeout | **no genuine occurrence** | never observed for a hosted provider; the nearest real event is a **503** from the *local* llama-server — a connect refusal, not a timeout | absent *(inferred)* |
| **provider 429** | **signature recorded** | **242 real Groq 429s**, all absorbed by 84 transport retries. Observed *below* the API boundary — **no 429 has ever traversed `/chat`** | **present on Groq, every one**; **absent on Google** (no rate-limit headers at all) |
| cold cache | **recorded — a miss, not a fault** | 200, normal SSE. Miss TTFT 7.790 s / total 17.4 s; hit 0.002 s | n/a |
| budget exhaustion | **split** | provider-side **has** happened (Groq `tpd`) but arrived **wearing the 429 row's signature exactly**. App-side spend guard has **never fired**; designed status 503 | absent by design |

**Only one of four classes has a genuine, recorded client signature, and even that one was observed
inside `stream_answer()` rather than at the API boundary.** **Whatever the injector produces for the
other three is a guess about provider behaviour, not a reproduction of an observed one.** That is not
a reason to skip them — **it is the fidelity cost the checkpoint had to price**, and it is now a fact
rather than an assumption.

**Three ambiguities the record cannot settle** `[reasoned]`: Google's in-band `error` frame is
classified as `TransportError` while the provider survey calls the same wire event in-band, and the
survey rows are **documentation-derived, not observed**; **any fault after the first token has no
status code at all** — once the 200 is committed, `TransportError` and `ContentRefused` become a
terminal SSE `error` event, so the same fault has two client signatures depending on which side of
first-token it lands, and the 242 recorded 429s were all retried *before* first token **only because
the retry window closes there by construction**; and `ContentRefused` (Google 200-with-refusal → 422)
**has never genuinely occurred** — the refusal code space is a doc survey, and R57 measured the
*schema*, not a real block.

**One structural note.** The only 429 the API can emit comes from `QuotaExhausted` — the **local**
limiter — and no finding records it firing live. **A provider 429 surfaces as 502.** Injections
producing 429 and injections producing 502 exercise **disjoint code paths**. `[read from source]`

**CP-4b closed (R93): option (b), a deployment-flagged gateway wrapper.** One `FaultProvider` below
`guarded_generate()`, selected in `_provider_for()`, behind a single `Config.FAULT_INJECT` env var.
Decided by **goal fit** over **not-overengineered**: the smaller diff structurally cannot produce the
phase's own Verify row. The client-side option was rejected on the operator's reason first — *"since
we're an API gateway we need to wrap it up"* — which survived testing: **Groq's 429 carries
`retry-after` and Google's carries none**, so a client-derived taxonomy is provider-conditional and
does not survive a `SERVING_MODEL` change. `[decided]`

**A premise the schedule got wrong, and correcting it is what made (b) cheap** `[read from source]`.
The schedule priced (b) as *"a versioned field and therefore a config version"*, engaging the
do-not-pool rule. **False.** The project's own split is that a field is versioned iff changing it
changes **what a correct answer is**; a fault toggle changes what the system does under failure, not
what a correct answer says, so it is a **deployment** field — the same class `EMPTY_RETRIES`,
`MAX_GENERATION_ATTEMPTS`, `SPEND_RATE_MAX_USD_PER_MINUTE` and `CACHE_ENABLED` already sit on.
**No `config_version` churn.**

**Per class, and the denominator that results** `[decided]` on `[measured]` input:

| class | injection point | faithful? | in taxonomy | client observes |
|---|---|---|---|---|
| cold cache | none — the corpus repeat-rate dial | genuine sig. on record | **no — not a fault** | 200, `cache_hit: false`, elevated TTFT |
| **429, absorbed** (n < `MAX_GENERATION_ATTEMPTS`) | gateway | **yes** | **YES — the only row** | **200**, `ttft_ms` inflated by the honoured wait |
| 429, exhausting | gateway | no | fidelity unverified | 502 |
| provider timeout | gateway | no | fidelity unverified | 502, identical |
| in-band refusal | gateway | no | fidelity unverified | 422 |
| budget exhaustion, app-side | `SPEND_RATE_MAX_USD_PER_MINUTE=0.0` | no | fidelity unverified | 503, $0, does not walk the ladder |
| budget exhaustion, provider-side | — | subsumed | not a distinct row | wears the 429 signature |

**Applying the exclusion rule forces denominator = 1.** `1/1` is not a publishable percentage, so
**the taxonomy ships as a per-class table under a fixed headline** — *of 8 injected rows, 1 has a
genuine client signature; the other 7 are fidelity unverified and excluded from both halves.*
**The faithfulness bar was not weakened to avoid this** — the constraint was explicit — **and the
small denominator is the finding, not a failure of the checkpoint.**

**The non-obvious part.** VP31's 502 collapse costs the fault runs their **diagnostic** value, not the
taxonomy its denominator: the one verified row's expected observation is **200**, not 502, so the
collapse constrains exactly the rows the fidelity rule already excluded. The true claim available is
*"injected X, client saw the 502 X shares with four other causes"*, **never** *"the X-specific
response"*. Found in the same pass: **503 is overloaded too** (UNAVAILABLE rung / spend guard / stale
readiness), and the exception paths emit no `meta` event, so **there is no served-rung field to
disambiguate with.** `[read from source]`

**Two facts verified in passing** `[read from source]`: `BudgetExceeded` is dead code on the serving
path (`budget=None`), and **the API emits `Retry-After` on no path at all**, so the faithfulness
bar's third property is vacuously satisfied everywhere and discriminates nothing — **kept in the bar,
explicitly not weakened, but it does no work today.**

**The one run worth its cost, and its result** (R98 §3) `[measured]`. `MITYA_FAULT_INJECT=429:1`,
`retry_after=2.0`. Two requests, both **HTTP 200** with full answers:

```
ttft 5821 ms and 5788 ms, against the clean metered baseline p50 of 3372 ms
delta: +2449 ms and +2416 ms
predicted band for retry_after=2.0: 2000-2500 ms  -> both inside
```

`[baseline: v2 CP4's own clean metered p50]`

**This is the first evidence anywhere in this repo, at any layer, that `exc.retry_after` is honoured
on the real serving path.** R88 recorded that **no test asserted it** before or after the remediation
batch; that batch added a unit-level assertion; this closed it end to end with a real provider behind
the injected failure. **It is also the single fidelity-verified row in the taxonomy, now measured
rather than predicted.**

**A guard added beyond spec, worth keeping.** `FaultProvider` logs at **every** activation: *"FAULT
INJECTION active: … this run's answers are not real."* A fault flag left set during a measurement run
is VP18's failure in new clothes — quality numbers computed on answers that were never real — and
this makes that case impossible to miss in a log. Verified: `FAULT_INJECT` is off by default and **no
serving config under `config/serving/` enables it.** `[read from source]`

---

<a id="VP30"></a>
## VP30 — The degradation ladder: coverage 3/5, and it descends on misconfiguration rather than on outage

**R83 — three of five rungs could not be entered at all** `[measured]`, call-site census over `src/`:

| rung | entered by | status 2026-08-30 |
|---|---|---|
| `PRIMARY` | `generation_available and not degraded` | reachable **with wiring** |
| `CHEAPER` | — | **unreachable by construction** — one `generation_available` flag covers both arms |
| `CACHED` | `cache_available and cache_hit` | reachable **with wiring** |
| `RETRIEVAL_ONLY` | `retrieval_available` | reachable **with wiring** |
| `UNAVAILABLE` | fallthrough | reachable **with wiring** |
| `REFUSED` | adapter raises `ContentRefused` | **already wired** |

`next_rung()` call sites: its own definition, the package re-export, and 9 test assertions. **Zero in
the request path.** The gap was narrower than stated in two directions: `readiness_from_status` *is*
called from request-adjacent code, but only by `/ready`, for **reporting**, not to select a rung; and
`REFUSED` was wired at CP2 as the module's own docstring promised. **What was dead is the decision
function.**

**R88 — this is a pattern, not an incident, and the second instance carries a live regression**
`[measured]`. Everything the request path imports from `src/gateway/` is a **type, an exception class,
or an adapter**; **neither policy function is imported.** `retry_transport` call sites: definition,
re-export, 3 tests. **Zero in the request path** — the same shape as `next_rung()`. **CP2 wired the
gateway's adapters into the request path and left its policy behind.** And the two retry
implementations **diverge on the one field that was added as a fix**:

```
generate.py:274-276    if exc.retry_after is not None:
                           delay = exc.retry_after * (1.0 + 0.25 * random.random())
                       else: <jittered backoff>

retry.py:88-89         delay = min(max_delay, base_delay * 2 ** (attempt - 1))
                       delay *= 0.5 + random.random()          # retry_after never read
```

Honouring `retry_after` was the R68 fix, and Groq's `Retry-After` latterly arrived **in minutes**.
`retry_transport()` never reads it, despite its own docstring arguing the provider's stated wait is
*"the only correct answer available"*. **If the gateway retry loop ever became the request path, the
R68 fix would be silently lost** — and `generate.py:50` carried a comment claiming parity that the
`retry_after` handling breaks. Resolved by **deleting** `retry_transport()` (−47 lines, its three
tests and their stub with it) rather than reconciling two loops; `TransportError` and
`record_if_empty_completion` stay, both live in the request path. `[measured]`

**R89 — the readiness probe still asked llama-server whether generation works** `[measured]`, source
read. The chain is **unconditional**: `/health` queries `Config.LLAMA_SERVER_URL` on every readiness
check regardless of which provider is actually serving, and `generation_available` is derived from
it. **So `generation_available` was a fact about llama-server, not about the provider that
generates.** Three consequences in ascending cost:

1. `/health` returns **503** and `status: "degraded"` on a deployment whose generation is perfectly
   healthy, whenever llama-server is not also running.
2. **The SPA shows degraded permanently** — rendered as *"Mitya's study is dimmed"*, a **user-visible
   false alarm**, not an operator-plane wrinkle.
3. **It was decisive for the wiring order.** Wiring `next_rung()` into `/chat` against *this* input
   would make the ladder descend to `CACHED`/`RETRIEVAL_ONLY` on **every request** while the provider
   is fine — turning a dormant defect into a serving one. **The fix had to land before the wiring.**

Scope, stated precisely: **the defect is in the coupling, not in the probe.** On a box where
llama-server runs, readiness reports correctly **by coincidence**. **Not measured:** whether any
CP2-era number was taken while `/health` was reporting degraded — nothing in the CP2 registers
depends on the readiness value, so no finding is suspected, **but this entry does not clear them, it
records that it did not look.**

**R94 — the fix narrowed what the rung means, and nobody predicted it** `[measured]`.
`credential_status()` is **config-only and deliberately not a live probe**, and the reasoning is
sound: `/health` is polled by the SPA every few seconds, Gemini Flash allows **5 requests per
minute**, so a readiness probe would spend the quota the deployment exists to serve with — and on a
token-bound provider it would spend the limit that actually binds. A provider *outage* already has a
code path (`TransportError` → 502).

> **`generation_available` now means "configured and credentialed", not "the provider is reachable".**

- On a hosted provider **outage**, the ladder does **not** descend. The request 502s exactly as
  before. `CACHED` and `RETRIEVAL_ONLY` are not entered.
- `RETRIEVAL_ONLY` is enterable essentially when the deployment is **misconfigured** — no credential,
  or a provider row with no adapter.

**Two things follow, pointing in opposite directions.** The stated goal is met — the rung forcer has a
lever, three of five rungs are enterable. **And the checkpoint is now choosing about a narrower floor
than the charter assumed**: *"is retrieval-only an acceptable product floor?"* reads differently when
the condition that enters it is *the operator forgot the key* rather than *the provider is down*.

**Compounded by G-P7, still open:** `Readiness.degraded` is declared and read and **assigned `True`
nowhere**; the spend-rate breaker is a module-global with no accessor that raises into the request
handler and **never reaches the ladder**. **Between the two, the ladder's descent conditions today
are misconfiguration only** — neither a provider outage nor a tripped spend breaker reaches it. That
is a strictly smaller set than the charter's ladder describes.

**The walk** (R98 §4) `[measured]` · `[baseline: none — first walk]`:

| rung | entered? | how |
|---|---|---|
| `primary_model` | **yes** | default state |
| `cached_answer` | **yes** | credential unset + warmed disk cache |
| `retrieval_only` | **yes** | credential unset + a query never asked before |
| `cheaper_model` | no | **unreachable by construction** |
| `503` / `UNAVAILABLE` | no | needs retrieval down too; **not walked** — it means breaking the vector store on purpose |

**Coverage: 3 of 5 entered, against an attainable ceiling of 4/5.** **The fraction never appears
without this caption:**

> `cheaper_model` is unreachable by construction: `Readiness` folds primary and cheaper into one
> `generation_available` flag, and routing was closed as not-adopted (VP16), so there is no second
> generation arm to fall to. Maximum attainable coverage is 4/5.

**R95's forceability table was wrong, and the walk disproved it** `[measured]`. R95 claimed
`cached_answer` was forceable over HTTP. **It is not**: with generation healthy, a cache hit returns
`rung=primary_model, cache_hit=True` (identical 2,661 chars on the repeat, `cost=$0.0`), because
`next_rung()` returns `PRIMARY` whenever generation is available **regardless of `cache_hit`**. **Only
`primary_model` is harness-forceable; the other three are operator-only**, and the lever for the
bottom two is removing a credential, which requires a **uvicorn restart** the harness cannot do over
HTTP by construction. **The ladder walk is therefore partly a documented manual procedure, not an
automated sweep** — and `src/harness/rungs.py`'s `force_cached` docstring carried the same wrong
expectation, so this was a code defect, not merely a register error.

**O9's answer, and it is not the comfortable one** (R98 §5) `[measured]`:

```
rung=retrieval_only  status=200  cache_hit=False  cost=$0.0
chars=0   chunk_ids=3   no_context=False   terminal=done
```

**It serves an HTTP 200 with an empty answer body and three source ids.** Retrieval worked — three
chunks, context present — **and there is no prose at all.** A client rendering only the answer text
shows the user a **blank successful response**, which is arguably worse than a 503: **a 503 is legible
as failure, an empty 200 is not.**

**CP-4c closed (R99): option (b) — internal-only. Users get a 503.** The rung stays reachable and
remains in the coverage denominator; users see a 503 rather than the rung's output. `[decided]`

| observation the rule required | measured |
|---|---|
| **distinguishable** from a normal answer without reading the text? | programmatically **yes** — `rung=retrieval_only` in `meta`, `chars=0` |
| does it **carry the passages**? | **yes** — `chunk_ids=3`, `no_context=False` |
| does anything **present ungrounded prose as an answer**? | **no** — zero prose at all |

The third is why dropping the rung was never attractive: with generation unavailable there is no
persona and no text, so v1's measured failure modes here — source-leak 26.4% (19/72), assistant-tell
9.7% (7/72) `[baseline: v1 recorded]` — **cannot occur.** Nothing is passed off as an answer.

**The rule was NOT fully satisfied, and this log says so.** The rule requires *"the client response
**and the SPA render** captured"*, and **the SPA render was never observed** — `frontend/` was
explicitly out of scope for every CP4 batch. The owner was offered a zero-spend observation window
and chose to decide without it. **This is a decision taken on two of three observations, recorded as
such rather than presented as a satisfied rule.** What makes it defensible: shipping it as a product
state was already **disqualified by the rule's own text** (it requires a presentation separating the
degraded state from a normal answer, and no such presentation exists or is scoped), and an empty HTTP
200 is close to the inconclusive branch's own definition of an internal rung — *"a rung nobody can
tell they are in is internal by observation, whatever the intent."* **If the SPA is ever observed and
renders the passages legibly, that option becomes live again** — it is not foreclosed, it is
unexamined.

**The consequence is not a no-op, and it is owed rather than built.** Today's behaviour is
**neither** option: `retrieval_only` currently serves HTTP 200 with an empty body and three
`chunk_ids`. Implementing the decision is a real change to `/chat`. **Two riders, neither settled:**
the rung must stay observable to operators, or the coverage denominator counts a rung nobody can
enter and the checkpoint's own answer becomes unverifiable; and **a 503 is already overloaded**, so
adding a fourth producer without a discriminator makes the error taxonomy worse.

**A1 (Qdrant server mode) did not fire at CP4, on a measured trigger.** Trigger (i) was retired
because the local-mode lock is **cross-process** (R84, below); trigger (ii) — the phase cannot
complete because an offline eval must share a window with a harness run — **did not fire**: the runs
completed in sequenced restart windows with **zero runs lost to the lock**. Deferred to CP5, where it
did not fire either. `[decided]`

**R84 — what the Qdrant lock actually is** `[measured]`, source read of
`qdrant_client/local/qdrant_local.py`. `QdrantLocal.__init__` opens `<location>/.lock` and takes
`portalocker.lock(..., EXCLUSIVE | NON_BLOCKING)`. Grep for `threading` / `Lock()` / `RLock` across
`qdrant_local.py` and `local_collection.py`: **no matches.** So: it is an **OS file lock, therefore
cross-process** — what it forbids is a second *process* opening the same folder (offline eval
alongside a live `uvicorn`, which is why the teardown rule exists). **It does not constrain concurrent
retrieval inside one uvicorn**, which shares one instance and never contends for the flock.
**Intra-process concurrent access is unguarded rather than serialized.** Server mode is not required
for concurrency *within* one process; it is required for concurrent *processes*.

---

<a id="VP31"></a>
## VP31 — Three numbers the harness cannot see from outside, and all three are findings about the API

**R90** `[measured]`, source read, produced at zero marginal cost **before any run**. The rule that
makes these findings rather than limitations: *a number the harness cannot see from outside, plus the
operator plane, is a finding about the API, not a reason to reach inside.*

**Gap 1 — token accounting never crosses the SSE boundary.** The `usage` dict accumulates
`prompt_tokens` and `completion_tokens` and is consumed **server-side** to price the answer. The
`meta` SSE event emits `request_id`, `tenant`, `config_version`, `corpus_version`, `route_name`,
`chunk_ids`, `ttft_ms`, `total_ms`, `filter_relaxed`, `no_context`, `cache_hit`, `model`, `cost_usd`,
`cost_metered` — and **no token counts**. **An external client observes cost as a scalar with no
denominator**: it cannot compute cost-per-token, cannot verify the price against the rate card, and
**cannot size a run against the token limits that VP14 shows are the binding ones on Groq.**

**Gap 2 — five distinct failure causes collapse into one opaque 502.**

```
except TransportError as exc:
    raise HTTPException(status_code=502, detail="provider unreachable")
```

`TransportError` covers a 429, a connect failure, a read timeout, a 5xx, and an in-band provider
`error` frame. **All arrive at an external client as the identical string under the identical status
code.** This is wider than *"timeout vs refusal are indistinguishable"*: **it means the error
taxonomy has a hard ceiling no injector design can raise** — the fraction of injected faults
producing the intended status code is measurable, but *which* fault produced a 502 is **not
recoverable from outside**.

> **Corrected, and the correction is P7-consistent with the register's own rule.** R90 counted
> ~~six~~ causes and claimed in the same sentence that the remediation batch *"does not touch any of
> these three"*. **Both are wrong on the same fact.** The sixth cause — a retry-budget projection
> raising `TransportError` **without a transport failure** — lived only inside `retry_transport()`,
> which that batch **deleted**. Post-remediation the collapse is **five** causes, and the one
> non-transport member is gone. The gap stands and its consequence is unchanged; what changes is that
> *"a 502 cannot be read as evidence the network broke"* **ceases to be true after the deletion** —
> every remaining member is a genuine transport-layer event. Recorded rather than edited in place.

**Gap 3 — there is no gauge. Queue depth and in-flight count are not exposed anywhere.**
`src/metrics.py` defines nine instruments: four histograms and five counters. Grep for
`create_gauge`, `create_up_down_counter`, `create_observable_*`: **no matches.** **Histograms record
what already completed; nothing reports live state.** The saturation work has to characterise the
behaviour of an unbounded `Semaphore(1)` wait, and **queue depth is precisely the quantity that
observation needs and precisely the one nothing publishes.** Waiting requests are invisible — they
appear only as latency in a histogram after they finish, **so a pile-up and a slow provider are
indistinguishable from the operator plane.** This also lands on CP5's bounded-concurrency work, which
needs the gauge to show the bound works.

**What the saturation observer can and cannot see, now fixed in code** (R95) `[read from source]`.
Observes: semaphore wait time (**inferred client-side**, since there are zero gauges), the local 429
rate and the provider 502 rate **as two separate numbers** (disjoint code paths), and silent p99
degradation — all sliced by concurrency and route. **Cannot observe: queue depth at all**, emitted as
a literal unobservable marker citing gap 3, **neither guessed nor omitted**; and cannot attribute a
502 to a specific injected cause.

**Two more asymmetries found by the dry-run rule** (R97) `[measured]`, two dry passes of two requests
each, **zero provider spend**:

**Defect 1 — a ramp against a cache-enabled server measures the cache.**

```
pass 1 (cold)   total_ms ~2100      cache_hit=False   rps   0.45
pass 2 (warm)   total_ms 1.7-2.6    cache_hit=True    rps 246.74
```

The first pass had **populated the cache**. Not a one-off: the ramp cycles its query list by design —
its job is *"a stated load shape, not a fixed corpus walk"* — so **every level after the first
replays queries the earlier levels already warmed, and the whole curve degenerates into cache-lookup
latency.** `rps = 246.7` is a number about the cache. **Fix is a run condition, not a code change:**
ramp with `CACHE_ENABLED=false`, re-measured at total_ms 2018-2175, baseline **2097 ms** — real
retrieval work. **This is a permanent constraint on every future ramp.** A cache-enabled curve is a
legitimate measurement *of the cache*, and if it is ever run it must carry its repeat rate as a
stated parameter. **The two curves answer different questions and must never be pooled.**

**Defect 2 — the DEFAULT serving path emits no terminal event (G-P8).** `ChatResult.terminal_event`
came back **`None`** on every uncached request, where the cached pass returned `"done"`:

```
cache-hit path    -> yields _sse("done")
STRUCTURAL path   -> yields _sse("done")
DEFAULT path      -> meta, then the stream simply ends
error path        -> yields _sse("error")
```

**The DEFAULT path — hybrid retrieval plus generation, which serves most traffic — is the only one
that does not mark completion, so a client cannot distinguish a completed answer from a truncated one
on the main serving path.** Errors remain detectable, so faults are not lost; what is missing is the
**positive confirmation of success.** Three consequences: the *"p99 degrades without any request
erroring"* observation is exactly the silent-failure case this blinds; it compounds the
after-first-token ambiguity, leaving the DEFAULT path's stream end uninformative **in both
directions**; and `fault_phase` derives `post_first_token` from `terminal_event == "error"`, so it is
sound for faults but cannot separate *success* from *silently truncated*. **Narrowed by the ladder
walk:** `retrieval_only` **does** emit `terminal=done`, so **G-P8 is specific to the generation path,
not general to the API.** Flagged, not fixed — **open**.

**One benign observation.** `cost_usd` is `None` on the fake arm rather than `0.0`, because the fake
returns no usage and the pricing path declines to fabricate a token count. **That is correct
behaviour**, and it confirms the framing: **a fake ramp measures the server and structurally cannot
produce cost rows.**

---

<a id="VP32"></a>
## VP32 — Six premises asserted and never checked, four caught in CP4

**This is the section worth re-reading at project close.** `[measured]` in every case, by
re-deriving the claim from source rather than trusting the document that carried it.

| # | The premise | The truth | Where it had reached |
|---|---|---|---|
| 1 | **`test/` is gitignored** (G-P5) | False — `git check-ignore` returns nothing; only two narrow `test/gate/` patterns are excluded. **It had blocked a batch for no reason.** | a design gap register |
| 2 | **Six causes collapse into one 502** (R90) | **Five**, after the remediation batch deleted the sixth — and the scope clause claiming the batch did not touch them was wrong in the same sentence | a research register |
| 3 | **The `FakeProvider` seam needs no code change** (R96, inherited from the schedule) | False — **the fake was not selectable at the serving layer at all.** Caught at **preflight**, so nothing was spent | a document that gated a run's approval |
| 4 | **`cached_answer` is forceable over HTTP** (R95) | False — `next_rung()` returns `PRIMARY` whenever generation is available, regardless of `cache_hit`. **Disproven by measurement** | a register **and** a shipped docstring |
| 5 | **`_abort()` releases and every call site is inside the critical section** (the lock-narrowing design) | False — **five of eight are after the release point.** Left as written it would have turned **every provider error into a 500**, in exactly the region the fault taxonomy is measured | the implementation plan |
| 6 | **The semaphore is what serializes retrieval** (the premise under C8 itself) | False — retrieval is plain sync on a single-worker event loop, so **the runtime serializes it** and the semaphore only ever stopped *generation* overlapping. **This reframed the whole decision** | the checkpoint question |

**Nos. 3 and 5 are the expensive ones avoided** — one would have burned a metered run on an
impossible plan, the other would have shipped a serving-path bug.

**And the dry-run rule paid for itself three times** (R97): a ramp that measured the **cache** instead
of the system; a 30-second client timeout silently truncating two levels; and a `ChatResult` that
retained no answer text, so a dry run could not show what came back. **All three produced clean HTTP
200s and looked exactly like success.**

**The corruption check, and why its reading had to be inverted** `[measured]`. Identical queries
issued concurrently returned identical `chunk_ids` at every level on both arms: **0 divergent of 11
distinct queries checked**, over 220 concurrent requests. Read naively that is a thread-safety
result. **It is not:** the retrieval it exercised was **already serialized by the runtime** (VP27), so
it is a **determinism** detector — valuable, and worth keeping — and it is **silent** on the
remove-the-lock option rather than supporting it. **Both R98's own closing paragraph and the
checkpoint rule as written read it the other way round.**

> **G-P9, open and routed:** the corruption check does not exercise concurrency but is labelled as
> though it does. **It becomes a false safety claim the moment retrieval is offloaded to a thread or
> a second worker is added.**

**Verified boundary property.** `grep` for application imports in `src/harness/` returns **empty**, so
the rule that the harness speaks only the public API holds **mechanically** as the harness grows, not
by discipline. The ramp's connection pool is sized to the requested `concurrency`, with the failure
mode named in the module docstring — *"getting this wrong looks identical to success"* — and asserted
by a test that the concurrency parameter reaches the transport. `[read from source]` + `[measured]`

**CP4 numbers, with their baselines** `[measured]` · **O10 applies to every harness-derived row**:

| number | value | baseline |
|---|---|---|
| knee, before | concurrency **1** | none — first measurement (v1 asserted `Semaphore(1)` qualitatively, `[E20 (v1)]`) |
| knee, after the lock narrowed | concurrency **4** | 1 `[baseline: v2 CP4]` |
| throughput @ conc 4 | **0.274 rps** (burst, n=5) | 0.158 rps `[baseline: v2 CP4]` |
| p50 @ conc 4 | **14,185 ms** | 19,305 ms (−26.5%) `[baseline: v2 CP4]` |
| p50 @ conc 1 | 6,456 ms | 6,227 ms — **indistinguishable at n=5** `[baseline: v2 CP4]` |
| ladder coverage | **3/5** | none — first walk |
| fault taxonomy | **1 of 8 rows fidelity-verified** | none — published as a table, never a rate |
| `retry_after` honoured | **+2,449 / +2,416 ms** | 2,000-2,500 ms predicted band |
| corruption | **0 divergent** of 11 queries over 220 requests | none — and see the inversion above |
| **CP4 spend** | **38 draws, ~$0.060, 7.6% of one day's `rpd`** | budget 19 draws |

---

<a id="VP33"></a>
## VP33 — Identity is real, tested, and costs nothing measurable

**Auth to D7's full posture, no clause relaxed** `[read from source]` + `[measured]` — 27 unit tests,
every clause named, plus **10 live probes**:

| clause | how |
|---|---|
| argon2id | `cryptography` 49.0.0's OpenSSL binding — **no new dependency** |
| session tokens | **opaque, server-side**, not JWT |
| cookie | `HttpOnly` + `Secure` + `SameSite=Lax`, **rotation on login** |
| login rate limits | per-account **and** per-IP, **reject rather than sleep** |
| enumeration | unknown-account and wrong-password indistinguishable by **status, body and timing** |
| registration | closed; accounts seeded by a script. No admin UI, no invite tokens |
| harness credentials | **real logins.** A test-only credential path is an auth bypass by another name |
| TLS | terminated in front |

**argon2id needed no new dependency** (R105) `[measured]`, file inspection of the installed
dist-info. `cryptography` 49.0.0 is already installed as a transitive dependency and exports
`Argon2id` bound to `rust_openssl.kdf.Argon2id`. No `argon2-cffi`, `passlib` or `bcrypt` is
installed. **The "already-installed dependency solves it" rung, taken.**

> **One thing is asserted from documentation and is UNVERIFIED on this machine.** The binding
> requires **OpenSSL 3.2+** at runtime and raises `UnsupportedAlgorithm` on the first `derive()`, not
> at import. `lifespan()` calls `argon2_available()` and **refuses to start** if it does not, and the
> hasher test fails loudly rather than falling back. **A fallback KDF was deliberately not written**:
> D7 names argon2id, and a silent fallback to scrypt would satisfy the code and violate the charter.
> The upgrade path (`pip install argon2-cffi`, two KDF call swaps) is noted in the module docstring,
> not built speculatively.

**`Secure` cookies and plain-HTTP local runs are incompatible, and that is a precondition on every
run** (R106) `[reasoned]` from RFC 6265 §5.4. TLS is terminated in front, so `Secure` is correct in
deployment — but **every run this project performs speaks plain `http://127.0.0.1`**. An HTTP client
accepts the `Set-Cookie` and then **never sends it back**, so the harness would log in successfully
and get 401 on every `/chat`, **with no error naming the cause.** Resolved with
`Config.SESSION_COOKIE_SECURE` (default **True**), warned about at startup;
`HarnessClient.login()` logs a specific warning on a 200 with no stored cookie, because that is the
exact silent failure.

> **The consequence carried on every CP5 number: every figure produced against a local server is
> produced with `Secure` OFF.** That does not affect isolation, rate-limit or enumeration results —
> none depend on the flag — **but the auth-surface probe list must say so rather than reporting
> "Secure: pass" from a run where it was disabled.** The flag is verified by a test asserting the
> *source default*, not the live value.

**The tenant binding point had no seam, and its existing input was an impersonation primitive**
(R102) `[measured]`, source read against the phase plan's premise *"nothing should need
re-instrumenting; if it does, that is a finding."* **Two claims that must not be collapsed:**

- **(a) The dimension itself was fully delivered and needed nothing.** The ContextVar, the logging
  filter, `LOG_FORMAT`'s `[%(tenant)s]` and the root-span attribute all work unchanged with an
  authenticated value. **Zero call sites downstream of the binding point were touched.**
- **(b) The binding point read an inbound, client-controlled `X-Tenant-ID`.** CP1 documented this in
  **three** places as unauthenticated and bounded it to *"until P5 makes identity real"* — **a
  deliberate deferral coming due, not a defect.** But the shape of coming due is not "add a source":
  the header had to be **retired**, because **an authenticated tenant that an unauthenticated header
  can still override is not authenticated.** Supplementing would have left the impersonation
  primitive in place.

**Cost, measured:** 3 edits in `src/observability.py`, 1 in `src/api/app.py`, and **2 existing tests
flipped from asserting the header wins to asserting it is ignored.** `parse_tenant()` survives
unchanged as the shape validator for an account id.

> **The premise read strictly is FALSE; read as "no downstream consumer of the tenant dimension needs
> re-instrumenting" it is TRUE. The distinction matters because the strict reading is the one that
> catches an intake path nobody re-examined.**

**A confused-deputy path found by reading, not by a failure.** `Config.DEFAULT_TENANT` (`"default"`)
is now the *anonymous* tenant and `require_tenant` rejects exactly that value — so **an account
allowed to take the name `default` would authenticate successfully and then be treated as anonymous
on every subsequent request, with no error anywhere.** `create_account` now refuses the name.
`[measured]`

**Session expiry is TWO cases, not one, and only one is reachable as a 401** (R114) `[measured]`,
source read of the streaming contract against the SPA:

| when the session dies | what the client sees | handled by |
|---|---|---|
| before `/chat` answers | HTTP **401**, thrown before the body opens | the login wall, **no page reload** |
| after the first token | **Nothing.** The 200 is already committed and cannot be retracted — **that is the transport, not an oversight** | the pre-existing terminal `error` SSE event; no new code |

**A stream already emitting tokens cannot be turned into a 401**: the status line is long gone. **Any
design promising "the user is bounced to the login wall mid-answer" would be promising something HTTP
does not offer over a committed streaming response.** What the system guarantees is that the *next*
request fails closed, which it does, because `require_tenant` runs per request off a server-side
session row rather than off a token the client holds. **A probe asserting "expiry is detected
mid-stream" must specify which case it is testing**; the reachable one is expiry between turns and on
the request that was about to stream. Rider: the login wall drops in **without reloading the page**,
so the transcript (UI-only state in `localStorage`) survives and signing back in resumes where the
reader was.

**The numbers** `[measured]`, all on the **fake arm** with the cache disabled:

| number | measured | baseline |
|---|---|---|
| **per-request auth overhead** | **2,045 ms p50** (level 1) | **2,194 ms** `[baseline: v2 CP4, pre-auth, same arm and level]` — **below the noise of retrieval** |
| auth surface | **10 probes pass, 1 vacuous by construction** | none — first datapoint |
| session expiry probe | 15 SSE frames, then 401 | none — first datapoint |
| **CP5 spend** | **0 metered draws, $0.00** | budget: none required |

**The auth-overhead comparison is valid only at levels 1 and 2**, and that is a consequence of the
admission gate: a single-tenant ramp above `per_tenant=2` is **no longer a latency curve** — levels 4
and 8 are *admission* curves, with 13 and 16 of 20 requests refused. **CP4's ramp levels above 2 are
therefore not reproducible post-CP5 for one tenant.** `[measured]`

**Zero metered draws was the right instrument, not a concession.** Auth, admission and fairness are
**provider-independent**, and the fake arm removes the provider variance that would otherwise swamp a
millisecond-scale middleware cost. `[reasoned]`

---

<a id="VP34"></a>
## VP34 — The queue is bounded, and it moved rather than vanished

**The knobs, chosen rather than defaulted** (R111) `[decided]` on `[measured]` input:

| knob | value | derivation |
|---|---|---|
| `CHAT_MAX_INFLIGHT_TOTAL` | **6** | Concurrency past the knee (1) buys zero throughput, so every admitted request beyond the first is queue; the question is only **how much queue is honest**. At CP4's ~2.1 s retrieval service time, 6 is a worst-case admitted wait of ~12.6 s — a slow answer instead of CP4's 62 s one |
| `CHAT_MAX_INFLIGHT_PER_TENANT` | **2** | One in flight plus one queued is what a reader with two browser tabs generates; the third is where "busy" starts. Caps any single tenant at a third of the deployment |
| max tenant count | **5** | owner-stated; sizes the metric-label budget, the per-tenant bound, and the fairness run |

**The deliberate inequality: `6 < 5 × 2`.** The total binds before five tenants can each reach their
own cap. **That is on purpose and it is a measurement decision, not a capacity one:** if the total
were `≥ max_tenants × per_tenant`, **no tenant could ever lose a slot to another and the starvation
number would be zero by construction** — the same vacuous-by-construction trap as the cache test, for
a different reason. Asserted by a test that pins the configured bounds to the ones the plan sized.

**Two properties tested rather than argued**, both being where this class of code fails: **the bound
rejects rather than waits** (a bound that waits is an unbounded queue with extra steps — precisely
what CP4 measured), and **the slot is released on every exit** including a streamed body, an
exception, and a client disconnect. The second is why it is a **pure-ASGI middleware** and not a
context manager inside the handler: the handler returns as soon as the `StreamingResponse` is
constructed, so a slot taken there would be released **before the tokens were sent.** One
`try/finally` around `await self.app(...)` covers all six exits. `[read from source]`

**It does not replace the retrieval semaphore, and conflating the two would be a real error.** That
lock is mutual exclusion over non-re-entrant retrieval components; this is **admission control over
whole requests.** Different questions, different scopes; CP4's decisions were not reopened.

**The bound works** (R118) `[measured]`, fake arm, cache off, 5 tenants ·
`[baseline: v2 CP4, same arm and level, same corpus]`:

```
                        p99 total_ms      rejections
CP4 (pre-bound), level 4      64,405                0
CP5,             level 4       4,429               13
```

**CP4's system never rejected; it queued, and the tail went to 64 s. CP5 refuses in ~4 s.** That is
the intended trade and it is delivered.

**And the queue moved rather than vanished** (R119) `[measured]`, seven runs across three
driver/gate configurations. Admitted requests show **p95 of 12-30 s against a 2.1 s idle baseline**,
and system throughput sits near **0.25 rps** — **which is CP4's knee, not the gate's capacity.**
`CHAT_MAX_INFLIGHT_TOTAL=6` **admits far more than the pipeline can serve**, because retrieval is
still mutually exclusive: the lock was narrowed to retrieval, **it was not parallelized**. Five of six
admitted requests are therefore queued **inside the box**, waiting on that lock.

> **The bound caps how many wait, not how long.** CP4 measured an unbounded queue at 62 s p99; CP5
> caps the queue's *population* at 6 and still shows 25-30 s tails, **because the cap sits above the
> knee.**

**Two smaller measured facts from the same runs, recorded so they are not rediscovered**
`[measured]`: 9b's p95 reached **25,772 ms** and **seven requests across the two runs exceeded the
client's 30 s timeout and returned as transport failures**; and the single-tenant ramp caveat noted in
VP33.

**Sizing the gate to the measured knee is the prerequisite for measuring fairness at all** — a
throughput decision, not a tuning detail — and it is what VP35 is blocked on.

---

<a id="VP35"></a>
## VP35 — Fairness is a NON-RESULT. Two mechanisms built, one retired, the other unevaluable. **G-P10 is OPEN.**

**This section is the phase's honest headline, and an absence here would read as a pass.** CP5
delivered **admission control**. It did **not** deliver per-tenant fairness, in either direction: one
mechanism **could never fire**, and the replacement **fires but cannot be evaluated**.

**Mechanism 1 — the gateway's token-pressure fair-share guard. Built, then RETIRED: it could never
fire.** (R117) `[measured]`, live, fake arm, cache disabled, two runs, **120 requests, $0**.
Artifacts `out/p5-fairness.json`, `out/p5-admission.json`.

```
mitya_pool_share_rejections_total      moved by ZERO across both multi-tenant runs
mitya_admission_rejections_total       carried ALL 71 rejections
                                       (split reason="tenant" / reason="total")
```

**This is structural, not a sizing accident.** The guard fires only above `POOL_FAIRNESS_PRESSURE=0.8`
of `limiter.tpm` (250,000) with ≥2 active tenants. Admission control caps in-flight requests at
**6**; at ~2.1 s service time that is under 3 requests per second, and ~2,766 tokens per request puts
sustained pressure near **8,000 tokens/minute — about 3% of the threshold.** **No offered load can
raise it, because the offered load is exactly what admission control is refusing.** `[computed]`

> **The guard sits behind a gate that makes its own precondition unreachable.**

**Both mechanisms are individually correct and were individually tested.** Nine in-process fairness
tests pass and the arithmetic is right. **What no in-process test could see is that another correct
component, added in the same batch for a different reason, removes its input.** **A control that has
never executed is indistinguishable from an absent one — and here the control is unreachable by
construction while its test suite is green.**

**CP5 must not report per-tenant fairness as delivered: what is delivered is admission control, and
what is written is a fairness guard nothing can reach.** The disposal is a real decision, not a
tuning nudge, and it was **not taken inside the batch**: raise `CHAT_MAX_INFLIGHT_TOTAL` until token
pressure can reach 0.8 (which gives back the unbounded-queue latency CP4 measured), lower
`POOL_FAIRNESS_PRESSURE` until the guard can fire under a bounded queue, make fairness a property of
the admission gate rather than a second mechanism behind it, or **accept that under a single shared
free tier the pool is never the binding constraint and retire the guard.** **Flagged as G-P10.**

**A USD-denominated fairness rule would have been INERT, and that trap was caught before it shipped**
(R109) `[measured]`, source read. The decision file fixed the quota **unit** as USD. Read as the
*enforcement primitive*, that produces a control that can never fire here, for two independent
reasons already in the repo: `_estimated_cost_usd()` returns `cost.usd or 0.0` and `cost_usd()`
returns `UNPRICED` for any model with no rate row, so **every hosted arm estimates 0.0**; and the
guard passes `budget=None` on the live path because **at the free tier there is no USD ceiling to
breach — Google's rolling cap is literally $0.** A rule of the form
`tenant_usd > pool_usd / active_tenants` **compares 0 against 0/n on every request, forever.** It
would pass code review, ship, appear in the docs, and reject nothing.

**Resolution, and it contradicts nothing.** Fairness is **enforced** on the pool's own meter —
reserved tokens against `RateLimiter.tpm` — and **reported** in USD. The charter fixes USD as the
*reporting* unit; it does not require the enforcement primitive to be a dollar. **On a free-tier arm
the USD column beside the fairness numbers legitimately reads $0.00, and that is a true statement
about a free tier, not a missing measurement — it must be labelled as such rather than reported as
"no overspend".** `[reasoned]` on `[measured]` inputs

**A degenerate case at the edge of the input domain, which was the deployment's normal state**
(R110) `[measured]`, found by arithmetic while writing the test, **before any run.** The rule as first
written was `mine + tokens > pool / active_tenants` with `active` including the caller. **With one
tenant, `active == 1` and the fair share is the whole pool** — which reads as correct and is not: a
lone tenant that fills the pool then trips **its own** share, and the guard rejects with
`PoolShareExceeded` (429, *"over your share"*) a request whose actual problem is that **the pool is
empty** — which is `QuotaExhausted`'s fact, with its own status, counter and `Retry-After` semantics.
**Two different causes collapse into one wrong answer, on the single-tenant deployment that is this
project's own daily case.** Fixed with `if len(active) < 2: return False`, covered by a test asserting
**both** halves — the lone tenant is not rejected, and *the same over-pool tenant is* rejected the
moment a second one contends, **so the `False` is the one-tenant rule rather than a dead guard.**

**Mechanism 2 — the admission gate's least-served rule. Fires, and cannot be evaluated.**

**The first reading was an artifact of the driver, not a property of the server** (R119, **corrects
R118**) `[measured]`. `run_fairness.py` fired a fixed count of attempts per tenant and **never
retried a rejection.** Rejection is instant, so **a tenant arriving during a saturated window burned
its entire offered load in milliseconds and finished with zero served**, while whichever tenant
already held a slot re-fired on completion and kept it. Two tenants offering identical load returned
**1.0 and 0.0** shares (one tenant 12/12 served, the other four 0/12, every rejection
`reason="total"`). **A single request against the same server moments later returned 200 in 2.1 s, so
nothing was leaked or stuck — the split was the client's behaviour.**

> **R118's magnitudes are WITHDRAWN.** ~~3.67~~, ~~3.00~~ and the post-gate ~~2.33~~ all came from
> this driver. **No starvation ratio from before this correction should be quoted.** R118's
> *directional* claim survives — FCFS on a shared cap distributes by arrival time, and
> `reason="total"` rejections did fall on light tenants — and its underlying observations stand:
> five tenants offering identical load received served shares 0.273 / 0.273 / 0.182 / 0.182 /
> **0.091**, and in a second run two *light* tenants at concurrency 1 were rejected **8 times each**
> on `reason="total"` while two others at the same offered load were rejected **zero** times.
> **Per-tenant fairness against a noisy neighbour does work** — the heavy tenant was correctly
> throttled to 0.129 by `reason="tenant"`, which is what `per_tenant=2` is for. **What has no
> fairness is the contest between everyone else for the global slots.** The charter asks for *"a
> global pool with per-tenant fairness on top"*; **half of that is real, and the global half
> distributes by arrival time, which under contention is arbitrary.**

**And then the fixed driver had no statistical power** (R119 defect 2) `[measured]`. Duration mode,
90 s, five tenants: **2.33 and 6.99 on two consecutive identical runs.** The reason is visible in the
counts — **the whole system served 26 and 22 requests in 90 seconds**, so per-tenant served counts
were **1 to 7**. **A ratio of small integers is noise, and two runs disagreeing by 3× is what that
looks like.**

**G-P10's disposal, stated exactly.** The least-served rule **fires** — the `reason="fairness"` label
appears once the metric's label allowlist was fixed — **but whether it improves share distribution is
unmeasurable at 22-26 served requests per run. G-P10 stays open, in both directions.** Sizing the gate
to the measured knee (VP34) is the prerequisite for measuring fairness at all, and that is a decision
about throughput.

**Two rules earned here and binding on the rest of the project** `[reasoned]`:

> **An instrument that has not been checked against a known-answer case is not evidence.**
>
> **A number computed from single-digit counts is not a number.**

**G-P10 was inherited unchanged by CP6 and is still open.** It is the largest open item after the
gate's 1b composition (G-B).

---

<a id="VP36"></a>
## VP36 — Tenancy plumbing: metric labels with an enforced budget, and a cache test that is vacuous on purpose

**`tenant` becomes a metric label, and the FORM of the lift matters more than the fact of it** (R107,
R113) `[decided]` on `[measured]` input. CP1's rule (VP10) said `tenant` becomes a label at CP5 once
identity is authenticated and the tenant set is therefore bounded and knowable. **Both halves now
hold** — the tenant comes from an authenticated session, and the set is the `accounts` table, which
only a seeding script can add to.

**The budget, stated so it can be checked** `[computed]`: `tenant` appears on `request_duration`,
`answer_cost`, `quota_rejections`, `pool_share_rejections` and `admission_rejections` — **five of ten
instruments**. Worst case at 5 tenants is `5 × (2 routes × 4 outcomes) = **40** histogram series` on
the largest; the rest are counters in single digits. **Trivial, which is the whole reason it is now
allowed.**

**It is not enforced by trusting the accounts table.** `_tenant_key()` is the **only** path a label
value can take, with three outcomes: a valid within-budget id passes through; anything failing the
tenant shape collapses to `"unknown"`; **a valid id arriving after `Config.MAX_TENANT_LABELS`
(default 8, headroom over the stated 5) distinct ones collapses to `"other"`**, logged once per
breach. **The memory-exhaustion path the original rule was written against is closed by code, and
stays closed if somebody seeds a sixth account.** `"other"` and `"unknown"` are deliberately
distinct: **collapsing them would hide a budget breach behind what looks like malformed input.**
`[read from source]`

**The test was flipped, and the flip is the security property.** The assertions **moved rather than
relaxed**: `tenant` must appear on **exactly** the allowlist and nowhere else, a malformed value must
collapse, and a value past the budget must collapse **differently**. **A test that only asserted "the
label exists" would pass on the unbounded implementation the rule exists to prevent.**

**One defensive detail, found by reading rather than by a failure.** The handler receives its tenant
through `Depends(require_tenant)`, and the offline tests that call it directly get the **unresolved
`Depends` object** in that parameter. `_tenant_key` therefore checks `isinstance(str)` before the
shape regex: **a label helper that raised there would turn a metric into a request-path failure.**
`[read from source]`

**The cross-tenant cache-leak test is recorded as VACUOUS BY CONSTRUCTION, and the real isolation
test is a tripwire that FAILS later rather than an assertion that passes now** (R112) `[decided]`.

**Two documents were in direct contradiction, and the batch stopped rather than improvising** (R103)
`[measured]`, text comparison:

| source | says |
|---|---|
| charter **A12(2)**, 2026-08-24 | the answer cache is **deliberately NOT tenant-keyed** while the corpus is shared; **tenant enters the key at CP6 and not before** |
| `decisions-p5.md` **C9**, 2026-08-30 | *"Keep the cache, tenant-keyed: key becomes `(tenant_id, query)`. One-line change. Deleting voids the USD-saved claim and is the larger diff."* |
| `src/rag/cache.py:57-60`, built to A12 | *"no `tenant` in the key. Shared corpus means tenant-keying costs hit rate and isolates nothing. **Add it at P6**"* |

**Opposite instructions for the same line of code**, and it changes what the phase's headline
isolation number *means*. **C9's stated rationale does not engage A12's argument at all** — it
reasons about diff size, not about the shared-corpus premise — **and it is self-defeating:
tenant-keying is what destroys the saving C9 cites protecting.** Later date does not settle it,
because the later document was not written against the earlier one. **C9 was RETRACTED.** The same
contradiction existed for quota shape (R104) and was resolved the same way: **a global pool with
per-tenant fairness on top**, not per-tenant isolation, so the evidence row becomes *share of the pool
captured by the heaviest tenant under contention, and starvation of the lightest* — **not per-tenant
cap overspend**, which is the shape A12 rejects.

**`src/rag/cache.py` is unmodified across two batches.** The deliverable is a **record**, and the
shape of the record is the finding — four assertions whose **ordering is the argument**
`[read from source]`:

1. `_key()` takes **no tenant parameter at all** — a caller cannot key on tenant even by accident.
2. Two tenants **share** one entry, demonstrated **concurrently**. **The leak attempt succeeds, and
   that is the intended behaviour**: shared corpus, no per-tenant content, so it is **one cost saving
   rather than one disclosure. CP5 does not get to report an isolation pass here.**
3. Nothing in a cached value is per-tenant — the stored shape is closed at
   `{answer, sources, footer, model}`, each field corpus- or config-derived. **This is what makes (2)
   safe rather than merely true.**
4. **The tripwire.** If per-tenant memory can reach the prompt — detected **from the code**, by either
   a `src.memory` module or a memory-shaped parameter on `build_messages()`, **never from a plan
   file** — then `AnswerCache._key` **must** take a tenant. **Vacuous today; a hard failure the moment
   memory lands without re-keying.**

Assertions 1-3 are (4)'s **non-vacuity**: they establish that the sharing is real, so the tripwire is
guarding something. A fifth test proves the tripwire's own predicate can fire.

> **Why detect from the code and not from a version marker.** The failure being guarded is somebody
> adding memory and not remembering the cache. **A guard keyed on anything that person would have to
> remember to update is not a guard.**

**The tripwire's disposition is the memory log's** (`EVIDENCE-MEMORY.md`): it fired, and the question
it forced — whether memory state enters the cache key — was closed on a measurement there.

**Storage is settled and cheap to extend** `[decided]`: stdlib `sqlite3`, a single file under
`data/`, `CREATE TABLE IF NOT EXISTS` at startup, **no ORM and no migration tool** — single writer at
this concurrency, and the heavier operational surface is ruled out by charter. Later phases add tables
the same way.

---

<a id="VP37"></a>
## VP37 — CP5's six instrument defects: the phase's most transferable finding, and it is not about auth

**Six times, the *instrument* was wrong and produced a confident, plausible, FALSE statement about the
product.** Two of the six pointed at a **more alarming** conclusion than the truth, which is the
direction that wastes the most time. **These are among the most valuable things in the project.**

| # | The instrument said | What it would have published | The truth | Caught by |
|---|---|---|---|---|
| **R108** | rate limiter implemented, **27 tests green** | *"per-account and per-IP login rate limits: PASS"* — a shipped, documented, reviewed security control | **It had never counted anything.** `src/db.py:connect()` commits on clean exit and **rolls back on any exception**; `login()` recorded the two failed-attempt rows and then raised `LoginFailed`, which unwound through that same context manager. **Both rows were rolled back on the way out**, so `_recent_failures()` returned 0 forever and **neither cap could ever be reached — in production exactly as in the test** | **first execution** of the suite |
| **R115** | *"`mitya_quota_rejections_total` carries no tenant label — the label was never wired"* | *"an unlabelled security-relevant counter"* — the more alarming reading, stated with total confidence | **The label WAS wired.** What was missing was the **emission**: the test's emitter never called `record_quota_rejection`, so that series was **absent from the scrape entirely** and the assertion failed on a **missing series** rather than an unlabelled one. One call added to the emitter; **no source change** | first execution |
| **R116a** | *"timing oracle, ratio 1.984"* across mixed `[401, 429]` statuses | *"user enumeration is possible by timing"* — a live auth vulnerability | **The probe blew its own budget.** It fired 10 unknown-user and 10 wrong-password logins for one account from one host against `LOGIN_MAX_PER_ACCOUNT=5` / `LOGIN_MAX_PER_IP=20`. **The 429s that followed skip the argon2id verify and return in roughly half the time of a real credential check** — an inert comparison wearing the shape of a timing oracle. Re-sized to 4 per side, 8 from the host; a `saturated` check now **refuses to score the probe at all** when a 429 appears | **reading the evidence, not the verdict** |
| **R116b** | *"enumeration probe FAILS"* | a failed security requirement, on a run whose measurement was a **clean pass** (one status `401`, one body `invalid credentials`, `timing_ratio 1.006`) | **The assertion could only ever fail.** `same_shape` compared the set of status **codes** to the set of response **bodies** — never true. Fixed to compare statuses to statuses and bodies to bodies | re-reading after a clean measurement |
| **the `unknown` bucket** | **9 rejections attributed to `unknown`** | a metric silently mis-bucketing admission decisions, hiding which rule fired | **`fairness` was missing from the metric's closed label set**, so the least-served rule's rejections were relabelled | `test_label_sets_are_closed_and_small` |
| **R119** | *"starvation ratio 3.67"*, then *"2.33"* | **a per-tenant fairness number** — the phase's headline claim | **The driver never retried a 429, so it measured arrival luck**, not server behaviour; and at 22-26 served requests per run **the ratio is noise anyway** | a repeat run disagreeing by **3×** |

**The shared shape.** None of these was a defect in the product. **Every one was the instrument, and
every one produced a statement about the product that was confident, plausible and false.** R108
found a control that had never executed; R115 found a control that was present and looked absent —
**the same lesson from opposite sides, and the only thing that distinguishes the two is executing
it.**

**Two consequences written into the process rather than remembered** `[reasoned]`:

> **A probe must record what it saw, not only its verdict**, or a wrong verdict is unfalsifiable after
> the run.
>
> **The probe list must exercise a control against a running server, not only in-process.** R108's
> bug was invisible to inspection and would have been invisible to a probe asserting on the code
> rather than on behaviour.

**Six false premises found in CP5's own plan, recorded beside the defects** `[measured]`:

1. **"Nothing should need re-instrumenting"** — false as written; true only under the narrow reading
   (VP33).
2. **"Quota unit is USD"** — true for reporting, **impossible as the enforcement primitive** (VP35).
3. **"Re-key the cache with tenant"** — reversed by the charter amendment that post-dates the plan
   (VP36).
4. **"Cross-tenant leak attempts fail"** — **vacuous at CP5 by construction** (VP36).
5. **"Overspend beyond the cap"** — measures a shape the charter rejects (VP35).
6. **The plan's implicit premise that fairness is measurable at CP5's throughput** — **false**
   (VP35).

**Two run drivers with dry modes have now caught three real defects between them**
(`scripts/probe_auth_surface.py`, `scripts/run_fairness.py`, plus `src/harness/runmeta.py`), and the
cache-off interlock **refuses to measure the cache and call it a load curve** (VP31 defect 1).
`[read from source]`

---

<a id="VP38"></a>
## VP38 — What the platform phases never measured

Recorded because **an unstated absence reads as a pass.** Everything below is a gap this log is
asserting, not a task list.

**Open gaps, carried out of the platform phases:**

| id | what is open | why it matters |
|---|---|---|
| **G-P10** | **per-tenant fairness is unresolved, in both directions.** The gateway guard is retired (it could never fire); the admission gate's least-served rule fires but **could not be evaluated** — a share ratio from single-digit counts is noise. **Blocked on sizing the gate to the measured knee**, which is a throughput decision | VP35 |
| **G-B** | **the gate's 1b guard composition.** `quality_1b` ships **empty** and cannot block. The groundedness-vs-persona weighting is deliberately left open, and **the gate's quality half is inert by construction until someone chooses it.** Not a defect — a decision nobody has taken. The measured options are all bad: the mechanical candidates cannot gate (MDE 0.19-0.29), the judge is the only thing that sees grounding, and A7 bars it from CI | VP20, VP21 |
| **G-E** | ceiling-vs-realised cost. The cost layer prices at the `MAX_TOKENS` ceiling **and says so**; which one the optimizer should minimize is undecided | VP20 |
| **G-G** | **no retention policy on the determinism baseline store**, and nothing prunes it — while R18-class detection **depends** on it | VP22 |
| **G-P7** | `Readiness.degraded` is assigned `True` **nowhere**, so the spend breaker never reaches the ladder. **It descends on misconfiguration only** | VP30 |
| **G-P8** | **the DEFAULT generation path emits no terminal SSE event**, so a client cannot distinguish a completed answer from a truncated one. Other paths do emit one | VP31 |
| **G-P9** | the corruption check **does not exercise concurrency but is labelled as though it does.** It becomes a false safety claim the moment retrieval is offloaded to a thread or a second worker is added | VP32 |
| **CP-4c's owed change** | `retrieval_only` still serves an **empty HTTP 200**. The decision says users get a 503. **Two riders unsettled**: how the rung stays observable to operators, and that a **fourth 503 producer arrives with no discriminator** | VP30 |
| **pool recall** | computed by the eval harness, **never read by the gate** — and it is the guard the optimizer's audit specified for `top_k_fused` | VP24 |
| **`entity_pair`** | a declared scored family with **zero rows on disk**, where the vacuous-denominator check never fires because a family with no rows never forms a bucket | VP1 |
| **`_eval_set_stamp()`** | hashes the file, so it **cannot detect a split** — two reports over different halves carry the same provenance stamp | VP1 |
| **R40** | an **orphaned deferral**: STRUCTURAL's excerpts overlap DEFAULT's reranked picks by **27-99%**, and the wholesale-vs-targeted question was deferred to *"P2 on the hosted model"*. CP2 shipped and answered a different question sharing the word *routing*. **`R40` appears exactly once in the repo — its own definition** | VP16 |

**Measurements never taken, and why:**

- **No real traffic exists, anywhere, at any point.** Every harness figure carries the O10 qualifier
  verbatim (VP26), and the synthetic query distribution is **a design parameter that decides, in
  advance and by default, whether a retrieval component can ever demonstrate value again** (VP25).
  **This does not expire; it is D3.**
- **The genuinely expensive model tier was never priced.** `gemini-3.5-flash` is rpd 20 — 7.5 days
  for one k=3 pass — so *"routing is not adopted"* means **among the arms measurable on a free tier**
  (VP16).
- **Quality was never compared across provider arms.** One arm scored 31/50 cells, and the instrument
  could not have arbitrated at that n anyway (VP19).
- **The correct-refusal rate of the output-side guard is unmeasured.** It needs a generation pass over
  the 25 adversarial rows; only the false-refusal half (0/54) exists (VP6).
- **`UNAVAILABLE` was never walked.** Entering it means breaking the vector store on purpose;
  coverage stands at **3/5** against an attainable **4/5** (VP30).
- **Seven of eight fault-taxonomy rows are fidelity-unverified**, and three of four fault classes have
  **no client-side signature on the record at all** (VP29).
- **The SPA render of a degraded rung was never observed**, and a product decision was taken on **two
  of three** required observations (VP30).
- **Sustained throughput was never measured.** The headline 0.274 rps is a **burst rate** from 5
  requests in 18.2 s, which never fills the limiter's 60-second window (VP27).
- **Thread-safety was never established, and nothing establishes it.** Retrieval runs sync on a
  single-worker event loop; the corruption check is a determinism detector (VP32).
- **Backpressure was never achieved, only relocated.** The pile moved from an unbounded queue to the
  limiter's sleep and to the retrieval lock (VP28, VP34).
- **Whether any CP2-era number was taken while `/health` was reporting degraded was not checked.**
  Nothing in those registers depends on the readiness value, so no finding is suspected — **but this
  log does not clear them; it records that nobody looked** (VP30).
- **The `ttft > total` pairing was never reproduced**, and no raw log survives. **Cause
  unestablished, not fixed.** The client/server 20.4 s vs 2.0 s divergence is likewise unexplained
  (VP5).
- **The magnitude of an embedder mismatch's quality damage is deliberately unmeasured** — it needs a
  second ONNX download plus a full re-ingest, and **the assertion's value rests on the silence, not
  on the delta** (VP9).
- **The labeling rate (~17 rows/hour) is the one cost line with no artifact behind it** — 3 of 20 rows
  stopwatched, AI-assisted, a third method rather than the arm the runbook defined. `[estimate]`,
  **re-measure before letting it carry a decision.**
- **Google's per-model quota separation is `[inferred]`**, not measured: two windows opened separately
  in our limiter and a full two-arm run produced no 429, **but the wire measurement (VP13 item 6) was
  against a different provider.** If Google shares one bucket, **the failure is mostly silent** —
  429s absorbed by transport retry into latency and burned quota.
- **The multi-worker rate-limiter split is unfixed.** The windows live in one process, so N uvicorn
  workers hold N sets and the effective quota is **N×**. Deployment runs one worker, which is what
  makes the current design correct — **and nothing enforces that it stays one** (VP15).
- **`tpd` is not enforced in code** on the one provider where it binds (VP14).

**Two structural absences that no amount of work inside v2 would have closed:**

1. **No statistical quality comparison this system can produce is detectable at any reachable n.**
   MDE 0.121-0.185 against observed effects of 0.000-0.050; n required 470-2195; **measured twice on
   two independent occasions with a bit-identical result.** The gate's routine detection power lives
   in the **invariant** and **cost** layers, and **both defects this project has ever actually
   suffered were invariant-class with no statistics involved** (VP2, VP22).
2. **The gate cannot see grounding, and ungrounding is 32% cheaper.** Not a power problem — the
   mechanical metrics are **identical zeros** across a total regression, and no sample size fixes a
   metric that does not respond (VP21).

**The evidence trail itself is untracked.** `plans/`, `status/`, `notes/` and `commits/` are excluded
by `.gitignore` **by deliberate choice** (A11), so the reasoning behind every number in this log lives
on one machine until it is published into a tracked document. **The eval sets are genuinely tracked**,
so every cited SHA and hash in VP1 stays independently checkable — that half of reproducibility
holds. `[measured]`
