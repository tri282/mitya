# 02 — The Fine-Tune

**What this document is:** how the style subsystem was built — synthetic dataset construction, QLoRA
adaptation, merge, GGUF quantization — the tradeoff behind each step, and an explicit account of what
about it was never measured. All figures live in [`EVIDENCE.md`](EVIDENCE.md).

This document is independent of `03`–`09`. A reader interested only in the RAG layer can skip it.

---

## 1. What is being trained, and why a fine-tune at all

The target is a **single narrow voice**: first-person Dostoevsky, in register, at length, without
plot invention. That is a style-transfer problem, not a knowledge problem — the knowledge is supplied
at request time by the retrieval layer ([`01-overview.md`](01-overview.md) §3). Prompting alone
produces pastiche that drifts back to assistant register over a long answer; a fine-tune moves the
default register itself.

Two consequences of "one narrow voice" recur below: the adapter is deliberately given real weight
rather than being damped, and the corpus is stylistically homogeneous enough that regularization
matters more than it would in a general instruct tune ([E3](EVIDENCE.md#E3)).

---

## 2. Base model: Mistral-7B-Instruct-v0.3

Chosen on three properties, in this order:

- **It fits.** 7B at 4-bit is the largest class that trains under QLoRA on free Colab and *serves*
  under 6 GB after quantization ([E1](EVIDENCE.md#E1)). Anything larger fails the binding constraint
  at serve time, which is the constraint that cannot be relaxed.
- **Instruct-tuned already.** The system/user/assistant turn structure is already learned, so the
  adapter spends its capacity on voice rather than on format.
- **The Unsloth 4-bit variant exists** (`unsloth/mistral-7b-instruct-v0.3-bnb-4bit`), which removes
  the on-the-fly quantization step and pairs with Unsloth's chat-template handling — a detail that
  matters later, at §5, because the training template and the served template must agree.

**Not benchmarked against alternatives.** No comparison against Llama-3-8B, Qwen, or a smaller base
was run. The choice is reasoned, not measured ([E30](EVIDENCE.md#E30)).

---

## 3. The dataset: synthetic Q&A over the novels

There is no corpus of "Dostoevsky answering reader questions". It had to be manufactured.

**The construction** ([E2](EVIDENCE.md#E2)): the five novels are ingested from scanned PDFs by
rendering pages with `pypdfium2` and OCRing with `pytesseract`, split with LangChain's
`RecursiveCharacterTextSplitter`, and each chunk is put through **two teacher passes** — first
generate an intrigued reader's thematic question about the chunk, then answer it **as Dostoevsky, in
the first person, under 350 words**, strictly from that chunk. Pairs are cleaned of HTML and
non-ASCII artifacts and emitted as ChatML. The result is **6,979 pairs at zero API spend**, entirely
on free-tier teacher quota ([E2](EVIDENCE.md#E2)).

**Why this shape.** The answer is what is being learned, so the answer prompt carries all the
constraints that matter: first person, in-voice, length-capped, grounded in the chunk in front of it.
The question prompt exists only to produce a plausible user turn. The word cap is doing real work —
it is what stops the teacher from producing essay-shaped answers that the student then reproduces at
inference time regardless of what was asked.

**What the method costs you, and this is the load-bearing paragraph of the document:**

1. **The student inherits the teacher's register, including its faults.** Every stylistic habit of the
   teacher's output — hedging, framing, academic distance — is what the model is being trained to
   reproduce. This is not a theoretical risk in this project: the dominant failure mode at evaluation
   was **meta-commentary**, answers that discuss the question as a generic academic assistant, and it
   was traced back to the teacher's register ([E25](EVIDENCE.md#E25)).
2. **The ceiling is set at dataset-construction time.** No amount of adapter tuning, quantization
   choice, or prompt engineering downstream raises a model above the data it was trained on. That is
   exactly what v1 measured — see §7.
3. **OCR noise is upstream of everything and was never quantified.** No OCR error rate was measured,
   and the number of OCR'd chunks the 6,979 pairs were drawn from is not recorded anywhere
   ([E2](EVIDENCE.md#E2), [E30](EVIDENCE.md#E30)).
4. **The teacher's identity was not logged.** The only surviving identification of the model that
   produced the training answers is a prose reference in the post-hoc diagnosis
   ([E2](EVIDENCE.md#E2)). For a pipeline whose output quality is bounded by its teacher, that is the
   single worst provenance gap in the project.

Note that the fine-tune corpus is **OCR'd PDFs**, while the RAG layer parses **clean plaintext** of
the same novels ([E5](EVIDENCE.md#E5)). Two different ingests of the same books, for two different
purposes, with different tolerance for noise: a garbled word in a training chunk is diluted across
7k pairs, whereas a garbled word in a retrieved passage is shown to the user.

---

## 4. QLoRA rather than a full fine-tune

**The reason is arithmetic, not preference.** Full fine-tuning a 7B model means optimizer state and
gradients in addition to full-precision weights — tens of gigabytes, off the table on both the
training box (free Colab) and the target card. QLoRA holds the base model frozen in 4-bit and trains
only low-rank adapters, which is what makes the run possible at all ([E3](EVIDENCE.md#E3)).

**The two non-default choices, and the one reason behind both** ([E3](EVIDENCE.md#E3)):

- **Rank : alpha at 1:1** (`r=32`, `lora_alpha=32`). A personalized style transfer wants the adapter
  to carry real weight rather than be scaled down; the usual 1:2 damping is tuned for adapters that
  should perturb a general model gently. Here the perturbation *is* the product.
- **`lora_dropout=0.1`.** The corpus is stylistically homogeneous — one author, one register, one
  translator — so it needs regularization more than a typical instruct tune, where input diversity
  provides it for free.

Everything else is deliberate default: all seven attention and MLP projections targeted, `adamw_8bit`,
gradient checkpointing, 3 epochs, linear schedule, packing on, seed fixed
([E3](EVIDENCE.md#E3)). Unsloth's `FastLanguageModel` plus `trl`'s `SFTTrainer` supply the loop; no
custom training code was written, which is the correct amount of training code to write.

**One coupling worth naming:** `max_seq_length` is 1024 tokens, and that is not an independent knob —
it is chosen to match the 1600-character upstream chunker, so a training row is never truncated
mid-answer ([E3](EVIDENCE.md#E3)).

**Training ran on Colab, not on the 6GB target card.** The 6 GB constraint binds at *serve* time. Two
different machines with two different budgets, and conflating them would have ruled out the project
before it started.

### 4.1 What the training run did not leave behind

`SFTTrainer` logs every 100 steps and **no log survives in the repository**. Consequently there is:

- no training wall-clock,
- no step count,
- no loss curve, final or eval,
- no record of which GPU Colab allocated.

([E3](EVIDENCE.md#E3), [E30](EVIDENCE.md#E30)). Reproducing the run would reproduce the
configuration, not the observation. The seed and every hyperparameter are literals in
`src/training.py`, so the configuration is recoverable; the run is not.

---

## 5. Merge and GGUF quantization

The adapter is merged into the base weights with Hugging Face's `merge_and_unload()`, converted to a
single F16 GGUF master, and quantized by `llama.cpp` into **three levels — `q4_k_m`, `q5_k_m`,
`q8_0`** ([E4](EVIDENCE.md#E4)).

**Why merge at all rather than serving base + adapter.** llama.cpp's serving path wants one GGUF.
Merging also removes a runtime composition step and any chance of a mismatched adapter at serve time.
The F16 master is retained as the thing all three quantizations are derived from, so re-quantizing
later costs no retraining.

**Why `q4_k_m` is the level actually served.** Purely the VRAM arithmetic of
[E1](EVIDENCE.md#E1): `q4_k_m` weights land near 4.4 GB and the full stack measures **5.0 GB of the
6.0 GB budget** with KV cache quantized to `q8_0` and full offload; `q5_k_m` weights alone are near
4.9 GB and leave no headroom at any useful context length ([E1](EVIDENCE.md#E1),
[E4](EVIDENCE.md#E4)). `q8_0` is not a serving candidate on this card at all.

**Be clear about what that argument is and is not.** It is a memory-fit argument. **No perplexity,
benchmark, or quality comparison across the three levels was ever run** ([E4](EVIDENCE.md#E4),
[E30](EVIDENCE.md#E30)). The claim supported is "`q4_k_m` is the only level that fits", not "`q4_k_m`
costs little quality". The GGUF file sizes were not recorded either.

**Toolchain detail that is easy to lose and expensive to rediscover** ([E4](EVIDENCE.md#E4)):
`llama.cpp` is built from source against the system CUDA toolkit for the offline tools and
`llama-server`; separately, `llama-cpp-python` must be compiled from source with `GGML_CUDA=on`,
because the published wheel is CPU-only. `requirements.txt` documents this as a comment block rather
than an installable line, since the build needs environment variables pip cannot carry.

**The chat template was verified, not assumed.** The GGUF embeds the official Mistral-v0.3 template,
matching the template Unsloth trained through — confirmed by prompt trace at P7
([E4](EVIDENCE.md#E4)). This check is what allows the serving layer to be *eliminated* as a cause in
§7 rather than merely suspected.

---

## 6. How the fine-tune constrains the rest of the system

Three downstream facts follow directly from decisions made here, and are documented where they take
effect:

- **The persona head served at request time is byte-identical to `training.py`'s system prompt**, and
  passages are appended to it as the author's own recollections rather than as quoted sources
  ([E17](EVIDENCE.md#E17)).
- **No inline citations are requested of the model**, because it was trained to refuse external
  references; the Sources list is built in code instead ([E17](EVIDENCE.md#E17)).
- **A temperature floor of 0.6 and a zero-token retry exist** because 10 of 72 generations in the
  quality A/B returned nothing — the weak q4 7B sampling EOS as its first token, per-run and never
  per-query ([E17](EVIDENCE.md#E17)). This is the one part of the quality ceiling that turned out to
  be fixable at the serving layer.

---

## 7. The honest status of "it writes like Dostoevsky"

**That claim was never measured.** There is no held-out evaluation of the fine-tune: no perplexity on
a held-out split, no style classifier, no before/after comparison against base Mistral, no human
preference test ([E30](EVIDENCE.md#E30)). A reader must not take the phrase as a result.

What *was* measured is downstream and end-to-end: an LLM judge scored 36 queries × 2 prompt
compositions on persona retention and groundedness ([E24](EVIDENCE.md#E24)). Persona retention landed
near 3 of 5 and groundedness near 2.5 of 5, with **no winner between the compositions** — and that
invariance is the evidence that the ceiling is the model, not the prompt plumbing
([E25](EVIDENCE.md#E25)). Combined with a clean prompt trace (§5) and a retrieval layer that put the
gold chapter in context roughly seven times in ten ([E23](EVIDENCE.md#E23)), the elimination points
at this document's subject.

**The actionable conclusion, recorded at the time:** the next real gain is a better training-data
pass — a stronger teacher, a logged teacher id, and a held-out evaluation — not retrieval tuning,
not a reranker swap, and not prompt composition ([E25](EVIDENCE.md#E25)).
