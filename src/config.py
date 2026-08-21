# src/config.py

import logging
import os
from dotenv import load_dotenv

from src.rag.names import normalize as _normalize_title

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
    # RERANK_MODEL verified at P4 impl against installed FlashRank 0.2.10
    # (.venv/.../flashrank/Config.py model_file_map) -- the placeholder id was
    # already correct, no change needed.
    EMBED_MODEL   = os.getenv("EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")  # confirmed in fastembed 0.8.0 registry (P2)
    RERANK_MODEL  = os.getenv("RERANK_MODEL", "ms-marco-MiniLM-L-12-v2")
    QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "dostoevsky")
    PREFETCH_LIMIT = int(os.getenv("PREFETCH_LIMIT", "50"))
    TOP_K_FUSED    = int(os.getenv("TOP_K_FUSED", "25"))
    TOP_K_RERANK   = int(os.getenv("TOP_K_RERANK", "3"))
    # design III.3 STRUCTURAL path: chapter summary + top-2 dense excerpts.
    # Added at P4 impl alongside retrieve.retrieve_structural_excerpts() so the
    # "2" isn't a hardcoded magic number -- follows the existing TOP_K_* pattern.
    TOP_K_STRUCTURAL_EXCERPTS = int(os.getenv("TOP_K_STRUCTURAL_EXCERPTS", "2"))
    RERANK_ENABLED = os.getenv("RERANK_ENABLED", "true").lower() == "true"
    CHUNK_TOKENS   = int(os.getenv("CHUNK_TOKENS", "500"))
    CHUNK_OVERLAP  = int(os.getenv("CHUNK_OVERLAP", "100"))

    # --- RAG: book title aliases (P4, authored 2026-07-19 -- not a prior
    # design decision, see plans/mitya-rag/05-impl/phase-4.md) ---
    # Users say "Demons"/"The Devils"; entities.json, Qdrant payloads, and
    # structure.json all carry the Garnett-edition title "The Possessed".
    # A wrong title becomes a hard filter matching zero points -- a silent
    # empty result, worse than a miss -- so unmapped titles must be rejected
    # loudly (see resolve_book_title), never passed through. Flag for review:
    # this list is not guaranteed exhaustive of every title readers might use.
    BOOK_TITLE_ALIASES: dict[str, str] = {
        "crime and punishment": "Crime and Punishment",
        "the brothers karamazov": "The Brothers Karamazov",
        "brothers karamazov": "The Brothers Karamazov",
        "the karamazov brothers": "The Brothers Karamazov",
        "the idiot": "The Idiot",
        "idiot": "The Idiot",
        "the possessed": "The Possessed",
        "possessed": "The Possessed",
        "demons": "The Possessed",
        "the demons": "The Possessed",
        "the devils": "The Possessed",
        "devils": "The Possessed",
        "notes from the underground": "Notes from the Underground",
        "notes from underground": "Notes from the Underground",
        "notes from the underground man": "Notes from the Underground",
    }

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

    # --- Gemini free-tier quota table -------------------------------------
    # Per-model free-tier limits (rpm, tpm, rpd). They are NOT derivable from
    # the model id and they differ by an order of magnitude between tiers, so
    # they live in one table instead of being copied into per-consumer env
    # vars. Source: the AI Studio dashboard rows, supplied 2026-08-21.
    #
    # The tiers are the whole story for planning a measurement run:
    #   * flash        -- stronger, rpd 20. Twenty calls a DAY. Fine for a
    #                     judgement call on a handful of rows, useless for a
    #                     batch. A 144-call run takes 8 days at this tier.
    #   * flash-lite   -- weaker, rpd 500. The only tier that can carry an
    #                     eval batch, and what every v1 number was measured on.
    #
    # `verified` records whether THIS repo has seen the id resolve on a live
    # call (test/bakeoff/RESULTS.md), not whether the dashboard lists it -- the
    # bake-off's whole finding was that the dashboard lists models that 404
    # ("2.5 Flash: no longer available to new users"). Unverified ids are
    # usable but unproven; confirm with a one-row smoke run before a batch.
    MODEL_LIMITS = {
        # id                              rpm     tpm     rpd   verified
        "models/gemini-3.7-flash":       {"rpm": 5,  "tpm": 250_000, "rpd": 20,  "verified": False},
        "models/gemini-3.6-flash":       {"rpm": 5,  "tpm": 250_000, "rpd": 20,  "verified": False},
        "models/gemini-3.5-flash":       {"rpm": 5,  "tpm": 250_000, "rpd": 20,  "verified": True},
        "models/gemini-3-flash-preview": {"rpm": 5,  "tpm": 250_000, "rpd": 20,  "verified": True},
        "models/gemini-3.5-flash-lite":  {"rpm": 15, "tpm": 250_000, "rpd": 500, "verified": False},
        "models/gemini-3.1-flash-lite":  {"rpm": 15, "tpm": 250_000, "rpd": 500, "verified": True},
    }

    # Unknown id -> the STRICTER tier, never the looser one. Guessing high
    # burns real quota and turns a batch into a 429 storm partway through;
    # guessing low only makes it slow. A warning is logged so the id gets
    # added to the table rather than silently running at a guess forever.
    UNKNOWN_MODEL_LIMITS = {"rpm": 5, "tpm": 250_000, "rpd": 20, "verified": False}

    @classmethod
    def limits_for(cls, model: str) -> dict:
        """Free-tier limits for a model id, falling back to the strict tier."""
        limits = cls.MODEL_LIMITS.get(model)
        if limits is None:
            logging.getLogger(__name__).warning(
                "no quota row for model %r -- assuming the strict tier (%d rpm / %d rpd). "
                "Add it to Config.MODEL_LIMITS once its dashboard row is known.",
                model, cls.UNKNOWN_MODEL_LIMITS["rpm"], cls.UNKNOWN_MODEL_LIMITS["rpd"],
            )
            return cls.UNKNOWN_MODEL_LIMITS
        return limits

    # --- RAG: LLM judge (P7 answer-quality eval, scripts/eval_quality.py) ---
    # Reuses TeacherClient (src/rag/teacher.py), but NOT the teacher's quota:
    # before 2026-08-21 the judge could be pointed at a different model while
    # the RateLimiter still read TEACHER_RPM/TPM/RPD, so any judge/teacher
    # model split silently ran against the wrong quota (too slow at best, a
    # 429 storm at worst). Judge limits now derive from JUDGE_MODEL's own row.
    JUDGE_MODEL = os.getenv("JUDGE_MODEL", TEACHER_MODEL)
    JUDGE_RPM = int(os.getenv("JUDGE_RPM", "0")) or None
    JUDGE_TPM = int(os.getenv("JUDGE_TPM", "0")) or None
    JUDGE_RPD = int(os.getenv("JUDGE_RPD", "0")) or None

    @classmethod
    def judge_limits(cls) -> dict:
        """JUDGE_MODEL's quota, with explicit env overrides winning. Overrides
        exist because a paid key or a raised limit is not in the table."""
        base = cls.limits_for(cls.JUDGE_MODEL)
        return {
            "rpm": cls.JUDGE_RPM or base["rpm"],
            "tpm": cls.JUDGE_TPM or base["tpm"],
            "rpd": cls.JUDGE_RPD or base["rpd"],
        }

    # --- RAG: serving ---
    LLAMA_SERVER_URL = os.getenv("LLAMA_SERVER_URL", "http://127.0.0.1:8080")
    CTX_SIZE   = int(os.getenv("CTX_SIZE", "4096"))
    MAX_TOKENS = int(os.getenv("MAX_TOKENS", "400"))
    # Sampling. A modest temperature (vs llama-server's ~0.8 default) cuts the
    # rate at which the weak q4 7B samples EOS as its first token and returns
    # an empty completion; the zero-token retry in generate.stream_answer is
    # the belt to this suspenders. EMPTY_RETRIES re-draws (new random seed) up
    # to N times when an attempt yields no tokens.
    GEN_TEMPERATURE = float(os.getenv("GEN_TEMPERATURE", "0.6"))
    GEN_TOP_P = float(os.getenv("GEN_TOP_P", "0.9"))
    EMPTY_RETRIES = int(os.getenv("EMPTY_RETRIES", "1"))
    CACHE_ENABLED = os.getenv("CACHE_ENABLED", "true").lower() == "true"
    CACHE_DIR = os.path.join(DATA_DIR, "cache")

    # --- RAG: prompt composition A/B (P7, ruling R19) ---
    # A (design default): context in the system prompt, framed as the
    # author's own recollections. B: context in the user turn instead. Both
    # are implemented in src/rag/generate.py; this flag only selects which
    # build_messages() branch P7's A/B runner (and, later, the API) uses.
    # Not read by src/api/app.py yet -- P6 hardcodes composition A by calling
    # generate.build_messages() with no mode argument (its default), see
    # plans/mitya-rag/05-impl/phase-7.md for why that stays true until the
    # A/B run picks a winner.
    PROMPT_COMPOSITION = os.getenv("PROMPT_COMPOSITION", "A")

    _NORMALIZED_BOOK_ALIASES: dict[str, str] | None = None  # lazy-built cache, see resolve_book_title

    @classmethod
    def resolve_book_title(cls, title: str) -> str:
        """Map a user-supplied book title to the canonical title used in
        entities.json / Qdrant payloads / structure.json.

        Unmapped titles are rejected loudly (raise ValueError) rather than
        passed through -- an unmapped title silently becomes a Qdrant hard
        filter matching zero points, which is worse than a miss.

        Both sides of the lookup go through `names.normalize()` (D5 fix):
        plain `.strip().lower()` rejects anything with a diacritic
        ("Karamázov"), a curly apostrophe, or doubled internal whitespace as
        a hard 400 for a cosmetically different title. `normalize()` already
        does casefold + NFKD diacritic strip + separator collapse for
        exactly this reason (see src/rag/names.py); reused here rather than
        re-implemented. `src/rag/names.py` has no imports from this package,
        so importing it here does not create an import cycle.
        """
        if cls._NORMALIZED_BOOK_ALIASES is None:
            cls._NORMALIZED_BOOK_ALIASES = {
                _normalize_title(alias): canonical
                for alias, canonical in cls.BOOK_TITLE_ALIASES.items()
            }
        key = _normalize_title(title)
        canonical = cls._NORMALIZED_BOOK_ALIASES.get(key)
        if canonical is None:
            raise ValueError(f"unrecognized book title: {title!r}")
        return canonical

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
