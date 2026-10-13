# Mitya

A question answering system over Dostoevsky's five novels. It answers in his voice, grounded in the
text, and prints the chapters the answer came from. The Sources list is built in code from retrieval
metadata, never parsed from the answer.

v1 asked whether that could run on one 6GB consumer GPU. It could (see `CHANGELOG.md`). v2, the
current version, asks a delivery question: at a fixed quality bar, what is the cheapest and most
reliable way to serve grounded answers, and can the system prove it stays that way as it changes.
Generation moved to hosted model APIs, and most of the work went into what lets the system make
measured claims about itself.

Three parts:
- **Substance.** Metadata-tagged RAG: per-book structural parse, Qdrant hybrid dense
  (nomic-embed-text-v1.5) and BM25 with native RRF, FlashRank rerank, an offline entity map and
  chapter summaries. Mostly unchanged from v1, by decision (`docs/v2/14-unchanged-from-v1.md`).
- **Generation.** Hosted APIs behind a gateway over two provider families (Google, Groq), each
  reached through its native API. Prompt composition C. The v1 fine-tune is retired from the request
  path, not deleted.
- **Platform.** Measurement contract, per-stage telemetry with content-hashed config identity, a CI
  eval gate, a traffic harness with a degradation ladder, auth and per-tenant admission, three-tier
  conversation memory, and a config optimizer. A Vue 3 SPA over `/chat` SSE.

Start at `docs/v2/00-INDEX.md`, then `docs/v2/DESIGN.md` for the whole system and one request traced
end to end. The v1 set is `docs/v1/`.

## Measured (v2)

Every figure, with its unit, n, date and provenance, is in `docs/v2/EVIDENCE-*.md`; anchors in
brackets. The security leg and the quality leg ran on different models and are two results, not one.

- **Retrieval.** `recall@5 = 0.7105` (27/38), `MRR = 0.5039` on the primary family, re-derived after
  two harness defects; v1's `0.720` measured a different set [VP4].
- **Latency.** Rerank is 661 of 917 ms of retrieval (72.1%) at the shipped fused depth of 10. Cutting
  the fused pool from 25 to 10 took retrieval from 2,121 to 917 ms with recall@5 flat [VO6].
- **Concurrency.** Knee 1 to 4 after narrowing the lock to retrieval [VP27]. Bounded admission took
  p99 at level 4 from 64,405 ms with 0 rejections to 4,429 ms with 13 [VP34].
- **Prompt security.** Composition C cut jailbreak violations from 0.5625 to 0.1250 over 16 attempts
  [VM14, VM20]. Prompt extraction is unmoved at 1/8 on every arm [VM15].
- **Quality.** Composition C is superior on groundedness, +0.724 against a measured MDE of 0.343
  [VM18].
- **Gate.** Three layers, no hand-set threshold [VP20]. Stripping every passage from the prompt moves
  both mechanical metrics by 0.0000; the gate cannot see grounding, and its report says so [VP21].
- **Cost.** $0.003982 per answer at list rate (production config of the time) [VO7]. Actual spend
  $0.00: every rate row is unmetered [VO3].

## Known open (v2)

- Retrieval never sees the conversation: 63% of multi-turn requests retrieve on a contentless
  string [VO8].
- Chunking (500/100, read from v1) is untested, not validated [VO11].
- The memory fence does not hold; an existence result, never a rate [VM11]. Per-tenant fairness is a
  non-result [VP35].
- The degradation ladder descends on misconfiguration, not on a real provider outage; readiness is
  config-only by design (`docs/v2/06-traffic-harness.md`).
- No real traffic anywhere; every harness and session figure is synthetic.

## Running the UI

Needs a `.env` (template `.env.example`) with `GOOGLE_API_KEY`: the promoted config
(`config/serving/CURRENT`) serves on a Google model. `GROQ_API_KEY` enables the second provider
family; it is read by `src/config.py` but not yet listed in `.env.example`. `/chat` requires a
session, so seed an account first; `scripts/seed_account.py` is the only account-creation path.

```bash
python scripts/seed_account.py <name>          # prompts for a password (or --password-env VAR)
cd frontend && npm install && npm run build   # type-checks (vue-tsc) then emits frontend/dist/
cd ..                                          # back to the repo root
uvicorn src.api.app:app                        # MUST run from the repo root, see caveat below
```

Traces and metrics are optional: `docker compose up` starts jaeger, prometheus and grafana, and
`OTEL_EXPORTER_OTLP_ENDPOINT` points the app at them.

