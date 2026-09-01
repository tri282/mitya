"""The memory writer: an ordinary gateway consumer (v2 P6 step 4, Batch C).

**The whole point of this module is that it is not special.** Step 4's stated
purpose is that memory writes appear in P1's cost attribution and count against
P2/P5's budgets like any other spend -- "a memory system with untracked write
cost is an unmetered money faucet" (`phase-6-memory.md:60`). So every draw here
goes through `src/rag/generate.py`'s `open_guarded_stream()`, the same seam
`stream_answer()` uses: same `guarded_generate` (request size, admission
fairness, per-model rate limiter, budget, spend-rate breaker), same
`ScopedRateLimiter`, same `SpendRateBreaker` window, same three-way cost split
into metered / unmetered / unpriced.

**Its own model, therefore its own quota window.** `Config.SUMMARIZER_MODEL`
defaults to `models/gemini-3.1-flash-lite` (G-P11, owner decision, recorded not
re-litigated), against `Config.SERVING_MODEL`'s `models/gemini-3.5-flash-lite`.
`quota_scope()` is `(provider, model)`, so the split gives two independent
windows in *our* limiter and dissolves R121's rpd squeeze. Google's own bucket
separation is `[inferred]` -- measured on the wire on Groq only -- and if it is
wrong the excess returns as 429s the transport retry absorbs into latency,
mostly silently. CP-6d's dry run is what watches for it.

**Placement: in-band, post-answer, same admission slot** (G-P13). The write runs
inside the turn's request lifecycle, after the final answer token and before the
SSE stream closes. Not a second admission slot -- that would be hold-and-wait
under contention and one user action counted twice against
`CHAT_MAX_INFLIGHT_TOTAL`. The honest accounting is one slot occupied for its
full duration, and the consequence is recorded: on turns where a write fires,
slot occupancy grows by the write's duration, so `src/config.py`'s ~12.6 s
worst-case admitted-wait arithmetic no longer holds on those turns.

**A failed write never fails the turn, and it is COUNTED** -- see
`record_memory_write_failure`. Every public function here swallows its
exceptions and returns a falsy result. The answer is already delivered; the
failure leaves memory at its previous consistent state, never half-written.

**Extraction input is the current turn's QUESTION ONLY** (O-E) -- not the
answer, not the transcript. Three reasons in the priority order: the prompt
loses the answer's up-to-`MAX_TOKENS`=1024 tokens, which is the larger half of
the draw; the answer is corpus-grounded persona output whose facts are facts
about Dostoevsky, not about the user; and keeping it out removes a route by
which verbatim retrieved corpus text could enter the durable store as a "user
fact". It cannot resolve an anaphoric question ("and how does it end?") and will
extract nothing from those turns -- correct behaviour, not a defect (R120(c):
those turns carry no user facts), but it means per-turn extraction fires on a
MINORITY of turns.
"""

import json
import logging
import re
import time

from src.config import Config
from src.memory import KINDS, LEVELS, Episode, ProfileFact
from src.memory_store import (
    MAX_CHAPTERS,
    MAX_FACTS_PER_WRITE,
    MAX_TOPICS,
    normalize,
    remember,
    remember_episode,
    write_allowed,
)
from src.metrics import (
    record_answer_cost,
    record_memory_write_failure,
    record_unmetered_answer,
    record_unpriced_answer,
)
from src.pricing import cost_usd

logger = logging.getLogger(__name__)

# The extractor's whole contract. Typed output, because CP-6a's schema is typed:
# there is no free-text column anywhere for a paragraph to land in, so a model
# that returns prose returns something this module drops on the floor (and
# counts, via the unparseable-output failure path the schedule names).


def _extract_system() -> str:
    """The extractor's system prompt, rendered from the VERSIONED template.

    A FUNCTION, not a constant, for the reason `active_persona_head()` is one:
    a module constant would snapshot the text (and `Config.MEMORY_VALUE_MAX_CHARS`)
    at import time and quietly serve the old bound after a config change.

    **The residue this docstring used to record is closed (G-P17, CP-6d).** The
    text itself now lives on `ServingConfig` as `extractor_prompt`, so editing
    it churns `config_version`. That matters more here than for the persona:
    this prompt writes state that OUTLIVES the request, so without the churn two
    runs days apart could hold facts extracted under different instructions with
    nothing recording it -- `writer_model` (G-P14) records which model wrote a
    row, never under which instructions.
    """
    return Config.EXTRACTOR_PROMPT.format(
        max_facts=MAX_FACTS_PER_WRITE, max_chars=Config.MEMORY_VALUE_MAX_CHARS,
    )


