# Mitya

Local LLM that speaks like Fyodor Dostoevsky and answers questions grounded in his books, tuned to run on a 6GB GPU.

Two parts:
- **Style** — Mistral-7B QLoRA fine-tune, merged and quantized (Q4_K_M / Q5_K_M) via `llama.cpp`. Done.
- **Substance** — metadata-tagged RAG over Dostoevsky's texts (Qdrant + hybrid dense/sparse retrieval + reranking). Not built yet.

See `docs/DESIGN.md` for full architecture (RAG design, caching, eval), `docs/FINE_TUNE.md` for the training pipeline, `docs/RAG.md` for RAG design detail, `docs/TODO.md` for status.

## Layout

```
mitya/
├── data/
│   ├── raw/                   # source PDFs
│   ├── processed/             # generated Q&A jsonl/csv
│   ├── rag/                   # Dostoevsky txts (gitignored)
│   └── vector_db/             # Qdrant local store
├── models/
│   ├── lora/                  # LoRA adapters
│   └── quantizations/         # merged + quantized GGUFs (gitignored)
├── src/
│   ├── config.py
│   ├── ingestion.py            # PDF -> text (OCR)
│   ├── training.py             # QLoRA fine-tune (Unsloth)
│   └── generation.py           # synthetic Q&A generation (Gemini/Ollama)
├── scripts/
│   ├── build_dataset.py        # PDF -> Q&A CSV
│   ├── train_model.py          # fine-tune entrypoint
│   ├── quantize.sh             # merge + GGUF quantization
│   ├── start_server.sh         # llama.cpp server (dev)
│   └── start_chat.sh           # llama.cpp CLI chat (dev)
└── requirements.txt
```

## Status

Fine-tuned model exists and runs via `scripts/start_chat.sh` / `start_server.sh`. RAG layer (ingestion, retrieval, FastAPI serving) is next — see `docs/TODO.md`.
