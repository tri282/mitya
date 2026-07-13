# Mitya

Local LLM that speaks like Fyodor Dostoevsky and answers questions grounded in his books, tuned to run on a 6GB GPU.

Two parts:
- **Style** — Mistral-7B QLoRA fine-tune (Unsloth), merged and quantized (Q4_K_M / Q5_K_M) via `llama.cpp`. Done.
- **Substance** — metadata-tagged RAG: per-book parsing, Qdrant hybrid dense/BM25 + native RRF + FlashRank rerank, single-pass in-character generation with a programmatic Sources list. Designed (`docs/RAG.md`), implementation next.

See `docs/DESIGN.md` for the high-level architecture map, `docs/FINE_TUNE.md` for the training pipeline, `docs/RAG.md` for the settled RAG design (retrieval, caching, eval), `docs/TODO.md` for status.

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
│   ├── generation.py           # synthetic Q&A generation (Gemini/Ollama)
│   ├── rag/                    # planned: ingest, router, retrieve, context, generate, cache
│   └── api/                    # planned: FastAPI app (/chat SSE, /health, /books)
├── scripts/
│   ├── build_dataset.py        # PDF -> Q&A CSV
│   ├── train_model.py          # fine-tune entrypoint
│   ├── quantize.sh             # merge + GGUF quantization
│   ├── start_server.sh         # llama.cpp server (dev)
│   ├── start_chat.sh           # llama.cpp CLI chat (dev)
│   ├── build_entities.py       # planned: entity map for RAG
│   ├── build_summaries.py      # planned: chapter summaries for RAG
│   └── ingest_corpus.py        # planned: RAG corpus -> Qdrant
└── requirements.txt
```

## Status

Fine-tuned model exists and runs via `scripts/start_chat.sh` / `start_server.sh`. RAG layer is designed (`docs/RAG.md`) and pending implementation — see `docs/TODO.md`.