def _episodic_system() -> str:
    """The episodic digest's system prompt, same split, same reason (G-P17).

    It joins the versioned half because CP-6d(a) gave `write_episode` its first
    caller, so it now writes durable rows too.
    """
    return Config.EPISODIC_PROMPT.format(max_topics=MAX_TOPICS, max_chapters=MAX_CHAPTERS)


# Models fence their JSON in ``` more often than not; one regex is cheaper than
# a tolerant parser and its failure mode is the same failure mode.
_FENCED = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


# In-process observation seam for the metered run (CP-6d's dry run, items 1 and
# 4). Left None in the server: nothing here logs a raw model response, because a
# raw response carries the reader's own words and the store's whole posture is
# that those are normalized and bounded before they persist. The metered driver
# (`scripts/run_multiturn.py`) sets it for the duration of a run, which is the
# only place the RAW TEXT is what has to be read -- CP-6d separates "the model
# found nothing" from "the model returned prose" by the raw text and explicitly
# not by a row count, because a row count is what made Batch D's fake-provider
# result look like a working defense.
draw_observer = None


async def _draw(system: str, user: str, kind: str) -> tuple[str, str]:
    """One guarded, priced, quota-counted draw. Returns `(text, model)`.

    Non-streaming in effect -- the chunks are drained here rather than yielded
    -- but it uses the streaming seam anyway so there is exactly one guarded
    path in the codebase rather than a second one that could drift out of sync
    with its guards.
    """
    from src.rag.generate import _provider_for, open_guarded_stream

    model = Config.SUMMARIZER_MODEL
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    t0 = time.monotonic()
    stream = await open_guarded_stream(_provider_for(model), messages, model)
    text = "".join([chunk async for chunk in stream.chunks])
    _record_cost(stream.usage, model)
    if draw_observer is not None:
        # Never raises into the turn: an observer defect must not become a
        # memory-write failure, which is a different event with its own counter.
        try:
            draw_observer({
                "kind": kind,
                "model": model,
                "text": text,
                "usage": stream.usage,
                "wall_ms": (time.monotonic() - t0) * 1000,
            })
        except Exception:  # pragma: no cover -- driver-side defect, not a write failure
            logger.warning("memory: draw observer raised; the write itself is unaffected")
    return text, model


def _record_cost(usage, model: str) -> None:
    """The same three-way split `src/api/app.py` applies to a served answer.

    Kept three-way here for the reason G5 gives there: an unmetered write must
    not enter the cost histogram as 0.0, because a zero is indistinguishable
    from a real metered-but-free call; and an unpriced write is a third thing
    again -- somebody billed us and the figure is missing. The summarizer arm
    has a rate row (`_DEFAULT_MODEL_RATES`, read 2026-08-31), so the unpriced
    branch is a tripwire on a missing row, not the expected path.
    """
    served = getattr(usage, "model", None) or model
    cost = cost_usd(
        served, getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None),
    )
    if not cost.metered:
        record_unmetered_answer(served)
    elif cost.usd is None:
        record_unpriced_answer(served)
    else:
        # `tenant` is deliberately the *ambient* tenant, the same value the
        # serving draw reports, so the Verify table's serving-vs-summarizer USD
        # split is two series under one tenant dimension rather than two
        # incomparable ones.
        from src.observability import current_tenant

        record_answer_cost(cost.usd, served, current_tenant())


_ECHO_NGRAM = 5


def _echoes(value: str, question: str) -> bool:
    """Whether `value` carries a >=5-word contiguous span of `question`.

    The last write-path check before persistence, and it is the one that makes
    the C-1 test able to fail. CP-6a is explicit that the length bound defeats
    **paragraph-scale** recovery by construction and does **not** defeat
    **clause-scale** recovery -- an 80-character value can hold a verbatim
    clause, and the extractor's "never quote the question" is a promise made by
    a model, which §IV's invariant is not allowed to rest on.

    **State what this does and does not buy.** It blocks a verbatim or
    near-verbatim ECHO of the turn being extracted from -- the shape an
    adversarial `refusal`-family turn takes when it tries to smuggle its own
    wording into the durable store. It does NOT block a paraphrase, a
    reordering, or a four-word span, and it says nothing about a turn other than
    the one in hand. Clause-scale recovery is therefore bounded here **by test
    against echo**, not defeated, and no report may upgrade that to
    "structural".
    """
    words = normalize(question, 10_000).split()
    if len(words) < _ECHO_NGRAM:
        return False
    hay = " " + normalize(value, 10_000) + " "
    return any(
        f" {' '.join(words[i:i + _ECHO_NGRAM])} " in hay
        for i in range(len(words) - _ECHO_NGRAM + 1)
    )


