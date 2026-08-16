# Fine-Tuning Pipeline

## Overview
E2E data generation, fine-tuning, and quantization pipeline designed to create my niche LLM use case. Specifically, it trains a model to adopt the literary, existential, and philosophical tone of Fyodor Dostoevsky. The pipeline spans from raw PDF ingestion and synthetic dataset generation to QLoRA fine-tuning and final deployment-ready GGUF quantization.

---

## Data Generation Pipeline

See `src/generation.py` & `scripts/build_dataset.py`.

* **Ingestion and OCR:** Raw text is extracted from PDF files. Because literary scans often contain images or complex formatting,  it uses `pypdfium2` to render pages as images and `pytesseract` (OCR) to extract the text reliably.

* **Chunking:** The raw text is split using LangChain's `RecursiveCharacterTextSplitter` (chunk size: 1600, overlap: 100).
* **Synthetic Q&A Generation:** A "Teacher-Student" LLM setup (with Gemini or Ollama) generates the dataset:
    * **1. Question Generation:** The LLM is prompted to act as an intrigued reader analyzing a specific text chunk, generating a deep, thematic question without directly referencing plot points.
    * **2. Answer Generation:** The LLM is then prompted to act as Dostoevsky himself, answering the generated question in the first person (under 350 words) based strictly on the text chunk.
* **Formatting:** The raw Q&A pairs are saved to a CSV, cleaned of HTML/non-ASCII artifacts, and dynamically converted into the standard ChatML format (System, User, Assistant) before being fed into the Hugging Face dataset.

**Dataset as built:** `data/processed/chatml_cleaned_data.jsonl`, **6,979 pairs** (~11MB), generated entirely on free-tier teacher quota (zero API spend).

---

## Fine-Tuning Architecture

See `src/training.py`. I use **QLoRA** (Quantized Low-Rank Adaptation) via the Unsloth library since I'm broke and memory stores up quickly in Colab.
* **Base Model:** `unsloth/mistral-7b-instruct-v0.3-bnb-4bit`.
* **Wrapper/Trainer:** The pipeline utilizes `SFTTrainer` (Supervised Fine-Tuning) from the `trl` library.

**Hyperparameters as trained** (`src/training.py`):

| | |
|---|---|
| LoRA rank / alpha | `r=32`, `lora_alpha=32` (1:1 — a personalized style transfer wants the adapter to carry real weight, not be damped) |
| Target modules | all 7 — `q/k/v/o_proj`, `gate/up/down_proj` |
| Dropout / bias | `lora_dropout=0.1`, `bias="lora_only"` — the corpus is stylistically homogeneous, so it needs regularization more than a typical instruct tune |
| Sequence length | 1024 (matches the 1600-char / 100-overlap chunker upstream) |
| Schedule | 3 epochs, `lr=2e-4`, per-device batch 4 with gradient accumulation, gradient checkpointing (`"unsloth"`) |

The rank/alpha and dropout choices are the two that are *not* defaults, and both follow from the same fact: the target is a single narrow voice, not broad instruction-following.

---

## Merging and Quantization

The base model and LoRA adapters are first merged (via Hugging Face's `merge_and_unload()`) to generate a master `F16.gguf` file. Then `llama.cpp` is used to quantize the merged model to the following quantizations: **Q4_K_M, Q5_K_M, Q8_0**

### llama.cpp toolchain

Two separate pieces, built against **system CUDA Toolkit 13.2** (`nvcc` at `/usr/local/cuda/bin/nvcc`):

* **Raw C++ executables** — `llama.cpp` is cloned from GitHub and compiled with `cmake` (`cmake -B build && cmake --build build --config Release -j`). The resulting binaries under `build/bin/` do the offline work: `convert_hf_to_gguf.py` (merge -> F16 GGUF) and `llama-quantize` (F16 -> Q4_K_M / Q5_K_M / Q8_0). At serve time `llama-server` exposes the OpenAI-compatible HTTP endpoint the RAG layer calls. Full sequence in `scripts/quantize.sh`. These are **not** pip packages.
* **`llama-cpp-python`** — the in-venv bindings, forced to compile **from source** against the system `nvcc` so the CUDA backend is built (a plain wheel ships CPU-only). Install manually, outside `pip install -r`:

```bash
CUDACXX=/usr/local/cuda/bin/nvcc CMAKE_ARGS="-DGGML_CUDA=on" \
  pip install --no-binary llama-cpp-python llama-cpp-python
```

The `# llama.cpp` block in `requirements.txt` documents this rather than listing an installable line, since the source build needs the env vars above.
