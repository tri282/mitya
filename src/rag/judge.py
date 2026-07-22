# src/rag/judge.py
"""LLM-judge scaffold for the P7 answer-quality eval (design VII.2, ruling R19).

Reuses `src/rag/teacher.py`'s `TeacherClient` -- the only Gemini integration
in this repo, with rate limiting + retry discipline already built for a
metered free-tier account (`docs/RUNBOOK.md`: "Never run two builders at
once. Each process has its own RateLimiter... nothing enforces this but
you." -- the judge is another metered consumer of the same discipline, not a
new API integration).

Scores one (query, answer) pair on the three axes the phase file specifies:

  * persona retention -- does the answer read as Dostoevsky in character,
    not a generic assistant breaking into meta-commentary?
  * groundedness -- is the answer's substance consistent with the retrieved
    passages (`passages_block`, i.e. what retrieval actually returned -- the
    programmatic Sources), not just internally plausible?
  * fabricated events -- does the answer invent plot, characters, or events
    absent from `passages_block`?

Judging against `passages_block` (never against an inline citation in the
answer, and never from the judge's own knowledge of the novels) is
deliberate: the persona is instructed to never mention passages or sources
at all (R19 -- "Give no editorial comments and references to external
sources"), so grounding can only be checked against what retrieval actually
surfaced for that request, not against anything the model said about its
sources.
"""

import logging

from src.rag.teacher import TeacherClient

logger = logging.getLogger(__name__)

_JUDGE_PROMPT = """You are grading one response from a Dostoevsky-persona \
chatbot as part of a retrieval-augmented-generation evaluation. Score \
strictly from the material given below -- do not use outside knowledge of \
the novels beyond what appears in RETRIEVED PASSAGES.

QUESTION:
{query}

RETRIEVED PASSAGES (programmatic Sources -- ground truth for this judgment; \
the persona itself is instructed never to mention these explicitly):
{passages_block}

RESPONSE TO GRADE:
{answer}

Score the RESPONSE on these axes and return ONLY a JSON object with exactly \
these four keys:
  "persona_retention": integer 1-5 (5 = fully in Dostoevsky's first-person \
literary voice throughout, no assistant-like breaks in character; 1 = reads \
like a generic assistant)
  "groundedness": integer 1-5 (5 = every substantive claim is consistent \
with RETRIEVED PASSAGES; 1 = largely unrelated to them)
  "fabricated_events": boolean (true if the response invents plot events, \
characters, or details absent from RETRIEVED PASSAGES; false otherwise)
  "rationale": string, one or two sentences justifying the three scores above
"""

_REQUIRED_KEYS = {"persona_retention", "groundedness", "fabricated_events", "rationale"}


def judge_answer(client: TeacherClient, query: str, answer: str, passages_block: str) -> dict:
    """One judge call -> {"persona_retention": int, "groundedness": int,
    "fabricated_events": bool, "rationale": str}.

    Raises `src.rag.teacher.TeacherError` on a fatal judge-API failure (see
    `TeacherClient.generate_json`'s own retry/quota handling). A structurally
    valid JSON response missing a required key is a judge-prompt defect, not
    something to silently paper over -- raised as `ValueError` so the caller
    (the A/B runner) records a visible `judge_error` row instead of trusting
    a malformed score.
    """
    prompt = _JUDGE_PROMPT.format(
        query=query,
        passages_block=passages_block or "(retrieval returned no passages)",
        answer=answer or "(empty response)",
    )
    result = client.generate_json(prompt)
    if not isinstance(result, dict):
        raise ValueError(f"judge: expected a JSON object, got {type(result).__name__}: {result!r}")

    missing = _REQUIRED_KEYS - result.keys()
    if missing:
        raise ValueError(f"judge: response missing keys {missing}: {result!r}")

    logger.info(
        "judge: persona_retention=%s groundedness=%s fabricated_events=%s",
        result["persona_retention"], result["groundedness"], result["fabricated_events"],
    )
    return result
