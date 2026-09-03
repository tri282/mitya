"""Assemble the prompt-context block + deduped Sources list (design III.3, R20).

DEFAULT: top-3 reranked chunks. STRUCTURAL: chapter summary + top-2 dense
excerpts hard-filtered to that chapter. Sources are deduped on
(book, part, chapter), preserving rank order (top chunks often share a
chapter); STRUCTURAL lists the summarized chapter first.

Pure formatter -- does not retrieve anything itself. `points` must already be
the caller's chosen set for the route: retrieve.retrieve()+rerank() output
for DEFAULT, retrieve.retrieve_structural_excerpts() output for STRUCTURAL.
"""

import logging
from dataclasses import dataclass

from src.limits import estimate_tokens

from src.rag.ingest import BOOK_META

logger = logging.getLogger(__name__)


def build_context(
    route: str,
    points: list,
    summaries: dict,
    scope: dict | None,
) -> tuple[str, list[dict]]:
    """Build (prompt_context_block, deduped_sources) for the given route."""
    if route == "STRUCTURAL" and scope is not None:
        return _build_structural(points, summaries, scope)
    return _build_default(points)


def _build_default(points: list) -> tuple[str, list[dict]]:
    passages: list[str] = []
    sources: list[dict] = []
    seen: set[tuple] = set()
    for i, point in enumerate(points, start=1):
        payload = point.payload or {}
        passages.append(f'[{i}] ({_label(payload)}) "{payload.get("text", "")}"')
        key = (payload.get("book"), payload.get("part"), payload.get("chapter"))
        if key not in seen:
            seen.add(key)
            sources.append({"book": key[0], "part": key[1], "chapter": key[2]})
    return "\n".join(passages), sources


def _build_structural(points: list, summaries: dict, scope: dict) -> tuple[str, list[dict]]:
    summary_key = f"{scope['book']}|{scope['part']}|{scope['chapter']}"
    summary = summaries.get(summary_key)
    if summary is None:
        # R15 (D4 fix): router.route() now validates the scope against
        # summaries.json before ever returning STRUCTURAL, so this branch
        # should be unreachable in the normal flow -- kept as defense in
        # depth in case a caller builds a scope some other way. Fall back to
        # the DEFAULT rendering of `points` rather than fabricating an empty
        # quotation and citing a chapter that contributed nothing.
        logger.error(
            "build_context: STRUCTURAL scope %s has no summary at key %r -- "
            "falling back to DEFAULT rendering (R15)", scope, summary_key,
        )
        return _build_default(points)

    passages = [f'[1] ({_label(scope)}) "{summary}"']
    scope_key = (scope["book"], scope["part"], scope["chapter"])
    sources = [{"book": scope_key[0], "part": scope_key[1], "chapter": scope_key[2]}]
    seen = {scope_key}

    for i, point in enumerate(points, start=2):
        payload = point.payload or {}
        passages.append(f'[{i}] ({_label(payload)}) "{payload.get("text", "")}"')
        key = (payload.get("book"), payload.get("part"), payload.get("chapter"))
        if key not in seen:
            seen.add(key)
            sources.append({"book": key[0], "part": key[1], "chapter": key[2]})

    return "\n".join(passages), sources


def _label(entry: dict) -> str:
    """"(Book Title, Part N, Chapter M)" -- part_label ("Part"/"Book") comes
    from src.rag.ingest.BOOK_META so Brothers Karamazov reads "Book 5" not
    "Part 5", matching design III.4's example verbatim."""
    book = entry.get("book")
    part_label = BOOK_META.get(book, {}).get("part_label", "Part")
    return f"{book}, {part_label} {entry.get('part')}, Chapter {entry.get('chapter')}"


