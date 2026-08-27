"""diskcache-backed response cache for repeated queries (design III.6).

Exact-match only, keyed on normalize(query) + filters + route (P1 5c), no
TTL (static corpus), stores only successful non-empty answers. A semantic
cache was considered and rejected: it would add an embedding index for a
single-user app whose hit rate is near zero anyway (docs/RAG.md III.6) --
this stays a ~20-line exact-match boundary instead.
"""

import hashlib
import json
import logging

import diskcache

from src.config import Config

logger = logging.getLogger(__name__)


class AnswerCache:
    """Exact-match cache over (query, filters, route) -> {answer, sources, footer}."""

    def __init__(self) -> None:
        # None when disabled -- get()/set() become no-ops rather than every
        # caller having to check Config.CACHE_ENABLED itself.
        self._cache: diskcache.Cache | None = (
            diskcache.Cache(Config.CACHE_DIR) if Config.CACHE_ENABLED else None
        )

    def _key(self, query: str, filters: dict | None, route: str) -> str:
        normalized = query.strip().lower()
        # ponytail: no `tenant` in the key. Shared corpus (A12) means tenant-keying
        # costs hit rate and isolates nothing. Add it at P6, the moment per-tenant
        # memory reaches the prompt -- that is the first per-tenant content there is.
        payload = normalized + json.dumps(filters or {}, sort_keys=True) + route
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, query: str, filters: dict | None, route: str) -> dict | None:
        """Return the cached {answer, sources, footer}, or None on a miss,
        when disabled, or when the stored value predates this shape (R6/C10 --
        self-healing: a legacy answer-only entry is treated as a miss and
        re-generated once, not migrated).

        Keying on `route` (P1 5c) invalidates every entry stored before this
        change -- correct and needs no migration, since a stale key is simply
        never looked up again."""
        if self._cache is None:
            return None
        key = self._key(query, filters, route)
        cached = self._cache.get(key)
        if not isinstance(cached, dict) or cached.keys() != {"answer", "sources", "footer"}:
            return None
        logger.info("AnswerCache: hit key=%s", key[:12])
        return cached

    def set(self, query: str, filters: dict | None, route: str, answer: str, sources: list[dict], footer: str) -> None:
        """Store a successful {answer, sources, footer}. No-op when disabled or answer is empty."""
        if self._cache is None or not answer:
            return
        key = self._key(query, filters, route)
        self._cache.set(key, {"answer": answer, "sources": sources, "footer": footer})
        logger.info("AnswerCache: stored key=%s", key[:12])
