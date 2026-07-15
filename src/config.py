# src/config.py

import logging
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    TESSERACT_PATH = os.getenv("TESSERACT_PATH", "/usr/bin/tesseract")
    # gemini-1.5-flash (the previous default) no longer exists in the API listing.
    # Only used by src/generation.py; the RAG side has its own TEACHER_MODEL.
    MODEL_NAME = os.getenv("MODEL_NAME", "models/gemini-3.1-flash-lite")
    LLM_BACKEND = os.getenv("LLM_BACKEND", "gemini") # 'gemini' or 'ollama'

    # paths
    DATA_DIR = os.path.join(os.getcwd(), "data")
    RAW_DATA_DIR = os.path.join(DATA_DIR, "raw")
    PROCESSED_DATA_DIR = os.path.join(DATA_DIR, "processed")

    MODEL_DIR = os.path.join(os.getcwd(), "models")
    LORA_DIR = os.path.join(MODEL_DIR, "lora")
    QUANTIZATIONS_DIR = os.path.join(MODEL_DIR, "quantizations")
    MERGE_DIR = os.path.join(MODEL_DIR, "merged")

    # --- RAG: paths ---
    RAG_DIR = os.path.join(DATA_DIR, "rag")
    VECTOR_DB_DIR = os.path.join(DATA_DIR, "vector_db")
    STRUCTURE_PATH = os.path.join(RAG_DIR, "structure.json")
    ENTITIES_PATH  = os.path.join(RAG_DIR, "entities.json")      # post-human-review (design II.4)
    ENTITIES_RAW_PATH = os.path.join(RAG_DIR, "entities.raw.json")  # machine output, safe to regenerate
    SUMMARIES_PATH = os.path.join(RAG_DIR, "summaries.json")

    # --- RAG: retrieval ---
    EMBED_MODEL   = os.getenv("EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")  # confirmed in fastembed 0.8.0 registry (P2)
    RERANK_MODEL  = os.getenv("RERANK_MODEL", "ms-marco-MiniLM-L-12-v2")        # placeholder, verify in P4
    QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "dostoevsky")
    PREFETCH_LIMIT = int(os.getenv("PREFETCH_LIMIT", "50"))
    TOP_K_FUSED    = int(os.getenv("TOP_K_FUSED", "25"))
    TOP_K_RERANK   = int(os.getenv("TOP_K_RERANK", "3"))
    RERANK_ENABLED = os.getenv("RERANK_ENABLED", "true").lower() == "true"
    CHUNK_TOKENS   = int(os.getenv("CHUNK_TOKENS", "500"))
    CHUNK_OVERLAP  = int(os.getenv("CHUNK_OVERLAP", "100"))

    # --- RAG: teacher LLM (offline artifact builds, phase 3) ---
    # Free-tier quotas differ wildly per model and the AI Studio dashboard lists
    # models that 404 on an actual call (2.5 Flash: "no longer available to new
    # users"). gemini-3.1-flash-lite was verified live and is the only free-tier
    # model with a daily quota large enough for a full corpus pass -- see
    # test/bakeoff/RESULTS.md. Limits below mirror its dashboard row; override in
    # .env if you switch models, they are not derivable from the model id.
    TEACHER_MODEL = os.getenv("TEACHER_MODEL", "models/gemini-3.1-flash-lite")
    TEACHER_RPM = int(os.getenv("TEACHER_RPM", "15"))
    TEACHER_TPM = int(os.getenv("TEACHER_TPM", "250000"))
    TEACHER_RPD = int(os.getenv("TEACHER_RPD", "500"))
    TEACHER_MAX_RETRIES = int(os.getenv("TEACHER_MAX_RETRIES", "3"))
    TEACHER_TIMEOUT_MS = int(os.getenv("TEACHER_TIMEOUT_MS", "120000"))
    TEACHER_MAX_OUTPUT_TOKENS = int(os.getenv("TEACHER_MAX_OUTPUT_TOKENS", "2048"))

    # --- RAG: serving ---
    LLAMA_SERVER_URL = os.getenv("LLAMA_SERVER_URL", "http://127.0.0.1:8080")
    CTX_SIZE   = int(os.getenv("CTX_SIZE", "4096"))
    MAX_TOKENS = int(os.getenv("MAX_TOKENS", "400"))
    CACHE_ENABLED = os.getenv("CACHE_ENABLED", "true").lower() == "true"
    CACHE_DIR = os.path.join(DATA_DIR, "cache")

    @classmethod
    def validate_rag(cls):
        log = logging.getLogger(__name__)
        required = {
            "structure.json": cls.STRUCTURE_PATH,
            "entities.json":  cls.ENTITIES_PATH,
            "summaries.json": cls.SUMMARIES_PATH,
            "vector_db":      cls.VECTOR_DB_DIR,
        }
        missing = [name for name, p in required.items() if not os.path.exists(p)]
        if missing:
            log.error("validate_rag: missing artifacts: %s", ", ".join(missing))
            raise FileNotFoundError(f"RAG artifacts missing: {missing}. "
                                    f"Run scripts/ingest_corpus.py + build_*.py first.")
        log.info("validate_rag: all artifacts present")

# validation
if not Config.GOOGLE_API_KEY and Config.LLM_BACKEND == 'gemini':
    print("WARNING: GOOGLE_API_KEY is missing in .env")
