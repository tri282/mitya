"""The versioned serving config (v2 P1 step 5, charter D9).

Config gets an identity: recordable, comparable, revertible. Every request
records which version served it; every answer traces to the exact config that
produced it.

The split this module encodes, which is the part that decides future fields
(P7's search space is "everything the artifact exposes"):

    Versioned  -- changing it changes what a correct answer *is*.  Here.
    Deployment -- changes where the system runs, how fast, or how much it
                  costs, but not what the answer says.  Stays on `Config`.
    Secret     -- authenticates.  Never versioned, never serialized.

Why a model and not `Config.VERSION = "1.4"`: P7's step 1 is "define the
search space as config versions" -- a proposal *is* a config version -- so
config must be a value you can instantiate N of, not a process-lifetime
singleton built from env at import.
"""

import hashlib
import json
import logging
import os
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.getcwd(), "config")
SERVING_DIR = os.path.join(CONFIG_DIR, "serving")
CURRENT_POINTER = os.path.join(SERVING_DIR, "CURRENT")
CORPUS_MANIFEST = os.path.join(CONFIG_DIR, "corpus", "manifest.json")

# The env override, used by P3's subprocess-per-arm and by P7's search. Wins
# over CURRENT for one process.
VERSION_ENV = "MITYA_CONFIG_VERSION"

VERSION_LENGTH = 12


class ConfigError(RuntimeError):
    """Refusing to serve. Every path that raises this would otherwise serve an
    unversioned or misidentified config while still stamping something on
    every span -- which is worse than not starting."""


# --- secrets --------------------------------------------------------------
# Field-name shapes that must never become artifact fields. The artifact is
# hashed, written to a git-tracked file, diffed in review, and shipped to
# Jaeger as a span attribute -- there is no such thing as a secret that is
# "only" in the config version.
#
# Matched per underscore-separated segment, not as a substring: `max_tokens`
# and `top_k_fused` are not secrets, while `api_key` and `auth_token` are.
# Plurals are deliberately absent for the same reason ("tokens" is a count).
_SECRET_NAME_SEGMENTS = frozenset({"key", "secret", "token", "password", "credential"})

# Value-side check. A secret does not need a secret-shaped field name to leak:
# it only needs to be assigned to one that is not.
_SECRET_ENV_VARS = ("GOOGLE_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY")


def _secret_values() -> list[str]:
    return [v for name in _SECRET_ENV_VARS if (v := os.getenv(name)) and len(v) >= 8]


# --- defaults -------------------------------------------------------------
# These literals moved here from `Config` because they ARE the artifact: a
# quota row, a price, or a title alias changes what a correct answer is, and
# each must therefore become a visible config version rather than an invisible
# edit. `Config` still serves them by name, through the facade.

