"""Tier 1 metric roster (mitya-v2 P0, charter §II, R7/R10/R11/R12). Every
function here is pure, deterministic, and calls no network and no judge.

That does NOT make them all runnable in the P3 gate unattended, and the
earlier version of this docstring claimed it did. A7's judge/no-judge line is
necessary and not sufficient: `source_leak_rate` and `assistant_tell_rate` are
regexes over *answers*, and an answer is a metered generation. The criterion
that matters to the gate is whether a metric's INPUT exists without a provider
call -- see TIER_1A / TIER_1B below (P3, F1).

Retrieval metrics (`distinct_target_chapters_hit_at_k`, `precision_at_k`)
operate on plain payload dicts, not `qdrant_client` point objects -- callers
pass `[p.payload or {} for p in points]` (see scripts/eval_retrieval.py's
`_rank_of` for the same contract). Keeping qdrant_client out of this module
is what lets it be imported and unit-tested without a running Qdrant.

Regex patterns for `source_leak_rate` / `assistant_tell_rate` were tightened
against the 72 v1 answers in `plans/mitya-rag/05-impl/quality_ab_results.json`
(P0 step 10) -- see `plans/mitya-v2/05-impl/phase-0.md` for the before/after
rates and the specific false positives removed.
"""

import logging
import math
import re
import statistics

logger = logging.getLogger(__name__)

_ChapterTuple = tuple[object, object, object]

# --- row families (mitya-v2 P0, charter §III CP0) -----------------------------
# Every metric is a fraction, and a fraction needs to know which rows are in its
# denominator. v1 had one boolean (`negative_control`) and therefore two buckets;
# the four families P0 adds each need their own, because mixing denominators is
# precisely what produced the 0.731 -> 0.720 correction (R13) and must not be
# rebuilt at 6x the scale.
FAMILIES = frozenset({
    "primary",                  # headline recall@k / MRR / distinct-chapters-hit@k
    "negative_control",         # reported separately, never in the headline (R14)
    "entity_pair",              # paired translations; entity_fold_rate's denominator (R13)
    "sensitive",                # sensitive-but-legitimate; false-refusal denominator
    "adversarial",              # correct-refusal denominator
    "book_named_no_chapter",    # the organic in-prompt scoping gap (R6)
    "structural",               # STRUCTURAL-routed; own denominator (R19)
    "quotation",                # verbatim quoted span in the query; own denominator (R73)
})

# Families whose rows have a correct chapter and are therefore scored for
# retrieval accuracy. The refusal families deliberately carry `targets: []` --
# they go through retrieval to produce a score distribution for R15's threshold
# calibration (P0 step 14), but there is no "right chapter" to find, so scoring
# them would pollute recall's denominator with guaranteed misses.
RETRIEVAL_SCORED_FAMILIES = frozenset({
    "primary", "negative_control", "entity_pair", "book_named_no_chapter",
    "structural", "quotation",
})

# `structural` is scored, but its hit is close to tautological: the router
# resolves the chapter and retrieve_structural_excerpts() hard-filters to it, so
# the target is found unless the plumbing itself is broken. That is worth
# measuring -- it is the STRUCTURAL path's end-to-end tripwire -- but it is not
# ranking quality, and mixing it into the headline would inflate recall with
# rows that cannot miss. Hence its own denominator (R19).


# --- gate tiers (mitya-v2 P3, F1) --------------------------------------------
# The criterion is NOT purity and NOT "does it call a judge" (A7's line, which
# is necessary and not sufficient). It is: does this metric's input exist
# without a provider call. source_leak_rate is a regex over a string -- and
# that string is a metered generation, so it cannot run unattended on every
# gate run. Names are the report schema's metric keys (P3 step 5), so the two
# files cannot drift apart silently.
TIER_1A = frozenset({
    "recall_at_k", "mrr", "distinct_target_chapters_hit_at_k", "precision_at_k",
    "entity_fold_rate", "route_match", "determinism_spread", "family_denominators",
    "computed_cost_usd", "config_version", "corpus_version",
})
TIER_1B = frozenset({
    "source_leak_rate", "assistant_tell_rate", "style_distance",
    "latency_ms", "measured_tokens", "measured_cost_usd",
})

# Public callables here that are NOT gate metrics. The coverage check in
# _demo() subtracts these and requires everything left to be classified, so a
# metric added to neither tier fails the self-check instead of silently
# running in the wrong leg (or not at all).
_NOT_A_METRIC = frozenset({"family_of", "extract_style_features"})


