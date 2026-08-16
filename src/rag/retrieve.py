"""Hybrid dense/sparse retrieval: Qdrant native RRF fusion + FlashRank rerank
(design III.2, rulings R7/R10/R11).

API shapes below were verified against the installed versions, not assumed
from the phase sketch (qdrant-client 1.18.0, flashrank 0.2.10) -- see
plans/mitya-rag/05-impl/phase-4.md for what was actually read.

`client`, `embedder`, `bm25`, `ranker` are all passed in by the caller
(constructed once, e.g. in the FastAPI lifespan) rather than held as module
globals -- matches src/rag/ingest.py's dependency-injection style, and avoids
a module import silently triggering a FlashRank model download.
"""

import logging

from flashrank import Ranker, RerankRequest
from qdrant_client import QdrantClient, models

from src.config import Config

logger = logging.getLogger(__name__)

# nomic-embed-text-v1.5 needs an explicit task-instruction prefix; fastembed
# never inserts it itself (verified: src/rag/ingest.py's _DOC_PREFIX comment
# records grepping fastembed/text/*.py and fastembed/common/*.py for
# "search_document"/"search_query" -- no hits). The document side used
# "search_document: " at ingest (src/rag/ingest.py _DOC_PREFIX); the query
# side must use the matching "search_query: " prefix or dense retrieval
# silently degrades (risk RR5).
_QUERY_PREFIX = "search_query: "


def build_ranker() -> Ranker:
    """Construct the FlashRank Ranker (downloads/loads the ONNX model on
    first use -- call once at startup, not per-request)."""
    return Ranker(model_name=Config.RERANK_MODEL)


def build_hard_filter(filters: dict | None) -> models.Filter | None:
    """Build a Qdrant `must` filter from a UI-supplied book/part/chapter dict.

    Book titles are resolved through Config.resolve_book_title() first --
    users say "Demons"/"The Devils", the corpus carries "The Possessed". An
    unmapped title is rejected loudly here (raises ValueError) rather than
    passed through: a wrong title becomes a filter matching zero points, a
    silent empty result that is worse than a miss.
    """
    if not filters:
        return None
    conditions: list[models.FieldCondition] = []
    if filters.get("book") is not None:
        canonical_book = Config.resolve_book_title(filters["book"])
        conditions.append(models.FieldCondition(key="book", match=models.MatchValue(value=canonical_book)))
    if filters.get("part") is not None:
        part = _coerce_int(filters["part"], "part")
        conditions.append(models.FieldCondition(key="part", match=models.MatchValue(value=part)))
    if filters.get("chapter") is not None:
        chapter = _coerce_int(filters["chapter"], "chapter")
        conditions.append(models.FieldCondition(key="chapter", match=models.MatchValue(value=chapter)))
    if not conditions:
        return None
    return models.Filter(must=conditions)