# Users say "Demons"/"The Devils"; entities.json, Qdrant payloads, and
# structure.json all carry the Garnett-edition title "The Possessed". An
# unmapped title becomes a hard filter matching zero points -- a silent empty
# result, worse than a miss -- so unmapped titles are rejected loudly by
# Config.resolve_book_title, never passed through. Not guaranteed exhaustive
# of every title readers might use.
_DEFAULT_BOOK_TITLE_ALIASES: dict[str, str] = {
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

# Per-model free-tier limits (rpm, tpm, rpd). NOT derivable from the model id
# and they differ by an order of magnitude between tiers, so they live in one
# table. Source: the AI Studio dashboard rows, supplied 2026-08-21.
#   * flash      -- stronger, rpd 20. Useless for a batch.
#   * flash-lite -- weaker, rpd 500. What every v1 number was measured on.
# `verified` records whether THIS repo has seen the id resolve on a live call
# (test/bakeoff/RESULTS.md), not whether the dashboard lists it -- the
# bake-off's finding was that the dashboard lists models that 404.
_DEFAULT_MODEL_LIMITS: dict[str, dict] = {
    # id                              provider   rpm     tpm      rpd    tpd    verified
    "models/gemini-3.7-flash":       {"provider": "google", "rpm": 5,  "tpm": 250_000, "rpd": 20,   "tpd": None,     "verified": False},
    "models/gemini-3.6-flash":       {"provider": "google", "rpm": 5,  "tpm": 250_000, "rpd": 20,   "tpd": None,     "verified": False},
    "models/gemini-3.5-flash":       {"provider": "google", "rpm": 5,  "tpm": 250_000, "rpd": 20,   "tpd": None,     "verified": True},
    "models/gemini-3-flash-preview": {"provider": "google", "rpm": 5,  "tpm": 250_000, "rpd": 20,   "tpd": None,     "verified": True},
    "models/gemini-3.5-flash-lite":  {"provider": "google", "rpm": 15, "tpm": 250_000, "rpd": 500,  "tpd": None,     "verified": True},
    "models/gemini-3.1-flash-lite":  {"provider": "google", "rpm": 15, "tpm": 250_000, "rpd": 500,  "tpd": None,     "verified": True},

    # --- NOT YET WIRED ----------------------------------------------------
    # Registry only. `TeacherClient` speaks the google-genai SDK and nothing
    # else, so selecting one of these today raises at client construction.
    # Here because the charter requires >= 2 provider families for v2 serving
    # (D1) and because quota rows decide whether a measurement run is even
    # possible. Both are materially weaker than Gemini's free tier for BATCH
    # work: OpenRouter's free pool caps at 50 rpd; Groq's tpm is 8000, roughly
    # 3% of Gemini's, and tpm binds long before rpm or rpd does.
    "openrouter/free":               {"provider": "openrouter", "rpm": 20, "tpm": None,   "rpd": 50,   "tpd": None,     "verified": False},
    # Promoted to verified=True (P2 Batch C step 3, R53): Groq's published
    # per-model table matches this row exactly on a desk read -- unlike
    # Google, Groq publishes free-tier limits.
    "openai/gpt-oss-120b":           {"provider": "groq",       "rpm": 30, "tpm": 8_000,  "rpd": 1000, "tpd": 200_000,  "verified": True},
    "qwen/qwen3.6-27b":              {"provider": "groq",       "rpm": 30, "tpm": 8_000,  "rpd": 1000, "tpd": 200_000,  "verified": False},
}

# Unknown id -> the STRICTER tier, never the looser one. Guessing high burns
# real quota and turns a batch into a 429 storm partway through; guessing low
# only makes it slow.
_DEFAULT_UNKNOWN_MODEL_LIMITS: dict = {
    "provider": None, "rpm": 5, "tpm": 250_000, "rpd": 20, "tpd": None, "verified": False,
}

# `tpm: None` means the provider publishes no per-minute token cap; the
# RateLimiter needs a real int.
_DEFAULT_NO_TPM_LIMIT = 10 ** 9

# Rates in USD per MILLION tokens, the unit every provider publishes.
# `metered` is the load-bearing field, and it is NOT "is this row filled in
# yet": metered=False means nothing bills us (local inference on hardware we
# own), and reporting that as "$0.00 of cost" would be false. Any aggregate
# containing an unmetered span is reported PROVISIONAL, never as a cost.
# P2's gateway adds provider rows here and changes nothing else.
# Verbatim as it stood when ownership moved here (v2 P2 step 7, R54). It was
# byte-identical to src/training.py's SYSTEM_PROMPT at that moment; the two are
# now free to diverge, since one describes the fine-tune's training and the
# other is a tunable serving knob.
_DEFAULT_PERSONA_HEAD = (
    "You are Fyodor Dostoevsky. Answer the user's question in the first person, "
    "incorporating your deep literary, existential, and philosophical tone. "
    "Give no editorial comments and references to external sources."
)

_DEFAULT_MODEL_RATES: dict[str, dict] = {
    # id             prompt $/Mtok  completion $/Mtok  metered
    "local-q4km":   {"prompt": 0.0, "completion": 0.0, "metered": False},

    # P2 Batch C step 3 (R48/R53/R55): published list rates for the two arms
    # step 3 wires. metered=False on BOTH -- these calls run against free-tier
    # quota (Config.MODEL_LIMITS), not a bill. R48's rule: price at list rate
    # so a USD/answer figure exists at all, report it everywhere as a
    # *list-rate estimate at the free tier*, never as spend. Each row cites
    # its pricing page and the date read; a wrong unverified price is worse
    # than a missing one (rate_for's comment above), so do not edit these
    # without re-reading the source.
    #
    # Google Gemini 3.5 Flash-Lite -- ai.google.dev/gemini-api/docs/pricing,
    # read 2026-08-28 (via p2-provider-survey.md P-1): $0.30 / $2.50 per Mtok.
    # Chosen over 3.5 Flash for its larger free-tier rpd (500 vs 20).
    "models/gemini-3.5-flash-lite": {"prompt": 0.30, "completion": 2.50, "metered": False},

    # Google Gemini 3.1 Flash-Lite -- ai.google.dev/gemini-api/docs/pricing,
    # read 2026-08-31 (v2 P6, G-P11 prereq 1). Paid-tier text input $0.25 /
    # Mtok, output $1.50 / Mtok. The page splits this row's input by modality
    # ($0.25 text/image/video, $0.50 audio) unlike the 3.5-lite row above,
    # which is unified; we send text only, so the text rate is the correct one
    # and the audio rate is deliberately not wired -- this table has no
    # modality column. The 3.5-lite row was re-read in the same pass and still
    # says $0.30 / $2.50, which is what makes this read a cross-check rather
    # than a single-source edit.
    #
    # P6's summarizer/memory-builder arm (G-P11, owner decision): it draws on
    # its OWN quota window, since quota_scope() is (provider, model)
    # (src/limits.py:167-186), and it is also cheaper than the serving arm on
    # both axes. Without this row summarizer draws are unpriced,
    # metrics.unpriced_answers fires (src/metrics.py:376), and phase-6 step 4's
    # stated purpose -- memory writes appear in cost attribution -- is defeated.
    "models/gemini-3.1-flash-lite": {"prompt": 0.25, "completion": 1.50, "metered": False},

    # Groq openai/gpt-oss-120b -- $0.15 / $0.60 per Mtok. Confirmed 2026-08-28
    # from secondary pricing trackers, NOT from Groq's own console page (that
    # page was not reachable in this session). Re-read the console before any
    # published USD figure leans on it:
    # cached-input read is $0.075/Mtok, not wired here -- this table has no
    # cache-rate column; see phase-2.md's Batch C write-up.
    # completion_tokens on this arm INCLUDES reasoning tokens (R55) -- priced
    # correctly here since that is what is billed; GroqUsage.reasoning_tokens
    # carries the split for a cross-arm comparison that must not conflate the
    # two (§II).
    "openai/gpt-oss-120b":          {"prompt": 0.15, "completion": 0.60, "metered": False},
}

# Substring triggers for the STRUCTURAL route. Moved off src/rag/router.py.
# Deliberately no trailing preposition governing the reference -- one of those
# was P1 step 3b's defect 2, and test_router.py's phrasing-invariance check is
# what catches a reintroduction.
_DEFAULT_SUMMARY_INTENT = ("summarize", "what happens", "overview of", "recap")


class ServingConfig(BaseModel):
    """The versioned half of the config. Frozen, so a version is a value.

    Field names are the lowercase of their `Config` counterparts; the facade
    in src/config.py maps between them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # retrieval
    embed_model: str
    rerank_model: str
    rerank_enabled: bool
    prefetch_limit: int
    top_k_fused: int
    top_k_rerank: int
    top_k_structural_excerpts: int

    # generation
    ctx_size: int
    max_tokens: int
    gen_temperature: float
    gen_top_p: float
    prompt_composition: str
    # Which model actually generates (v2 P2 step 9, R51). Moved off `Config`
    # as a plain attribute for the same reason CTX_SIZE/MAX_TOKENS/
    # PROMPT_COMPOSITION already are: it changes what a correct answer is
    # (D9's versioned/deployment split), so it belongs in the hash. Its
    # absence here was CP-delta's real defect (R51): switching generation
    # arms did not churn config_version, so the cache did not invalidate
    # across arms and arm B was served arm A's cached answers -- contaminating
    # quality, latency, and cost measurements alike, not just cost.
    serving_model: str
    # The persona system prompt (v2 P2 Batch C step 7, C5 decided in favour of
    # moving OWNERSHIP, not merely pinning). It used to be a module constant at
    # src/rag/generate.py, kept word-for-word in sync with src/training.py's
    # SYSTEM_PROMPT by a comment and nothing else -- P-3 measured that (R54):
    # no test imported both, `config_version` did not move when the text
    # changed, and the frozen row's own artifacts recorded no config_version,
    # git SHA, or timestamp at all.
    #
    # Two things follow from putting it here rather than hashing it (the
    # option NOT taken). First, editing the persona now churns config_version,
    # so the cache invalidates and a measurement cannot silently span two
    # prompts -- the same defect R51 closed for serving_model. Second, and this
    # is the actual C5 decision: with the fine-tune out of the request path the
    # persona is an ordinary prompt, so it becomes a P7 SEARCH AXIS rather than
    # a constant someone must remember to keep in sync.
    #
    # src/training.py's SYSTEM_PROMPT is deliberately NOT changed to read this.
    # That constant describes what the fine-tune was TRAINED on, which is
    # history and must not drift when a serving knob is tuned. The two were
    # byte-identical at 208 chars when they were split (R54, verified); from
    # here they are free to diverge, and that divergence is the point.
    persona_head: str

    # --- context allocation (v2 P6 step 3) ---
    # Which allocator policy composes the prompt. Versioned, not deployment:
    # a different allocation is a different prompt, so it changes what a
    # correct answer is (D9's split) and must churn `config_version` -- the
    # R51 defect, applied to the allocator. `src/memory.py`'s `POLICIES`
    # registry is the set of legal values; `active_policy()` raises on an
    # unknown one rather than silently falling back to a default, because a
    # silent fallback is how two policies' rows get pooled under one arm.
    #
    # This IS the seam P7 searches: swapping the policy is this field and
    # nothing else -- no call site names a policy class.
    allocation_policy: str
    # Ceiling on tokens the memory tiers may take, per request. 306 is C-2's
    # measured headroom (`plans/mitya-v2/02-decide/cp-6a-input.md`): CTX_SIZE
    # 3072 - MAX_TOKENS 1024 = 2048 of prompt budget, against a measured P4
    # prompt draw of 1742 (R121/R96). It is deliberately NOT the store cap --
    # N-1: the store holds 30 rows and the allocator injects a handful, and
    # the first person to tune one will conflate them if they share a name.
    memory_budget_tokens: int

    # --- memory, the persisted tiers (v2 P6 Batch C, CP-6a) ---
    # The summarizer / memory-writer arm. A SEPARATE field from
    # `serving_model`, not a `tier` lookup: R123 measured that `tier` is read
    # by zero model-selection sites, so "a cheap tier" was a phrase with no
    # mechanism behind it. Two ids means two quota windows, because
    # `quota_scope()` is (provider, model) (`src/limits.py:167-186`) -- which
    # is what dissolves R121's rpd squeeze (472/500 of one window becomes
    # 256/500 serving + 216/500 summarizer). The Google per-model bucket
    # separation is `[inferred]`, measured on the wire on Groq only; if Google
    # in fact shares one bucket across flash-lite variants the excess returns
    # as 429s that the transport retry absorbs into latency, MOSTLY SILENTLY,
    # and CP-6d's dry run is what watches for it.
    summarizer_model: str
    # The two STORE caps, enforced per `kind` so neither class can starve the
    # other (CP-6a O-C). Distinct names from `memory_budget_tokens` on purpose
    # (N-1): 10 + 20 = 30 rows cannot be injected into 306 tokens even once, so
    # these bound STORAGE and that one bounds INJECTION. The first person to
    # tune one will conflate them if they share a name.
    memory_profile_max_facts: int
    memory_profile_max_prefs: int
    # Episodic digests kept per tenant, oldest evicted first.
    memory_episodic_max_per_tenant: int
    # Staleness window on `last_confirmed` / `ended_at`, in days. 90 is an
    # `[estimate]` and is NEVER promoted to a measured figure: R122 measured
    # that the frozen corpus carries no timestamp at all, so no time-boxed
    # window of >= 5 minutes is crossed by any session and a window short enough
    # to fire would be measuring harness pacing (the R116a instrument-tuned-to-
    # its-own-result shape). This is a user-data posture (charter IV), chosen
    # off-corpus and labelled so. Enforced LAZILY on the write path and at read,
    # never by a job -- the `src/auth.py:252-256` `_prune_attempts` precedent.
    memory_retention_days: int
    # Hard ceiling on a stored `value`, in characters. This is the structural
    # half of C-1: a length-bounded typed field has nowhere to put a paragraph.
    # The honest bound, per CP-6a: it defeats paragraph-scale recovery BY
    # CONSTRUCTION and does NOT defeat clause-scale recovery, which is what the
    # adversarial `refusal`-family test covers instead.
    memory_value_max_chars: int

    # routing. Moved off src/rag/router.py: config by another name, and a P7
    # search axis. Left there, P7 finds an un-versionable knob.
    summary_intent: tuple[str, ...]

    # An unmapped title becomes a hard filter matching zero points, so this
    # list is answer-affecting in the nastiest way (config.py flags it as
    # non-exhaustive).
    book_title_aliases: dict[str, str]

    # A quota or price change should be a visible config version, not an
    # invisible edit -- 5b's stated point.
    model_limits: dict[str, dict[str, Any]]
    unknown_model_limits: dict[str, Any]
    no_tpm_limit: int
    model_rates: dict[str, dict[str, Any]]

    @model_validator(mode="after")
    def _no_secrets(self) -> "ServingConfig":
        """Secrets cannot enter, asserted at build time rather than trusted to
        the field list. Two ways in, both closed here: a secret-shaped field
        added later, and a real secret value assigned to an innocent field."""
        for name in type(self).model_fields:
            if _SECRET_NAME_SEGMENTS & set(name.lower().split("_")):
                raise ConfigError(
                    f"{name!r} is a secret-shaped field name and cannot be part of the "
                    f"versioned artifact -- the artifact is hashed, written to git, and "
                    f"exported to the tracing backend. Keep it on Config as a secret."
                )
        secrets = _secret_values()
        if secrets:
            blob = _canonical_json(self.model_dump(mode="json"))
            for value in secrets:
                if value in blob:
                    raise ConfigError(
                        "a value from the environment's API keys appears in the serving "
                        "config -- refusing to build an artifact that would be hashed, "
                        "written to git, and exported to the tracing backend."
                    )
        return self

    @classmethod
    def from_env(cls) -> "ServingConfig":
        """Build from the environment, with the same defaults `Config` used
        before these knobs moved here.

        The env parsing lives with the artifact rather than on `Config` so
        bootstrapping a fresh clone has no chicken-and-egg: after the facade
        lands, reading `Config.TOP_K_FUSED` requires a pointer, and the
        pointer is what bootstrap is trying to create.
        """
        return cls(
            embed_model=os.getenv("EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5"),
            rerank_model=os.getenv("RERANK_MODEL", "ms-marco-MiniLM-L-12-v2"),
            rerank_enabled=os.getenv("RERANK_ENABLED", "true").lower() == "true",
            prefetch_limit=int(os.getenv("PREFETCH_LIMIT", "50")),
            top_k_fused=int(os.getenv("TOP_K_FUSED", "25")),
            top_k_rerank=int(os.getenv("TOP_K_RERANK", "3")),
            top_k_structural_excerpts=int(os.getenv("TOP_K_STRUCTURAL_EXCERPTS", "2")),
            ctx_size=int(os.getenv("CTX_SIZE", "3072")),
            # Raised 400 -> 1024 (v2 P2, R66/R68). 400 was never an
            # answer-length decision: it was sized to fit CTX_SIZE=3072 on the
            # 1060 (~1742-token prompt + 400 = 2142), i.e. a fossil of the
            # hardware this phase exists to escape. It is an API cutoff the
            # model is never told about, while the persona asks for a "deep
            # literary, existential, and philosophical tone" -- so 11a judged
            # both arms on answers that stopped mid-sentence (128/150 and
            # 91/141).
            #
            # 1024 is the largest value that clears all three constraints at
            # once, which is why it is not larger:
            #   * local fit  -- 1742 + 1024 = 2766, still inside CTX_SIZE 3072,
            #     so LocalProvider keeps working (1330 would sit exactly at the
            #     ceiling with no headroom for a longer prompt);
            #   * Groq tpd  -- 200k / 2766 = 72 draws/day, down from 93 (R68);
            #   * cost      -- roughly 2x per answer on Google
            #     ($0.001523 -> $0.003083), since completion is the expensive leg.
            #
            # Raising this CHURNS config_version, so quality measured at 400 and
            # at 1024 are different arms and must not be pooled (R51).
            max_tokens=int(os.getenv("MAX_TOKENS", "1024")),
            gen_temperature=float(os.getenv("GEN_TEMPERATURE", "0.6")),
            gen_top_p=float(os.getenv("GEN_TOP_P", "0.9")),
            prompt_composition=os.getenv("PROMPT_COMPOSITION", "A"),
            # Same env var and default Config.SERVING_MODEL used before this
            # field existed (config.py's plain attribute is now removed --
            # this is the one place the default lives).
            # Default changed from "local-q4km" at Batch C step 6. A3 makes
            # local-q4km frozen recorded numbers -- a chart line, not a live
            # arm -- and nothing in the request path may start llama-server.
            # gemini-3.5-flash-lite over gemini-3.5-flash for two measured
            # reasons: rpd 500 vs 20, and R56 measured it at ZERO thought
            # tokens, so MAX_TOKENS is not consumed by thinking before the
            # answer starts (3.5-flash spent 288 of 300 and returned 8).
            serving_model=os.getenv("SERVING_MODEL", "models/gemini-3.5-flash-lite"),
            persona_head=os.getenv("PERSONA_HEAD", _DEFAULT_PERSONA_HEAD),
            # "passages_first" is the one real policy P6 ships; "passages_only"
            # is the trivial second one that proves the seam, and doubles as
            # the Verify table's no-memory baseline arm and Batch B's starved
            # arm. Neither is claimed to be a good policy -- O4 is P7's.
            allocation_policy=os.getenv("ALLOCATION_POLICY", "passages_first"),
            memory_budget_tokens=int(os.getenv("MEMORY_BUDGET_TOKENS", "306")),
            # Owner decision (G-P11), recorded not re-litigated. Its rate row
            # exists (`_DEFAULT_MODEL_RATES` above, read 2026-08-31) -- without
            # one every summarizer draw fires `unpriced_answers` and step 4's
            # stated purpose, memory writes appearing in cost attribution, is
            # defeated.
            summarizer_model=os.getenv("SUMMARIZER_MODEL", "models/gemini-3.1-flash-lite"),
            memory_profile_max_facts=int(os.getenv("MEMORY_PROFILE_MAX_FACTS", "10")),
            memory_profile_max_prefs=int(os.getenv("MEMORY_PROFILE_MAX_PREFS", "20")),
            memory_episodic_max_per_tenant=int(os.getenv("MEMORY_EPISODIC_MAX_PER_TENANT", "20")),
            memory_retention_days=int(os.getenv("MEMORY_RETENTION_DAYS", "90")),
            memory_value_max_chars=int(os.getenv("MEMORY_VALUE_MAX_CHARS", "80")),
            summary_intent=_DEFAULT_SUMMARY_INTENT,
            book_title_aliases=_DEFAULT_BOOK_TITLE_ALIASES,
            model_limits=_DEFAULT_MODEL_LIMITS,
            unknown_model_limits=_DEFAULT_UNKNOWN_MODEL_LIMITS,
            no_tpm_limit=_DEFAULT_NO_TPM_LIMIT,
            model_rates=_DEFAULT_MODEL_RATES,
        )

    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))

    def version(self) -> str:
        """Content-addressed identity. Two identical configs are the same
        version by construction; an optimizer candidate gets one for free; no
        counter to coordinate."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:VERSION_LENGTH]


def _canonical_json(data: Any) -> str:
    """One serialization, so the hash is stable across processes and machines.

    Hashing runs over *parsed* values, never input text -- `"3"` and `3` are
    the same config and must hash identically, which is why callers serialize
    a validated model rather than the JSON they read. Floats need no special
    handling: json.dumps emits `float.__repr__`, which has been round-trip
    stable and platform-independent since Python 3.1, so 0.7 cannot become
    0.7000000000000001.
    """
    return json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


# --- corpus manifest (v2 P1 step 8) ----------------------------------------
# CHUNK_TOKENS/CHUNK_OVERLAP/EMBED_MODEL change answers only through an index
# rebuild -- they are baked into Qdrant -- so they are a CORPUS version, not a
# serving config version. Conflating them means a config hash implies a
# corpus it cannot guarantee. Not a hash alone: "the index changed" has to be
# a readable event, not just a different opaque id.


class CorpusManifest(BaseModel):
    """What `scripts/ingest_corpus.py` wrote the last time it ran. Read at
    startup and checked against the active ServingConfig's EMBED_MODEL --
    see `assert_embed_model_matches` below for the bug this closes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_tokens: int
    chunk_overlap: int
    dense_model: str
    dense_dim: int
    sparse_model: str
    qdrant_collection: str
    point_count: int
    books: list[str]
    ingested_at: str  # ISO 8601, UTC

    # Derived-artifact provenance: summaries.json is itself not corpus-
    # neutral (a different TEACHER_MODEL/date changes its content). See
    # scripts/ingest_corpus.py's _summaries_provenance() for what these two
    # fields actually record when summaries.json carries no provenance of
    # its own.
    teacher_model: str
    summaries_generated_at: str

    # How this manifest came to exist, and therefore how much of it is READ
    # versus ASSERTED:
    #   "ingested" -- written by scripts/ingest_corpus.py at the end of the
    #                 run that built the index. Every field is a record of
    #                 what that run actually did.
    #   "adopted"  -- written against an index that already existed, by
    #                 scripts/bootstrap_config.py --adopt-corpus. Collection
    #                 name, dimension and point count are read back from
    #                 Qdrant; `dense_model`, `chunk_tokens` and
    #                 `chunk_overlap` are the CURRENT Config values, because
    #                 the index records none of them (that absence is the bug
    #                 this manifest exists to close). An adopted manifest
    #                 therefore pins today's values as the index's identity
    #                 and catches every FUTURE swap -- it cannot detect a
    #                 mismatch that already existed at adoption time.
    provenance: str = "ingested"

    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))

    def version(self) -> str:
        """Same hashing rule as ServingConfig.version() -- one rule, reused,
        not a second one invented for the corpus side."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:VERSION_LENGTH]


def save_corpus_manifest(manifest: CorpusManifest) -> str:
    """Write the manifest. Returns its version (see `CorpusManifest.version`)."""
    os.makedirs(os.path.dirname(CORPUS_MANIFEST), exist_ok=True)
    with open(CORPUS_MANIFEST, "w", encoding="utf-8") as f:
        f.write(manifest.canonical_json())
    version = manifest.version()
    logger.info("wrote corpus manifest %s (dense_model=%s, version=%s)",
                CORPUS_MANIFEST, manifest.dense_model, version)
    return version


def load_corpus_manifest() -> CorpusManifest:
    """Fail-closed, same posture as `load()`: a missing or invalid manifest
    must not silently serve an unversioned corpus."""
    if not os.path.exists(CORPUS_MANIFEST):
        raise ConfigError(
            f"no corpus manifest at {CORPUS_MANIFEST} -- refusing to start. "
            f"Re-run scripts/ingest_corpus.py to build the corpus and its manifest."
        )
    try:
        with open(CORPUS_MANIFEST, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"corpus manifest {CORPUS_MANIFEST!r} is unreadable: {exc}") from exc
    try:
        return CorpusManifest(**data)
    except Exception as exc:
        raise ConfigError(f"corpus manifest {CORPUS_MANIFEST!r} failed validation: {exc}") from exc


def assert_embed_model_matches(embed_model: str, manifest: CorpusManifest) -> None:
    """The live bug step 1 reproduced: EMBED_MODEL is read at ingest and at
    query from the same global, with nothing checking they agree. Swap it and
    restart without re-ingesting, and query vectors are compared against an
    index built by a different model -- retrieval degrades silently, no
    error, no log line. This is the startup assertion that closes it."""
    if embed_model != manifest.dense_model:
        raise ConfigError(
            f"EMBED_MODEL mismatch: serving config names {embed_model!r}, corpus "
            f"manifest names {manifest.dense_model!r} -- refusing to serve. Either "
            f"re-run scripts/ingest_corpus.py against the current EMBED_MODEL, or "
            f"point {VERSION_ENV} at a serving config version whose embed_model "
            f"matches the built index."
        )


# --- store ----------------------------------------------------------------

def artifact_path(version: str) -> str:
    return os.path.join(SERVING_DIR, f"{version}.json")


def save(cfg: ServingConfig) -> str:
    """Write the artifact under its own hash. Returns the version."""
    version = cfg.version()
    os.makedirs(SERVING_DIR, exist_ok=True)
    with open(artifact_path(version), "w", encoding="utf-8") as f:
        f.write(cfg.canonical_json())
    logger.info("wrote serving config %s", version)
    return version


def load(version: str) -> ServingConfig:
    """Materialize one version. Fail-closed at every step: a fallback here
    would silently serve an unversioned config."""
    path = artifact_path(version)
    if not os.path.exists(path):
        raise ConfigError(
            f"serving config {version!r} not found at {path} -- the pointer or "
            f"{VERSION_ENV} names a version that is not in the store."
        )
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"serving config {version!r} is unreadable: {exc}") from exc

    try:
        cfg = ServingConfig(**data)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"serving config {version!r} failed validation: {exc}") from exc

    # Content-addressing is only worth something if it is checked. A file whose
    # contents no longer hash to its name has been hand-edited, and the version
    # stamped on every span would name a config nobody ran.
    actual = cfg.version()
    if actual != version:
        raise ConfigError(
            f"serving config {version!r} hashes to {actual!r} -- the file was edited in "
            f"place. Write a new version instead; reverting is a pointer write."
        )
    return cfg


