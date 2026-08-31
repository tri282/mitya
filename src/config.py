# src/config.py

import logging
import os
from dotenv import load_dotenv

from src.config_artifact import active_serving_config
from src.rag.names import normalize as _normalize_title

load_dotenv()


class _ConfigMeta(type):
    """Facade onto the versioned serving config (v2 P1 step 5/7, D9).

    The ~18 versioned knobs (EMBED_MODEL, TOP_K_FUSED, ... see
    src/config_artifact.py's ServingConfig) are no longer class attributes on
    `Config` -- normal attribute lookup misses, and this metaclass's
    `__getattr__` (which only runs on a miss) resolves the name through the
    active artifact instead. `Config.TOP_K_FUSED` therefore still works, and
    all ~200 existing call sites across 36 modules are unmodified.

    Honest costs, paid deliberately: type checkers cannot see through
    `__getattr__`, so these ~18 knobs lose static checking (a typo in a knob
    name fails at runtime, not at lint time); and this is action-at-a-distance
    -- reading `Config.X` no longer shows at the read site whether `X` is an
    ordinary class attribute or resolves through a versioned artifact loaded
    from disk. Both are accepted in exchange for not touching 36 modules.
    The upgrade path, if a caller ever needs two configs live in one process
    (P2 A/B routing) rather than one process-wide active version, is an
    explicit `cfg` parameter threaded through -- by then `ServingConfig` is
    already a value object, so it is a mechanical change, not a redesign.
    """

    def __getattr__(cls, name):  # only runs when normal lookup FAILS
        cfg = active_serving_config()
        try:
            return getattr(cfg, name.lower())
        except AttributeError:
            raise AttributeError(
                f"{name} is not a Config field or a serving knob"
            ) from None


