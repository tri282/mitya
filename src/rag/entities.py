"""Entity map: query expansion, never a Qdrant score boost (ruling R6).

Resolves character mentions in a query against the human-reviewed
`entities.json` and appends the matched canonical name + aliases to the query
text before embedding -- the BM25 leg then naturally ranks entity-bearing
chunks higher, and the dense leg gets a richer query. This is NOT a Qdrant
`should` filter: Qdrant filters are binary, and a should-only filter excludes
every non-matching point, which would silently gut recall.

Matching runs on `src.rag.names.fold_tokens()` (P3 step 4), so a P&V-spelled
query ("Dunya", "Razumikhin") resolves against the Garnett-built index. This
module imports that matching key, it does not define one -- see
src/rag/names.py's module docstring for why the fold rules live there and are
not to be re-implemented here.

Resolution is book-scoped (design II.4): a hard filter takes the matching
candidate; with no filter, more than one candidate for a name means the name
does not identify a person, so expansion is skipped rather than guessed.
"""

import json
import logging

from src.config import Config
from src.rag.names import fold_tokens

logger = logging.getLogger(__name__)


class EntityMap:
    """Loads `entities.json` once; resolves query mentions for expansion."""

    def __init__(self, path: str = Config.ENTITIES_PATH):
        try:
            with open(path, encoding="utf-8") as f:
                self._entities: dict[str, list[dict]] = json.load(f)
        except json.JSONDecodeError as exc:
            # entities.json is the curated P3 artifact -- not something this
            # module may edit. Surface the exact failure loudly rather than
            # letting a bare traceback reach the caller; see
            # plans/mitya-rag/05-impl/phase-4.md for the known syntax errors
            # found at P4 impl time (two missing commas in alias lists).
            logger.error("EntityMap: %s is not valid JSON (%s)", path, exc)
            raise

        # Reverse index: folded surface-phrase tokens -> the reference-name
        # keys (entities.json top-level keys) they belong to. Every surface a
        # query might use -- the reference name itself, each record's
        # canonical, and each record's aliases -- is indexed by its *whole*
        # folded token tuple, so matching in resolve() compares whole tokens,
        # never substrings (measured failure: "liza" substring-matching
        # inside "lizaveta").
        self._index: dict[tuple[str, ...], set[str]] = {}
        self._max_surface_len = 1
        for name, records in self._entities.items():
            surfaces = {name}
            for record in records:
                surfaces.add(record["canonical"])
                surfaces.update(record.get("aliases", []))
            for surface in surfaces:
                tokens = fold_tokens(surface)
                if not tokens:
                    continue
                self._index.setdefault(tokens, set()).add(name)
                self._max_surface_len = max(self._max_surface_len, len(tokens))

        logger.info(
            "EntityMap: loaded %d reference names, %d indexed surface forms",
            len(self._entities), len(self._index),
        )

    def resolve(self, query: str, active_book: str | None = None) -> tuple[str, list[str]]:
        """Resolve entity mentions in `query`; return (expanded_query, resolved_names).

        `expanded_query` appends each resolved entity's verbatim `canonical`
        + `aliases` -- real corpus text, never a fold key (`fold("Count") ==
        "kunt"` is a match key only and would poison the BM25 leg with tokens
        that appear nowhere). `resolved_names` are the matched entities'
        canonical names, for request logging (P6 consumes this).

        `active_book`, if given, accepts anything `Config.resolve_book_title()`
        accepts -- a raw user-facing title ("Demons") or an already-canonical
        one ("The Possessed"); resolution is idempotent on canonicals (D3
        fix). Records in entities.json carry only canonical titles, so
        comparing a raw title against them silently disabled expansion for
        every book-filtered request. Raises ValueError on an unmapped title,
        matching resolve_book_title()'s loud-reject contract.
        """
        query_tokens = fold_tokens(query)
        matched_keys = self._scan(query_tokens)

        canonical_active_book = Config.resolve_book_title(active_book) if active_book is not None else None

        expansion_terms: list[str] = []
        seen_terms: set[str] = set()
        resolved_names: list[str] = []
        for key in matched_keys:
            record = self._resolve_book(self._entities[key], canonical_active_book, key)
            if record is None:
                continue
            resolved_names.append(record["canonical"])
            for term in (record["canonical"], *record.get("aliases", [])):
                if term not in seen_terms:
                    seen_terms.add(term)
                    expansion_terms.append(term)

        if not expansion_terms:
            return query, resolved_names

        expanded_query = query + " " + " ".join(expansion_terms)
        logger.info("EntityMap.resolve: query_chars=%d resolved=%s", len(query), resolved_names)
        return expanded_query, resolved_names

    def _scan(self, query_tokens: tuple[str, ...]) -> list[str]:
        """Scan `query_tokens` for indexed surface spans, longest first at
        each start position (so "Lizaveta Prokofyevna" wins over a shorter
        surface sharing its first token), then skip past the matched span.

        Returns keys in **query-occurrence order**, sorted within one span.
        This used to return a set, and resolve() iterated it: with two
        entities in one query the alias blocks swapped places depending on
        PYTHONHASHSEED, so the same query produced different expanded text in
        different processes, hence a different dense embedding and a different
        ranking. Measured as recall@5 flipping 0.720/0.760 run to run (P0/R17).
        Order is load-bearing here, not cosmetic.
        """
        matched: dict[str, None] = {}
        n = len(query_tokens)
        i = 0
        while i < n:
            matched_len = 0
            for span in range(min(self._max_surface_len, n - i), 0, -1):
                keys = self._index.get(query_tokens[i:i + span])
                if keys:
                    matched.update(dict.fromkeys(sorted(keys)))
                    matched_len = span
                    break
            i += matched_len if matched_len else 1
        return list(matched)

    @staticmethod
    def _resolve_book(records: list[dict], active_book: str | None, key: str) -> dict | None:
        """Book-scoped candidate resolution (design II.4): hard filter -> the
        matching record; no filter + exactly one candidate -> it; no filter +
        several -> skip expansion entirely. Expanding with the wrong novel's
        aliases is worse than not expanding -- never guess."""
        if active_book is not None:
            for record in records:
                if record["book"] == active_book:
                    return record
            return None
        if len(records) == 1:
            return records[0]
        logger.debug(
            "EntityMap: %r is ambiguous across %d books with no active filter -- skipping expansion",
            key, len(records),
        )
        return None
