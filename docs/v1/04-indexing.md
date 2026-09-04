# 04 — Indexing

**What this document answers:** how 231 parsed chapters ([E5](EVIDENCE.md#E5)) became a searchable
Qdrant collection — how text was cut, how it was embedded on both a dense and a sparse axis, and
which of those choices were forced by the 6GB budget rather than chosen freely.

`src/rag/ingest.py`, driven by `scripts/ingest_corpus.py`. Every parameter is a `Config` literal;
nothing here is tuned at request time.

---

## 1. Chunking

**500 tokens with 100 of overlap** ([E6](EVIDENCE.md#E6)). The size is set by what has to fit
downstream, not by a retrieval-quality argument: three chunks are the reranked context budget
([E15](EVIDENCE.md#E15)), and three 500-token chunks land inside the served model's context window
with room for the persona head and the answer. Larger chunks would have meant fewer of them in
context and coarser attribution in the Sources list; smaller ones would have split scenes that only
make sense whole. The overlap exists so a passage straddling a window boundary is still wholly
present in one of the two windows.

**Counted with the embedder's own tokenizer, not a generic one.** `chunk_units()` windows over
`Encoding.offsets` from the real `tokenizers.Tokenizer` that fastembed loaded for this model, reached
at `embedder.model.tokenizer` ([E6](EVIDENCE.md#E6)). This is the difference between a chunk that
fits the encoder and one that does not: a "500-token" chunk measured with a different vocabulary
either overflows the encoder — which then silently truncates, so the tail of the chunk is indexed as
text but never embedded — or wastes the window. Neither failure raises. The accessor is not a
documented fastembed API; it was read out of the installed 0.8.0 source rather than guessed.

`add_special_tokens=False` is load-bearing for the same reason. `[CLS]`/`[SEP]` carry `(0, 0)`
offsets, and a windowing scheme that maps token indices back to character spans would slice from
character zero on every window that touched them. They are re-added transparently at embed time, so
omitting them here changes the arithmetic and not what the model sees.

**Chunks never cross a chapter boundary.** A `Unit` *is* one chapter, and the window resets per
unit. This is a structural decision, not a quality heuristic: every chunk carries a
`{book, part, chapter}` payload, and that payload is what makes hard filters, the STRUCTURAL route
and the Sources list correct ([E8](EVIDENCE.md#E8)). A chunk spanning two chapters would have no
truthful label to carry, and the cost of the rule — a short tail chunk at the end of each chapter —
is paid once at build time.

Point ids are a single deterministic counter over (book order × unit order × window order), so a
re-ingest overwrites rather than duplicates.

**Never ablated.** 500/100 was picked by reasoning and never varied ([E30](EVIDENCE.md#E30)); a
sweep costs a full re-ingest per cell. The honest status is "plausible and unmeasured", not "tuned".

---

## 2. The dense vector

**`nomic-ai/nomic-embed-text-v1.5`, 768 dimensions, cosine** ([E7](EVIDENCE.md#E7)). The reasons it
was chosen, in the order they mattered:

- **It runs on CPU as quantized ONNX.** The GPU is fully committed to the 7B model
  ([E1](EVIDENCE.md#E1)); an embedder that wants CUDA is not a candidate at any quality.
- **Its input cap is far above the chunk size**, so a 500-token chunk is never near truncation.
- **It has an asymmetric task-prefix scheme**, which matches how the index is actually used —
  passages and queries are different objects and are encoded as such.

**This was reasoning, not a bakeoff.** No embedder comparison against `bge-small`, `e5`, or anything
else was ever run on this corpus ([E30](EVIDENCE.md#E30)). The only competitive selection v1 ran was
for the teacher model ([E13](EVIDENCE.md#E13)). The choice is defensible on constraints and
undefended on quality, and should be stated that way.

**fastembed rather than sentence-transformers.** sentence-transformers pulls torch into the serving
process for a model that never needs it, and `nomic-embed-text-v1.5` on the transformers path wants
`trust_remote_code`. fastembed gives a quantized ONNX runtime, no torch import, no remote code
execution at load. On a machine whose entire design constraint is "what else is resident", not
importing torch to embed a sentence is the whole argument.

**Matryoshka truncation was available and not used** — the vectors are full 768-dim. At this
collection size the memory saving is not worth the recall risk ([E7](EVIDENCE.md#E7)).

### 2.1 The prefixes, and what silently breaks

Documents are embedded with `"search_document: "` prepended; queries with `"search_query: "`
([E7](EVIDENCE.md#E7)). Both include the trailing space, and both are applied by *this* code.

fastembed does not insert them. That was established by reading the installed package —
`TextEmbeddingBase.query_embed`/`passage_embed` are pass-throughs, `PooledEmbedding` overrides
neither, and a grep for the prefix strings across the package returns nothing. The API *looks* like
it handles this and does not.

The failure mode is the reason this gets a section. A missing or mismatched prefix does not raise,
does not warn, and does not empty the result set. It returns a full, plausible, uniformly worse
ranking — the query is embedded into a different region of the space than the documents were. This is
the pipeline's characteristic failure: degraded quality, not an exception. Its tripwire is the
retrieval eval, where a *uniform* recall collapse across every query is the signature of a prefix
mismatch rather than of bad ranking ([E23](EVIDENCE.md#E23)).

---

## 3. The sparse vector and the IDF modifier

The sparse leg is fastembed's `Qdrant/bm25` ([E9](EVIDENCE.md#E9)). It exists because dense
similarity is bad at exactly what this corpus is queried with: proper names, rare spellings, and
near-quotations. See `06-retrieval.md` for what fusion does with it.

**The collection must declare `Modifier.IDF` on the sparse vector, or it is not BM25.** fastembed
emits **term-frequency weights only** on the document side; the inverse-document-frequency half is
computed server-side by Qdrant, and only if the collection was created with the modifier set
([E9](EVIDENCE.md#E9)). Omit it and the collection still builds, still indexes, still returns
ranked results — scored by raw term frequency, with no corpus-level statistics anywhere in the
scoring. It is a one-line config value that changes which algorithm is running, with no error on
either side of the mistake.

The two sides are deliberately asymmetric for the same reason: ingest calls `.embed()` (raw TF),
query time calls `.query_embed()` (flat weight per token). Reading that as a bug and "fixing" it to
match would break the scoring.

**Why not SPLADE or another learned sparse encoder.** A neural sparse encoder is a second GPU
resident, and the 6GB is already fully allocated to the served model ([E1](EVIDENCE.md#E1),
[E9](EVIDENCE.md#E9)). This is the constraint doing the deciding, not a judgment that BM25 is
better.

---

## 4. The Qdrant collection

One collection, `dostoevsky`, with two named vectors — `dense` (768, cosine) and `bm25` (sparse) —
over **3,284 points** ([E8](EVIDENCE.md#E8)). Each point's payload carries
`{book, part, chapter, canonical_entities, text}`. That payload is the contract with everything
downstream: hard filters read `book`/`part`/`chapter`, context assembly reads `text`, and the Sources
list is built from the same three fields, so a citation shown to a user is the same object retrieval
matched on.

`canonical_entities` is populated but not used in the request path — entity information reaches
retrieval as query expansion instead (see `05-offline-artifacts.md`). It is kept for evaluation and
for a future client-side boost.

**Local mode, not a Qdrant server.** `QdrantClient(path=…)` against `data/vector_db/`
([E8](EVIDENCE.md#E8)). For a single user over a static corpus of a few thousand points, a server
container is operational surface with nothing to show for it: no second machine, no concurrent
writer, no horizontal scaling story that this deployment will ever need.

The cost is real and is accepted rather than hidden: **local mode holds an exclusive process lock on
the directory**. The API server and any ingest or eval script cannot run at the same time. That
constraint is written into the ingest script's own docstring, because it is the thing that will
confuse the next person, and it is documented as an operational rule rather than engineered around.

**Ingest verifies itself.** After upsert, `scripts/ingest_corpus.py` compares Qdrant's `count()` to
the chunk count it logged and warns on a mismatch — a tripwire for a partially-failed batch, which
otherwise presents as slightly worse retrieval forever ([E8](EVIDENCE.md#E8),
[E29](EVIDENCE.md#E29)). The same point count was re-confirmed later at FastAPI startup, which is
where a stale or half-built index would surface in normal use.

---

## 5. What this stage was never measured on

Stated plainly, because an unstated absence reads as a pass — all of these are in
[E30](EVIDENCE.md#E30):

- No embedder bakeoff. nomic was chosen on constraints.
- No chunk-size or overlap sweep. 500/100 was never varied.
- No `PREFETCH_LIMIT` / `TOP_K_FUSED` sweep; that was ruled out of the ablation's scope as tuning
  rather than attribution (`11-ablation.md`).

What *was* measured about this stage is indirect: the retrieval eval ([E23](EVIDENCE.md#E23))
scores the index end to end, which is enough to catch a broken prefix pairing or a missing IDF
modifier, and not enough to say any of these parameters is well chosen.
