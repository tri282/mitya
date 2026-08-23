# Mitya

Local LLM that speaks like Fyodor Dostoevsky and answers questions grounded in his books, tuned to run on a 6GB GPU.

Two parts, plus a thin UI:
- **Style** — Mistral-7B QLoRA fine-tune (Unsloth), merged and quantized (Q4_K_M / Q5_K_M) via `llama.cpp`. Done.
- **Substance** — metadata-tagged RAG: per-book parsing, Qdrant hybrid dense/BM25 + native RRF + FlashRank rerank, single-pass in-character generation with a programmatic Sources list. Built and evaluated (`docs/RAG.md`).
- **UI** — Vue 3 + Vite + TS single-page chat client over `/chat` SSE, "Evening Reading Room" theme (`DESIGN.md`). Built (`frontend/`).

See `docs/DESIGN.md` for the high-level architecture map, `DESIGN.md` for the UI visual design system, `docs/FINE_TUNE.md` for the training pipeline, `docs/RAG.md` for the settled RAG design (retrieval, caching, eval).

## Measured

All numbers from the target hardware (**GTX 1060, 6GB**) unless noted. Method and caveats in
`docs/RAG.md` §VI–§VII.

**Corpus & index** — 5 novels, 231 chapters parsed, **3,284 chunks** in Qdrant. Entity layer:
678 people extracted by a teacher LLM over all 231 chapters, curated 625 → **256 canonical keys /
441 aliases**, cross-translation name folding, zero same-book collisions at the gate.

**Retrieval** — `recall@5 = 0.720` (18/25), `MRR = 0.406` on a hand-labeled set, scored on the
fusion leg (dense nomic-embed-v1.5 + BM25, native RRF, 50/leg prefetch → 25 fused → FlashRank
`ms-marco-MiniLM-L-12-v2` → 3). *(Superseded 2026-08-21. `0.731 (19/26)` / `MRR = 0.408` counted
the two negative-control rows inside the headline; `0.720` then turned out to be one draw of a
bimodal distribution caused by a set-ordering defect in the entity map, measured by a harness that
ran the wrong retrieval path for structural rows. Current: `recall@5 = 0.7250` (29/40),
`MRR = 0.5287` on the `primary` family. See `docs/RAG.md` §VII.1 — the full ablation is withdrawn,
not merely restated.)*

**Serving** — **5.0GB / 6.0GB VRAM** at 4096 ctx with full offload (967MiB free). TTFT ~7.9s,
end-to-end ~21.3s at ~23.6 tok/s, retrieval-only ~2.25s (rerank-bound). Exact-match disk cache
hit **0.008s** — ~2,600× the cold path. Single-flight `Semaphore(1)`; 503/504 resolved before the
SSE stream opens, so a dead llama-server fails immediately instead of hanging a stream.

**Fine-tune** — Mistral-7B-Instruct-v0.3, QLoRA r=32 / α=32 on all 7 projections, 3 epochs over
**6,979 synthetic ChatML pairs**, merged and quantized to Q4_K_M / Q5_K_M / Q8_0.

**Evaluation** — LLM-judged prompt-composition A/B, 36 queries × 2 arms, scored on persona /
groundedness / fabrication. **No winner** (within noise), so both compositions were kept rather
than pruned. The A/B's real yield was two systemic findings above any composition signal:
meta-commentary answers (traced to the fine-tune's training data, not the serving prompt — trace
verified clean) and stochastic empty completions (fixed: zero-token retry + sampling floor).
Answer quality is capped by the fine-tune, not the retrieval pipeline — see `docs/RAG.md` §IX.

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
│   ├── rag/                    # ingest, names, entities, router, retrieve, context, generate, cache, judge
│   └── api/                    # FastAPI app (/chat SSE, /health, /books) + frontend/dist mount
├── frontend/                    # Vue 3 + Vite + TS SPA
├── scripts/
│   ├── build_dataset.py        # PDF -> Q&A CSV
│   ├── train_model.py          # fine-tune entrypoint
│   ├── quantize.sh             # merge + GGUF quantization
│   ├── start_server.sh         # llama.cpp server (dev)
│   ├── start_chat.sh           # llama.cpp CLI chat (dev)
│   ├── build_entities.py       # entity map for RAG (teacher LLM)
│   ├── build_summaries.py      # chapter summaries for RAG (teacher LLM)
│   ├── ingest_corpus.py        # RAG corpus -> Qdrant
│   ├── eval_retrieval.py       # recall@5 + MRR
│   ├── eval_quality.py         # prompt-composition A/B, LLM-judged
│   └── measure_latency.py      # retrieval-only + end-to-end timings
└── requirements.txt
```

## Status

Complete and running end to end: fine-tuned model, RAG pipeline (P0–P7), and UI (P1–P10) are all
built, evaluated on the target card, and committed.

Known residuals, all documented rather than hidden:
- **Answer quality is capped by the fine-tune**, not the retrieval pipeline (weak teacher-generated
  training data + an old q4 7B) — groundedness ~2.5/5, occasional meta-commentary. Not fixable at
  the serving layer; needs a training-data pass. `docs/RAG.md` §VII.2, §IX.
- **Latency exceeds the pre-build estimates** (retrieval ~2.25s, TTFT ~7.9s), accepted for a solo
  local deploy on a 6GB card. `docs/RAG.md` §VI.
- **Retrieval attribution is pending** — the absolute numbers exist, the per-stage deltas do not.
  The ablation arms are built (`scripts/eval_retrieval.py --arm`); the full six-arm table is not
  written up yet.

## License & attribution

Code is MIT — see `LICENSE`. That covers **this repository's code only**. The pieces it runs on
carry their own terms, and none of them are redistributed here:

- **Base model** — `mistralai/Mistral-7B-Instruct-v0.3` (Apache-2.0), fine-tuned with QLoRA via
  Unsloth. No adapters, merged weights, or GGUF files are published in this repository; `models/`
  is gitignored. Anyone reproducing the pipeline pulls the base model themselves.
- **Training data** — 6,979 synthetic Q&A pairs generated with the Gemini API. The dataset is not
  published (`data/` is gitignored); this repository ships the *generator*, not its output. Anyone
  regenerating it is bound by their own Gemini API terms.
- **Corpus** — Constance Garnett's translations of Dostoevsky, public domain. Not redistributed
  here either; the pipeline reads `.txt` files you supply.
- **Dependencies** — nomic-embed-text-v1.5, FlashRank, fastembed, and qdrant-client are Apache-2.0;
  FastAPI, llama.cpp, and Vue are MIT; Spectral is under the SIL Open Font License and is pulled at
  build time via `@fontsource/spectral`, never vendored into this repository.
