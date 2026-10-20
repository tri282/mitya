"""diskcache-backed response cache for repeated queries (design III.6).

Exact-match only, keyed on normalize(query) + filters + route (P1 5c), no
TTL (static corpus), stores only successful non-empty answers. A semantic
cache (L2) was re-examined for v2 P2 (R49) and refuted on measurement, not
plausibility (R50): dense query cosine does not predict retrieved chunks,
because the retrieval pipeline's sparse leg and cross-encoder rerank are both
lexical. This stays an exact-match boundary.

The cached VALUE carries `model` (v2 P2 step 9, R51/CP-delta): the producing
arm. Its absence was CP-delta's real defect -- `ServingConfig` carried no
generation-model field, so switching arms did not churn `config_version` and
did not invalidate the cache; arm B was served arm A's answers, contaminating
quality and latency measurements as well as cost. `config_artifact.py`'s new
`serving_model` field fixes the SEQUENTIAL-arms case (config_version now
churns per arm, so R42's existing invalidate-on-config-change drops the
cache with no new cache code). `model` on the value fixes the
PER-REQUEST-routing case (Batch D): once one config version can serve more
than one arm, `config_version` alone can no longer say which arm produced a
given hit, so the hit has to say so itself.
"""

import hashlib
import json
import logging

import diskcache

from src.config import Config

logger = logging.getLogger(__name__)

_VALUE_KEYS = {"answer", "sources", "footer", "model", "finish_reason"}


class AnswerCache:
    """Exact-match cache over (query, filters, route) -> {answer, sources,
    footer, model, finish_reason}."""

    def __init__(self) -> None:
        # None when disabled -- get()/set() become no-ops rather than every
        # caller having to check Config.CACHE_ENABLED itself.
        self._cache: diskcache.Cache | None = (
            diskcache.Cache(Config.CACHE_DIR) if Config.CACHE_ENABLED else None
        )

    @property
    def enabled(self) -> bool:
        """Whether this cache can serve a hit at all -- what step 10's
        degradation ladder (`src/gateway/ladder.py`) reads as
        `Readiness.cache_available`. A public accessor rather than a caller
        reaching for `self._cache is not None` directly."""
        return self._cache is not None

    def _key(
        self, query: str, filters: dict | None, route: str,
        config_version: str, corpus_version: str,
    ) -> str:
        normalized = query.strip().lower()
        # ponytail: no `tenant` in the key. Shared corpus (A12) means tenant-keying
        # costs hit rate and isolates nothing. Add it at P6, the moment per-tenant
        # memory reaches the prompt -- that is the first per-tenant content there is.
        #
        # config_version (P1 step 9, 5c's remaining half): a changed serving
        # config can change what a correct answer is (that is the whole point
        # of D9's versioned/deployment split), so a stale answer from a prior
        # config must not be served as a hit under a new one.
        #
        # corpus_version (P1 step 9 extension, coordinator-approved): a
        # re-ingest replaces the index that grounded every cached answer.
        # CorpusManifest.version() churns on any re-ingest, including a
        # no-op one (ingested_at is part of the hash) -- so a re-ingest drops
        # the whole cache. That is the intended behavior, not a side effect:
        # the alternative is silently serving an answer grounded in points
        # that may no longer exist.
        payload = (
            normalized + json.dumps(filters or {}, sort_keys=True)
            + route + config_version + corpus_version
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(
        self, query: str, filters: dict | None, route: str,
        config_version: str, corpus_version: str,
    ) -> dict | None:
        """Return the cached {answer, sources, footer, model, finish_reason}, or None on a
        miss, when disabled, or when the stored value predates this shape
        (R6/C10, extended v2 P2 step 9 -- self-healing: a legacy entry
        without `model` is treated as a miss and re-generated once, not
        migrated. A stored value has no way to *acquire* a producing arm
        after the fact, so migration would mean fabricating one. The same
        holds for `finish_reason` (v3 P0 B2, P0-F6): an entry stored before
        it existed could be a truncated answer, so it is a miss, not a
        "complete" hit).

        Keying on `route` (P1 5c), `config_version` and `corpus_version` (P1
        step 9) invalidates every entry stored before those changes --
        correct and needs no migration, since a stale key is simply never
        looked up again."""
        if self._cache is None:
            return None
        key = self._key(query, filters, route, config_version, corpus_version)
        cached = self._cache.get(key)
        if not isinstance(cached, dict) or cached.keys() != _VALUE_KEYS:
            return None
        logger.info("AnswerCache: hit key=%s model=%s", key[:12], cached["model"])
        return cached

    def set(
        self, query: str, filters: dict | None, route: str,
        config_version: str, corpus_version: str,
        answer: str, sources: list[dict], footer: str, model: str,
        *, finish_reason: str,
    ) -> None:
        """Store a successful {answer, sources, footer, model, finish_reason}. No-op
        when disabled or answer is empty.

        `finish_reason` is the normalized value the answer ended with; a hit replays
        it, so a truncated answer never replays as complete. Required, like `model`.

        `model` (v2 P2 step 9, R51) is the producing arm -- required, not
        optional, so a caller cannot silently reintroduce an unattributed
        entry that `get()` would then have to guess about."""
        if self._cache is None or not answer:
            return
        key = self._key(query, filters, route, config_version, corpus_version)
        self._cache.set(key, {
            "answer": answer, "sources": sources, "footer": footer, "model": model,
            "finish_reason": finish_reason,
        })
        logger.info("AnswerCache: stored key=%s model=%s", key[:12], model)