def family_of(row: dict) -> str:
    """The row's denominator family, derived so the pre-family rows need no edit.

    The 27 rows committed at P0 step 4 carry no `family` key and must never gain
    one -- A9 makes them immutable. They are classified by the `negative_control`
    boolean they already have, which is exactly the two-bucket split v1 used.

    Two of those legacy rows (r-0024, r-0025) declare `expected_route:
    STRUCTURAL`: they are hard-filtered to a resolved chapter, return rank 1 in
    every arm, and are therefore incapable of discordance in any paired test
    (R72). Deriving them into `primary` inflated the headline denominator with
    guaranteed hits -- the 0.731 failure mode in a new costume (R19). The route
    the row already declares decides the family, so the file still needs no
    edit. Corrected 2026-08-29, Q4 / `plans/mitya-v2/02-decide/decisions.md`.
    """
    family = row.get("family")
    if family:
        return family
    if row.get("expected_route") == "STRUCTURAL":
        return "structural"
    return "negative_control" if row.get("negative_control") else "primary"


def _target_tuples(targets: list[dict]) -> set[_ChapterTuple]:
    return {(t["book"], t["part"], t["chapter"]) for t in targets}


def _payload_tuple(payload: dict) -> _ChapterTuple:
    return (payload.get("book"), payload.get("part"), payload.get("chapter"))


def distinct_target_chapters_hit_at_k(payloads: list[dict], targets: list[dict]) -> float:
    """Headline coverage metric (R10): |targets in top-k| / |targets|.

    Immune to chunk density, unlike precision@k -- a chapter split into many
    chunks does not inflate this score just because several of its chunks
    happen to appear in the top-k.
    """
    target_set = _target_tuples(targets)
    if not target_set:
        return 0.0
    hit = {_payload_tuple(p) for p in payloads} & target_set
    return len(hit) / len(target_set)


def precision_at_k(payloads: list[dict], targets: list[dict]) -> float:
    """Secondary only (R10) -- carries a chunk-density caveat: labels are
    chapters, retrievals are chunks, so this partly measures how finely a
    chapter was split rather than retrieval quality. Report alongside
    `distinct_target_chapters_hit_at_k`, never in its place.
    """
    if not payloads:
        return 0.0
    target_set = _target_tuples(targets)
    relevant = sum(1 for p in payloads if _payload_tuple(p) in target_set)
    return relevant / len(payloads)


def entity_fold_rate(pairs: list[tuple[int | None, int | None]]) -> float:
    """Fraction of Garnett/alt-translator query pairs that BOTH hit their
    shared target at k (R13's entity-translation-pairs family). Each pair is
    (rank_garnett, rank_alt) -- 1-based rank from `_rank_of`, or None on a
    miss. Answers whether the alias folded, as distinct from whether the
    query was simply hard (which a plain recall slice cannot separate).
    """
    if not pairs:
        return 0.0
    agree = sum(1 for a, b in pairs if a is not None and b is not None)
    return agree / len(pairs)


# --- source-leak / assistant-tell (R4, R5) -----------------------------------
#
# Tightened against the 72 v1 transcripts (P0 step 10). Two known false
# positives removed without losing any true positive on that corpus:
#   - bare "the source" matched "...the source of his softened heart..."
#     (legitimate causal prose, not a source citation) -- narrowed to
#     "the source material/text/provided/above" and "the sources" (plural).
#   - case-insensitive "certainly[,!]" matched mid-sentence "...yes,
#     certainly, but..." -- narrowed to a start-of-response or
#     start-of-sentence position, which is where the genuine assistant-tell
#     ("Certainly! I can help...") actually occurs.
#
# THIRD false positive removed 2026-09-03 (v2 P7 Batch D, R155), same shape as
# the first two and found the same way -- by reading the matches rather than the
# rate. Bare "I cannot" fired on first-person prose the persona is SUPPOSED to
# produce: "...I cannot say, for even he does not fully know...", "I cannot tell
# you that this is a rational way to live". It scored the arm at 0.3966 against
# 0.0000, and 26 of 26 matches were verified false. It is worse than a nuisance
# here: composition C INSTRUCTS the model to decline with "I cannot recall" when
# the recollections do not reach, so the metric penalised an arm precisely for
# obeying its grounding rule, while the arm that fabricated instead scored a
# clean zero.
#
# Narrowed to the assistant register the metric is named for. Checked against
# the same 72 v1 transcripts as the earlier tightenings: bare "I cannot" caught
# ZERO true positives there, so nothing is lost.