def _coerce_int(value, field_name: str) -> int:
    """Coerce a filter value to int; raise loudly on anything non-integral.

    D2 fix: `part`/`chapter` payload fields in Qdrant are ints, but the UI
    filter dict may hand them over as strings (JSON query params, form
    inputs) -- `MatchValue(value="1")` against an integer payload field
    matches zero points, both prefetch legs return nothing, and the request
    silently yields empty context. Matches Config.resolve_book_title()'s
    loud-reject contract: raise ValueError rather than pass a
    silently-wrong type through.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field_name} filter must be an integer, got bool: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ValueError(f"{field_name} filter is not an integer: {value!r}")


def retrieve(
    client: QdrantClient,
    embedder,
    bm25,
    query_expanded: str,
    hard_filter: models.Filter | None,
    legs: tuple[str, ...] = ("dense", "bm25"),
) -> list[models.ScoredPoint]:
    """Hybrid retrieve: dense (nomic) + sparse (BM25) prefetch, fused
    server-side with native RRF (ruling R7 -- no client-side merge, internal
    k not tunable; per-leg prefetch limits are the actual knobs). Returns the
    fused top Config.TOP_K_FUSED points, NOT reranked -- call rerank()
    separately.

    `legs` exists only for the retrieval ablation (plans/mitya-ablation/plan.md):
    dropping a leg is how the eval attributes recall to dense vs sparse. The
    default is the shipped hybrid config, so no production caller passes it.
    RRF over a single prefetch list is order-preserving, so a one-leg run is a
    valid query rather than a degenerate one.
    """
    prefetch: list[models.Prefetch] = []

    if "dense" in legs:
        # fastembed's TextEmbedding.embed() never auto-prefixes (see module
        # docstring); .tolist() matches src/rag/ingest.py's embed_dense().
        dense_vec = list(embedder.embed([_QUERY_PREFIX + query_expanded]))[0].tolist()
        prefetch.append(
            models.Prefetch(
                query=dense_vec,
                using="dense",
                limit=Config.PREFETCH_LIMIT,
                filter=hard_filter,
            )
        )

    if "bm25" in legs:
        # query_embed() (not embed()) is deliberate -- confirmed via
        # fastembed/sparse/bm25.py and src/rag/ingest.py's embed_sparse()
        # comment: .embed() (document/ingest side) emits raw TF weights meant
        # to combine with the collection's server-side Modifier.IDF;
        # .query_embed() (query side) instead assigns a flat weight of 1.0 per
        # token. The two are intentionally asymmetric -- using .embed() here
        # would be wrong, not a style choice.
        sparse_vec = list(bm25.query_embed(query_expanded))[0]
        prefetch.append(
            models.Prefetch(
                # SparseVector requires List[int]/List[float] (pydantic field
                # types, verified against qdrant_client.http.models); fastembed
                # returns numpy arrays, so .tolist() is required -- mirrors the
                # exact conversion src/rag/ingest.py's upsert_chunks() already
                # does for the document side.
                query=models.SparseVector(
                    indices=sparse_vec.indices.tolist(),
                    values=sparse_vec.values.tolist(),
                ),
                using="bm25",
                limit=Config.PREFETCH_LIMIT,
                filter=hard_filter,
            )
        )

    if not prefetch:
        raise ValueError(f"retrieve() needs at least one leg, got legs={legs!r}")

    response = client.query_points(
        Config.QDRANT_COLLECTION,
        prefetch=prefetch,
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=Config.TOP_K_FUSED,
        with_payload=True,
    )
    points = response.points
    logger.info(
        "retrieve: fused %d points (legs=%s, prefetch_limit=%d, top_k_fused=%d, filtered=%s)",
        len(points), ",".join(legs), Config.PREFETCH_LIMIT, Config.TOP_K_FUSED,
        hard_filter is not None,
    )
    return points


def rerank(query: str, points: list[models.ScoredPoint], ranker: Ranker) -> list[models.ScoredPoint]:
    """FlashRank rerank: fused top-K -> Config.TOP_K_RERANK (3). Always on;
    RERANK_ENABLED is a static kill-switch only, no runtime latency governor
    (ruling R11 -- 25 pairs on CPU is ~0.3-0.5s, inside budget)."""
    if not Config.RERANK_ENABLED:
        return points[:Config.TOP_K_RERANK]
    if not points:
        return points

    passages = [{"id": p.id, "text": (p.payload or {}).get("text", ""), "meta": p.payload} for p in points]
    ranked = ranker.rerank(RerankRequest(query=query, passages=passages))

    # Ranker.rerank() (verified: .venv/.../flashrank/Ranker.py) returns the
    # same passage dicts sorted by score, not the original ScoredPoint
    # objects -- reattach the source ScoredPoint so context.py keeps working
    # with a consistent point type.
    by_id = {p.id: p for p in points}
    top = [by_id[r["id"]] for r in ranked[:Config.TOP_K_RERANK]]
    logger.info("rerank: %d -> %d (model=%s)", len(points), len(top), Config.RERANK_MODEL)
    return top


def retrieve_structural_excerpts(
    client: QdrantClient,
    embedder,
    query: str,
    book: str,
    part: int,
    chapter: int,
    limit: int = Config.TOP_K_STRUCTURAL_EXCERPTS,
) -> list[models.ScoredPoint]:
    """Dense-only retrieval hard-filtered to one chapter -- design III.3
    STRUCTURAL path: chapter summary + top-2 dense excerpts. No BM25 leg, no
    rerank: the chapter is already fully identified by the hard filter (the
    router already resolved it against structure.json), so fusion/rerank buy
    nothing here; a plain dense nearest-neighbour search against the original
    query picks the most relevant excerpts within it.

    Not in the phase-4 sketch verbatim -- added because context.py's
    STRUCTURAL branch (per its documented signature) only assembles
    already-fetched points, and something has to produce them. See
    plans/mitya-rag/05-impl/phase-4.md deviations.
    """
    hard_filter = models.Filter(
        must=[
            models.FieldCondition(key="book", match=models.MatchValue(value=book)),
            models.FieldCondition(key="part", match=models.MatchValue(value=part)),
            models.FieldCondition(key="chapter", match=models.MatchValue(value=chapter)),
        ]
    )
    dense_vec = list(embedder.embed([_QUERY_PREFIX + query]))[0].tolist()
    response = client.query_points(
        Config.QDRANT_COLLECTION,
        query=dense_vec,
        using="dense",
        query_filter=hard_filter,  # top-level param name (verified: differs from Prefetch's `filter`)
        limit=limit,
        with_payload=True,
    )
    return response.points