**CWD caveat:** `src/api/app.py` mounts `StaticFiles(directory="frontend/dist")` as its last
statement; that path resolves relative to uvicorn's **process CWD**, not the file's location.
Running `uvicorn` from anywhere other than the repo root 404s every UI route. Worse: if
`frontend/dist/` does not exist yet (i.e. `npm run build` was never run), the app raises
`RuntimeError` at startup and refuses to boot at all. **This applies even if you only intend to
use the Vite dev server**, since `uvicorn src.api.app:app` is the same process either way. Run
`npm run build` at least once before the backend's first boot.

Dev mode (frontend/backend served separately, backend still requires the one-time build above to
boot): `cd frontend && npm run dev` (Vite dev server on :5173, proxies `/chat`, `/health`, `/books`
to `127.0.0.1:8000`). The proxy does not forward `/auth/*` (`frontend/vite.config.ts`), so sign-in
works only against the built UI served by `uvicorn` on :8000.

## Layout

```
mitya/
├── config/
│   ├── serving/               # content-hashed ServingConfig artifacts; CURRENT names the promoted one
│   └── corpus/                # corpus manifest (embedder, point count), checked at startup
├── data/                      # raw PDFs, processed Q&A, corpus txts, Qdrant local store (gitignored)
├── docs/
│   ├── v1/                    # v1 documentation set
│   └── v2/                    # v2 documentation set and EVIDENCE logs
├── frontend/                  # Vue 3 + Vite + TS SPA
├── models/                    # v1 LoRA adapters and GGUFs (gitignored)
├── ops/                       # prometheus and grafana provisioning
├── src/
│   ├── api/                   # FastAPI app: /chat SSE, /auth, /health, /ready, /books, /metrics
│   ├── gateway/               # provider contract, Google and Groq adapters, guard, ladder, retry, fault, fake
│   ├── harness/               # HTTP-only traffic harness: ramp, rungs, replay, multi-turn
│   ├── rag/                   # ingest, names, entities, router, retrieve, context, generate, cache, gate, judge, metrics
│   ├── config.py, config_artifact.py   # env config and the versioned serving artifact
│   ├── auth.py, db.py, limits.py       # accounts, sessions, admission and rate limits
│   ├── memory.py, memory_store.py      # conversation memory tiers
│   ├── guard.py, observability.py, metrics.py, pricing.py
│   ├── optimizer.py, search.py, p7_objective.py
│   └── ingestion.py, generation.py, training.py   # v1 fine-tune pipeline
├── scripts/                   # eval_*, run_* (harness), probe_*, build_* and ingest_corpus (offline
│                              # artifacts), gate.py, seed_account.py, v1 train_model.py and quantize.sh
├── test/                      # pytest suite and eval sets
├── docker-compose.yml         # jaeger, prometheus, grafana
└── requirements.txt
```

## Versions

- **v1** (tag `v1`): the hardware question. A Mistral-7B QLoRA fine-tune and the RAG layer on one
  6GB GPU; the quality ceiling was the fine-tune, not the pipeline.
- **v2** (tag `v2`, current): the delivery question. Hosted generation and the measurement platform
  above.
- **v3** (in research, no code yet): intent-conditioned serving. Route each request to the cheapest
  execution path that meets its quality bar, check the output mechanically before it is accepted, and
  prove the cost on real bills. Hosted APIs only, nothing that bills while idle, prompt-only
  generation. Decisions wait on measurements.

Per-version detail is in `CHANGELOG.md`.

## License & attribution

Code is MIT, see `LICENSE`. That covers **this repository's code only**. The pieces it runs on
carry their own terms, and none of them are redistributed here:

- **Base model**: `mistralai/Mistral-7B-Instruct-v0.3` (Apache-2.0), fine-tuned with QLoRA via
  Unsloth. No adapters, merged weights, or GGUF files are published in this repository; `models/`
  is gitignored. Anyone reproducing the pipeline pulls the base model themselves.
- **Training data**: 6,979 synthetic Q&A pairs generated with the Gemini API. The dataset is not
  published (`data/` is gitignored); this repository ships the *generator*, not its output. Anyone
  regenerating it is bound by their own Gemini API terms.
- **Corpus**: Constance Garnett's translations of Dostoevsky, public domain. Not redistributed
  here either; the pipeline reads `.txt` files you supply.
- **Hosted generation**: v2 serves answers through third-party model APIs under each provider's
  own terms; no model output is redistributed here.
- **Dependencies**: nomic-embed-text-v1.5, FlashRank, fastembed, and qdrant-client are Apache-2.0;
  FastAPI, llama.cpp, and Vue are MIT; Spectral is under the SIL Open Font License and is pulled at
  build time via `@fontsource/spectral`, never vendored into this repository.