SOURCE_LEAK_RE = re.compile(
    r"the passages?|the text (above|provided|states)|the excerpts?|"
    r"according to the|the source (material|text|provided|above)|the sources|"
    r"retrieved|the context (above|provided)",
    re.IGNORECASE,
)

ASSISTANT_TELL_RE = re.compile(
    r"As an AI|I (?:cannot|can't|am unable to|'m unable to) "
    r"(?:assist|help with|provide|fulfil|fulfill|comply|generate|answer that)|"
    r"It is important to|It's important|"
    r"(?:^|[.!?]\s+)Certainly[,!]|In conclusion|"
    r"^\s*[-*]\s|^#{1,6}\s",
    re.IGNORECASE | re.MULTILINE,
)


def source_leak_rate(answers: list[str]) -> float:
    """R19 violation rate -- persona referring to passages/sources it was
    told never to mention. Fraction of answers matching `SOURCE_LEAK_RE`.
    """
    if not answers:
        return 0.0
    return sum(1 for a in answers if SOURCE_LEAK_RE.search(a)) / len(answers)


def assistant_tell_rate(answers: list[str]) -> float:
    """Fraction of answers breaking into generic-assistant register."""
    if not answers:
        return 0.0
    return sum(1 for a in answers if ASSISTANT_TELL_RE.search(a)) / len(answers)


# --- style distance (R12) -----------------------------------------------------
#
# Surface features only, no model. Reference = the currently promoted
# config's answers, recomputed pairwise at each promotion (rolling, never
# the frozen local-q4km baseline -- distance from a known-weak model has no
# direction). Reported as a per-feature z-vector, NEVER collapsed to a
# composite scalar (R12) -- "sentences got shorter and first-person dropped"
# is diagnostic; a single number that moved is not.

_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+(?:\s+|$)")
_SUBORDINATOR_RE = re.compile(
    r"\b(because|although|though|while|since|if|unless|whereas|"
    r"whether|before|after|when|as)\b",
    re.IGNORECASE,
)
_FIRST_PERSON_RE = re.compile(r"\b(I|me|my|mine|myself|we|us|our|ours|ourselves)\b")
_WORD_RE = re.compile(r"[A-Za-z']+")


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]


def _guiraud_ttr(words: list[str]) -> float:
    """Length-normalized type-token ratio (Guiraud's R): types / sqrt(tokens).

    Plain TTR falls with length by construction (a longer text keeps
    reusing words), so comparing plain TTR across answers of different
    length confounds "style" with "how much the model wrote" (R12). Guiraud's
    index divides by sqrt(n) instead of n, which is the standard fix.
    """
    if not words:
        return 0.0
    types = {w.lower() for w in words}
    return len(types) / math.sqrt(len(words))


def extract_style_features(text: str) -> dict[str, float]:
    """One answer's surface-feature vector (R12's option A)."""
    sentences = _sentences(text)
    words = _WORD_RE.findall(text)
    n_words = max(len(words), 1)
    n_sentences = max(len(sentences), 1)
    return {
        "mean_sentence_length": len(words) / n_sentences,
        "subordinate_clause_rate": len(_SUBORDINATOR_RE.findall(text)) / n_sentences,
        "first_person_rate": len(_FIRST_PERSON_RE.findall(text)) / n_words,
        "em_dash_rate": text.count("—") / n_words,
        "ellipsis_rate": (text.count("...") + text.count("…")) / n_words,
        "question_mark_rate": text.count("?") / n_words,
        "ttr_guiraud": _guiraud_ttr(words),
    }


