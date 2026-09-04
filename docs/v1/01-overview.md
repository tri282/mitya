# 01 — Overview

**What this document is:** the problem Mitya solves, the one constraint that shaped every later
decision, the two subsystems and where they meet, and one request traced end to end. Every figure
cited here lives in [`EVIDENCE.md`](EVIDENCE.md).

---

## 1. The problem

Mitya answers questions about Dostoevsky's five novels **in Dostoevsky's voice**, grounded in the
actual text, entirely on one consumer desktop. Two properties are wanted at once and they pull
against each other:

- **Style** — the answer should read as the author, first-person, in register.
- **Substance** — the answer should be about what is actually in the books, and the reader should be
  able to see which chapters it came from.

A general instruction model with a good system prompt gives style without substance and drifts into
plot invention. A plain RAG stack over the novels gives substance in the voice of a research
assistant. v1 builds the two separately and joins them at generation time: a fine-tune supplies the
voice, a retrieval layer supplies the passages, and the passages are injected into the fine-tuned
model's prompt at request time.

The second, quieter goal was methodological: build it so that every claim about it has an address.
That is why this documentation set is split — prose here, numbers in `EVIDENCE.md`.

---

## 2. The binding constraint: one 6GB GPU

The target machine is a single **GTX 1060, 6.0 GB VRAM** ([E1](EVIDENCE.md#E1)). This is not a
performance preference. It is the constraint that decides the architecture, and the project's stated
priority order puts **hardware fit above everything else**, above goal fit and usability both.

What the budget forced, decision by decision:

| Decision | Forced by the budget |
|---|---|
| QLoRA rather than a full fine-tune | 7B full fine-tuning does not fit consumer memory ([E3](EVIDENCE.md#E3)) |
| GGUF `q4_k_m` as the served weights | `q5_k_m` leaves no headroom at any useful context ([E1](EVIDENCE.md#E1), [E4](EVIDENCE.md#E4)) |
| CPU embedder (quantized ONNX, no torch) | the GPU is already fully allocated ([E7](EVIDENCE.md#E7)) |
| BM25 rather than SPLADE or any learned sparse encoder | a neural sparse encoder is a second GPU tenant ([E9](EVIDENCE.md#E9)) |
| FlashRank (~100 MB ONNX, CPU) rather than a bge/mxbai cross-encoder | same ([E15](EVIDENCE.md#E15)) |
| Qdrant in local (embedded) mode | zero-ops for a static corpus, no server process ([E8](EVIDENCE.md#E8)) |
| Chapter summaries pre-generated offline by a teacher LLM | generating them at request time would contend for the GPU ([E12](EVIDENCE.md#E12)) |

The design predicted 5.2–5.5 GB and the measurement came back at **5.0 GB of 6.0 GB used**
([E1](EVIDENCE.md#E1)). The prediction was conservative rather than wrong, and it is retained,
because the headroom argument for `q4_k_m` over `q5_k_m` rests on it.

### 2.1 The sequencing property that makes it work

The budget survives because **only one component ever touches the GPU**. llama-server holds the
quantized model; the embedder, the BM25 encoder, Qdrant and the reranker are all CPU-only. Within a
request the two phases are strictly sequential — retrieval completes, then generation starts — and
the service admits one request at a time by semaphore ([E20](EVIDENCE.md#E20)). GPU contention
therefore does not exist by construction, not by tuning. Nothing has to be balanced at runtime
because nothing competes.

The cost of that property is latency, and it was paid knowingly: rerank alone accounts for 87% of
retrieval time, and total end-to-end sits near **21 s** ([E28](EVIDENCE.md#E28)). Both figures
exceeded their pre-build budgets and were accepted rather than chased, under the same priority
order.

---

## 3. The two subsystems

**Style — the fine-tune.** Mistral-7B-Instruct-v0.3, QLoRA-adapted on synthetic first-person Q&A
generated from the novels by a teacher LLM, merged to F16 and quantized to GGUF, served by
llama-server. See [`02-finetune.md`](02-finetune.md).

**Substance — the RAG layer.** Clean plaintext of the same five novels, parsed per-book into
`book/part/chapter` units, chunked, indexed in Qdrant with a dense and a sparse vector, retrieved
hybrid and reranked, with an offline entity map and per-chapter summary set alongside. See
[`03-corpus-parsing.md`](03-corpus-parsing.md) through [`06-retrieval.md`](06-retrieval.md).

The two were built independently and share no code. They meet in exactly one place: **the prompt**.

### 3.1 The seam, stated exactly

The fine-tune's training rows never contained retrieved context, and its system prompt explicitly
forbids referencing external sources ([E17](EVIDENCE.md#E17)). Injected passages are therefore
*out-of-distribution* for the model that has to use them. This is the central design tension of the
whole system, and two consequences follow from it:

1. **The passages are framed as the author's own recollections**, appended to the verbatim fine-tuned
   persona text in the system prompt — aligned with the "no external references" conditioning rather
   than fighting it ([E17](EVIDENCE.md#E17)).
2. **Grounding is displayed programmatically, never as inline citations.** The original design asked
   the model to emit `[Book, Chapter]` markers; a model trained to refuse external references will
   not do that reliably. The Sources list is instead assembled in code from retrieval metadata after
   the stream ends, which makes grounding display independent of whether the model cooperates
   ([E17](EVIDENCE.md#E17)). This is the single reason chunk metadata has to be correct — see
   [`03-corpus-parsing.md`](03-corpus-parsing.md).

---

## 4. One request, end to end

A `POST /chat` with a query and optional `book/part/chapter` filters:

```
  request
    -> semaphore acquire (one in flight)          E20
    -> cache lookup, exact-match on normalized query + filters   E18
         hit  -> whole answer as one token event, then sources, meta, done
    -> route()                                     E14
         STRUCTURAL if summary intent AND a resolvable chapter scope
         DEFAULT    otherwise
    -> entity resolution + query expansion         E10, E11
         canonical name and aliases appended to the query text
    -> retrieve()                                  E15
         dense (nomic, CPU) 50  \
                                 >- Qdrant native RRF -> 25 -> FlashRank -> 3
         sparse (BM25, IDF)  50 /
    -> context assembly                            E16
         DEFAULT:    top-3 reranked chunks
         STRUCTURAL: chapter summary + 2 chapter-filtered excerpts
    -> prompt composition                          E17
         persona head (verbatim from training) + passages as recollections
         user turn carries the question only
    -> llama-server /v1/chat/completions, streamed E17
    -> SSE: token* -> sources -> meta -> close     E19
    -> cache.set, then meta emitted last
```

Notable properties of that path:

- **Routing is a pure function**, keyword intent AND a resolvable chapter key, with no model in it.
  A classifier would be probabilistic where an exact test is available and free
  ([E14](EVIDENCE.md#E14)).
- **The first token is primed before the SSE stream opens**, so an unreachable or slow llama-server
  becomes a clean 503/504 rather than a hung stream ([E20](EVIDENCE.md#E20)).
- **Failures degrade rather than abort**: a hard filter matching zero chunks is retried unfiltered
  (`filter_relaxed`); a missing summary falls STRUCTURAL back to DEFAULT; no context at all still
  answers, with Sources omitted ([E20](EVIDENCE.md#E20)).
- **`meta` is emitted last, after the cache write**, so `total_ms` is a truthful end-to-end figure
  ([E19](EVIDENCE.md#E19)).

A Vue 3 SPA consumes the same endpoint through `fetch` + a stream reader, and is served as static
files by the same FastAPI process ([E21](EVIDENCE.md#E21)). It adds no backend component and no GPU
consumer.

---

## 5. What v1 learned about itself

**The retrieval pipeline worked. The fine-tune was the ceiling.** Stated plainly because it is the
most important result in the project, and because it was **established by measurement, not
assumed** ([E25](EVIDENCE.md#E25)).

Retrieval delivered the gold chapter into the context window roughly seven times in ten
(`recall@5 = 0.720`, 18/25 on the v1 harness — [E23](EVIDENCE.md#E23)), with full artifact coverage
and router accuracy 6/6 on every ablation arm. The model then scored about **2.5 of 5 on
groundedness** ([E24](EVIDENCE.md#E24)) — and, decisively, scored the same on *both* prompt
compositions. A prompt-plumbing problem moves when the plumbing moves; this did not. The prompt trace
was separately verified byte-clean between training and serving, eliminating the serving layer. The
residual traces to the training data: a weak teacher pass, whose academic register the student
reproduces as meta-commentary ([E25](EVIDENCE.md#E25)).

Two corollaries the project acted on:

- **The prompt-composition A/B was the wrong experiment for the state of the system.** It measured a
  seam while the binding constraint sat two layers upstream. Both compositions were kept, because
  there was no winner to keep ([E24](EVIDENCE.md#E24)).
- **The per-stage ablation's deltas were later withdrawn as noise.** A 27-row eval set cannot resolve
  a one-row effect, and the decision rule that says so was written down *before* the run. The project
  caught its own false positive on re-analysis rather than carrying an attractive attribution table
  forward ([E26](EVIDENCE.md#E26), [E27](EVIDENCE.md#E27) — a later correction, not what v1 believed
  at the time). See [`11-ablation.md`](11-ablation.md).

**What was never measured is listed as such** ([E30](EVIDENCE.md#E30)) — no held-out fine-tune
evaluation, no embedder or reranker bake-off, no confidence interval on any metric. An unstated
absence reads as a pass, so the absences are stated.
