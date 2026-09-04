# Mitya v1 — The Style Pipeline

**What this document is:** the fine-tune at feature level — the stages that turn five novels into
the GGUF file `llama-server` loads, and the artifact each stage leaves behind. Why each choice was
made, and what about it was never measured, is [`02-finetune.md`](02-finetune.md); every number is in
[`EVIDENCE.md`](EVIDENCE.md).

This pipeline is **entirely offline**. Nothing here runs at request time; the request path consumes
exactly one of its outputs. For the whole-system view see [`DESIGN.md`](DESIGN.md).

---

## 1. The stages

```
   novels (scanned PDFs)
        │  pypdfium2 render + pytesseract OCR
        ▼
   raw text
        │  RecursiveCharacterTextSplitter
        ▼
   chunks
        │  teacher pass 1: a reader's thematic question about this chunk
        │  teacher pass 2: the answer, first person, in voice, length-capped,
        │                  strictly from this chunk
        ▼
   ChatML pairs  ──────────────────────────────▶  data/dataset  (6,979 pairs, E2)
        │
        │  QLoRA over unsloth/mistral-7b-instruct-v0.3-bnb-4bit
        ▼
   LoRA adapter
        │  merge_and_unload()
        ▼
   merged F16 weights
        │  llama.cpp convert
        ▼
   F16 GGUF master
        │  llama.cpp quantize  ×3
        ▼
   q4_k_m · q5_k_m · q8_0  ─────────────────────▶  q4_k_m is what gets served (E4)
```

---

## 2. Corpus to dataset

**Input:** the five novels as scanned PDFs. Pages are rendered with `pypdfium2` and OCR'd with
`pytesseract`, then split with LangChain's `RecursiveCharacterTextSplitter`. This is a **different
ingest from the RAG layer's**, which parses clean plaintext of the same novels ([`RAG.md`](RAG.md)
§1) — two ingests of the same books, with different tolerance for noise.

**Two teacher passes per chunk.** First a question generation pass — an intrigued reader's thematic
question about the chunk, existing only to produce a plausible user turn. Then an answer pass: the
answer **as Dostoevsky, first person, under 350 words, strictly from that chunk**. The answer is what
the model is learning, so that prompt carries every constraint that matters, the length cap included.
Pairs are cleaned of HTML and non-ASCII artifacts and emitted as **ChatML** rows.

