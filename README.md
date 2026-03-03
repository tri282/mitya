# Mitya

```
mitya/
├── .env
├── requirements.txt
├── data/
│   ├── raw/                   # raw pdfs
│   ├── processed/             # generated csvs
│   └── vector_db/             # rag embeddings
├── src
│   ├── __init__.py
│   ├── config.py              # config loader
│   ├── ingestion.py           # pdf -> text
│   ├── generation.py          # text -> prompt
│   └── rag.py                 # vector store & retrieval
├── scripts/
│   ├── build_dataset.py       # create dataset
│   └── push_model.py          # push to Ollama
└── app.py                     # ui
```

# Library Metadata Enhanced Rag

## Ingestion pipeline

1. Cleaning & Standardization
* Regex clean headers/footers/page numbers

2. Chunking
* Vector chunks: 500 tokens, 150 overlap (for search)
* Context chunks: entire chapter (for summary generation)

3.  Generate metadata
For each chunk:
```json
{
    "book": "The Brothers Karamazov",
    "part": 1,
    "chapter": 12,
    "canonical_entities": ["Rodion Romanovich Raskolnikov", "Sofya Semyonovna Marmeladov"]
}
```

Store:
* Text embedding
* Metadata embedding
* Raw metadata fields

## Idea

1. Meta-data filtered retrieval

* Hard Filter
    * If user specifices `Book`, `Chapter`, and `Part` via the UI, we create a hard filter to reduce retrieval time

* Payload Boosting
    * Create a json entity map that maps names to `canonical_entities`
    * Apply a `should` condition that adds to the score to match `canonical_entities` in the metadata with the JSON mapping

2. Hybrid Search
* We search by:
    * **Dense Vector (semantic)**: captures quotes, scenes
    * **Sparse Vector (BM25/SPLADE)**: captures exact keywords

3. Global Context Injector
* RAG fails at "summarize this" questions since it only retrive small chunks
* **Fix**: Pre-generate summaries of every chapter and store in a JSON dictionary
* **Logic**: Classify the query by type and dynamically choose to do a DB search or context injection

3. Reranking (cross-encoder)

4. Top-k chunks $\rightarrow$ generation

5. Generation
* System prompt: biased towards evidence traceability
* Citation: strict `[Book I, Chapter 2]` format required

## Flow

**Input:** *"What does it mean to be guilty for everything"*

1. Query Classification
* Classify if query is `SPECIFIC` (requires quote/scenes), `THEMATIC` (requires analysis), or `STRUCTURAL` (requires summary)

2. Keyword Extraction

3. Hybrid Retrieval
* Calculate score dynamically based on query type
    * `STRUCTURAL`: get summary context via JSON dictionary + Some key exercepts per character
    * `SPECIFIC`: $\text{Score} = 0.3 \cdot \text{Dense} + 0.7 \cdot \text{Sparse}$
    * `THEMATIC`: $\text{Score} = 0.8 \cdot \text{Dense} + 0.2 \cdot \text{Sparse}$

4. Reranking
* Take top 25 chunks
* Pass through Cross-Encoder
* Keep top 3

5. Prompt Construction

## Tech Stack

* **LLM**
    * Fine-tune 7B-15B model
    * 4-bit quantized

* **Inference Engine**
    * `llama.cpp`

* **Orchestration**
    * Python `FastAPI` + `LiteLLM`

* **Embeddings**
    * Dense: `nomic-embed-text-v1.5`
    * Sparse: `BM25`/`SPLADE`

* **Vector Store**
    * Qdrant

* **Reranking**
    * `mixedbreak-ai/mxbai-rerank-base-v1`
    * `cross-encoder/ms-marco-MiniLM-L-6-v2`

## Conversation Memory

* Stored per session:
```json
{
    "user_interests": ["The Underground Man", "Myshkin's innocence"],
    "interpretive_bias": "skeptical"
}
```

* Inject context into system prompt

## Caching

* Cache `SPECIFIC` and `STRUCTURAL` query
* Do a search on past queries with cosine similary and return answer if match immediately
* Use `diskcache`

## Evaluation

1. Canon Fildelity Evaluation
Does this answer relevant to the text?

**Method: Evidence Traceability**
* For every evaluated answer, require:
    * $\geq$ 1 cited chunks for each major claim
    * chunk ids logged alongside answer

2. Latency Budget
* Target: Time to First Token (TTFT) < 1.5 seconds
* Total Generation: < 10 seconds
* Constraint: If Reranking takes > 3 seconds, disable and Search only

3. Interpretive Restraint Score
Does the model avoid over-explain or closing ambiguity

**Heuristic Evaluation**
* Score answers on
    * Presence of hedging language ("suggests", "may")
    * Absence of absolute claims unless text is explicit
    * Use of uncertainty when `certainty_level < 0.5`
```python
score_restraint =
  (hedging_tokens / total_tokens)
  * uncertainty_alignment
```

4. Evaluation Dataset

**Three datasets (30-50 each)**
1. Why/Motivation
2. Belief & Philosophy
3. Ambiguous Moral Questions