def style_distance(candidate_texts: list[str], reference_texts: list[str]) -> dict[str, float]:
    """Per-feature z-vector: (candidate_mean - reference_mean) / reference_std.

    `reference_texts` are the currently promoted config's answers (rolling
    reference, R12); `candidate_texts` are the config being evaluated.
    Returns one z-score per feature -- never averaged or combined into a
    single distance. A feature with reference_std == 0 (e.g. a reference
    corpus too small to vary) is reported as None rather than dividing by
    zero or silently reporting 0.
    """
    if not candidate_texts or not reference_texts:
        raise ValueError("style_distance needs at least one candidate and one reference text")

    cand_features = [extract_style_features(t) for t in candidate_texts]
    ref_features = [extract_style_features(t) for t in reference_texts]

    z_vector: dict[str, float] = {}
    for feature in cand_features[0]:
        cand_mean = statistics.fmean(f[feature] for f in cand_features)
        ref_values = [f[feature] for f in ref_features]
        ref_mean = statistics.fmean(ref_values)
        ref_std = statistics.pstdev(ref_values) if len(ref_values) > 1 else 0.0
        z_vector[feature] = (cand_mean - ref_mean) / ref_std if ref_std > 0 else None
    return z_vector


def _demo() -> None:
    """Smallest runnable check -- not a test suite, just a tripwire."""
    payloads = [
        {"book": "Crime and Punishment", "part": 1, "chapter": 7},
        {"book": "Crime and Punishment", "part": 5, "chapter": 4},
    ]
    targets = [
        {"book": "Crime and Punishment", "part": 1, "chapter": 7},
        {"book": "Crime and Punishment", "part": 9, "chapter": 9},  # not retrieved
    ]
    assert distinct_target_chapters_hit_at_k(payloads, targets) == 0.5
    assert precision_at_k(payloads, targets) == 0.5

    assert entity_fold_rate([(1, 2), (None, 3), (1, None)]) == 1 / 3
    assert entity_fold_rate([]) == 0.0

    # the two known false positives from the v1 transcripts must NOT match
    assert not SOURCE_LEAK_RE.search(
        "the crowning achievement of the monk's seclusion, the source of his softened heart"
    )
    assert not ASSISTANT_TELL_RE.search("Despair, yes, certainly, but was it solely his own?")
    # genuine violations must still match
    assert SOURCE_LEAK_RE.search("The passage highlights the tension between Ivan and his father.")
    assert ASSISTANT_TELL_RE.search("Certainly! I would be happy to help with that.")

    assert source_leak_rate([]) == 0.0
    assert source_leak_rate(["no leak here"]) == 0.0
    assert source_leak_rate(["The excerpt provided offers a glimpse"]) == 1.0

    same_text = ["A short sentence. Another one here."] * 3
    z = style_distance(same_text, same_text)
    assert all(v is None for v in z.values()), "identical corpora must have zero reference variance"

    # family derivation: legacy rows classify off `negative_control`, new rows
    # off `family`, and every name the drafter can emit must be a known family.
    assert family_of({"id": "r-0001"}) == "primary"
    assert family_of({"id": "r-0026", "negative_control": True}) == "negative_control"
    assert family_of({"id": "r-0031", "family": "entity_pair"}) == "entity_pair"
    assert family_of({"family": "sensitive", "negative_control": False}) == "sensitive"
    # Q4: a legacy row with no `family` key derives `structural` from the route
    # it already declares; an explicit key still wins, so a future row can
    # classify itself against the route.
    assert family_of({"id": "r-0024", "expected_route": "STRUCTURAL"}) == "structural"
    assert family_of({"id": "r-0025", "expected_route": "STRUCTURAL"}) == "structural"
    assert family_of({"expected_route": "DEFAULT"}) == "primary"
    assert family_of({"family": "primary", "expected_route": "STRUCTURAL"}) == "primary"
    assert "structural" in FAMILIES and "structural" in RETRIEVAL_SCORED_FAMILIES
    assert "quotation" in FAMILIES and "quotation" in RETRIEVAL_SCORED_FAMILIES
    assert family_of({"id": "r-0107", "family": "quotation"}) == "quotation"

    # gate tiers (P3 F1): disjoint, and no metric function escapes classification.
    assert TIER_1A.isdisjoint(TIER_1B), "a metric cannot be both free and metered"
    exported = {
        name for name, obj in globals().items()
        if callable(obj) and not name.startswith("_") and getattr(obj, "__module__", None) == __name__
    }
    unclassified = exported - _NOT_A_METRIC - TIER_1A - TIER_1B
    assert not unclassified, f"metric(s) in neither tier: {sorted(unclassified)}"
    assert RETRIEVAL_SCORED_FAMILIES <= FAMILIES
    assert "sensitive" not in RETRIEVAL_SCORED_FAMILIES
    assert "adversarial" not in RETRIEVAL_SCORED_FAMILIES

    logger.info("src/rag/metrics.py self-check passed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _demo()