def _payload(text: str):
    body = _FENCED.search(text)
    return json.loads((body.group(1) if body else text).strip())


async def write_profile_from_turn(
    tenant: str, question: str, *, now: float | None = None, db_path: str | None = None,
) -> int:
    """Extract profile facts from ONE question and persist them. Never raises.

    Returns rows written; 0 covers every uninteresting outcome -- the tenant is
    over its write-rate cap, the extractor correctly found nothing, or the write
    failed. Only the last of those increments the failure counter, because the
    first two are not failures.
    """
    if not question or not write_allowed(tenant, now):
        return 0
    try:
        text, model = await _draw(_extract_system(), question, "extract")
        facts = [
            ProfileFact(
                kind=item["kind"], key=str(item["key"]), value=str(item["value"]),
                importance=str(item.get("importance", "medium")),
                confidence=str(item.get("confidence", "medium")),
                # `permanent` is never set from extractor output. It is an
                # eviction exemption (N-3), and a model's self-report is exactly
                # the class of signal R63 found unable to arbitrate -- letting it
                # mint exemptions would make the caps unenforceable from outside.
                permanent=False,
            )
            for item in _payload(text)
            if isinstance(item, dict) and item.get("kind") in KINDS and item.get("key")
            and str(item.get("importance", "medium")) in LEVELS
        ]
        kept = [f for f in facts if not _echoes(f.value, question)]
        if len(kept) != len(facts):
            logger.warning(
                "memory: dropped %d extracted fact(s) echoing the question verbatim "
                "for tenant %s -- charter IV, C-1", len(facts) - len(kept), tenant,
            )
        return remember(tenant, kept, model, now=now, db_path=db_path)
    except Exception as exc:
        # Unparseable extractor output lands here alongside a provider failure,
        # deliberately: the schedule names unparseable output as a write-path
        # error the cost row must include, and from the turn's point of view the
        # two are the same event -- memory did not advance.
        record_memory_write_failure("extract")
        logger.warning(
            "memory: profile extraction failed for tenant %s -- turn unaffected: %s",
            tenant, exc,
        )
        return 0


async def write_episode(
    tenant: str,
    session_id: str,
    transcript: str,
    turn_count: int,
    *,
    now: float | None = None,
    db_path: str | None = None,
) -> bool:
    """Summarize one finished session into a typed digest. Never raises.

    **Its caller is the LAZY TRIGGER (CP-6d(a), G-P15 closed).** Session *k-1*
    is summarized on the first turn of session *k* for the same tenant, from
    `src/api/app.py`'s `/chat` handler, in band and post-answer -- G-P13's
    placement, unchanged, on a turn that was going to run anyway.

    G-P15 was open because **no session-end signal exists**: `ChatRequest`
    carries `query`, `filters` and `session_id` and nothing marks a session
    finished, and the auth session is a different object on a 12h TTL. The lazy
    trigger needs none: `SessionTurns` is keyed `(tenant, session_id)` in an LRU,
    so the tenant's previous live session is already addressable in process. No
    new endpoint, no client contract, no new signal, no background task.

    **What it does not cover, stated rather than engineered around:** a session
    with no successor is never summarized (the last session of every tenant --
    39 of 40 fire in a full replay), and a process restart loses every
    un-summarized transcript with the RAM it lived in. Accepted at CP-6d; there
    is no orphaned-write machinery, consistent with G-P13's fourth residual risk.
    """
    try:
        text, model = await _draw(_episodic_system(), transcript, "episodic")
        payload = _payload(text)
        remember_episode(
            tenant,
            Episode(
                session_id=session_id,
                ended_at=now if now is not None else time.time(),
                turn_count=turn_count,
                topics=tuple(str(t) for t in payload.get("topics", []))[:MAX_TOPICS],
                chapters=tuple(c for c in payload.get("chapters", []) if isinstance(c, dict))[
                    :MAX_CHAPTERS
                ],
            ),
            model,
            now=now,
            db_path=db_path,
        )
        return True
    except Exception as exc:
        record_memory_write_failure("episodic")
        logger.warning(
            "memory: episodic summary failed for tenant %s session %s -- turn unaffected: %s",
            tenant, session_id, exc,
        )
        return False
