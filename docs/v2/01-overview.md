# 01 — Overview: from v1 to v2

**v1 asked a hardware question. v2 asks a delivery question.** Nothing about the domain changed, no
novel was added, and the retrieval stack that answers a question is very nearly the one v1 shipped.
What changed is what surrounds it, what can be said about it, and where the tokens are generated.

**v1 lives on branch `main`, not in this tree.** Read a v1 document with
`git show main:docs/v1/<file>`; its evidence anchors are cited here as `[E23 (v1)]`.

---

## 1. What v1 answered

v1 built a Dostoevsky-voiced grounded assistant that fits on **one 6GB consumer GPU**: a Mistral-7B
QLoRA fine-tune quantized to GGUF, and a metadata-tagged RAG layer over the five novels — structural
parse, hybrid dense/sparse retrieval fused by native RRF, a FlashRank reranker, an offline entity map
and chapter summaries, FastAPI over SSE, a Vue SPA. It worked, and its closing finding was that the
**quality ceiling was the fine-tune, not the pipeline** (`docs/v1/01-overview.md`,
`docs/v1/10-evaluation.md`).

Every design decision in that set traces back to one constraint. The 6GB budget picked the
quantization, picked the reranker, picked local-mode Qdrant, picked `Semaphore(1)` for concurrency,
and picked the generation token ceiling. v1 was honest about this: the budget is named as *the*
binding constraint on page one.

## 2. What v2 asks instead

**At a fixed quality bar, what is the cheapest and most reliable way to serve grounded answers — and
can the system prove it stays that way as it changes?**

Three words carry weight. *Cheapest* means a cost number with a stated basis, not a vibe. *Most
reliable* means measured under concurrency, faults and hostile input rather than asserted. *Prove*
means an instrument exists, its detection floor is known, and a claim outside that floor is refused
even when it would flatter the work. v2 is a phase about **operating and measuring** a system that
already worked, which is why this set documents only the delta.

## 3. The architectural pivot: serving left the GPU

