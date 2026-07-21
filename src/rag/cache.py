"""diskcache-backed response cache for repeated queries (design III.6).

Exact-match only, keyed on normalize(query) + filters, no TTL (static
corpus), stores only successful non-empty answers. A semantic cache was
considered and rejected: it would add an embedding index for a single-user
app whose hit rate is near zero anyway (docs/RAG.md III.6) -- this stays a
~15-line exact-match boundary instead.
"""

import hashlib
import json
import logging

import diskcache

from src.config import Config

logger = logging.getLogger(__name__)


class AnswerCache:
    """Exact-match cache over (query, filters) -> final answer text."""

    def __init__(self) -> None:
        # None when disabled -- get()/set() become no-ops rather than every
        # caller having to check Config.CACHE_ENABLED itself.
        self._cache: diskcache.Cache | None = (
            diskcache.Cache(Config.CACHE_DIR) if Config.CACHE_ENABLED else None
        )

    def _key(self, query: str, filters: dict | None) -> str:
        normalized = query.strip().lower()
        payload = normalized + json.dumps(filters or {}, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, query: str, filters: dict | None = None) -> str | None:
        """Return the cached answer, or None on a miss or when disabled."""
        if self._cache is None:
            return None
        key = self._key(query, filters)
        answer = self._cache.get(key)
        if answer is not None:
            logger.info("AnswerCache: hit key=%s", key[:12])
        return answer

    def set(self, query: str, filters: dict | None, answer: str) -> None:
        """Store a successful answer. No-op when disabled or answer is empty."""
        if self._cache is None or not answer:
            return
        key = self._key(query, filters)
        self._cache.set(key, answer)
        logger.info("AnswerCache: stored key=%s", key[:12])
