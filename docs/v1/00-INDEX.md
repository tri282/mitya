# Mitya v1 — Documentation Set

**Mitya** is a Dostoevsky-voiced local LLM that answers questions grounded in his five novels, on a
single 6GB consumer GPU. It has two independent subsystems that meet at generation time. **Style** is
a Mistral-7B-Instruct-v0.3 QLoRA fine-tune trained on synthetic Q&A generated from the novels, merged
and quantized to GGUF via `llama.cpp`. **Substance** is a metadata-tagged RAG layer over clean
plaintext of the same five novels: a per-book structural parser that yields `book/part/chapter`
labels, a Qdrant collection with dense (nomic) and sparse (BM25) vectors fused by native RRF, a
FlashRank reranker, an offline entity map and per-chapter summary set built by a teacher LLM and
human-reviewed, and a FastAPI service that streams answers over SSE. A **Vue 3 SPA** sits on the
`/chat` endpoint. The GPU runs only the quantized LLM; every retrieval component runs on CPU, and
within a request the two phases are sequential, so the 6GB budget is never contended. Grounding is
shown as a **programmatic Sources list** built from retrieval metadata after the stream ends —
never as inline citations, because the fine-tune was explicitly trained to refuse external
references.

---

## Reading order

| # | Document | What it answers |
|---|---|---|
| — | `DESIGN.md` | **Start here.** What the system is: subsystem map, components, API surface, and one request traced end to end |
| — | `RAG.md` | The substance layer at feature level: corpus, artifacts, index, query pipeline, prompt, cache, API |
| — | `FINE_TUNE.md` | The style pipeline at feature level: dataset → QLoRA → merge → GGUF → what gets served |
| **01** | `01-overview.md` | The problem, the 6GB budget as the binding constraint, the subsystem map, and one request end to end |
| **02** | `02-finetune.md` | Dataset construction → QLoRA → merge → GGUF quantization |
| **03** | `03-corpus-parsing.md` | Novels → structural units; what metadata exists on a chunk and why it has to |
| **04** | `04-indexing.md` | Chunking, embedding, the Qdrant collection, the dense and sparse vectors |
| **05** | `05-offline-artifacts.md` | The entity map and chapter summaries: built once, by an LLM, then human-reviewed |
| **06** | `06-retrieval.md` | Router → hybrid retrieval → RRF fusion → rerank → context assembly |
| **07** | `07-generation.md` | Prompt composition, the persona/grounding seam, decoding, the cache |
| **08** | `08-api.md` | Endpoints, the SSE contract, concurrency, failure modes |
| **09** | `09-frontend.md` | The Vue 3 SPA, SSE consumption, the design system |
| **10** | `10-evaluation.md` | Eval sets, metrics, the retrieval headline, answer quality |
| **11** | `11-ablation.md` | Per-stage attribution, the arms, and the withdrawal |
| **12** | `12-decisions.md` | Rejected options and why; forward pointers to v2 |
| — | `EVIDENCE.md` | **Every number**, with unit, date, producer, and provenance tag |

**Three tiers.** `DESIGN.md` / `RAG.md` / `FINE_TUNE.md` show **what the system is** — skim these
first. `01`-`12` argue **why it is that way**; read `01` first and `12` last. `EVIDENCE.md` holds
**every number** and is reached by anchor from both tiers above.

Within tier 2: `02` is independent of `03`-`09` and can be skipped by a reader who only cares about
the RAG layer. `11` depends on `10`.

---

## Headline numbers

| | Value | Evidence |
|---|---|---|
| GPU budget / measured use | 6.0 GB ceiling · **5.0 GB** at 4096 ctx, full offload | [E1](EVIDENCE.md#E1) |
| Fine-tune | Mistral-7B QLoRA, r=32 / α=32, 3 epochs, **6,979** synthetic ChatML pairs | [E2](EVIDENCE.md#E2), [E3](EVIDENCE.md#E3) |
| Served model | GGUF **`q4_k_m`** (of three built) | [E4](EVIDENCE.md#E4) |
| Corpus | 5 novels → **231 chapters** parsed | [E5](EVIDENCE.md#E5) |
| Index | **3,284** points, 500-token chunks / 100 overlap, 768-dim nomic + BM25 (IDF) | [E6](EVIDENCE.md#E6), [E7](EVIDENCE.md#E7), [E8](EVIDENCE.md#E8) |
| Entity map | 678 people extracted → **256 keys** after a human gate, zero same-book collisions | [E10](EVIDENCE.md#E10) |
| Summaries | **231 / 231** chapters, 0 gaps | [E12](EVIDENCE.md#E12) |
| Retrieval pipeline | 50/leg prefetch → RRF → 25 fused → FlashRank → **3** | [E15](EVIDENCE.md#E15) |
| **Retrieval headline** | **`recall@5 = 0.720` (18/25)** as v1 closed · **0.7105 (27/38)** after later correction | [E23](EVIDENCE.md#E23) |
| Answer quality (LLM-judged, 36×2) | persona **3.11 / 3.22**, groundedness **2.44 / 2.53** of 5 — **no A/B winner** | [E24](EVIDENCE.md#E24) |
| The ceiling | capped by the **fine-tune**, not the pipeline — established by measurement | [E25](EVIDENCE.md#E25) |
| Ablation | 6 arms, under 10 min, zero API spend — **deltas later withdrawn as noise** | [E26](EVIDENCE.md#E26), [E27](EVIDENCE.md#E27) |
| Latency | retrieval **~2.25 s** (87% rerank) · TTFT **~7.9 s** · total **~21.3 s** · cache hit **~0.008 s** | [E28](EVIDENCE.md#E28) |
| Frontend | 10 Vue components, 2,746 LOC, 19/19 unit tests, 340 KB build | [E21](EVIDENCE.md#E21) |
| What was never measured | the absences, listed plainly | [E30](EVIDENCE.md#E30) |

---

**Every number in this set lives in `EVIDENCE.md`.** Documents `01`–`12` reference it by anchor and
state no figure of their own. Anchors `E1`–`E30` are frozen and are never renumbered.