**Artifact:** 6,979 synthetic pairs, produced at zero API spend on free-tier teacher quota
([E2](EVIDENCE.md#E2)).

**The property that governs everything downstream:** the student inherits the teacher's register,
faults included, and no later stage raises a model above the data it was trained on
([`02-finetune.md`](02-finetune.md) §3).

---

## 3. QLoRA training

**Base model:** `unsloth/mistral-7b-instruct-v0.3-bnb-4bit` — instruct-tuned already, so the adapter
spends its capacity on voice rather than on turn format, and pre-quantized to 4-bit, which removes an
on-the-fly quantization step.

**Method:** the base model is held frozen in 4-bit and only low-rank adapters are trained. Unsloth's
`FastLanguageModel` supplies the model, `trl`'s `SFTTrainer` the loop; no custom training code was
written.

**Configuration** (`src/training.py`, every value a literal, seed fixed —
[E3](EVIDENCE.md#E3)):

| | |
|---|---|
| Adapter | `r=32`, `lora_alpha=32` (1:1, not the usual 1:2 damping), `lora_dropout=0.1` |
| Targets | All seven attention and MLP projections |
| Optimizer / schedule | `adamw_8bit`, linear, 3 epochs, gradient checkpointing, packing on |
| Sequence length | `max_seq_length=1024`, chosen to match the upstream chunker so a training row is never truncated mid-answer |

`src/training.py` also holds the **canonical persona system prompt** — the one that the serving path
reproduces byte-identically ([`RAG.md`](RAG.md) §6).

**Where it ran:** Colab, not the 6 GB target card. The 6 GB constraint binds at *serve* time.

**Artifact:** a LoRA adapter. No training log, loss curve, step count or wall-clock survives in the
repository ([`02-finetune.md`](02-finetune.md) §4.1).

---

## 4. Merge

The adapter is merged into the base weights with Hugging Face's `merge_and_unload()`, then converted
by `llama.cpp` into a **single F16 GGUF master** — llama.cpp's serving path wants one GGUF, and
merging removes a runtime composition step and any chance of a mismatched adapter at serve time. The
F16 master is retained as the thing all quantizations derive from, so re-quantizing costs no
retraining.

---

## 5. GGUF quantization

`llama.cpp` quantizes the F16 master into **three levels — `q4_k_m`, `q5_k_m`, `q8_0`**
([E4](EVIDENCE.md#E4)).

**`q4_k_m` is the level actually served**, on memory fit and nothing else: it is the level that
leaves headroom on a 6.0 GB card at a useful context length with the KV cache quantized and the model
fully offloaded ([E1](EVIDENCE.md#E1)). No quality comparison across the three levels was ever run
([`02-finetune.md`](02-finetune.md) §5).

**The chat template was verified, not assumed.** The GGUF embeds the official Mistral-v0.3 template,
matching the template training ran through — confirmed by prompt trace. That check is what allows the
serving layer to be *eliminated* as a cause when answer quality is diagnosed
([`10-evaluation.md`](10-evaluation.md)).

**Toolchain, easy to lose and expensive to rediscover:** `llama.cpp` is built from source against the
system CUDA toolkit for both the offline tools and `llama-server`; separately, `llama-cpp-python`
must be compiled from source with `GGML_CUDA=on`, because the published wheel is CPU-only.
`requirements.txt` documents this as a comment block rather than an installable line.

---

## 6. What gets served

`llama-server` loads the `q4_k_m` GGUF, fully offloaded, with the KV cache quantized, and exposes an
OpenAI-compatible `/v1/chat/completions`. It is **the only GPU resident in the system**
([`DESIGN.md`](DESIGN.md) §6): every retrieval component runs on CPU, and within a request retrieval
finishes before generation starts.

The RAG layer talks to it over localhost HTTP and nothing else. The two subsystems share no code.

---

## 7. Three things this pipeline imposes on the rest of the system

They look like serving-layer decisions and are not; each is documented where it takes effect.

1. **The served persona head is byte-identical to `training.py`'s system prompt**, with retrieved
   passages appended as the author's own recollections rather than as quoted sources
   ([`RAG.md`](RAG.md) §6). It is inlined in the serving module rather than imported, because
   `training.py` pulls unsloth, torch, trl, peft and transformers at import scope.
2. **The model is never asked for inline citations**, because it was trained to refuse external
   references; grounding is a programmatic Sources list instead
   ([`07-generation.md`](07-generation.md)).
3. **A temperature floor and a zero-token retry exist in the serving path**, because the quantized 7B
   occasionally samples EOS as its first token — stochastic per run, never per query.

---

## 8. The honest status

**"It writes like Dostoevsky" was never measured.** There is no held-out evaluation of the fine-tune:
no perplexity on a held-out split, no style classifier, no before/after against base Mistral, no
human preference test ([E30](EVIDENCE.md#E30)).

What *was* measured is end-to-end and downstream, and its conclusion points back at this pipeline:
**the fine-tune, not the retrieval layer, is what caps answer quality** — established by measurement
rather than assumed ([`10-evaluation.md`](10-evaluation.md), [E25](EVIDENCE.md#E25)). The recorded
next step is a better training-data pass — a stronger teacher, a logged teacher id, and a held-out
evaluation — not retrieval tuning and not prompt composition.

---

## 9. Where to read next

The reasoning, the tradeoffs and the gaps: [`02-finetune.md`](02-finetune.md). What the prompt does
with the persona head: [`07-generation.md`](07-generation.md). How the ceiling was established:
[`10-evaluation.md`](10-evaluation.md). The substance layer this one joins: [`RAG.md`](RAG.md).