Generation moved to hosted model APIs behind a two-provider gateway. The pair was chosen for
**divergence** across streaming shape, error vocabulary and rate-limit surface rather than for price
— an abstraction validated against two near-identical providers is not validated at all
([VP12](EVIDENCE-PLATFORM.md#VP12)).

The consequence is larger than a latency figure: **the constraint that shaped every v1 decision
retired.** Once nothing is generating on the card, the arguments underneath a whole row of v1
choices stop being arguments and become inherited defaults that have to be re-examined one at a time.
Three did not survive contact:

- The global `Semaphore(1)` was justified by `llama-server --parallel 1`. With generation off the
  box the lock binds on retrieval alone, and it was measurably the wall
  ([VP27](EVIDENCE-PLATFORM.md#VP27)).
- The generation token ceiling of 400 was sized to fit a 3072-token local context. Carried into
  hosted serving it guillotined most answers mid-sentence ([VP18](EVIDENCE-PLATFORM.md#VP18)).
- *"The GPU is already paid for"* stopped being a cost argument at all, and local compute is now
  reportable only against a named reference instance ([VP14](EVIDENCE-PLATFORM.md#VP14)).

The general shape is worth stating once, because it recurs: **a number inherited from a constraint
you have escaped is not a default, it is a bug with a plausible history.**

## 4. The eight checkpoints

**CP0 — the measurement contract.** Before any optimization, establish what this project is able to
detect. It produced versioned eval sets with immutable row ids, a judge instrument with its variance
attributed, a frozen v1 baseline, and the finding that governs everything after it: the detection
floor sits **above** the effects being chased. See [`02-measurement-contract.md`](02-measurement-contract.md).

**CP1 — telemetry and config identity.** One span per stage boundary, feeding trace and metric from
the same instrumentation, plus a content-hashed frozen config artifact whose version *is* its
identity. It bought the first per-stage attribution the project ever had — rerank is the retrieval
budget, entirely — and it made "which arm produced this row" a fact on the span rather than a claim
in a notebook ([VP7](EVIDENCE-PLATFORM.md#VP7)). See [`03-telemetry-and-config.md`](03-telemetry-and-config.md).

**CP2 — the provider gateway.** Two adapters behind one function signature, with error mapping that
speaks the gateway's vocabulary rather than collapsing everything to 502. Routing between a cheap and
an expensive arm was simulated over the recorded cost matrix and **rejected before it was built** —
every routing family costs more than always sending traffic to the cheapest arm
([VP16](EVIDENCE-PLATFORM.md#VP16)). See [`04-provider-gateway.md`](04-provider-gateway.md).

**CP3 — the CI eval gate.** Three layers — invariants, cost, sampled quality — with no hand-set
threshold anywhere, because a constant goes stale as the system improves. Its most valuable output is
a negative one: it was measured against a total grounding regression and could not see it
([VP21](EVIDENCE-PLATFORM.md#VP21)). See [`05-ci-eval-gate.md`](05-ci-eval-gate.md).

**CP4 — the traffic harness.** Concurrency ramps, fault injection and a degradation ladder against a
live server. It found the knee at 1, moved it to 4 by narrowing the lock to retrieval, and found that
the system **never rejects anything** — it queues silently and unboundedly
([VP27](EVIDENCE-PLATFORM.md#VP27), [VP28](EVIDENCE-PLATFORM.md#VP28)). See
[`06-traffic-harness.md`](06-traffic-harness.md).

**CP5 — auth and tenancy.** argon2id on an already-installed dependency, opaque server-side sessions,
per-account and per-IP login limits, admission control against the queue CP4 measured. Identity costs
nothing measurable. **Per-tenant fairness is a non-result** and is reported as one
([VP35](EVIDENCE-PLATFORM.md#VP35)). See [`07-auth-and-tenancy.md`](07-auth-and-tenancy.md).

**CP6 — conversation memory.** Three tiers competing for one token budget — verbatim recent turns in
process memory, a typed episodic digest, a typed semantic profile — behind a swappable allocator
seam, with retention, caps and per-tenant purge. It is priced honestly and its benefit is left
unclaimed ([VM5](EVIDENCE-MEMORY.md#VM5)). See [`08-memory.md`](08-memory.md).

**CP7 — the optimizer.** A default-deny allowlist over the versioned config, an explicit value domain
per axis, an evolutionary search, and a gate that must not block. Four of five candidate axes did not
survive measurement, and the one cost saving it priced was **not** promoted; what did reach the
serving artifact is the fused depth at 10, Batch H's merge-only policy and its two span guards, and
composition C. It also produced the largest user-visible defect the phase found: retrieval
never sees the conversation ([VO8](EVIDENCE-OPTIMIZER.md#VO8)). See
[`10-optimizer.md`](10-optimizer.md) and [`11-cost-and-performance.md`](11-cost-and-performance.md).

The prompt/injection security work spans CP6 and CP7 and has its own document,
[`09-prompt-security.md`](09-prompt-security.md).

---

## 5. The delta map

Every v1 subsystem, and what v2 did to it. `unchanged` means v2 deliberately left it — see
[`14-unchanged-from-v1.md`](14-unchanged-from-v1.md) for why, and the v1 document for what it is.

| v1 subsystem | v2 | Note | Read |
|---|---|---|---|
| Corpus parsing (five novels → chapters) | **unchanged** | No novel added, no parser touched. The corpus stamp `515c707a5d69` never moved ([VP9](EVIDENCE-PLATFORM.md#VP9)) | `docs/v1/03-corpus-parsing.md` |
| Chunking (500/100, [E6 (v1)]) | **unchanged** | Ablation priced and deferred — **untested, not validated** | [14](14-unchanged-from-v1.md), `docs/v1/04-indexing.md` |
| Index (Qdrant, nomic + BM25, [E7 (v1)]) | **unchanged** | Vectors and collection identical; what was added is a *manifest* asserting which embedder built it, checked at startup | [03](03-telemetry-and-config.md), `docs/v1/04-indexing.md` |
| Offline artifacts — entity map | **changed** | Content untouched; `EntityMap._scan()` returned a `set`, making expansion order — and therefore the served chunks — vary by process. Fixed to occurrence order | [02](02-measurement-contract.md), `docs/v1/05-offline-artifacts.md` |
| Offline artifacts — chapter summaries | **unchanged** | Full coverage carried forward as-is ([E12 (v1)]) | `docs/v1/05-offline-artifacts.md` |
| Router (STRUCTURAL / DEFAULT) | **unchanged** | Same two routes, same conjunction rule. Its intent-keyword set moved out of the module onto the versioned artifact, because a knob the optimizer may search cannot be a module constant | [03](03-telemetry-and-config.md), `docs/v1/06-retrieval.md` |
| Hybrid retrieval + RRF fusion | **unchanged, one depth cut** | Two legs, native RRF, same prefetch. The BM25 leg was finally shown to carry a callable effect, on a query family v1's set did not contain. The **fused pool depth** is the one value that moved: 25 → **10**, measured as a latency axis worth 2.3× at 0.04% of cost ([VO6](EVIDENCE-OPTIMIZER.md#VO6)) | [10](10-optimizer.md), `docs/v1/06-retrieval.md` |
| Rerank (FlashRank, [E15 (v1)]) | **unchanged** | Not re-tuned; *attributed* for the first time, and it is ~84% of the retrieval path at the pre-cut fused depth ([VP7](EVIDENCE-PLATFORM.md#VP7)), 72.1% of a 917 ms path at the shipped depth of 10 ([VO6](EVIDENCE-OPTIMIZER.md#VO6)). `top_k_rerank` measured out pinned at 3 | [03](03-telemetry-and-config.md), [10](10-optimizer.md) |
| Context assembly | **changed in the module, not yet on the path** | The merge of adjacent served chunks and the de-duplication of their overlap was built and promoted, but the request path guards the whole step on a non-zero expansion radius, so at the promoted radius of 0 it does not run ([VO9](EVIDENCE-OPTIMIZER.md#VO9)). Neighbour *expansion* measured out at zero and ships off | [10](10-optimizer.md), [13](13-decisions-and-open.md) |
| Prompt composition | **changed** | Composition C promoted: violations fall and groundedness rises outside its MDE. v1's A/B result was withdrawn at CP0 | [09](09-prompt-security.md), [02](02-measurement-contract.md) |
| Generation | **changed** | Off the GPU onto two hosted providers; token ceiling 400 → 1024 ([VP18](EVIDENCE-PLATFORM.md#VP18)); hosted context raised to match ([VP9](EVIDENCE-PLATFORM.md#VP9)). The local arm survives as a subclassed adapter, never the default | [04](04-provider-gateway.md) |
| Cache | **changed** | L1 exact-match survives, keyed additionally on config and corpus version; memory-bearing requests bypass it entirely. A semantic L2 was built as a probe and **refuted**; a provider prefix cache cannot engage | [04](04-provider-gateway.md), [08](08-memory.md) |
| API surface | **changed** | Same `/chat` SSE contract, plus auth, admission control, per-tenant labels, a typed error mapping, and a degradation ladder | [07](07-auth-and-tenancy.md), [06](06-traffic-harness.md) |
| Frontend (Vue SPA) | **unchanged** | Explicitly out of scope for every batch — with a cost: a product decision about a degraded rung was taken without the SPA render anyone required ([VP30](EVIDENCE-PLATFORM.md#VP30)) | [06](06-traffic-harness.md), `docs/v1/09-frontend.md` |
| Fine-tune / local `q4_km` arm | **retired from the request path** | Not deleted, not retrained. v2 excludes weight training by charter | `docs/v1/02-finetune.md`, `docs/v1/FINE_TUNE.md` |
| — | **new** | Measurement contract: versioned eval sets, power analysis, judge variance attribution, frozen baseline | [02](02-measurement-contract.md) |
| — | **new** | Telemetry spine and content-hashed config identity; the operator plane | [03](03-telemetry-and-config.md) |
| — | **new** | Provider gateway, pricing rows, rate-limit tables, retries | [04](04-provider-gateway.md) |
| — | **new** | CI eval gate | [05](05-ci-eval-gate.md) |
| — | **new** | Traffic harness, fault injection, degradation ladder | [06](06-traffic-harness.md) |
| — | **new** | Authentication, sessions, per-tenant isolation, admission control | [07](07-auth-and-tenancy.md) |
| — | **new** | Conversation memory: three tiers, one budget, an allocator seam | [08](08-memory.md) |
| — | **new** | Injection/benign eval sets and the prompt-security work | [09](09-prompt-security.md) |
| — | **new** | Config optimizer: allowlist, domains, search, objective | [10](10-optimizer.md) |

**How to read the `unchanged` rows.** They are not omissions and they are not free. Each one is a
decision to spend the phase's scarce resource — quota, and the exclusive lock on the vector store —
somewhere else. Two of them are decisions with a price attached and no result behind them, and
[`14-unchanged-from-v1.md`](14-unchanged-from-v1.md) says which.

---

## 6. What this set will not tell you

Three things are worth knowing before reading further, because each is a claim v2 declined to make.

**No real traffic exists, anywhere, at any point.** Every harness and session figure is synthetic, by
a documented and deliberate construction, and the synthetic query distribution decides in advance
whether a retrieval component can demonstrate value at all
([VP38](EVIDENCE-PLATFORM.md#VP38), [VP26](EVIDENCE-PLATFORM.md#VP26)).

**The quality instrument cannot arbitrate most of the changes anyone would want to make.** That is
CP0's central finding and it is not a power problem that a bigger run would fix — see
[`02-measurement-contract.md`](02-measurement-contract.md).

**The instrument was wrong more often than the system was.** More than a dozen instruments produced
confident, plausible, false statements across four phases, and not one was caught by a clean run.
[`12-instruments.md`](12-instruments.md) collects them; [`13-decisions-and-open.md`](13-decisions-and-open.md)
states the honest headline.