# --- neighbour expansion (v2 P7 Batch H; D-13, D-14) -------------------------
#
# Retrieval hands back `k` disjoint 500-token windows that are often literally
# consecutive in the novel, with the shared `chunk_overlap` duplicated. This
# fetches the chunks either side of each hit BY POINT ID and stitches them into
# contiguous prose.
#
# Point ids are ints in "global, deterministic parse order"
# (src/rag/ingest.py:334) -- so id+/-1 is the adjacent chunk in reading order,
# and ids run ACROSS chapter boundaries. That is why rule 1 below is a
# correctness requirement and not a quality one.


@dataclass(frozen=True)
class Span:
    """One merged run of windows, rendered once. Quacks like a ScoredPoint for
    `build_context()`, which only ever reads `.payload`."""

    payload: dict


def _stitch(texts: list[str]) -> str:
    """Join consecutive window texts, dropping the duplicated overlap.

    Measured on the STRINGS rather than computed from `chunk_overlap`: the
    dedup IS this arm's cost mechanism, so deriving it from window arithmetic
    would be estimating the quantity under test. That is R136's mis-enforced
    ceiling (`memory_budget_tokens`, ~56 tok + ~4/turn) and this batch does not
    build the second one.
    """
    out = texts[0]
    for nxt in texts[1:]:
        # longest suffix of `out` that prefixes `nxt`; bounded by the shorter
        # string, so an unexpected overlap size costs correctness nothing.
        limit = min(len(out), len(nxt))
        overlap = 0
        for size in range(limit, 0, -1):
            if out.endswith(nxt[:size]):
                overlap = size
                break
        out += nxt[overlap:]
    return out


def _chapter_of(payload: dict) -> tuple:
    return (payload.get("book"), payload.get("part"), payload.get("chapter"))


def expand_spans(client, collection, hits: list, pool: list, radius: int,
                 max_span_tokens: int, policy: str,
                 max_context_tokens: int | None = None) -> list[Span]:
    """D-14's five rules, in enforcement order. `hits` is the top-k slice;
    `pool` is the full reranked list, used only by BACKFILL."""
    # policy "off" is the disabled state, NOT radius 0. The two are independent
    # and conflating them hid an arm: radius 0 with merging still ON is
    # MERGE-ONLY -- adjacent hits collapse and their duplicated `chunk_overlap`
    # is dropped, adding nothing. It is strictly <= baseline in tokens, and
    # provably invariant on chapter-level metrics because a merge is same-chapter
    # by construction. It is the only variant that can register as a cost
    # IMPROVEMENT rather than a BREAK (src/rag/gate.py:312).
    if policy == "off" or not hits:
        return [Span(dict(p.payload or {})) for p in hits]
    radius = max(0, radius)

    k = len(hits)
    admitted = list(hits)
    seen_ids = {p.id for p in admitted}
    spans, refilled = _spans_from(client, collection, admitted, radius, max_span_tokens), 0

    # RULE 5: BACKFILL refills in ONE pass over the ranked pool. No iteration --
    # a fixed point would let each refill trigger another merge and another
    # refill. A candidate is kept ONLY if it raises the span count: the freed
    # slot is for DISTINCT material, and a next-rank chunk that merges into an
    # existing span has filled nothing. Skipping it is not a special case, it is
    # what "pull the next rank into the freed slot" means.
    # ponytail: single pass; iterate only if a measured case shows slots left empty.
    if policy == "backfill" and len(spans) < k:
        for cand in pool:
            if len(spans) >= k:
                break
            if cand.id in seen_ids:
                continue
            seen_ids.add(cand.id)
            trial = _spans_from(client, collection, admitted + [cand], radius, max_span_tokens)
            if len(trial) <= len(spans):
                continue                    # merged into an existing span: no slot filled
            if max_context_tokens is not None and _total_tokens(trial) > max_context_tokens:
                break                       # the budget is the other stop condition
            admitted.append(cand)
            spans = trial
            refilled += 1
    if refilled:
        logger.info("expand_spans: policy=backfill refilled=%d spans=%d k=%d", refilled, len(spans), k)
    return spans


