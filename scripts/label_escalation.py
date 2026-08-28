"""P-6 — Q1's escalation labels: which of P0's 50 quality rows a cheap tier
*should have escalated* to an expensive one.

Feeds CP-beta's tier-assignment accuracy. The schedule's hard constraint:
**label before any arm is measured.** A label assigned after the results exist
is fitting the label to the outcome, and the resulting "accuracy" measures
nothing. This script therefore runs on the query text alone -- it never reads
an answer, a judge score, or a per-theme result. No API calls, no judge, no
spend.

## What a label means

`escalate: true` = a cheap tier is *expected* to be insufficient here, judged
from the query alone. It is a **prediction registered in advance**, not a
measurement, and CP-beta compares a router's decisions against it. It is not
ground truth about answer quality: nothing here has been graded.

## The rule, fixed before the numbers exist

A row escalates if ANY of five signals fires. All five are computable from the
query alone, because that is all a router sees at inference time.

  E1  multi-entity synthesis -- the project's own `EntityMap` resolves two or
      more distinct entities, so the answer must relate them rather than
      expound one. Deliberately the router's own observable, not a new one.
  E2  adjudicated disjunction -- a polar question posing alternatives ("X or
      Y?"). The answer must arbitrate and defend a side, not describe.
  E3  unstated inference -- asks about intent or fact the text does not state
      ("even without ever saying so", "in spirit, even though not in fact").
  E4  contradiction to reconcile -- an explicit reversal marker ("even as",
      "only to", "despite", "even though", "instead of", "rather than").
  E5  compound -- two or more distinct interrogatives in one query.

A row with no signal is labelled cheap-sufficient: single-entity exposition or
direct textual retrieval, which is recall-and-restate work.

## Known weaknesses, stated here rather than discovered at CP-beta

* **The labels correlate heavily with `theme`.** The Ambiguity theme is
  disjunctive by construction, so E2 fires across nearly all of it. A router
  that merely recovers theme would score well. Theme is not available at
  inference time, so this is not leakage -- but it does mean the accuracy
  figure is close to a theme-recovery score, and it should be read that way.
* **The rule is lexical.** q-0005 ("torment Liza after she offers him
  tenderness") is a genuine contradiction with no marker from E4's list, so it
  labels cheap. Widening E4 to catch it would be fitting the rule to rows the
  author already had opinions about. The list stays as written.
* **These are predictions, not grades.** If CP-beta finds a router disagreeing
  with these labels, that is not automatically router error -- it may be that
  the label was wrong. The schedule already treats this metric as *reported,
  never a gate*; it has no MDE and no baseline.
"""

import json
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config  # noqa: E402
from src.rag.entities import EntityMap  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

QUALITY_SET = os.path.join("test", "quality_eval.json")
OUT = os.path.join("plans", "mitya-v2", "research", "p6-escalation-labels.json")

# E4's reversal markers. Fixed list -- see the module docstring on why it is
# not widened to catch unmarked contradictions.
_CONTRADICTION = re.compile(
    r"\b(even as|even though|even without|only to|despite|instead of|rather than|yet)\b",
    re.IGNORECASE,
)
_UNSTATED = re.compile(
    r"\b(even without ever|in spirit|without ever saying|felt almost|without a word)\b",
    re.IGNORECASE,
)
_POLAR = re.compile(r"^\s*(is|was|are|were|did|does|do|has|have|can|could|would)\b", re.IGNORECASE)


def _signals(query: str, entity_count: int) -> list[str]:
    """Which escalation signals fire for this query. Query text only."""
    fired = []
    if entity_count >= 2:
        fired.append("E1_multi_entity")
    # E2: a polar question that offers alternatives. Both halves required --
    # a bare "or" inside a descriptive clause is not an adjudication.
    if re.search(r"\bor\b", query, re.IGNORECASE) and (
        _POLAR.match(query) or re.search(r"\bor\s+(does|did|is|was|has)\b", query, re.IGNORECASE)
    ):
        fired.append("E2_disjunction")
    if _UNSTATED.search(query):
        fired.append("E3_unstated")
    if _CONTRADICTION.search(query):
        fired.append("E4_contradiction")
    if query.count("?") >= 2:
        fired.append("E5_compound")
    return fired


def main() -> None:
    with open(QUALITY_SET, encoding="utf-8") as f:
        rows = json.load(f)
    logger.info("loaded %d quality rows from %s", len(rows), QUALITY_SET)

    entities = EntityMap(Config.ENTITIES_PATH)
    labelled = []
    for row in rows:
        query = row["query"]
        _, resolved = entities.resolve(query)
        fired = _signals(query, len(set(resolved)))
        labelled.append({
            "id": row["id"],
            "theme": row["theme"],
            "query": query,
            "resolved_entities": sorted(set(resolved)),
            "signals": fired,
            "escalate": bool(fired),
        })

    escalate = [r["id"] for r in labelled if r["escalate"]]
    cheap = [r["id"] for r in labelled if not r["escalate"]]

    # Per-theme and per-signal breakdowns: the docstring claims the labels
    # track theme closely, and a reader should be able to check that claim
    # without re-deriving it.
    by_theme: dict[str, list[int]] = {}
    for r in labelled:
        seen = by_theme.setdefault(r["theme"], [0, 0])
        seen[0 if r["escalate"] else 1] += 1
    by_signal: dict[str, int] = {}
    for r in labelled:
        for s in r["signals"]:
            by_signal[s] = by_signal.get(s, 0) + 1

    artifact = {
        "generated": "P-6, Q1's escalation labels",
        "source_set": QUALITY_SET,
        "n": len(labelled),
        "rule": "escalate if any of E1_multi_entity, E2_disjunction, "
                "E3_unstated, E4_contradiction, E5_compound fires; see "
                "scripts/label_escalation.py's docstring for the full statement",
        "labelled_before_any_arm_was_measured": True,
        "escalate_ids": escalate,
        "cheap_ids": cheap,
        "by_theme_escalate_vs_cheap": by_theme,
        "by_signal": by_signal,
        "rows": labelled,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(artifact, f, indent=2, ensure_ascii=False)

    logger.info("escalate %d / %d (%.0f%%)", len(escalate), len(labelled),
                100 * len(escalate) / len(labelled))
    for theme, (esc, ch) in sorted(by_theme.items()):
        logger.info("  %-11s escalate %2d  cheap %2d", theme, esc, ch)
    logger.info("signal firings: %s", by_signal)
    logger.info("wrote %s", OUT)


if __name__ == "__main__":
    main()
