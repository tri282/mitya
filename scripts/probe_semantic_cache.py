"""P-7 — is an L2 semantic retrieval cache separable on this corpus? (R49)

The question, stated so it can fail: is there a cosine threshold T such that
every pair of queries that MUST NOT share retrieved chunks scores below T,
while genuine paraphrases -- which a semantic cache exists to serve -- score
above it? If the two populations overlap, no threshold exists, L2 is refuted,
and CP-delta records it.

Three things this probe is careful about.

**It tests the population L2 actually serves.** L2 is DEFAULT-only (R49): a
STRUCTURAL query is hard-filtered to a resolved (book, part, chapter) scope and
never reaches the cache. So every row is routed through the real `route()` and
STRUCTURAL rows are excluded from the threshold argument -- reported
separately, as the containment check they are, not as evidence about L2.

**It embeds queries exactly as retrieval does.** nomic-embed-text-v1.5 needs
the "search_query: " task prefix (src/rag/retrieve.py:30); without it the
cosines are real numbers about a different vector space than the one the system
uses.

**Its must-hit class is authored here, not in the eval set.** The eval set is
authored for coverage, so it contains almost no genuine paraphrase pairs --
distinct queries are the point of it (the same construction R47 flags for hit
rate). Measuring only the must-miss side would report "nothing collides!" while
saying nothing about whether a cache would ever hit. The paraphrases below are
therefore written for this probe and live in this file, NOT appended to
test/retrieval_eval.json: they are probe inputs, not eval rows, and A9's
append-only discipline is not a thing to route around. Their provenance is
[authored], and the finding must say so.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import sys
from dataclasses import dataclass

import numpy as np
from fastembed import TextEmbedding

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config  # noqa: E402
from src.rag.metrics import family_of  # noqa: E402
from src.rag.retrieve import _QUERY_PREFIX  # noqa: E402
from src.rag.router import route  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("probe_semantic_cache")

# Must-hit class. Each entry paraphrases an existing DEFAULT-routed eval row
# without changing the information need: a cache that cannot serve these has no
# reason to exist. Deliberately varied in shape -- word order, register,
# synonym, question form -- rather than five rewordings of one trick.
PARAPHRASES: list[tuple[str, str]] = [
    ("r-0001", "How does the scene where Raskolnikov murders the old pawnbroker with an axe unfold?"),
    ("r-0002", "What happens when Raskolnikov finally admits the killing to Sonia?"),
    ("r-0090", "In The Idiot, how does the text portray the epileptic fit?"),
    ("r-0092", "In Crime and Punishment, who kills himself, and by what means?"),
    ("r-0096", "In Crime and Punishment, what effect does a father being publicly shamed have on his household?"),
]

# Must-miss class, taken from the eval set rather than authored: the five
# matched pairs of `book_named_no_chapter` (R6/step 7). Same question shape,
# different novel, disjoint targets -- the exact adversary an embedding-only
# cache is weak against, and DEFAULT-routed, so L2 would see them.
MATCHED_BOOK_PAIRS: list[tuple[str, str]] = [
    ("r-0090", "r-0091"),  # epileptic seizure: The Idiot / Karamazov
    ("r-0092", "r-0093"),  # suicide: C&P / The Possessed
    ("r-0094", "r-0095"),  # duel: Notes from the Underground / The Possessed
    ("r-0096", "r-0097"),  # father's humiliation: C&P / Karamazov
    ("r-0098", "r-0099"),  # confession before a hostile audience: The Idiot / Karamazov
]


@dataclass(frozen=True)
class Row:
    id: str
    query: str
    family: str
    route: str
    targets: tuple[tuple[str, int | None, int | None], ...]


def _targets(row: dict) -> tuple[tuple[str, int | None, int | None], ...]:
    return tuple(
        (t["book"], t.get("part"), t.get("chapter")) for t in row.get("targets", [])
    )


def load_rows() -> list[Row]:
    rows = json.load(open(os.path.join("test", "retrieval_eval.json")))
    structure = json.load(open(os.path.join(Config.RAG_DIR, "structure.json")))
    summaries = json.load(open(os.path.join(Config.RAG_DIR, "summaries.json")))
    out = []
    for r in rows:
        # filters=None: the eval harness's DEFAULT path passes no hard filter,
        # so this is the route a cached request would actually carry (R19).
        route_name, _ = route(r["query"], None, structure, summaries)
        out.append(
            Row(
                id=r["id"],
                query=r["query"],
                family=r.get("family") or family_of(r),
                route=route_name,
                targets=_targets(r),
            )
        )
    return out


def embed(embedder: TextEmbedding, texts: list[str]) -> np.ndarray:
    vecs = np.array([v for v in embedder.embed([_QUERY_PREFIX + t for t in texts])])
    # Cosine via dot product on unit vectors -- one normalize, not one per pair.
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def main() -> int:
    rows = load_rows()
    by_id = {r.id: r for r in rows}
    default_rows = [r for r in rows if r.route == "DEFAULT"]
    logger.info(
        "%d eval rows: %d DEFAULT (L2's population), %d STRUCTURAL (never reaches L2)",
        len(rows), len(default_rows), len(rows) - len(default_rows),
    )

    embedder = TextEmbedding(model_name=Config.EMBED_MODEL)

    # --- must-hit: authored paraphrase vs its source row ---------------------
    para_texts = [p for _, p in PARAPHRASES]
    para_src = [by_id[i].query for i, _ in PARAPHRASES]
    pv, sv = embed(embedder, para_texts), embed(embedder, para_src)
    must_hit = [(PARAPHRASES[k][0], float(pv[k] @ sv[k])) for k in range(len(PARAPHRASES))]

    print("\n=== MUST HIT: authored paraphrase vs source row (higher is better) ===")
    for rid, c in must_hit:
        flag = "" if by_id[rid].route == "DEFAULT" else f"  [!] source routes {by_id[rid].route}"
        print(f"  {rid}  cos={c:.4f}{flag}")
    min_hit = min(c for _, c in must_hit)

    # --- must-miss: the five matched book pairs ------------------------------
    pair_a = embed(embedder, [by_id[a].query for a, _ in MATCHED_BOOK_PAIRS])
    pair_b = embed(embedder, [by_id[b].query for _, b in MATCHED_BOOK_PAIRS])
    must_miss = [
        (a, b, float(pair_a[k] @ pair_b[k])) for k, (a, b) in enumerate(MATCHED_BOOK_PAIRS)
    ]

    print("\n=== MUST MISS: matched pairs, same question, different novel (lower is better) ===")
    for a, b, c in must_miss:
        print(f"  {a}/{b}  cos={c:.4f}")
    max_miss_matched = max(c for _, _, c in must_miss)

    # --- must-miss, whole population: every DEFAULT pair with disjoint targets
    # A matched pair is the adversary we anticipated; this is the one we did
    # not. If any unrelated DEFAULT pair outscores the paraphrases, the
    # threshold is dead regardless of how the matched pairs behave.
    # Rows with NO targets are refusal rows (adversarial/sensitive families):
    # the correct behavior is a refusal, not a retrieval, so there is no
    # "chunks that must not be reused" claim to make about them. They are
    # excluded, not treated as disjoint from everything -- an empty set
    # intersects nothing, which silently made every refusal row collide with
    # all 94 DEFAULT rows. That defect produced this probe's first verdict.
    scored = [r for r in default_rows if r.targets]
    dv = embed(embedder, [r.query for r in scored])
    worst: list[tuple[str, str, float]] = []
    for i, j in itertools.combinations(range(len(scored)), 2):
        ri, rj = scored[i], scored[j]
        if set(ri.targets) & set(rj.targets):
            continue  # shares a target: a hit here is not a defect
        worst.append((ri.id, rj.id, float(dv[i] @ dv[j])))
    worst.sort(key=lambda t: -t[2])

    print(f"\n=== MUST MISS: worst 10 disjoint-target DEFAULT pairs "
          f"({len(scored)} retrieval-scored rows; "
          f"{len(default_rows) - len(scored)} refusal rows excluded) ===")
    for a, b, c in worst[:10]:
        print(f"  {a}/{b}  cos={c:.4f}")
        print(f"      {by_id[a].query[:88]}")
        print(f"      {by_id[b].query[:88]}")
    max_miss_all = worst[0][2]

    # --- containment check: STRUCTURAL, reported but not part of the argument
    struct = [r for r in rows if r.route == "STRUCTURAL"]
    if len(struct) > 1:
        sv2 = embed(embedder, [r.query for r in struct])
        sc = [
            (struct[i].id, struct[j].id, float(sv2[i] @ sv2[j]))
            for i, j in itertools.combinations(range(len(struct)), 2)
            if set(struct[i].targets) & set(struct[j].targets) == set()
        ]
        sc.sort(key=lambda t: -t[2])
        print("\n=== CONTAINMENT (not part of the threshold argument): worst 5 STRUCTURAL pairs ===")
        print("    L2 never serves these -- the router hard-filters them. Shown to size the")
        print("    hazard that DEFAULT-only exists to avoid.")
        for a, b, c in sc[:5]:
            print(f"  {a}/{b}  cos={c:.4f}  {by_id[a].query[:60]!r} vs {by_id[b].query[:60]!r}")

    # --- verdict -------------------------------------------------------------
    margin = min_hit - max_miss_all
    print("\n=== VERDICT ===")
    print(f"  min(must-hit)            = {min_hit:.4f}")
    print(f"  max(must-miss, matched)  = {max_miss_matched:.4f}")
    print(f"  max(must-miss, all pairs)= {max_miss_all:.4f}")
    print(f"  separation margin        = {margin:+.4f}")
    if margin <= 0:
        print("  REFUTED: the populations overlap. No threshold serves paraphrases")
        print("  without also serving a pair that must not share chunks.")
    elif margin < 0.05:
        print("  REFUTED ON MARGIN: a threshold exists but there is no room in it.")
        print("  R49/CP-delta: separation with no margin is a refutation, not a pass.")
    else:
        lo, hi = max_miss_all, min_hit
        print(f"  SEPARABLE: any T in ({lo:.4f}, {hi:.4f}) works. Suggested T = {(lo + hi) / 2:.4f}")
    print("\n  Provenance: must-miss rows [measured] from test/retrieval_eval.json;")
    print("  must-hit paraphrases [authored] in this file for this probe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