def read_pointer() -> str:
    """The A6 pointer: 'current production config' resolves with no human
    input. Promotion and revert are both a write to this one line."""
    if not os.path.exists(CURRENT_POINTER):
        raise ConfigError(
            f"no serving config pointer at {CURRENT_POINTER} -- refusing to start. "
            f"Bootstrap one with scripts/bootstrap_config.py."
        )
    with open(CURRENT_POINTER, encoding="utf-8") as f:
        version = f.read().strip()
    if not version:
        raise ConfigError(f"serving config pointer {CURRENT_POINTER} is empty -- refusing to start.")
    return version


def active_version() -> str:
    return os.environ.get(VERSION_ENV) or read_pointer()


_active: ServingConfig | None = None
_active_version: str | None = None


def active_serving_config() -> ServingConfig:
    """The one config this process serves. Cached: one uvicorn worker, so a
    single in-process config is correct (5b's stated single-worker
    assumption). Two live configs in one process is a P2 problem, and the
    upgrade path is an explicit `cfg` parameter, not a second cache."""
    global _active, _active_version
    if _active is None:
        version = active_version()
        _active = load(version)
        _active_version = version
        logger.info("active serving config: %s", version)
    return _active


def active_config_version() -> str:
    """The version stamped on spans, the SSE meta, and the cache key."""
    active_serving_config()
    assert _active_version is not None
    return _active_version