class Config(metaclass=_ConfigMeta):
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    # Additional provider keys (charter D1 wants >= 2 families for v2 serving).
    # Read from the environment only -- never logged, never defaulted, and no
    # client speaks to them yet; see MODEL_LIMITS' "NOT YET WIRED" block.
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
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
    # EMBED_MODEL, RERANK_MODEL, RERANK_ENABLED, PREFETCH_LIMIT, TOP_K_FUSED,
    # TOP_K_RERANK, TOP_K_STRUCTURAL_EXCERPTS moved to the versioned serving
    # config (v2 P1 step 5/7, D9) -- they change what a correct answer is, so
    # they are now ServingConfig fields in src/config_artifact.py, resolved
    # here through the _ConfigMeta facade above.
    QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "dostoevsky")
    CHUNK_TOKENS   = int(os.getenv("CHUNK_TOKENS", "500"))
    CHUNK_OVERLAP  = int(os.getenv("CHUNK_OVERLAP", "100"))

    # BOOK_TITLE_ALIASES moved to the versioned serving config for the same
    # reason (see src/config_artifact.py's _DEFAULT_BOOK_TITLE_ALIASES for the
    # full comment on why it is answer-affecting) -- resolved through the
    # facade above.

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
    # MODEL_LIMITS, UNKNOWN_MODEL_LIMITS, NO_TPM_LIMIT moved to the versioned
    # serving config (v2 P1 step 5/7, D9): a quota or price change should be a
    # visible config version, not an invisible edit. See
    # src/config_artifact.py's _DEFAULT_MODEL_LIMITS for the full table and
    # its comments (tier meaning, `verified`, "NOT YET WIRED" rows). Resolved
    # here through the _ConfigMeta facade above.

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

    # --- Cost attribution (v2 P1 step 3, charter G5) --------------------
    # MODEL_RATES moved to the versioned serving config for the same reason
    # as MODEL_LIMITS: a price change should be a visible config version. See
    # src/config_artifact.py's _DEFAULT_MODEL_RATES for the table and the
    # `metered` field's full comment. Resolved through the facade above.

    @classmethod
    def rate_for(cls, model: str) -> dict | None:
        """Billing rate for a model id, or None when the id has no row.

        Deliberately the OPPOSITE instinct to limits_for(). That one guesses
        the STRICTER quota tier, because guessing high burns real quota. Cost
        must never guess LOW: a silent zero is how a bill goes unnoticed. An
        unknown id is unpriced, and an unpriced span makes its whole aggregate
        unpublishable -- but it does not raise, because a missing price row
        must not turn a chat into a 500.
        """
        rate = cls.MODEL_RATES.get(model)
        if rate is None:
            logging.getLogger(__name__).warning(
                "no rate row for model %r -- cost is UNPRICED, not zero. Any aggregate "
                "containing this request cannot be published. Add it to Config.MODEL_RATES.",
                model,
            )
        return rate

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
            # base["tpm"] is None for providers with no per-minute token cap.
            "tpm": cls.JUDGE_TPM or base["tpm"] or cls.NO_TPM_LIMIT,
            "rpd": cls.JUDGE_RPD or base["rpd"],
        }

    # --- RAG: serving ---
    # SERVING_MODEL moved to the versioned serving config (v2 P2 step 9,
    # R51): it is the id the serving path prices against and the evidence log
    # already names the frozen v1 baseline by (status/mitya-v2/STATUS.md) --
    # a billing identity, not a file path (llama-server is addressed by URL)
    # -- and it changes what a correct answer is (which model answered),
    # exactly D9's criterion for belonging in the hash. Its absence here was
    # the defect R51 found: switching generation arms did not churn
    # config_version, so the answer cache did not invalidate across arms.
    # Resolved through the _ConfigMeta facade above; default lives in
    # src/config_artifact.py's ServingConfig.from_env().
    #
    # One synthetic tenant until P5 makes identity real (v2 P1 step 4, A12).
    # The X-Tenant-ID header that overrides this is UNAUTHENTICATED -- anyone
    # who can reach the endpoint can claim any tenant -- so it is shape-
    # validated, never used to build a registry. See src/observability.py's
    # parse_tenant().
    DEFAULT_TENANT = os.getenv("DEFAULT_TENANT", "default")
    LLAMA_SERVER_URL = os.getenv("LLAMA_SERVER_URL", "http://127.0.0.1:8080")
    # CTX_SIZE, MAX_TOKENS, GEN_TEMPERATURE, GEN_TOP_P moved to the versioned
    # serving config -- they change what a correct answer is. Resolved through
    # the facade above. EMPTY_RETRIES stays: it is a retry policy (deployment),
    # not a generation parameter that changes what a correct answer says.
    #
    # 4, not 1. Measured on the 1060 (P0 step 9a / R28), one theme, k=3:
    # 19.4% empty with no guard, 16.7% at EMPTY_RETRIES=1, 8.3% at 4. The
    # residual is not transient -- it concentrates in specific queries that
    # fail across independent runs and settings, and it is NOT prompt size
    # (the failing prompts are smaller than the passing ones).
    EMPTY_RETRIES = int(os.getenv("EMPTY_RETRIES", "4"))

    # Spend-rate circuit breaker cap (v2 P2 Batch C step 6, wiring Batch B's
    # SpendRateBreaker). DEPLOYMENT, not versioned: it is an operational
    # safety limit and changing it does not change what a correct answer is,
    # so it stays on Config rather than in the hash -- same split EMPTY_RETRIES
    # sits on.
    #
    # $0.10/min is a BACKSTOP, deliberately loose. At the cheap arm's list rate
    # (~$0.00157/answer, R60) that is ~63 answers/minute, well above
    # gemini-3.5-flash-lite's own rpm of 15 -- so under normal operation the
    # QUOTA limiter binds first and this never fires. It exists for the case the
    # limiter cannot see: an expensive or newly-added model whose per-answer
    # cost is an order of magnitude higher, where 15 rpm is still within quota
    # but the spend rate is not. Trip it and the ladder degrades rather than
    # spending (charter IV).
    SPEND_RATE_MAX_USD_PER_MINUTE = float(os.getenv("SPEND_RATE_MAX_USD_PER_MINUTE", "0.10"))

    # --- Retry policy (v2 P2 step 8(b), scoped by CP-γ / R70) -----------
    # ONE attempt budget shared by both retry classes, so a transport retry and
    # a quality retry cannot compound: three of each would be nine generations
    # for one answer, and step 8 is explicit that "retries are a cost
    # multiplier here". Deployment, not versioned -- a retry policy does not
    # change what a correct answer is (same split EMPTY_RETRIES sits on).
    MAX_GENERATION_ATTEMPTS = int(os.getenv("MAX_GENERATION_ATTEMPTS", "3"))

    # Fault injection (v2 P4 Batch C step 7, decided at CP-4b:
    # `plans/mitya-v2/02-decide/decisions-p4-cp4b.md` D7-D9). DEPLOYMENT, not
    # versioned: an availability switch that either changes nothing (off) or
    # prevents an answer rather than altering one (on) -- never what a
    # correct answer SAYS, which is D9's own criterion (same split
    # EMPTY_RETRIES / MAX_GENERATION_ATTEMPTS / SPEND_RATE_MAX_USD_PER_MINUTE /
    # CACHE_ENABLED sit on). Empty string = off, the default, and off in
    # every deployment that does not set it deliberately -- no serving config
    # under config/serving/ may set it, since it is a JSON versioned artifact
    # and this is an env var read straight onto Config. Grammar
    # `<class>:<attempts|*>` (e.g. `429:2`), parsed by `src/gateway/fault.py`.
    FAULT_INJECT = os.getenv("MITYA_FAULT_INJECT", "")

    # Replace the vendor adapter with `src.gateway.fake.FakeProvider`. A
    # DEPLOYMENT field, on the same split FAULT_INJECT sits on: it changes what
    # the system does, not what a correct answer IS, so it never enters the
    # serving artifact's hash (config_artifact.py's versioned/deployment rule).
    #
    # This exists because the P4 ramp measures the SERVER's concurrency -- the
    # Semaphore(1), retrieval, the non-re-entrant local components -- and a real
    # provider would only add its own latency to every point on that curve while
    # spending quota to do it (R96). Off by default, and like FAULT_INJECT it
    # must never be set on a run whose answers are meant to be real.
    FAKE_PROVIDER = os.getenv("MITYA_FAKE_PROVIDER", "") not in ("", "0", "false", "False")

    # Models where a zero-token completion is re-drawn. Default EMPTY, i.e. off
    # everywhere, which is CP-γ's decision: 11a measured Google at 0 zero-token
    # completions over 200 draws and Groq at 1 over 150 (R70). The rule keeps
    # the mechanism on a single occurrence but defaults it off for providers
    # measured at zero -- keeping a disabled mechanism costs a config field,
    # deleting one that was needed costs a rediscovery.
    #
    # R56 is the reason this is not defaulted on for reasoning models: an empty
    # answer there can mean the token budget was consumed by thinking, and a
    # re-draw re-pays the full thinking cost and truncates identically.
    QUALITY_RETRY_MODELS = frozenset(
        m.strip() for m in os.getenv("QUALITY_RETRY_MODELS", "").split(",") if m.strip()
    )
    CACHE_ENABLED = os.getenv("CACHE_ENABLED", "true").lower() == "true"
    CACHE_DIR = os.path.join(DATA_DIR, "cache")

    # --- Telemetry (v2 P1, C1 settled by R32: OpenTelemetry span tree) ---
    # Tracing is ON by default but EXPORT is not: with no endpoint configured
    # the SDK still builds spans and drops them, so a dev run needs no Jaeger
    # and no docker. Point OTEL_EXPORTER_OTLP_ENDPOINT at the collector to
    # ship them (Jaeger all-in-one listens for OTLP/HTTP on :4318).
    OTEL_ENABLED = os.getenv("OTEL_ENABLED", "true").lower() == "true"
    OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "mitya-rag")
    OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    # Print spans as JSON on stdout. For local work with no collector running,
    # and for verifying the tree where the host cannot reach one.
    OTEL_CONSOLE_EXPORT = os.getenv("OTEL_CONSOLE_EXPORT", "false").lower() == "true"


    # PROMPT_COMPOSITION (P7 A/B axis, ruling R19) moved to the versioned
    # serving config -- it selects which generate.build_messages() branch
    # runs, which changes what a correct answer is. Resolved through the
    # facade above.

    # Lazy-built cache keyed off BOOK_TITLE_ALIASES (now a facade-resolved
    # field). Left as an ordinary class attribute: it starts as None, so
    # normal attribute lookup succeeds here and _ConfigMeta.__getattr__ never
    # fires for this name.
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
