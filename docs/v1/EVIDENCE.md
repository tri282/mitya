# Mitya v1 — Evidence Log

**Scope:** every number in the v1 documentation set. The architecture documents (`01`–`12`) cite
section anchors here and state no figure of their own.

**Contract:** anchor IDs `E1`–`E30` are frozen. They are never renumbered; a superseded section is
marked superseded in place.

**Citations to `docs/RAG.md`, `docs/DESIGN.md` and `docs/FINE_TUNE.md`** are **provenance, not
navigation**: they name the file a number was read from at the time it was recorded. Those three
documents were superseded by this set and removed from the tree; they remain readable at tag `v1`
and in every commit before the removal. Do not rewrite these citations to point at the new docs —
a provenance record that names its actual source is the point.

**Provenance tags**

| tag | meaning |
|---|---|
| `[measured]` | produced by an execution of a named script/command on named hardware |
| `[computed]` | arithmetic over other rows in this file |
| `[read from source]` | a literal in tracked source, config, or a build artifact |
| `[reasoned]` | a design-time derivation from first principles (e.g. KV-cache arithmetic) |
| `[estimate]` | a pre-build guess, retained only where a later measurement is compared against it |

A number whose producer could not be identified is tagged `[read from source, producer unknown]`.
A number that could not be sourced at all is **absent** — see [E30](#E30).

**Hardware, everywhere below:** a single **NVIDIA GTX 1060, 6GB VRAM**, ≥16GB system RAM, CPU for
all retrieval. Unless a row says otherwise, that is the machine.

---

<a id="E1"></a>
## E1 — Hardware budget and the 6GB constraint

The binding constraint on every design decision. GPU runs the quantized LLM and nothing else; the
embedder, BM25, Qdrant and the reranker are CPU-only, and within a request the two phases are
sequential, so GPU contention does not exist by construction.

| Quantity | Value | Date / phase | How produced | Tag |
|---|---|---|---|---|
| VRAM budget (hard ceiling) | 6.0 GB | design, 2026-07-13 | hardware fact, `docs/RAG.md` §V | `[read from source]` |
| Mistral-7B `q4_k_m` weights | ≈ 4.4 GB | design, 2026-07-13 | `docs/RAG.md` §V arithmetic | `[reasoned]` |
| KV cache, fp16 @ 4096 ctx | 0.50 GB | design, 2026-07-13 | GQA arithmetic: 32 layers, 8 KV heads, head_dim 128 → 128 KiB/token | `[reasoned]` |
| KV cache, `q8_0` @ 4096 ctx | ≈ 0.27 GB | design, 2026-07-13 | same, halved by 8-bit KV | `[reasoned]` |
| Compute buffers + CUDA context | 0.5–0.8 GB | design, 2026-07-13 | `docs/RAG.md` §V | `[estimate]` |
| **Predicted total** | **5.2–5.5 GB** | design, 2026-07-13 | sum of the above | `[reasoned]` |
| **Measured total, full offload, `-c 4096`** | **5.0 GB / 6.0 GB used, 967 MiB free** | P5, 2026-07-21 | `scripts/start_server.sh` (llama-server) + `nvidia-smi` after warmup | `[measured]` |
| `q5_k_m` weights (rejected) | ≈ 4.9 GB | design, 2026-07-13 | `docs/RAG.md` §V | `[reasoned]` |
| Python service resident RAM | ≈ 1.5 GB | design, 2026-07-13 | fastembed nomic ~0.5 GB + FlashRank ~0.1–0.3 GB + FastAPI ~0.3 GB + Qdrant local (negligible at ~3.3k points) | `[estimate]` |
| llama-server mmapped weights | ≈ 4.5 GB | design, 2026-07-13 | `docs/RAG.md` §V | `[estimate]` |

**Disagreement recorded:** the design predicted 5.2–5.5 GB; the measurement returned 5.0 GB. The
prediction was conservative, not wrong — it is retained because the headroom argument for choosing
`q4_k_m` over `q5_k_m` rests on it.

**Serving flags as run** (`docs/RAG.md` §V, `scripts/start_server.sh`) `[read from source]`:
`-ngl 99 -c 4096 -fa on --cache-type-k q8_0 --cache-type-v q8_0 --parallel 1 --host 127.0.0.1
--port 8080`. V-cache quantization requires `-fa`; without it the flag is silently ignored.

**Conditional:** the 4096-ctx fit assumes a **headless** GPU. If a desktop compositor renders on the
same card, `-c 3072`. `[reasoned]`

---

<a id="E2"></a>
## E2 — Fine-tune dataset construction

| Quantity | Value | Date / phase | How produced | Tag |
|---|---|---|---|---|
| Source | 5 Dostoevsky novels as scanned PDFs | fine-tune phase | `src/ingestion.py` | `[read from source]` |
| OCR path | `pypdfium2` page render → `pytesseract` | fine-tune phase | `src/ingestion.py:10-27` | `[read from source]` |
| Chunker | LangChain `RecursiveCharacterTextSplitter` | fine-tune phase | `src/ingestion.py:31` | `[read from source]` |
| Chunk size / overlap | **1600 chars / 100 chars** | fine-tune phase | `src/ingestion.py:31` defaults | `[read from source]` |
| Generation shape | two teacher passes per chunk: question, then first-person answer | fine-tune phase | `src/generation.py:79-100` | `[read from source]` |
| Answer length cap in the prompt | **< 350 words** | fine-tune phase | `src/generation.py:97` | `[read from source]` |
| Teacher backends | Gemini (default) or Ollama | fine-tune phase | `src/generation.py`, `Config.LLM_BACKEND` | `[read from source]` |
| Output format | ChatML (system / user / assistant) | fine-tune phase | `src/training.py:47-72` | `[read from source]` |
| **Dataset as built** | **6,979 pairs**, ≈ 11 MB, `data/processed/chatml_cleaned_data.jsonl` | fine-tune phase | `scripts/build_dataset.py`; recorded `docs/FINE_TUNE.md` | `[read from source, producer unknown]` |
| API spend | **zero** — entirely free-tier teacher quota | fine-tune phase | `docs/FINE_TUNE.md` | `[read from source, producer unknown]` |
| Cleaning | HTML + non-ASCII stripped (`re.sub(r'[^\x00-\x7f]', '', …)`) | fine-tune phase | `src/training.py:39` | `[read from source]` |

**Not recorded anywhere:** the number of OCR'd chunks the 6,979 pairs were drawn from, and the
teacher model id actually used for the training-data pass. The quality eval later attributes the
answer ceiling to "the weak gemini-2.5-flash training data" ([E25](#E25)) — that is the only
surviving identification of the training teacher, and it appears in prose, not in a run log.

---

<a id="E3"></a>
## E3 — QLoRA configuration and training

All values are literals in `src/training.py` unless noted. `[read from source]`

| Parameter | Value | Source |
|---|---|---|
| Base model | `unsloth/mistral-7b-instruct-v0.3-bnb-4bit` | `src/training.py:24` |
| Loading | 4-bit (`load_in_4bit = True`) | `:28` |
| Framework | Unsloth `FastLanguageModel` + `trl` `SFTTrainer` / `SFTConfig` | `:102`, `:166` |
| LoRA rank `r` | **32** | `:113` |
| `lora_alpha` | **32** (1:1 with rank) | `:119` |
| Target modules | all 7 — `q_proj`, `k_proj`, `v_proj`, `o_proj`, `gate_proj`, `up_proj`, `down_proj` | `:115-117` |
| `lora_dropout` | **0.1** | `:121` |
| `bias` | `"lora_only"` | `:123` |
| `use_rslora` | `True` | `:129` |
| `use_dora` | `False` | `:130` |
| Gradient checkpointing | `"unsloth"` | `:125` |
| `max_seq_length` | **1024 tokens** | `:24` |
| Per-device batch size | **4** | `:138` |
| Gradient accumulation | **4** (effective batch 16) | `:142` |
| Epochs | **3** (`max_steps = -1`) | `:146-147` |
| Learning rate | **2e-4** | `:148` |
| Scheduler | `linear`, `warmup_steps = 5` | `:144`, `:157` |
| Optimizer | `adamw_8bit` | `:153` |
| Weight decay | **0.01** | `:156` |
| Precision | bf16 where supported, else fp16 | `:149-150` |
| Packing | `True` | `:161` |
| Seed / `random_state` | **2802** | `:162`, `:126` |
| Chat template | `"mistral"` via Unsloth `get_chat_template` | `:81-83` |

**The two non-default choices**, and the one reason behind both (`docs/FINE_TUNE.md`): rank:alpha at
1:1 and dropout 0.1 follow from the target being a single narrow voice rather than broad
instruction-following. `[reasoned]`

**Sequence length is not independent:** 1024 tokens matches the 1600-char / 100-overlap chunker
upstream ([E2](#E2)). `[reasoned]`

**Absent:** training wall-clock, step count, final loss, GPU used for training. `src/training.py`
logs every 100 steps but no log survives in the repo. Training ran on Colab (`docs/FINE_TUNE.md`),
i.e. not on the 6GB target card. See [E30](#E30).

---

<a id="E4"></a>
## E4 — Merge and GGUF quantization

| Quantity | Value | Date / phase | How produced | Tag |
|---|---|---|---|---|
| Merge | HF `merge_and_unload()` → F16 master | fine-tune phase | `docs/FINE_TUNE.md` | `[read from source]` |
| F16 conversion | `convert_hf_to_gguf.py --outtype f16` → `mitya-2.0-F16.gguf` | fine-tune phase | `scripts/quantize.sh` | `[read from source]` |
| Quantization levels produced | **`q4_k_m`, `q5_k_m`, `q8_0`** — three | fine-tune phase | `scripts/quantize.sh` (three `llama-quantize` invocations) | `[read from source]` |
| Level actually served | **`q4_k_m`** (`models/quantizations/mitya-2.0_q4_k_m.gguf`) | P5 onward | `docs/RAG.md` §V launch command; `q5_k_m` has no headroom at any useful context ([E1](#E1)) | `[read from source]` |
| Toolchain | `llama.cpp` from GitHub, `cmake -B build && cmake --build build --config Release -j`, against system CUDA Toolkit **13.2** (`/usr/local/cuda/bin/nvcc`) | fine-tune phase | `docs/FINE_TUNE.md`, `scripts/quantize.sh` | `[read from source]` |
| `llama-cpp-python` | built **from source** (`--no-binary`, `CMAKE_ARGS="-DGGML_CUDA=on"`); the published wheel is CPU-only | fine-tune phase | `docs/FINE_TUNE.md`; documented as a comment block in `requirements.txt`, not an installable line | `[read from source]` |
| Chat template in the GGUF | the official Mistral-v0.3 template, matching Unsloth's training template | P7, 2026-07-22 | prompt-trace verification during the A/B ([E25](#E25)) | `[measured]` |

**Disagreement recorded:** `README.md` ("Measured" section) names two quantizations, `Q4_K_M /
Q5_K_M`; `docs/FINE_TUNE.md` and `scripts/quantize.sh` name three, including `Q8_0`. The script is
the producer, so three were built. `README.md` is the outlier.

**Absent:** file sizes of the produced GGUFs, and any perplexity or quality comparison between the
three quantization levels. `q4_k_m` was selected on the VRAM arithmetic of [E1](#E1) alone.

---

<a id="E5"></a>
## E5 — Corpus and structural parse

Parsed by `src/rag/ingest.py` (`python -m src.rag.ingest --parse-only`), emitting
`data/rag/structure.json`. Heading formats differ per book, so the parser is a small rule table, not
one regex — `book/part/chapter` payloads are what make hard filters, the STRUCTURAL route and the
Sources list correct.

| Quantity | Value | Date / phase | How produced | Tag |
|---|---|---|---|---|
| Books | **5** clean plaintext novels (`data/rag/*.txt`, no OCR — distinct from [E2](#E2)'s PDFs) | P1, 2026-07-21 | `docs/RAG.md` §II.1 | `[read from source]` |
| **Total structural units (chapters)** | **231** | P1, run 2026-07-21 | `src/rag/ingest.py --parse-only`; counts matched a manual reference tripwire | `[measured]` |
| Translation | Constance Garnett throughout | design | `docs/RAG.md` §II.4 | `[read from source]` |

**Per-book parse**, all resolved by `grep` over heading lines during P1 (2026-07-21, impl log
`plans/mitya-rag/05-impl/phase-1.md`) — no novel body text was read: `[measured]`

| Book | Scheme | Parts / books | Chapters |
|---|---|---|---|
| Crime and Punishment | `PART N` + `CHAPTER N` | 6 + epilogue (mapped to part 7) | 39 + 2 epilogue = **41** |
| The Possessed | `PART N` + `CHAPTER N. TITLE` | 3, no separate epilogue | **23** |
| The Idiot | `PART N` + **bare roman numerals**, no `CHAPTER` keyword | 4 (16/12/10/12) | **50** |
| Notes from the Underground | `PART N` + numbered sections | 2 (11 + 10) | **21** |
| The Brothers Karamazov | `Book N`, chapter numbering **resets per book** | 11 explicit `Book N.` + 1 implicit Book I = 12, + EPILOGUE as part 13 | **96** `[computed]` |

The Karamazov figure is `[computed]`: 231 − (41 + 23 + 21 + 50) = 96. The other four are read
directly from the P1 grep evidence; only the 231 total was logged by the run.

**Three parse facts that were discovered, not assumed** `[measured]`:
1. The Idiot has **no `CHAPTER` keyword anywhere** — 0 grep matches. Its 50 chapters are bare roman
   numerals, recorded in `structure.json` as `chapter_label: null`. This null later reached the
   frontend and rendered as the literal string `"null 5"` ([E21](#E21)).
2. The Brothers Karamazov has **no `Book I.` heading** — Book I is inferred as everything before
   `Book II.`. The book count is read from the file (11 + 1), never hardcoded.
3. Crime and Punishment's Part VI content range is clipped at the `EPILOGUE` heading, or its last
   chapter swallows the epilogue.

**`structure.json` is three artifacts at once** (`docs/RAG.md` §II.1): the parser's own verification
tripwire, the backing data for `GET /books`, and the key space `summaries.json` must cover.
`[read from source]`

**Units note:** the FastAPI lifespan logs `structure=5`, not 231 — `structure.json` is keyed by
**book**. A runbook gate that expected 231 there was corrected at P6. `[measured]`

---

<a id="E6"></a>
## E6 — Chunking

| Quantity | Value | Date / phase | How produced | Tag |
|---|---|---|---|---|
| Chunk size | **500 tokens** (`Config.CHUNK_TOKENS`) | P0/P2 | `src/config.py` | `[read from source]` |
| Overlap | **100 tokens** (`Config.CHUNK_OVERLAP`) | P0/P2 | `src/config.py` | `[read from source]` |
| Counted with | **the embedder's own tokenizer** — the real `tokenizers.Tokenizer` reached at `embedder.model.tokenizer` | P2 | `get_tokenizer()` / `chunk_units()` in `src/rag/ingest.py` | `[read from source]` |
| Windowing | over `Encoding.offsets` (char spans), `add_special_tokens=False` | P2 | `src/rag/ingest.py` | `[read from source]` |
| Boundary rule | chunks **never cross a chapter boundary** — a `Unit` already is one chapter | P2 | `src/rag/ingest.py` | `[read from source]` |
| Point ids | stable deterministic integers, one global counter over (book order × unit order × window order) | P2 | `src/rag/ingest.py` | `[read from source]` |
| Mistral-token budget factor | ≈ **1.3×** when converting nomic tokens to Mistral tokens; 3 chunks ≈ 2k prompt tokens, inside the 4096 ctx | design | `docs/RAG.md` §II.2 | `[estimate]` |

**Why the embedder's tokenizer and not a character count:** a 500-token chunk sized with the wrong
tokenizer either truncates inside the embedder or wastes its window. `add_special_tokens=False` is
load-bearing — `[CLS]`/`[SEP]` carry `(0, 0)` offsets and would pollute the windowing, without
changing what the model sees at inference time. `[reasoned]`

---

<a id="E7"></a>
## E7 — Embedding model and dense vector

| Quantity | Value | Date / phase | How produced | Tag |
|---|---|---|---|---|
| Model | `nomic-ai/nomic-embed-text-v1.5` | P2 | `Config.EMBED_MODEL`; confirmed against the fastembed **0.8.0** registry (`fastembed/text/pooled_embedding.py`) | `[read from source]` |
| Dimensionality | **768**, full (no Matryoshka truncation) | P2 | `models.VectorParams(size=768, …)` in `src/rag/ingest.py` | `[read from source]` |
| Parameters | 137M | design | `docs/RAG.md` §II.2 | `[read from source, producer unknown]` |
| Max input | 8192 tokens | design | `docs/RAG.md` §II.2 | `[read from source, producer unknown]` |
| Distance | `COSINE` | P2 | `src/rag/ingest.py` | `[read from source]` |
| Runtime | fastembed, quantized **ONNX, CPU** — no torch inference path, no `trust_remote_code` | P2 | `src/rag/ingest.py` | `[read from source]` |
| Ingest prefix | `"search_document: "` (with trailing space) | P2 | `embed_dense()` in `src/rag/ingest.py` | `[read from source]` |
| Query prefix | `"search_query: "` (with trailing space) | P4 | `src/rag/retrieve.py`; pairing re-verified at P4 | `[read from source]` |

**The prefix trap, resolved from library source rather than guessed** (P2, 2026-07-14/21):
`grep -rn "search_document\|search_query"` over the installed `fastembed` package returns **zero
hits**; `TextEmbeddingBase.query_embed`/`passage_embed` are plain pass-throughs and `PooledEmbedding`
overrides neither. Conclusion: **fastembed does not auto-prefix**, so the application must. A
mismatched pair degrades retrieval silently — this is ruling RR5, and the retrieval eval is its
tripwire ([E22](#E22)). `[measured]` (from installed source, not execution)

**Matryoshka truncation cut:** at ~3.3k vectors the memory saving is meaningless. `[reasoned]`

---

<a id="E8"></a>
## E8 — Qdrant collection

| Quantity | Value | Date / phase | How produced | Tag |
|---|---|---|---|---|
| **Point count** | **3,284** | P2, run confirmed 2026-07-21/22 | `scripts/ingest_corpus.py`; re-confirmed at P6 lifespan startup (`qdrant_points=3284`) | `[measured]` |
| Chunks per chapter, mean | **14.2** | — | 3284 / 231 | `[computed]` |
| Collection name | `dostoevsky` (`Config.QDRANT_COLLECTION`) | P0 | `src/config.py` | `[read from source]` |
| Deployment | **local mode**, `QdrantClient(path=Config.VECTOR_DB_DIR)` → `data/vector_db/` | P2 | `src/rag/ingest.py` | `[read from source]` |
| Client version | qdrant-client **1.18.0** (pin `>=1.10` for the Query API / native fusion) | P2/P4 | verified against installed `qdrant_client/http/models/models.py` | `[measured]` |
| Named vectors | `dense` (768, COSINE) and `bm25` (sparse) | P2 | `ensure_collection()` in `src/rag/ingest.py` | `[read from source]` |
| Payload per point | `{book, part, chapter, canonical_entities, text}` + a derived normalized field for exact match | design/P2 | `docs/RAG.md` §II.3, §II.4 | `[read from source]` |
| Upsert batch size | 64 (default, `--batch-size`) | P2 | `scripts/ingest_corpus.py` | `[read from source]` |
| Verification | post-upsert `client.count()` compared to the logged chunk count; mismatch warns | P2 | `scripts/ingest_corpus.py` | `[read from source]` |

**Local mode holds an exclusive process lock.** `ingest_corpus.py` and every eval script can only run
while the API server is stopped. This is an operational constraint, not a nuisance — it is documented
in the ingest script's own docstring. `[read from source]`

**Why local mode over a server container:** zero-ops for a solo, static corpus. `[reasoned]`

---

<a id="E9"></a>
## E9 — Sparse leg (BM25) and the IDF modifier

| Quantity | Value | Date / phase | How produced | Tag |
|---|---|---|---|---|
| Sparse encoder | fastembed `Qdrant/bm25` (`SparseTextEmbedding`) | P2 | `src/rag/ingest.py` | `[read from source]` |
| Collection config | `models.SparseVectorParams(modifier=models.Modifier.IDF)` on the `bm25` vector | P2 | `ensure_collection()`, verified against qdrant-client 1.18.0's `Modifier` enum | `[read from source]` |
| Sparse vector at query time | requires `.tolist()` on the values | P4 | verified against installed qdrant-client source | `[measured]` |

**The whole point of the modifier:** fastembed emits **TF-side weights only**. Without
`Modifier.IDF` set on the collection, "BM25" degrades to raw term frequency and the corpus-level
statistics never enter the score. This is a one-line config value that silently changes what the
algorithm is. `[reasoned]`

**Why not SPLADE or a learned sparse encoder:** a neural sparse encoder is a second GPU consumer
against a 6GB budget already fully allocated to the LLM ([E1](#E1)). `[reasoned]`

---

<a id="E10"></a>
## E10 — Entity map (`entities.json`)

Built once, offline, by a teacher LLM, then put through a human gate and two scripted curation
passes. `scripts/build_entities.py` → `scripts/fix_entities.py` → `scripts/check_entity_folds.py`.

| Quantity | Value | Date / phase | How produced | Tag |
|---|---|---|---|---|
| Chapters extracted | **231 / 231**, 0 failures | P3, 2026-07-16 | `scripts/build_entities.py` (teacher = `models/gemini-3.1-flash-lite`) | `[measured]` |
| Machine output | **678 people across 625 reference names** | P3, 2026-07-16 | `data/rag/entities.raw.json` | `[measured]` |
| Names shared across books | **41** | P3, 2026-07-16 | merge report | `[measured]` |
| People the flat schema could not hold | **53** | P3, 2026-07-16 | merge report | `[measured]` |
| Review report | 290 merge candidates, 251 rare (≤2 mentions), **28 unusable aliases** | P3, 2026-07-16 | `entities.review.md` | `[measured]` |
| Manual gate | 625 → ~277 keys | P3, 2026-07-17 | human pass | `[measured]` |
| Scripted cleanup | 277 → **255** keys | P3, 2026-07-17 | `scratchpad/curate.py` + `fold.py` | `[measured]` |
| After `names.py` curation pass | **256 keys / 441 aliases** (from 487) — 10 explicit ops, 51 pruned | P3 close, 2026-07-19 | `scripts/fix_entities.py` | `[measured]` |
| After the data-ticket pass | **256 keys / 465 aliases** (from 579 pre-datafix) | 2026-07-21 | `scripts/fix_entities.py` extended, committed `6b6e9db` | `[measured]` |
| Runtime load count | `entities=256` | P6, 2026-07-22 | FastAPI lifespan log | `[measured]` |
| Fold gate | 37 fold collision buckets vs 32 normalize baseline, 9 introduced pairs, **zero same-book collisions**, exit 0 | P3, 2026-07-19 | `scripts/check_entity_folds.py` | `[measured]` |
| Fold probe | 25 / 26 | P3, 2026-07-19 | `scripts/check_entity_folds.py` probe list | `[measured]` |

**Disagreement recorded — alias count.** `README.md` states **441** aliases; the last curation pass
on record (2026-07-21, `6b6e9db`) ends at **465**, after a hand edit re-added aliases (peaking at 579)
between the two. The key count is 256 in both. `README.md` is describing the 2026-07-19 state.

**Schema — the design that had to change.** `docs/RAG.md` §II.4 originally specified a flat
`{name: {canonical, aliases, book}}`, which assumes reference names are unique across five novels.
They are not: Katerina Ivanovna is Marmeladov's wife in *Crime and Punishment* **and**
Verhovtsev's daughter in *The Brothers Karamazov*; Nikolay is a painter in C&P and Stavrogin in
*The Possessed*; Dmitri, Marya and Lizaveta collide the same way. The flat shape had nowhere to put
the second person, so `merge_entities` fused them — unioning two people's aliases, taking the longer
canonical, keeping the **first** book's label. The observed artifact of that bug: a `Nikolay` record
labelled `Crime and Punishment` carrying the canonical `Nikolay Vsyevolodovitch Stavrogin`, who
appears **287 times in The Possessed and never in C&P**. The schema became
`{name: [ {canonical, aliases, book}, … ]}` — one record per book. `[measured]`

**Resolution rule (three cases, exact):** with a hard book filter active, take the candidate whose
`book` matches. With no filter and more than one candidate, the name does not identify a person —
**skip expansion rather than guess**, because expanding with the wrong novel's aliases is worse than
not expanding. `[read from source]`

**Runtime use is query expansion, not payload boosting.** Resolved canonical + aliases are appended
to the query text before embedding. Why not a Qdrant `should`-boost (the original design): Qdrant
filters are **binary** — a `should` clause adds no score, and a should-only filter *excludes*
non-matching points, which would have silently gutted recall. `canonical_entities` stays in the
payload for eval and future client-side boosting. `[reasoned]`

**Curation discipline** `[read from source]`:
- `entities.raw.json` (machine) is kept separate from `entities.json` (reviewed) — re-running must
  never clobber hand corrections.
- `entities.responses.jsonl` stores every teacher response verbatim, so `--remerge` rebuilds the
  machine artifact with **0 API calls**. The key-space fix had cost 231 billed calls precisely
  because the only copies of those extractions were already fused inside the merged output.
- Curation is an auditable Python script with explicit op dicts and a timestamped backup, not
  dozens of hand edits — deterministic and reversible.
- Gate criterion: **same-book collisions fail (exit 1), cross-book collisions pass.** Book-scoped
  resolution cannot separate two identities inside one novel; across books the filter separates them
  and the unfiltered resolver already declines to guess.

**Absent:** no measurement of entity-map recall or precision against a ground-truth character list.
`docs/CHARACTERS.md` was written as the human ground truth for under-extraction, but no coverage
number was ever computed. See [E30](#E30).

---

<a id="E11"></a>
## E11 — Name normalization and transliteration folds

`src/rag/names.py`, landed `d0d473b`, design committed `2711bbb`. Two folds, both **code**; only
irregular variants are **data**. Both are derived match keys applied **symmetrically** to the match
index and the incoming query; the `canonical` field keeps its true spelling and is never
overwritten. `[read from source]`

| Function | What it does |
|---|---|
| `normalize()` | casefold, NFKD diacritic strip, ligature expansion (`Æ`→`Ae`), separator collapse over whitespace/hyphen/apostrophe **and sentence/quote punctuation** (`? ! ; : " ( ) …`) |
| `fold()` | `normalize()` plus an **ordered** transliteration ruleset |
| `fold_tokens()` | matching on **token boundaries**, not substrings |

**Why `fold()` exists:** the corpus is **Garnett only**, while readers know these novels through
other translators. A P&V reader types `Dunya`, `Razumikhin`, `Sonya` where Garnett wrote `Dounia`,
`Razumihin`, `Sonia`. Without the fold the entity never resolves, no expansion happens, and the BM25
leg searches tokens that occur **nowhere in the corpus**. `[reasoned]`

**Ruleset coverage** (order is load-bearing — several rules manufacture input for later ones): щ
(`tch`/`stch`/`shch`/`tsch`), `kh`↔`h`, `ph`↔`f`, `zh`↔`j`, `ff$`↔`v`, `ou`↔`u`, ё (`yo`↔`e`),
`ya`/`yu`↔`ia`/`iu`, `y`↔`i`, Garnett's French `c` (`Colia`↔`Kolya`), German-tradition `ja`/`ju`/`w`,
`x`↔`ks`, doubled-consonant collapse. `[read from source]`

**The code/data line is "derivable vs irregular", not "mechanical vs semantic"** (a revision of the
2026-07-17 position). Anything a rule reaches — including `Rogojin`↔`Rogozhin` and
`Dmitri`↔`Dmitry` — is code. Only what no rule reaches is data: `Muishkin`↔`Myshkin`,
`Shigalov`↔`Shigalyov`, `Ferdishenko`↔`Ferdyshchenko`. `[reasoned]`

**One rule was rejected on evidence.** `ui→i` catches `Muishkin`↔`Myshkin`, but folds the German
"Luise Ivanovna" into "Lise", merging her with Lise Khokhlakova and Lizaveta Drozdova. **A rule
earning one win and one false merge belongs in the data, not the code** — Muishkin became a curated
alias instead. `[reasoned]`

**Two bugs the folds caught or caused** `[measured]`:
- **The `?`-terminated-name bug** (found during the P4 eval run, 2026-07-21): `What happens to
  Dunya?` must fold to the same token as the bare `Dunya`, or the trailing mark strands the last
  token and the whole-token index lookup misses. **Most natural questions were silently not
  expanding** until this was fixed.
- **Substring matching does not travel.** `names.fold_tokens()` exists precisely because `liza ⊂
  lizaveta` resolves "Lizaveta Prokofyevna" to "Liza". `EntityMap` used it correctly; `router.py`,
  one file over, matched book aliases with `if alias in lower`, so "demons" matched inside
  "demonstration". Defect D1 of the P4 review.

**Fold output is not human-readable** (`fold("Count") == "kunt"`) — harmless precisely because it is
applied to both sides and never surfaces. Expansion appends the **verbatim** `canonical`, never a
fold key; that invariant was checked by the P4 review. `[measured]`

**Dense vs sparse asymmetry:** the dense leg embeds **raw** query text (embeddings absorb spelling
fuzz on their own); the folds apply to the BM25 / entity-match leg where exact tokens matter.
`[reasoned]`

**Book titles are a separate map**, not part of `entities.json`: `Config.BOOK_TITLE_ALIASES` (15
entries) + `Config.resolve_book_title()`, which **raises on an unmapped title**. Rationale: a wrong
title becomes a hard filter matching zero points — a silent empty result, worse than a miss. Both
sides of the lookup go through `names.normalize()` so a diacritic or curly apostrophe is not a hard
400. `[read from source]`

---

<a id="E12"></a>
## E12 — Chapter summaries (`summaries.json`)

| Quantity | Value | Date / phase | How produced | Tag |
|---|---|---|---|---|
| Coverage | **231 / 231 chapters**, 0 failures, 0 gaps against `structure.json` keys | P3, 2026-07-16 | `scripts/build_summaries.py` | `[measured]` |
| Runtime load count | `summaries=231` | P6, 2026-07-22 | FastAPI lifespan log | `[measured]` |
| Chapters refused by the teacher | **2** — Svidrigailov's confession, Dmitri's "Confession of a Passionate Heart" | P3, 2026-07-16 | Gemini `PROHIBITED_CONTENT` | `[measured]` |
| Recovery | both recovered via `FALLBACK_PROMPT_TMPL` | P3, 2026-07-16 | `scripts/build_summaries.py` | `[measured]` |
| Summaries from the primary prompt | **229 / 231** | P3 | — | `[computed]` |
| Runtime DEFAULT fallbacks needed | **0** | P3 | full key parity with `structure.json` | `[measured]` |

**Why summaries exist at all:** chunk-RAG **structurally cannot** answer "what happens in chapter N"
— the answer is spread across a whole chapter, and no top-k over chunks reconstructs it.
Pre-generation is bounded one-time work over a static corpus and removes the failure mode entirely
for scoped summary queries. `[reasoned]`

**Why a teacher LLM and not Mitya:** Mitya would summarize in persona voice and would contend for
the GPU. The teacher runs once, offline, in a neutral voice; summaries are internal context, never
shown to the user. `[reasoned]`

**`ContentBlocked` is fatal, never retried** (`2550fab`): `PROHIBITED_CONTENT` sits **above** the four
configurable `HARM_CATEGORY_*` settings — `BLOCK_NONE` cannot reach it. Refusal is deterministic, so
retrying bought 3 billed failures per chapter. It is **output-side, not the text**: the same two
chapters pass *entity extraction* untouched — narrating the events trips the filter, extracting the
same facts does not. `[measured]`

**`FALLBACK_PROMPT_TMPL` stays a fallback.** 229/231 already came from the narrative prompt; swapping
the default would mean re-running all 231 for register consistency. `[reasoned]`

---

<a id="E13"></a>
## E13 — Teacher-model bake-off

The only competitive model selection v1 ran. `test/bakeoff/spike_bakeoff.py`, results in
`test/bakeoff/RESULTS.md` + `raw_results.json`.

**Method** (2026-07-15): 2 fixed chapters — `Crime and Punishment|6|3` (~5,225 tokens, entity
extraction) and `The Brothers Karamazov|3|4` (~4,538 tokens, chapter summary) — across 7 models.
**10 real API calls total.** Pass criterion: **strict JSON parse**, plus latency. `[measured]`

| Model | Task | Strict JSON | Latency | Failure |
|---|---|---|---|---|
| Gemini 2.5 Flash | entity_extraction | **no** | 1.7 s | `404 no longer available to new users` |
| Gemini 2.5 Flash Lite | entity_extraction | **no** | 0.6 s | `404 no longer available to new users` |
| Gemini 3 Flash Preview | entity_extraction | yes | 24.6 s | — |
| **Gemini 3.1 Flash Lite** | entity_extraction | **yes** | **2.2 s** | — |
| **Gemini 3.1 Flash Lite** | chapter_summary | **yes** | **3.6 s** | — |
| Gemini 3.5 Flash | entity_extraction | **no** | 21.2 s | `429 quota exceeded` |
| Gemma 4 26B A4B IT | entity_extraction | **no** | 127.3 s | chain-of-thought before the JSON |
| Gemma 4 26B A4B IT | chapter_summary | **no** | 34.3 s | chain-of-thought before the JSON |
| Gemma 4 31B IT | entity_extraction | **no** | 76.1 s | chain-of-thought before the JSON |
| Gemma 4 31B IT | chapter_summary | **no** | 27.1 s | chain-of-thought before the JSON |

**Winner: `models/gemini-3.1-flash-lite`** — the only free-tier text model whose **daily** quota
(500 rpd) is large enough for a 231-chapter pass; every other free-tier text model is **20/day**.
Latency was secondary; the binding constraint was quota. `[measured]`

**Rate limits as configured** (`src/config.py`, mirroring the dashboard row — not derivable from the
model id): `TEACHER_RPM=15`, `TEACHER_TPM=250000`, `TEACHER_RPD=500`,
`TEACHER_MAX_OUTPUT_TOKENS=2048`, `TEACHER_TIMEOUT_MS=120000`, `TEACHER_MAX_RETRIES=3`.
`[read from source]`

**Three billing findings from the same period** `[measured]`:
- **SDK auto-retry disabled (`attempts=1`).** google-genai defaults to 5 attempts with 429 retriable;
  one rate-limited call fires 5 billed requests. Confirmed: **1 intended call showed as 11** on the
  dashboard.
- **Circuit breaker: 3 consecutive 429s aborts.** The local RPD counter only ever sees this process's
  requests.
- **Never run two builders at once** — each process has its own `RateLimiter(rpm=15)` seeing only its
  own traffic, so two concurrent runs bill ~30 rpm against a 15 rpm limit. Discipline only, not
  enforced.
- **Operating rule: the dashboard is ground truth, never local counters.** Quota accounting was never
  fully reconciled (closed won't-fix, 2026-07-22).

**Note on scope:** this is a *teacher* bake-off. **No embedder or reranker bake-off was ever run** —
see [E30](#E30).

---

<a id="E14"></a>
## E14 — Router

`src/rag/router.py`. Two routes, a pure function, zero latency. `[read from source]`

| Route | Condition |
|---|---|
| **STRUCTURAL** | a summary-intent keyword (`summarize`, `what happens in`, `overview of`, `recap`) **AND** a resolvable chapter/part scope — from the UI filter, or a `book-title + chapter/part N` regex resolved against `structure.json` **and** present in `summaries.json` |
| **DEFAULT** | everything else |

**Why the AND, not an OR:** the STRUCTURAL path is a `summaries.json` lookup keyed by chapter. Intent
without a resolvable chapter has **nothing to look up**; thematic queries like "summarize
Raskolnikov's theory" are served correctly by DEFAULT. `[reasoned]`

**Why deterministic and not an ML classifier** (DeBERTa / NLI / logistic regression, all in the
original design): two routes, one of which is only valid with a resolvable chapter key. A
keyword-plus-scope test is **exact** where a classifier is merely probabilistic, and it costs
nothing. `[reasoned]`

**`route()` takes `summaries` as an argument** (a signature deviation from the phase sketch, defect
D4 of the P4 review): ruling R15 requires a missing summary to fall back STRUCTURAL → DEFAULT, and
route time is the correct seam. `_validate_scope` checked `structure.json` and had no way to reach
`summaries.json`. Latent only because the two artifacts have 231/231 key parity ([E12](#E12)).
`[measured]`

| Quantity | Value | Date / phase | How produced | Tag |
|---|---|---|---|---|
| Router accuracy on the eval set | **6 / 6 expected-route rows matched, on every one of 6 ablation arms** | 2026-08-16 | `scripts/eval_retrieval.py`, `route_matched`/`route_checked` in `test/ablation.json` | `[measured]` |

---

<a id="E15"></a>
## E15 — Hybrid retrieval, fusion, and rerank

`src/rag/retrieve.py`. All values are `Config` literals unless noted. `[read from source]`

| Parameter | Value | Config field |
|---|---|---|
| Hard filter | UI `book`/`part`/`chapter` → a Qdrant `must` filter | — |
| Dense prefetch limit | **50** | `PREFETCH_LIMIT` |
| Sparse prefetch limit | **50** | `PREFETCH_LIMIT` |
| Fusion | Qdrant **native RRF**, server-side, one round trip | — |
| Fused top-k | **25** | `TOP_K_FUSED` |
| Reranker | **FlashRank `ms-marco-MiniLM-L-12-v2`**, ONNX, CPU | `RERANK_MODEL` |
| Reranked top-k | **3** | `TOP_K_RERANK` |
| Rerank kill-switch | `true` (always on; no runtime latency governor) | `RERANK_ENABLED` |
| STRUCTURAL dense excerpts | **2**, hard-filtered to the chapter | `TOP_K_STRUCTURAL_EXCERPTS` |
| FlashRank version | **0.2.10**; the model id was verified against `flashrank/Config.py`'s `model_file_map` at P4 | — |
| Reranker size | ≈ 100 MB ONNX | — |

**Why RRF over weighted score blending:** dense cosine and BM25 scores live on **incomparable
scales**. The original design's per-query-type weight tables are hand-tuned magic numbers; rank
fusion is scale-free and robust. `[reasoned]`

**Why Qdrant-native RRF over client-side:** no hand-rolled merge code, one round trip. The internal
`k` is not tunable — acceptable, because the per-leg prefetch limits are the actual knobs.
`[reasoned]`

**Why FlashRank over bge-reranker / mxbai:** no torch and no GPU dependency, ~100 MB ONNX, sufficient
for 25→3 on a small literary corpus. The GPU is fully allocated ([E1](#E1)). `[reasoned]`

**Why no dynamic rerank fallback** (drafted earlier and cut): 25 pairs on CPU were *estimated* at
~0.3–0.5 s, inside budget, so a mid-flight abort is machinery for a problem that does not exist. The
later measurement put it at **~1.96 s** ([E28](#E28)) — the estimate was off by ~4×, and the design
conclusion (no governor) was kept anyway, for a solo local deploy. `[reasoned]` / `[measured]`

**One-leg arms are valid queries, not degenerate ones:** RRF over a single prefetch list is
order-preserving. This is what made the ablation ([E26](#E26)) a two-line, default-preserving change
(`retrieve(..., legs=("dense","bm25"))`). `[reasoned]`

**qdrant-client 1.18.0 API shape, verified from installed source at P4:** `query_filter` at top
level, `filter` only inside `Prefetch`. `[measured]`

---

<a id="E16"></a>
## E16 — Context assembly

`src/rag/context.py`. `[read from source]`

| Route | Context |
|---|---|
| DEFAULT | top-**3** reranked chunks |
| STRUCTURAL | the chapter summary + top-**2** dense excerpts hard-filtered to that chapter |

- Source list is **deduped on `(book, part, chapter)` preserving rank order** — top-3 chunks
  frequently share a chapter.
- STRUCTURAL lists the summarized chapter first.
- On a missing summary, `context.py` falls back rather than emitting a fabricated empty quotation.

| Quantity | Value | Date / phase | How produced | Tag |
|---|---|---|---|---|
| Prompt context size | ≈ 2,000 Mistral tokens for 3 chunks | design | 3 × 500 nomic tokens × ~1.3 ([E6](#E6)) | `[estimate]` |
| Context window | 4096 (`Config.CTX_SIZE`) | P0 | `src/config.py` | `[read from source]` |

---

<a id="E17"></a>
## E17 — Prompt composition, the persona/grounding seam, decoding

**The seam, stated exactly.** The fine-tune's training rows **never contained retrieved context in
the prompt**, and its system prompt explicitly forbids referencing external sources. Injected
passages are therefore out-of-distribution for the model that has to use them. `[reasoned]`

**Composition A** (`Config.PROMPT_COMPOSITION = "A"`, the shipped default, ruling R19): the system
prompt starts with the **verbatim fine-tuned persona text** — verified at P7 as byte-identical to
`training.py`'s `SYSTEM_PROMPT` — then appends the passages framed as the author's **own
recollections**, first-person-compatible and aligned *with* the "no external references"
conditioning rather than against it. The user turn carries the **question only**. `[read from source]`

**Composition B**: persona head unchanged; passages **and** question both move into the user turn.
Added at P7 for the A/B; never deleted. `src/api/app.py` calls `build_messages()` with no `mode=`
argument, so serving uses A. `[read from source]`

**Why programmatic Sources over inline citations** (the original design demanded strict
`[Book I, Chapter 2]` citations): the model was fine-tuned to **refuse** external references. Inline
citation enforcement fights the persona head-on and fails unpredictably. Appending the deduped source
list **in code** makes grounding display independent of whether the model cooperates. `[reasoned]`

**Decoding** (`src/rag/generate.py`, `src/config.py`): `[read from source]`

| Parameter | Value | Config field |
|---|---|---|
| `max_tokens` | **400** | `MAX_TOKENS` |
| `temperature` | **0.6** (vs llama-server's ~0.8 default) | `GEN_TEMPERATURE` |
| `top_p` | **0.9** | `GEN_TOP_P` |
| Empty-completion retries | **1** (re-draw with a new seed) | `EMPTY_RETRIES` |
| Transport | `httpx` streaming to llama-server's OpenAI-compatible `/v1/chat/completions` | `LLAMA_SERVER_URL` |
| Timeouts | connect **5 s**, read **120 s**, write 5 s, pool 5 s | `httpx.Timeout` |

**The sampling floor and the retry exist because of a measured failure**, not a hunch: **10 of 72**
generations in the P7 A/B returned zero tokens — the weak q4 7B sampling EOS as its first token. The
failure was **per-run, never per-query** (i.e. stochastic, not a bad input). Fixed by the temperature
floor plus the zero-token retry, committed `253a8ee`. `[measured]`

**Why `httpx` over LiteLLM** (drafted, and already in `requirements.txt`): LiteLLM abstracts 100+
providers in order to call exactly one localhost endpoint — a heavy dependency with its own churn,
where the whole need is a ~30-line streaming client. `litellm` was removed, `httpx` added.
`[reasoned]`

---

<a id="E18"></a>
## E18 — Cache

`src/rag/cache.py`. `[read from source]`

| Property | Value |
|---|---|
| Backend | `diskcache` (SQLite-backed), `data/cache/` |
| Key | `normalize(query) + filters` — **exact match only** |
| TTL | none (static corpus) |
| Stored | successful, non-empty answers only |
| Value shape | `{answer, sources, footer}` |
| Flag | `Config.CACHE_ENABLED = true` |
| Size | ~15 lines |

**Why exact-match only:** a semantic cache adds an embedding index for a single-user app whose hit
rate is near zero anyway. The cache exists as the product's cache **boundary**, deliberately kept
trivial. Low hit rate is an accepted outcome, not a defect. `[reasoned]`

**Why diskcache over Redis:** no external service for a solo local deploy. `[reasoned]`

**Legacy migration by contract, not by script:** `cache.get` treats any stored value that is not the
`{answer, sources, footer}` dict — i.e. a bare answer string written before the UI work — as a
**miss**, regenerating once and self-healing on the next hit. No migration script, no key-namespace
bump. `[read from source]`

**Behavioural consequence, by design:** the cache path emits the whole answer as a **single** `token`
event, so a hit renders as one instant block rather than streaming prose. `[read from source]`

Measured hit latency: [E28](#E28).

---

<a id="E19"></a>
## E19 — API surface and the SSE contract

`src/api/app.py`, 320 lines. `[read from source]`

| Endpoint | Contract |
|---|---|
| `POST /chat` | `{query, filters?, session_id?}` → SSE stream (`text/event-stream`) |
| `GET /health` | llama-server reachability + Qdrant collection presence/count + artifact load status; **200 or 503** |
| `GET /books` | the `structure.json` tree, feeding the filter UI |
| `/` (mounted **last**) | `StaticFiles(directory="frontend/dist", html=True)` |

**Event order** `[read from source]`:
- **Live path:** `token`* → `sources` → `meta` → stream close. There is **no `done` event on the live
  path by design**; the client relies on stream close.
- **Cache path:** `token` (one, whole answer) → `sources` (re-emitted from the stored value) →
  `meta` (`cache_hit: true`, `ttft_ms: null`, `route_name: "CACHE"`) → `done`.

**`meta` payload:** `{route_name, chunk_ids, ttft_ms, total_ms, filter_relaxed, no_context,
cache_hit}` — fields the pipeline already computed and previously only logged. Always on, no request
flag (single local user; query text is already in local logs). It is emitted **last, after
`cache.set`**, so `total_ms` is a truthful end-to-end figure. Clients must treat `meta` as optional
and non-terminal. `[read from source]`

**`session_id` is log-correlation only** in v1. Conversation memory is out of scope. `[read from source]`

**Binding:** uvicorn and llama-server both bind `127.0.0.1` by default. Query text appears in local
logs only. v1 shipped **unauthenticated, single-user, by design**. `[read from source]`

**Mount-last is load-bearing and was AST-verified**, not eyeballed: the `StaticFiles` mount is the
literal last module statement (routes at lines 126 / 161 / 167, mount at 320). Consequence, accepted
deliberately: `StaticFiles` **raises at startup if `frontend/dist/` is missing**, so `npm run build`
is mandatory before *any* uvicorn boot, dev-proxy workflows included. Chosen over a guarded mount
because failing loud at boot beats silently 404-ing a missing build. `[measured]`

**CWD caveat:** `directory="frontend/dist"` resolves against uvicorn's **process CWD**. Running from
anywhere but the repo root 404s every UI route. `[read from source]`

---

<a id="E20"></a>
## E20 — Concurrency, timeouts, failure modes

`[read from source]` unless noted.

| Mechanism | Value |
|---|---|
| Concurrency | `asyncio.Semaphore(1)` — one request at a time through retrieval **and** generation, held into the generator's `finally` |
| Why 1 | llama-server runs `--parallel 1`; the fastembed embedder is not re-entrant |
| Behaviour under load | concurrent submits **queue**; they do not fail |
| llama-server unreachable | **503**, resolved by priming the first token **before** the SSE stream opens |
| llama-server timeout | **504**, same mechanism |
| Unmapped book title | **400** (`Config.resolve_book_title` raises) |
| Errors | structured JSON, **never a hung stream** |

**Failure modes with fallbacks** `[read from source]`:
- **Qdrant collection missing or empty** → fail fast at FastAPI **startup** (`Config.validate_rag()`
  checks `structure.json`, `entities.json`, `summaries.json`, `data/vector_db/`).
- **Zero chunks under a hard filter** → retry once **without** the filter, log `filter_relaxed=true`;
  still zero → answer without context, omit Sources, log `no_context=true`.
- **Missing chapter summary** → STRUCTURAL falls back to DEFAULT (logged).
- **Models load once** at lifespan startup — fastembed, FlashRank, entities, summaries, structure,
  cache, semaphore. Never lazily per query.

**Lifespan load counts as observed** (P6, 2026-07-22): `qdrant_points=3284, entities=256,
summaries=231, structure=5`. `[measured]`

**Verified failure behaviour, P6, on the real card** (2026-07-22, `docs/RUNBOOK-P6.md` procedure):
llama-server killed → **503 immediately, no hung stream**, and `/health` 503; unmapped title →
**400**; `filter_relaxed=true` retry exercised; cache hit reports `cache_hit=true` with
filter-sensitive keys. `[measured]`

**Ordering note, settled statically (2026-07-31) as intentional:** the semaphore is acquired
*before* the cache is checked (`app.py:173` before `:176`). With one user submitting sequentially,
nothing ever queues behind a draining stream. Recorded because it looks like a defect and is not.
`[reasoned]`

**Logging:** stdlib `logging`, no `print`; structured key-value per request — query, route, filters,
resolved entities, `chunk_ids` (**not** chunk text), cache hit, `ttft_ms`, `total_ms`, fallback flags.
`[read from source]`

**`no_context` is effectively unprovable through the UI**: `filter_relaxed` retries unfiltered
whenever a hard filter matches zero chunks, and `rerank` always returns its top-k, so narrowing the
scope cannot starve `sources`. Covered by code reading only. `[reasoned]`

---

<a id="E21"></a>
## E21 — Frontend

`frontend/`, Vue 3 SPA over the existing `/chat` SSE endpoint. Adds **no** backend component and no
GPU consumer. `[read from source]`

| Quantity | Value | Source |
|---|---|---|
| Tracked source files under `frontend/src` | **28** | `git ls-files` |
| Vue single-file components | **10** | `git ls-files` |
| Lines of tracked frontend source | **2,746** | `[computed]` over `git show` of the 28 files |
| Vitest test files | **5** | `api/chatStream`, `composables/useChat`, `composables/useBooks`, `state/session`, `components/FilterControl` |
| Unit tests passing | **19 / 19** at P10 (2026-07-31 fixes expected to make it 20/20 — **never re-run**) | `npm run test` |
| Production bundle | **340 KB**, 14 content-hashed self-hosted Spectral font files | `npm run build`, P9 |

**Pinned versions** (`frontend/package.json`, exact pins, no ranges): `vue` 3.5.40,
`@fontsource/spectral` 5.3.0, `vite` 8.1.5, `vitest` 4.1.10, `typescript` **5.9.3**, `vue-tsc` 3.3.8,
`jsdom` 30.0.0, `@vue/test-utils` 2.4.11, `@types/node` 26.1.2. Node **26.5.0** via `nvm` (not the
system default, 22.22.2). `[read from source]`

**TypeScript pinned to 5.9.3, not 7.0.2**, deliberately: `vue-tsc` type-checks through TypeScript's
**JavaScript** compiler API, and TS 7.0 (the Go port) ships no programmatic API until 7.1, so
`vue-tsc` 3.3.8 fails with `ERR_PACKAGE_PATH_NOT_EXPORTED: './lib/tsc'`. Vite never invokes `tsc`, so
nothing is lost. Bump trigger: TS 7.1's programmatic API plus a `vue-tsc` release consuming it.
`[measured]`

**SSE is read with `fetch` + `response.body.getReader()`, never `EventSource`** — the browser's
`EventSource` cannot `POST`. All network access is typed and confined to `frontend/src/api/*`, which
is the app's **only** `fetch` site. `[read from source]`

**Serving:** production `frontend/dist/` mounted last by FastAPI ([E19](#E19)); dev uses Vite's own
server proxying `/chat`, `/health`, `/books` to `127.0.0.1:8000`. **Same relative paths in both
modes** — no hardcoded host, no CORS. `[read from source]`

**Design system — "The Evening Reading Room"**, one dim theme, no light theme in v1
(`frontend/src/styles/tokens.css`, values computed from OKLCH rather than measured on a render):
`[read from source]`

| Token group | Values |
|---|---|
| Surfaces | `--surface #16130f`, `--panel #211d17`, `--panel-2 #2a251d` |
| Ink | `--ink #ede7dc`, `--ink-muted #aaa08d` |
| Accent (**exactly one**) | `--accent #e8a33d` "Lamplight Amber", + hover/active/muted |
| Type stacks | reading `"Spectral"` serif; chrome `system-ui` sans |
| Type scale | 32 / 24 / **19 (reading)** / 16 / 14 / 13 px, ratio ~1.2, root 16px |
| Spacing | 4px base — 4/8/12/16/24/32/48/64 |
| Radii | 4 / 8 / 12 px, no fully-round pills |
| Motion | `cubic-bezier(0.16, 1, 0.3, 1)`, 150 ms / 220 ms, collapsed under `prefers-reduced-motion` |
| Elevation | exactly one shadow, warm/ambient |
| Reading measure | **66ch at line-height 1.7** |
| Focus ring | global **2px `--accent`** |
| Z-index | semantic scale 0/10/20/30/40; nothing else creates a stacking context |

**Fonts are self-hosted** — no CDN, no runtime external fetch. `@fontsource/spectral` 5.3.0 with six
explicit per-subset/per-weight imports (latin 400/400i/500/600, cyrillic 400/400i), never the barrel;
the variable package `@fontsource-variable/spectral` **does not exist** — Spectral is a static family.
`[measured]`

**Four defects worth their space, all found by running the thing** `[measured]`:
1. **`tokens.css` was never imported anywhere.** It would have shipped unstyled. Caught in the P8
   round; the import was added to `main.ts`.
2. **Request fan-out.** `/health` fired ×3 and `/books` refetched per turn. State was module-scoped,
   but `onMounted` registers **per calling component** (3 call sites each). Fixed with module-level
   once-guards; `repoll` deliberately left unguarded.
3. **Turns must be `reactive()` *before* the push into `session.turns`.** `useChat` built each `Turn`
   as a plain object, pushed it, then mutated the raw local reference. Vue stores a pushed object raw
   and only proxies it on read, so every `turn.answer +=` bypassed the set trap: **values correct in
   memory, DOM never updated**. It presented as a permanently blank answer plus a debug row stuck on
   "diagnostics unavailable" — which looked like a cache regression and was not. **All 19 unit tests
   were green through this**, because they assert values and the defect is purely a render
   notification. No unit test can catch it.
4. **`chapter_label: null` rendered as the string `"null 5"`.** `structure.json` carries
   `chapter_label: null` for The Idiot and Notes from the Underground ([E5](#E5)), but `api/types.ts`
   declared the field as the literal `"Chapter"`, so the null was never type-checked. Type widened to
   `"Chapter" | null`; both render sites fall back to `"Chapter"`. Fixed frontend-side — the type has
   to tolerate null regardless of the generator.

**Accepted risk:** 6 `high` npm-audit findings, all one **dev-only** chain
(`@vue/test-utils → js-beautify → editorconfig → minimatch → brace-expansion`,
GHSA-mh99-v99m-4gvg). Never in the browser bundle; `audit fix --force` would downgrade test-utils.
`[measured]`

---

<a id="E22"></a>
## E22 — Retrieval eval set

`test/retrieval_eval.json`, 8.1 KB. Hand-labeled `query → (book, part, chapter)`.
`[read from source]`

| Quantity | Value |
|---|---|
| **Total rows** | **27** |
| Primary (headline) rows | **25** |
| Negative-control rows | **2**, scored and reported in a separate bucket, excluded from the headline |
| Rows carrying an explicit hard filter | 2 (book-alias rows: `"Demons"`, `"The Devils"`) |
| Rows carrying `expected_route` | 6 — 2 `STRUCTURAL` (+ `expected_scope`), 4 `DEFAULT` |
| Label granularity | one gold `(book, part, chapter)` per query |

**Per-book coverage** `[computed]` from the set: Crime and Punishment 8, The Brothers Karamazov 7,
The Possessed 6, The Idiot 4, Notes from the Underground 2.

**Row families** `[read from source]`: axe/plot-event grounding rows; entity-resolution rows;
cross-translation fold tripwires; book-title alias rows; STRUCTURAL routing rows; and the 2 negative
controls, whose whole purpose is proving `route()` stays `DEFAULT` on broad/thematic phrasing that
carries a summary keyword without a resolvable scope (ruling R14's AND condition).

**How rows were grounded:** each carries a `note` citing the `summaries.json` key it was written
against (e.g. `"grounded: summaries.json 'Crime and Punishment|1|7' -- axe murder of Alyona and
Lizaveta"`), and character spellings were checked against `docs/CHARACTERS.md`. `[read from source]`

**Cross-translation tripwires (the RR5 canaries)** — rows deliberately spelled the way a
non-Garnett reader would type them, so a fold regression or a lost `search_query: ` prefix shows up
on *exactly those rows* rather than as a diffuse aggregate drop: `Dunya`, `Razumikhin`, `Sonya`
(C&P), `Myshkin` (The Idiot), `Zosima` vs corpus `Zossima` (BK, doubled-consonant collapse),
`Mikhailovna` vs corpus `Mihailovna` (The Possessed, `kh→h`). Both BK and Possessed rows were
verified against `entities.json` to confirm **no existing alias makes the fold redundant** — the
fold is genuinely load-bearing on them. A Verhovensky row was considered and **rejected** as a
misleading test: `entities.json` already carries both spellings directly, so no fold is exercised.
`[measured]`

**Set growth:** 26 rows → **27** at P7 (2026-07-22), when the driver was fixed and the two
cross-translation tripwires were annotated/added. `[measured]`

**Known weaknesses, stated at the time and never fixed in v1** `[reasoned]`:
- The plan called for ~30 pairs; the set is 27 with ~25 effective. Earlier the effective count was
  put as low as **20–22 pairs**.
- The set is **Garnett-heavy**: only **2 rows** exercise the cross-translation folding that the whole
  entity/expansion layer was built for. Aggregate recall therefore measures that layer on the wrong
  workload.
- Before P7, the driver called **neither** `route()` nor `build_hard_filter()`, so the six rows
  documented as router/alias cases **verified nothing they claimed**. Fixed at P7.

---

<a id="E23"></a>
## E23 — Retrieval metrics and the headline number

**What is measured:** `scripts/eval_retrieval.py` scores the **hybrid + fusion leg, pre-rerank** —
`retrieve()`'s fused output sliced to `k`. Because `Config.TOP_K_RERANK` is **3**, smaller than 5,
**rerank is structurally invisible at k=5** and must be measured in its own k=3 bucket against its
own baseline. The two buckets are not comparable. `[read from source]`

**Metric definitions** (`scripts/eval_retrieval.py:117-138`): `[read from source]`
- `hits = ` rows whose gold `(book, part, chapter)` appears anywhere in the top-k.
- `recall@k = hits / rows`.
- `MRR = mean(1/rank)` over rows, 0 for a miss.

**Why recall-shaped and not precision-shaped:** each query has **exactly one** labeled gold chapter,
so `recall@k` here is a hit-rate — "did the right chapter reach the context window at all". That is
the question that matters upstream of generation: a chapter that never reaches the top-k cannot be
recovered by any prompt. Precision is not meaningful against a single gold label, and NDCG needs
graded relevance the set does not carry. `[reasoned]`

**The headline — and a three-way disagreement across sources. Recorded, not resolved:**

| Value | Rows | Date | Source | Status |
|---|---|---|---|---|
| `recall@5 = 0.731`, `MRR = 0.408` | **19/26** | 2026-07-21 (P4 run) | `plans/mitya-rag/05-impl/phase-4.md`; still quoted in `README.md` | **superseded** — predates the P7 driver fix and counts the 2 negative controls inside the headline |
| **`recall@5 = 0.720`**, `MRR = 0.406` | **18/25** | 2026-08-16 | `scripts/eval_retrieval.py --arm full`, `test/ablation.json`; correction written into `docs/RAG.md` §VII.1 | **the v1 number.** 0.731 is explicitly "not reproducible with the current harness" |
| **`0.7105`** | **27/38** | later v2 re-analysis | registers R20 / R72 (detail in v2 material this document does not read) | **the corrected number.** A different, larger denominator — the eval set grew after v1 |

The three numbers are close and mean different things. The denominator is the tell: **26 → 25 → 38**.
An architecture document quoting "recall@5" without its denominator is quoting nothing.

**Negative-control bucket, `full` arm** (2026-08-16, reported separately by design):
`recall@5 = 0.500 (1/2)`, `MRR = 0.125`. `[measured]`

**Resolution floor:** 25 primary rows ⇒ **one row is worth 0.040 recall**. Any delta under ~0.08 is a
single row. `[computed]`

**The first recall number is a tripwire, not a grade.** Uniformly poor recall across every query is
the signature of a query/document **prefix mismatch** (ruling RR5, [E7](#E7)), not of bad ranking.
Recall was not uniformly poor at P4, so the prefix pairing was intact. Of the 7 P4 misses, entities
resolved on 6 — the misses were **ranking depth**, not resolution failure. `[measured]`

---

<a id="E24"></a>
## E24 — Answer-quality evaluation and the prompt-composition A/B

`test/quality_eval.json` (36 rows) → `scripts/eval_quality.py` → `src/rag/judge.py`.
Run and judged 2026-07-22 on the real card. `[measured]`

**Set:** 36 queries, **12 each** across three themes — Motivation / Philosophy / Ambiguity. All
open, unscoped, thematic questions; no hard filter, matching their nature. `[read from source]`

**Judge:** one Gemini call per answer through the existing `TeacherClient`
(`Config.JUDGE_MODEL = TEACHER_MODEL = models/gemini-3.1-flash-lite`), scoring
`persona_retention` (1–5), `groundedness` (1–5), `fabricated_events` (bool), and a free-text
rationale. **Groundedness is judged against the retrieved `passages_block`** — the content the
programmatic Sources actually returned — never against inline citations, since the persona is
instructed to never mention sources at all; an inline-citation check would fail by design on every
response. `[read from source]`

**Design: 36 queries × 2 compositions = 72 generations, each judged.** `[computed]`

**Result — no winner** (`plans/mitya-rag/05-impl/quality_ab_results_summary.json`): `[measured]`

| Metric | **A** (context in system prompt, "recollections") | **B** (context in user turn) |
|---|---|---|
| n judged | 36 | 36 |
| Mean persona retention (1–5) | **3.111** | **3.222** |
| Mean groundedness (1–5) | **2.444** | **2.528** |
| Fabrication rate | **0.472** | **0.500** |
| Generation errors | 0 | 0 |
| Judge errors | 0 | 0 |

**Per theme** (mean persona / mean groundedness, A vs B): `[measured]`

| Theme | A persona | B persona | A grounded | B grounded |
|---|---|---|---|---|
| Motivation | 3.333 | 3.167 | 2.000 | 2.000 |
| Philosophy | 3.167 | 3.083 | 2.750 | 2.833 |
| Ambiguity | 2.833 | 3.417 | 2.583 | 2.750 |

**Decision: both compositions kept.** The planned "delete the loser" prune was **not taken**,
because there was no loser — the gaps are ~0.1 on a 1–5 scale at n=36, and B fabricates slightly
more while scoring slightly higher. `Config.PROMPT_COMPOSITION` remains a flag and `src/rag/generate.py`
retains both branches. `[measured]`

**Set size is a stated shortfall, not an oversight:** the plan asked for 30–50 queries *per theme*
(90–150 total); 36 was shipped as a seed, with the growth path documented (append `{theme, query}`
rows; the runner picks up the new count with no code change) rather than padding to 150 with
low-signal queries. `[read from source]`

---

<a id="E25"></a>
## E25 — The quality ceiling: the fine-tune, not the retrieval pipeline

**The claim:** in v1, answer quality was capped by the **fine-tuned model**, not by retrieval. This
was established by measurement and by an eliminative trace, not assumed.

**What established it** `[measured]`:

1. **Groundedness ≈ 2.5 / 5 on *both* compositions** ([E24](#E24): 2.444 and 2.528). The failure is
   invariant to how context is presented. A prompt-plumbing problem would move when the plumbing
   moved; this did not.
2. **The dominant failure mode is meta-commentary** — answers that discuss the question as a generic
   academic assistant instead of answering in persona from the passages. From the judge's own
   rationale on the worst A row: *"fails to adopt the Dostoevsky persona, instead acting as a generic
   academic assistant … offering only meta-commentary on the question rather than addressing the
   content of the text."*
3. **The prompt trace was verified clean, eliminating the serving layer.** `generate.PERSONA_HEAD`
   is byte-identical to `training.py`'s `SYSTEM_PROMPT`; the GGUF embeds the official Mistral-v0.3
   chat template, matching Unsloth's training template. There is no template or persona drift between
   training and serving to blame.
4. **The residual traces to the training data.** The training answers came from a weak
   `gemini-2.5-flash` teacher pass, on an older q4 7B base. Meta-commentary in the outputs is the
   teacher's register reproduced by the student.
5. **Stochastic empty completions, 10/72**, per-run and never per-query — the same weak q4 7B
   sampling EOS first. Fixed at the serving layer (temperature floor + zero-token retry, `253a8ee`),
   which is the *only* part of the ceiling that was serving-fixable.

**By contrast, what the retrieval side scored** `[measured]`: `recall@5 = 0.720 (18/25)` on the v1
harness ([E23](#E23)), with router accuracy 6/6 on every arm ([E14](#E14)), full artifact coverage
(231/231 summaries, 231/231 entity chapters, 3,284 points), and a fusion leg beating both of its
single legs ([E26](#E26)). Retrieval delivered the right chapter into the context window roughly 7
times in 10; the model then scored 2.5/5 on using it.

**Consequence, recorded at the time:** the A/B was the wrong experiment for the state of the system —
it measured a prompt seam while the binding constraint sat two layers upstream. Revisit the A/B only
after a better training-data / model pass. `[reasoned]`

**This is not resolvable at the serving layer.** No retrieval tuning, reranker swap, or prompt
composition moves it. `[reasoned]`

---

<a id="E26"></a>
## E26 — The ablation: arms and results as v1 recorded them

> **Read this section together with [E27](#E27).** The deltas below are what v1 measured and
> believed on 2026-08-16. They were **later withdrawn as noise**. This section is preserved as the
> record of what was run; [E27](#E27) is the correction.

**Motivation:** P4 and P7 answered *"is retrieval working"* but not *"what is each stage worth"*.
The entity layer alone cost 231 teacher chapter passes plus a human curation gate ([E10](#E10)) and
nothing measured whether it moved recall at all. `[reasoned]`

**Cost of the run:** 27 rows × 6 arms; the four k=5 arms are embed + Qdrant only (~0.3 s/row, ~30 s
each), the two rerank arms add FlashRank on CPU (~2 s/row, ~1 min each). **Under 10 minutes total,
zero API spend, no llama-server, no GPU.** `[estimate]`, plan-time

**Code changes: two, both additive, both default-preserving.** `retrieve()` gained
`legs: tuple[str, ...] = ("dense", "bm25")` — the default tuple is today's behaviour byte for byte,
so no caller changed — and `scripts/eval_retrieval.py` gained `--arm` plus `--json` so arms
accumulate into `test/ablation.json` instead of living in scrollback. `[read from source]`

**Arms** (baseline = the shipped config: entity-expanded query, dense+BM25 RRF, prefetch 50, fused
25): `[read from source]`

| Arm | Change | Question | k |
|---|---|---|---|
| `full` | none | reproduces the headline? | 5 |
| `no-expansion` | skip `EntityMap.resolve()`, send the raw query | what the entity map is worth | 5 |
| `dense-only` | drop the BM25 prefetch | what the sparse leg is worth | 5 |
| `sparse-only` | drop the dense prefetch | is BM25 carrying it or riding along | 5 |
| `rerank` | fused[:3] vs `rerank(raw_query, fused)[:3]` | what FlashRank is worth | 3 |
| `rerank-expanded` | same, on the **expanded** query | closes the P6 carry-forward | 3 |

**Results, 2026-08-16, `scripts/eval_retrieval.py --arm … --json test/ablation.json`**, primary
bucket (25 rows), verbatim from `test/ablation.json`: `[measured]`

| Arm | k | recall@k | hits | MRR | vs. its baseline |
|---|---|---|---|---|---|
| **full** (shipped) | 5 | **0.720** | 18/25 | 0.4060 | — |
| no-expansion | 5 | 0.680 | 17/25 | 0.4867 | −1 row, **+0.081 MRR** |
| dense-only | 5 | 0.680 | 17/25 | 0.4167 | −1 row |
| sparse-only | 5 | 0.640 | 16/25 | 0.4040 | −2 rows |
| rerank — fused top-3 | 3 | 0.560 | 14/25 | 0.3733 | own baseline |
| rerank — FlashRank on **raw** query (shipped) | 3 | 0.520 | 13/25 | 0.5000 | −1 row, +0.127 MRR |
| rerank — FlashRank on **expanded** query | 3 | **0.600** | 15/25 | 0.5067 | +1 row, +0.134 MRR |

Router matched **6/6** expected-route rows on **every** arm — routing is arm-independent, and a
mismatch would have meant an arm broke something it should not touch. `[measured]`

**What v1 concluded from this table on 2026-08-16** (`docs/RAG.md` §VII.1) — retained here as the
historical record: `[reasoned]`
- Hybrid fusion does real work, **modestly**: full (0.720) beats both single legs; dense worth 2
  rows, sparse worth 1. RRF is not decoration, but the effect is 1–2 rows, not a step change.
- Entity expansion buys **exactly one row** of recall while *lowering* MRR by 0.081 — it surfaces one
  document the raw query misses and pushes correct documents down the ranking on the rest. Framed at
  the time as measuring the layer on the wrong workload (only 2 of 25 rows exercise cross-translation
  folding, [E22](#E22)), i.e. a reason to grow the eval set, not to remove the layer.
- Flagged **ACTIONABLE**: reranking the raw query loses a row against no reranking (0.560 → 0.520)
  while expanded-query rerank gains one (0.560 → 0.600). Both rerank arms beat the unreranked
  ordering on MRR, so the reranker earns its ~1.96 s either way — it was being fed the wrong string.
  The 1-line switch in `src/api/app.py` was **not taken**, pending a confirming run, since 2 rows sits
  right at the noise floor.

**The plan pre-committed to reading a null result honestly**, before the run, in writing: *"the delta
table is the deliverable whatever it says … 'measured each stage's contribution and found the entity
layer flat at n=25' is a stronger engineering signal than a number with no attribution."*
`[read from source]`

---

<a id="E27"></a>
## E27 — THE WITHDRAWAL

> **This is a LATER CORRECTION. It is not what v1 believed at the time.** v1 shipped [E26](#E26)'s
> table and its three conclusions on 2026-08-16. The withdrawal came out of a subsequent
> re-analysis, after v1 closed.

**What was withdrawn:** the per-stage attribution deltas of [E26](#E26) — every one of them. On
re-analysis the deltas sit **below the minimum detectable effect at every reachable sample size**,
so none of them is distinguishable from noise. This was found once (register **R20**) and
reconfirmed independently later (register **R72**). `[read from source, producer unknown]` — the
R-registers live in v2 material outside this document's sources; they are cited here as pointers, not
reproduced.

**What survives:** exactly one effect — **BM25 on quotation queries** (register **R80**). The sparse
leg does measurable work on queries that quote or near-quote the text; that is where lexical match
beats dense similarity, and it is the one attribution claim v1's ablation can still support.
`[read from source, producer unknown]`

**Corrected headline:** **0.7105 (27/38)**. Note the denominator — 38, not v1's 25. See
[E23](#E23) for all three headline figures side by side. `[read from source, producer unknown]`

**Why the original table did not survive, in v1's own terms.** The arithmetic was on the page the
whole time and the run was sized against it anyway:
- 25 primary rows ⇒ 1 row = 0.040 recall ([E23](#E23)).
- The plan's own stated floor: *"any delta under ~0.08 is one row and is noise."*
- **The largest k=5 delta in the entire table is 0.080** (`full` 0.720 vs `sparse-only` 0.640, two
  rows). **Every other delta is one row, 0.040.** The rerank arms' deltas are one row each.
- So the table's own decision rule, applied strictly, admits **nothing** at k=5 and nothing at k=3.
  v1 wrote "on that rule only two results clear the bar, and both barely" and then reasoned from them
  anyway. The re-analysis is the same rule applied without the "barely". `[computed]`

**What this is, honestly stated.** A 27-row eval set cannot resolve a 1-row effect. The arms were
correct, the code was correct, the run was cheap and reproducible, the router check held on every
arm — and the conclusions were still unsupported, because the instrument's resolution was coarser
than the effects being claimed. The project **caught its own false positive** on re-analysis rather
than carrying an attractive per-stage attribution table forward into v2 as fact. The interesting
artifact is not the table; it is that the decision rule was written down *before* the run, in
`plans/mitya-ablation/plan.md`, which is what made the later withdrawal checkable rather than a
matter of taste.

**What did not change:** the shipped configuration. The ablation never proposed a production change
that was taken — the one `ACTIONABLE` (rerank on the expanded query) was explicitly deferred pending
confirmation and was never applied in v1. The withdrawal therefore costs no shipped behaviour; it
costs a claim. `[read from source]`

---

<a id="E28"></a>
## E28 — Latency, measured

All on the target **GTX 1060 6GB**. Sources: P7 `scripts/measure_latency.py` and the P6 server
request log (`ttft_ms` / `total_ms` fields). These **replace** the pre-build estimates; the estimate
is kept where it was materially off. `[measured]`

| Stage | Measured | Pre-build estimate | Date / phase | How produced |
|---|---|---|---|---|
| **Retrieval only** (embed + Qdrant fuse + rerank 25→3) | **~2.25 s** | ≤ 0.6 s | P7, 2026-07-22 | `scripts/measure_latency.py --retrieval-only` |
| — of which FlashRank rerank | **~1.96 s** | (unbudgeted; §III.2 assumed 0.3–0.5 s) | P7, 2026-07-22 | same |
| — of which embed + Qdrant fuse | **~0.3 s** | — | P7, 2026-07-22 | same `[computed]` from the two above |
| **TTFT** (~2k-token prompt prefill) | **~7.9 s** (`ttft_ms=7922`) | ≤ 3 s | P6, 2026-07-22 | `/chat` request log |
| **Total**, ≤400 decode tokens | **~21.3 s** (`total_ms=21320`) | ≤ 20 s | P6, 2026-07-22 | `/chat` request log |
| Decode throughput | **~23.6 tok/s** | — | P5, 2026-07-21 | stream smoke on the real card |
| **Cache hit, end to end** | **~0.008 s** | < 50 ms | P7, 2026-07-22 | `scripts/measure_latency.py`, two consecutive identical `/chat` calls |
| Cache-hit speedup vs cold path | **~2,660×** | — | — | 21.3 / 0.008 `[computed]` |

**Rerank dominates retrieval — 87% of it** (1.96 / 2.25) `[computed]`. This is the honest floor:
cutting it needs a smaller reranker, a GPU reranker (impossible under [E1](#E1)), or fewer
candidates. All were declared out of scope for v1.

**Two budgets were exceeded and accepted rather than chased**, under the project's own priority order
(hardware fit > goal fit > usability): retrieval by ~3.75× and TTFT by ~2.6×. For a solo local
deploy on a 6GB card, this was recorded as accepted, not as a regression. `[reasoned]`

**Measurement caveat, stated by the script itself:** there is **no clean cold cache-miss end-to-end
number**. Client-side, retrieval time is not separable from llama-server's own first-token latency;
and `measure_latency.py`'s cache-miss line is unreliable once the query is already cached. The only
clean generation numbers are P6's, above. `[read from source]`

---

<a id="E29"></a>
## E29 — Verification inventory

What was actually checked, and by what. `[measured]` unless noted.

| Gate | Result | When | Tool |
|---|---|---|---|
| Retrieval unit suite | **9 / 9** green (plain-assert script; pytest not installed) | P4 → ablation | `python test/test_retrieve.py` |
| Router / entities suites | present, green | P4 | `test/test_router.py`, `test/test_entities.py` |
| Entity fold gate | exit 0, **zero same-book collisions** | P3, 2026-07-19 | `scripts/check_entity_folds.py` |
| Structure tripwire | per-book unit counts vs manual reference chapter counts | P1, 2026-07-21 | diff of `structure.json` |
| Point-count tripwire | Qdrant `count()` == logged chunk count, mismatch warns | P2 | `scripts/ingest_corpus.py` |
| Formal code review | **6 defects / 3 undocumented deviations / 4 unverified claims / 5 plan defects** on P4 | 2026-07-19 | `design-review`, `plans/mitya-rag/06-review/phase-4.md` |
| Frontend type check | `vue-tsc --noEmit` exit 0 across P4–P10 | P3–P10 | `npm run build` |
| Frontend unit tests | **19 / 19** | P10, 2026-07-31 | `npm run test` (Vitest + jsdom) |
| Frontend offline harnesses | **12 / 12** — SSE split-frame (mid-record, mid-separator, multi-record chunk, unknown-event drop, trailing-partial discard, multi-byte codepoint split) and transcript storage (quota retry-drop-oldest terminates, 50-turn soft cap, corrupt/non-array JSON load) | 2026-07-28 | offline harness |
| Live browser round | streaming, book/part/chapter lists, all endpoints, llama-server-down 503 — all pass | 2026-07-28 | manual, full stack |
| Production build + boot | `frontend/dist/` 340 KB, all three API endpoints answer, mount-last ordering holds | P9 | `npm run build` + uvicorn |

**The two review findings worth carrying forward** `[reasoned]`:
- **P4 was reviewed before it was ever run, and that was the right order.** Two of the six defects
  (D1 substring book match, D2 untyped part/chapter) fail **silently** — they return plausible
  results rather than raising. Running the eval first would have produced a `recall@5` that looked
  like a tuning problem. This stack's characteristic failure is *degraded quality*, not an exception,
  so static review has leverage that execution does not.
- **A green majority proves nothing.** Three of five frontend test files passed while the
  `localStorage` shim was a silent no-op, because they never touch storage; and all 19 passed through
  the reactivity defect ([E21](#E21)), because they assert values while the defect was purely a
  render notification.

**Not tracked in git:** the `test/` regression suite is covered by `.gitignore:21`, so the P4 D1–D5
and names regression tests exist only on disk. The fixes are committed; the tests that prove them are
not. Same exposure as `data/` and `plans/`. Recorded as a deliberate open decision, not an accident.
`[read from source]`

---

<a id="E30"></a>
## E30 — What v1 never measured

An unstated absence reads as a pass. These are the absences.

**Fine-tune**
- **No training wall-clock, step count, or final/eval loss.** `SFTTrainer` logs every 100 steps; no
  log survives in the repo.
- **No held-out evaluation of the fine-tune itself.** No perplexity, no style classifier, no
  before/after comparison against the base Mistral. The claim "it writes like Dostoevsky" was never
  quantified against the base model.
- **No comparison across the three quantization levels.** `q4_k_m` was chosen by VRAM arithmetic
  ([E1](#E1)); `q5_k_m` and `q8_0` were built and never evaluated.
- **No count of the OCR'd chunks** the 6,979 pairs were generated from, and no OCR error rate.
- **No record of the teacher model used for the training data**, beyond a prose reference to
  `gemini-2.5-flash` in the post-hoc diagnosis.

**Retrieval and index**
- **No embedder bake-off.** `nomic-embed-text-v1.5` was chosen by reasoning (params, input cap,
  asymmetric prefixes, CPU-quantized ONNX), never against `bge-small` or `e5` on this corpus. The
  only bake-off v1 ran was for the **teacher** model ([E13](#E13)).
- **No reranker bake-off.** FlashRank was chosen for its dependency profile, not on measured
  ranking quality against `bge-reranker` or `mxbai`.
- **No chunk-size or overlap sweep.** 500/100 was never varied — it requires a full re-ingest.
- **No `PREFETCH_LIMIT` / `TOP_K_FUSED` sweep.** Explicitly out of scope of the ablation (tuning, not
  attribution).
- **No entity-map coverage number.** `docs/CHARACTERS.md` was written as human ground truth for
  under-extraction, and the comparison was never computed.
- **No measurement of the summary path.** The STRUCTURAL route has 2 routing rows in the eval set and
  **zero** rows measuring summary answer quality.

**Evaluation instrument**
- **The eval set was never grown**, despite three separate notes saying it should be. 27 rows, ~25
  effective, 2 cross-translation rows. Every delta the ablation tried to read is at or below its
  resolution ([E27](#E27)).
- **No inter-annotator or judge-calibration check.** The LLM judge's scores were never validated
  against human scores on any subset.
- **No confidence intervals anywhere.** No metric in v1 carries one. That absence is the direct cause
  of [E27](#E27).
- **No cold cache-miss end-to-end latency** ([E28](#E28)).
- **No concurrency or load measurement.** `Semaphore(1)` makes it a queue by construction; queue
  depth and wait time under multiple submitters were never measured.

**Frontend and operations**
- **The P10 fixes were never re-run.** `npm run test` / `npm run build` were not executed against
  commit `c4d7127`; 20/20 is an expectation, not a result ([E21](#E21)).
- **The §VII.2 acceptance checklist was never completed** — items 1 and 2 recorded FAIL with
  evidence, items 3–13 untouched.
- **No accessibility audit**, no Lighthouse or bundle-performance measurement beyond the 340 KB
  build size.
- **No measurement of cache hit rate in use.** A near-zero rate was predicted and accepted at design
  time; it was never observed.
- **`entities.json` is not versioned** (`data/` is gitignored). The 625→256 curation exists on disk
  with two `.bak` files as its entire history; `--remerge` rebuilds only the *machine* artifact and
  cannot replay the human gate.

**Out of scope for v1 by decision, not omission** (`docs/RAG.md` §VIII): semantic cache; conversation
memory / `interpretive_bias`; interpretive-restraint hedging metric; any ML query classification;
Qdrant server deployment; multi-user concurrency; authentication and tenancy.
