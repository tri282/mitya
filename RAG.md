# Library Metadata Enhanced RAG

## Overview
A local-first, privacy-focused RAG architecture designed for high-fidelity literary analysis. This system prioritizes structural awareness (book/chapter hierarchy), thematic interpretation, and strict evidence traceability using a lightweight stack optimized for consumer hardware.

## System Architecture

### 1. Ingestion Pipeline
**A Cleaning & Standardization**
* Regex normalization of headers, footers, and page numbers.
* Standardization of character names to canonical forms prior to indexing.

**B. Dual-Path Chunking**
* **Vector Chunks:** 500 tokens, 150 token overlap (Optimized for semantic search).
* **Context Chunks:** Entire chapter text (Stored specifically for summary generation).

**C. Metadata Extraction**
For each vector chunk, the following metadata is generated and stored:
```json
{
    "book": "The Brothers Karamazov",
    "part": 1,
    "chapter": 12,
    "canonical_entities": ["Rodion Romanovich Raskolnikov", "Sofya Semyonovna Marmeladov"]
}
```

### 2. Retrieval Strategy
The system utilizes a **Query Router** to classify user intent (`SPECIFIC`, `THEMATIC`, or `STRUCTURAL`) and selects the appropriate retrieval path.

**A. Retrieval Logic**
* **Hard Filters:** Utilized when the user explicitly scopes `Book`, `Part`, and/or `Chapter` via the UI.
* **Payload Boosting:** Uses a JSON entity map to resolve user input (e.g., "Sonya") to `canonical_entities`.
    * Applies a `should condition` to boost scores for chunks containing the resolved entity.

**B. Global Context Injection (Structural Queries)**
Addresses the limitation of RAG in summarization tasks.

* **Mechanism:** Pre-generated summaries for every chapter are stored in a key-value store.
* **Execution:** If the router detects a `STRUCTURAL` query, the system bypasses vector search and injects the pre-written summary combined with top-2 density-retrieved excerpts.

**C. Hybrid Search Scoring**
Combines semantic understanding with keyword precision.
* **Dense Vector:** Captures semantic meaning (scenes, quotes).
* **Sparse Vector:** Captures exact keywords (BM25).
* **Dynamic Scoring Weights:**
| Query Type | Dense Weight | Sparse Weight | Focus                      |
| SPECIFIC   | 0.3          | 0.7           | Exact quotes, names, facts | 
| THEMATIC   | 0.8          | 0.2           | Concepts, analysis, vibes  |

### 3. Reranking & Optimization
* **Selection:** Top 25 chunks are retrieved via Hybrid Search.
* **Refinement:** A lightweight quantized cross-encoder re-scores the candidates.
* **Final Context:** Top 3 chunks are passed to the generator.

## Technical Stack
Designed for consumer hardware (4-12GB VRAM, 32GB RAM).

* **Inference Engine:** `llama.cpp` (Server mode)
* **LLM:** Mistral-7B Parameter Model (GGUF Format, 4-bit Quantization).
* **Orchestration:** Python `FastAPI` + `LiteLLM` abstraction layer.
* **Vector Database:** `Qdrant` (Local Mode).
    * *Note: Utilizes Qdrant's native BM25 for sparse retrieval to avoid GPU overhead from SPLADE.*
* **Embeddings:** `nomic-embed-text-v1.5` (Matryoshka representation enabled for memory efficiency).
* **Reranking:** `FlashRank` (Quantized `ms-marco-MiniLM-L-6-v2`).

## Memory & Caching

**Conversation Memory**
Stored per session to guide system prompts rather than biasing retrieval weights.
```json
{
    "user_interests": ["The Underground Man", "Myshkin's innocence"],
    "interpretive_bias": "skeptical"
}
```

**Caching Strategy**

Implemented via diskcache (SQLite-backed) to persist across restarts without external services.

* **A. Router Check:** Determine query type.
* **B. Semantic Cache:** Applied only to `SPECIFIC` and `STRUCTURAL` queries.
    * Checks past query embeddings for cosine similarity (>0.95).
    * Returns cached response immediately on hit.
* **C.Thematic Bypass:** `THEMATIC` queries always trigger a fresh generation to ensure context sensitivity.

## Evaluation & Guardrails

### 1. Canon Fidelity (Evidence Traceability)
Ensures answers are grounded in the text.
* **Metric:** Unsupported Claim Rate (UCR).
* **Requirement:** $\geq$ 1 cited chunk (`[Book I, Chapter 2]`) for every major claim.

### 2. Interpretive Restraint
Prevents hallucinated certainty on ambiguous literary topics.
* **Heuristic:** $$ \text{Score} = \left( \frac{\text{hedging_tokens}}{\text{total_tokens}} \right) \times \text{uncertainty_alignment} $$

### 3. Latency Budget
* **TTFT (Time to First Token):** < 1.5 seconds.
* **Total Generation:** < 10 seconds.
* **Fallback:** If reranking exceeds 2000ms, the system falls back to pure Hybrid Search scores.

### 4. Test Datasets
Three distinct sets (30-50 samples each) for validation:
* **Motivation:** "Why did X do Y?"
* **Philosophy:** "What does the author imply about faith?"
* **Ambiguity:** Moral questions without clear textual answers.