def _total_tokens(spans: list[Span]) -> int:
    return sum(estimate_tokens(s.payload.get("text", "")) for s in spans)


def _spans_from(client, collection, hits: list, radius: int, max_span_tokens: int) -> list[Span]:
    by_id = {p.id: dict(p.payload or {}) for p in hits}
    hit_ids = set(by_id)

    # RULE 1: payload match prunes FIRST. A neighbour in a different
    # (book, part, chapter) is never admitted -- point ids run across chapter
    # boundaries and splicing makes the programmatic Sources list lie. It is
    # also what bounds the runaway structurally: a span cannot exceed a chapter.
    wanted = {n for pid in hit_ids for n in range(pid - radius, pid + radius + 1)} - hit_ids
    fetched = {}
    if wanted:
        for pt in client.retrieve(collection_name=collection, ids=sorted(wanted), with_payload=True):
            fetched[pt.id] = dict(pt.payload or {})

    admitted: dict[int, dict] = dict(by_id)
    for pid in sorted(hit_ids):
        for nid in range(pid - radius, pid + radius + 1):
            if nid in admitted or nid not in fetched:
                continue
            if _chapter_of(fetched[nid]) == _chapter_of(by_id[pid]):
                admitted[nid] = fetched[nid]

    # RULE 2: merge is DEFINED -- interval union, then transitive closure of
    # overlapping-or-TOUCHING runs. Deterministic, not left to the caller.
    # Consecutive ids merge ONLY within one (book, part, chapter). Rule 1 prunes
    # NEIGHBOURS across a boundary, and that is not sufficient on its own: two
    # top-k HITS can themselves be adjacent ids in different chapters (ids run
    # continuously across the boundary), and merging those would render one span
    # of two chapters' prose while its payload claims a single chapter -- the
    # citation lie the guard exists to prevent, arriving by the one door the
    # guard did not cover.
    runs: list[list[int]] = []
    for pid in sorted(admitted):
        if (runs and pid == runs[-1][-1] + 1
                and _chapter_of(admitted[pid]) == _chapter_of(admitted[runs[-1][-1]])):
            runs[-1].append(pid)
        else:
            runs.append([pid])

    spans: list[Span] = []
    for run in runs:
        # RULE 3: cap each span, dropping EXPANDED NEIGHBOURS from the ends.
        # A hit is never evicted by expansion, so `k` keeps meaning what the
        # arm says it means.
        while True:
            text = _stitch([admitted[i] for i in run] and [admitted[i]["text"] for i in run])
            # RULE 4: measured on the RENDERED text, never computed from window
            # arithmetic.
            if estimate_tokens(text) <= max_span_tokens:
                break
            ends = [i for i in (run[0], run[-1]) if i not in hit_ids]
            if not ends:
                break                      # all-hit span: nothing evictable, keep it
            run.remove(ends[0] if run[0] in ends else ends[-1])
        head = admitted[[i for i in run if i in hit_ids][0]]
        spans.append(Span({**head, "text": text}))
    # spans inherit their first hit's rank order
    return spans


