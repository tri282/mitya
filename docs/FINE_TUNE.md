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

---

## Fine-Tuning Architecture

See `src/training.py`. I use **QLoRA** (Quantized Low-Rank Adaptation) via the Unsloth library since I'm broke and memory stores up quickly in Colab.
* **Base Model:** `unsloth/mistral-7b-instruct-v0.3-bnb-4bit`.
* **Wrapper/Trainer:** The pipeline utilizes `SFTTrainer` (Supervised Fine-Tuning) from the `trl` library.

---

## Merging and Quantization

The model base model and LORA adapters are first merged (via Hugginface's `merge_and_unload()`) to generate a master `F16.guuf` file. Then `llama.cpp` is used to quantize the merged model to the following quantizations: **Q4_K_M, Q5_K_M, Q8_0**