def reset_active() -> None:
    """Drop the cache. For tests and for bootstrap, which builds an artifact
    before a pointer exists."""
    global _active, _active_version
    _active = None
    _active_version = None


_active_corpus_manifest: CorpusManifest | None = None
_active_corpus_version: str | None = None


def active_corpus_manifest() -> CorpusManifest:
    """The one corpus manifest this process serves. Cached for the same
    reason as active_serving_config(): one uvicorn worker, one in-process
    read."""
    global _active_corpus_manifest, _active_corpus_version
    if _active_corpus_manifest is None:
        manifest = load_corpus_manifest()
        _active_corpus_manifest = manifest
        _active_corpus_version = manifest.version()
        logger.info("active corpus manifest: %s", _active_corpus_version)
    return _active_corpus_manifest


def active_corpus_version() -> str:
    """The version stamped on spans, the SSE meta, and the cache key."""
    active_corpus_manifest()
    assert _active_corpus_version is not None
    return _active_corpus_version


def reset_active_corpus() -> None:
    """Drop the corpus manifest cache. Mirrors reset_active(); kept separate
    since the two caches invalidate on independent events (a config promotion
    vs. a re-ingest)."""
    global _active_corpus_manifest, _active_corpus_version
    _active_corpus_manifest = None
    _active_corpus_version = None