def _demo() -> None:
    """The one runnable check. Asserts each of D-14's five rules."""
    class _P:
        def __init__(self, i, ch, text): self.id, self.payload = i, {"book": "B", "part": 1, "chapter": ch, "text": text}

    class _Client:
        def __init__(self, pts): self._p = {p.id: p for p in pts}
        def retrieve(self, collection_name, ids, with_payload=True):
            return [self._p[i] for i in ids if i in self._p]

    # windows share a 10-char overlap, so the stitch must drop it
    W = {i: f"<{i:02d}>" + ("x" * 40) + f"[tail{i:02d}]" for i in range(10, 22)}
    for i in range(10, 21):
        W[i + 1] = W[i][-10:] + W[i + 1]
    pts = [_P(i, 1 if i < 19 else 2, W[i]) for i in range(10, 22)]   # chapter flips at 19
    client = _Client(pts)

    # RULE 2 + the runaway case: hits 13,14,15 and 16,17 close into ONE span
    hits = [p for p in pts if p.id in (13, 14, 15, 16, 17)]
    spans = expand_spans(client, "c", hits, pts, 1, 10_000, "align_only")
    assert len(spans) == 1, f"transitive closure failed: {len(spans)} spans"

    # RULE 1: a neighbour across the chapter boundary is refused
    hits = [p for p in pts if p.id == 18]
    span = expand_spans(client, "c", hits, pts, 1, 10_000, "align_only")[0]
    assert "<19>" not in span.payload["text"], "cross-chapter neighbour admitted"
    assert "<17>" in span.payload["text"], "same-chapter neighbour refused"

    # RULE 1, the hit-hit case: two ADJACENT HITS in different chapters must not
    # merge, even though neither is a pruned neighbour
    hits = [p for p in pts if p.id in (18, 19)]
    out = expand_spans(client, "c", hits, pts, 1, 10_000, "align_only")
    assert len(out) == 2, f"adjacent hits across a chapter boundary merged: {len(out)} span(s)"
    assert {s.payload["chapter"] for s in out} == {1, 2}, "merged span misreports its chapter"

    # RULE 3 + 4: the cap evicts an expanded end, never the hit, measured rendered
    hits = [p for p in pts if p.id == 14]
    wide = expand_spans(client, "c", hits, pts, 2, 10_000, "align_only")[0].payload["text"]
    tight = expand_spans(client, "c", hits, pts, 2, estimate_tokens(wide) - 5, "align_only")[0].payload["text"]
    assert len(tight) < len(wide) and "<14>" in tight, "cap evicted the hit or did nothing"

    # RULE 5 + policy: two adjacent hits merge to one span; backfill refills, align-only does not.
    # The pool is RANK-ordered in production, so the next rank sits elsewhere in id
    # space -- here 12, 13 (adjacent, they merge) then 20 (far away, a real slot).
    hits = [p for p in pts if p.id in (12, 13)]
    ranked = [p for p in pts if p.id in (12, 13, 20, 11)]
    assert len(expand_spans(client, "c", hits, ranked, 1, 10_000, "align_only")) == 1
    assert len(expand_spans(client, "c", hits, ranked, 1, 10_000, "backfill")) == 2
    # the budget is the OTHER stop condition: a budget below the merged span's own
    # size admits no refill at all
    only = expand_spans(client, "c", hits, ranked, 0, 10_000, "backfill", max_context_tokens=1)
    assert len(only) == 1, "backfill ignored max_context_tokens"

    # a next-rank candidate that merges into the existing span fills no slot
    assert len(expand_spans(client, "c", hits, [p for p in pts if p.id in (12, 13, 11)], 1,
                            10_000, "backfill")) == 1

    # policy "off" is byte-identical passthrough at any radius
    assert [s.payload["text"] for s in expand_spans(client, "c", hits, ranked, 1, 10_000, "off")] \
        == [p.payload["text"] for p in hits]

    # MERGE-ONLY: radius 0 with merging on. Adjacent hits collapse, overlap is
    # dropped, NOTHING is added -- so it must be strictly cheaper than passthrough
    # and must not change the chapter set.
    merged = expand_spans(client, "c", hits, ranked, 0, 10_000, "align_only")
    plain = expand_spans(client, "c", hits, ranked, 1, 10_000, "off")
    assert len(merged) == 1 and len(plain) == 2, (len(merged), len(plain))
    assert sum(estimate_tokens(s.payload["text"]) for s in merged) \
        < sum(estimate_tokens(s.payload["text"]) for s in plain), "merge-only did not save tokens"
    assert {s.payload["chapter"] for s in merged} == {s.payload["chapter"] for s in plain}
    print("expand_spans: all five rules hold")


if __name__ == "__main__":
    _demo()
