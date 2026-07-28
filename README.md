# Mitya

Local LLM that speaks like Fyodor Dostoevsky and answers questions grounded in his books, tuned to run on a 6GB GPU.

Two parts, plus a thin UI:
- **Style** — Mistral-7B QLoRA fine-tune (Unsloth), merged and quantized (Q4_K_M / Q5_K_M) via `llama.cpp`. Done.
- **Substance** — metadata-tagged RAG: per-book parsing, Qdrant hybrid dense/BM25 + native RRF + FlashRank rerank, single-pass in-character generation with a programmatic Sources list. Designed (`docs/RAG.md`), implementation next.
- **UI** — Vue 3 + Vite + TS single-page chat client over `/chat` SSE, "Evening Reading Room" theme (`DESIGN.md`). Built (`plans/mitya-ui/`, `frontend/`).

See `docs/DESIGN.md` for the high-level architecture map, `DESIGN.md` for the UI visual design system, `docs/FINE_TUNE.md` for the training pipeline, `docs/RAG.md` for the settled RAG design (retrieval, caching, eval), `docs/TODO.md` for status.

## Running the UI

```bash
cd frontend && npm install && npm run build   # type-checks (vue-tsc) then emits frontend/dist/
cd ..                                          # back to the repo root
uvicorn src.api.app:app                        # MUST run from the repo root — see caveat below
```

**CWD caveat:** `src/api/app.py` mounts `StaticFiles(directory="frontend/dist")` as its last
statement; that path resolves relative to uvicorn's **process CWD**, not the file's location.
Running `uvicorn` from anywhere other than the repo root 404s every UI route. Worse: if
`frontend/dist/` does not exist yet (i.e. `npm run build` was never run), the app raises
`RuntimeError` at startup and refuses to boot at all — **this applies even if you only intend to
use the Vite dev server**, since `uvicorn src.api.app:app` is the same process either way. Run
`npm run build` at least once before the backend's first boot.

Dev mode (frontend/backend served separately, backend still requires the one-time build above to
boot): `cd frontend && npm run dev` (Vite dev server on :5173, proxies `/chat`, `/health`, `/books`
to `127.0.0.1:8000`).

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
├── frontend/                    # Vue 3 + Vite + TS SPA (see plans/mitya-ui/)
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
