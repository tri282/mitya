"""httpx streaming client for the local llama-server completion endpoint.

Prompt composition (design III.4, ruling R19): the fine-tune's training rows
never contained context in the prompt and its system prompt forbids
referencing external sources, so injected passages are out-of-distribution.
Composition A (design default): system = the verbatim fine-tuned persona
head (stay near the training distribution) + retrieved passages framed as
the author's own recollections; the user turn carries the question only.
Composition B (P7 A/B candidate, added alongside A -- neither deleted until
a human judges the transcripts): system = persona head unmodified; passages
move into the user turn instead. Both are implemented in build_messages()
below, gated on Config.PROMPT_COMPOSITION / an explicit `mode=` argument; A
is the default so no existing caller changes behavior.

Generation streams OpenAI-compatible SSE deltas from llama-server's
/v1/chat/completions (ruling R16 -- httpx, not LiteLLM: the whole need is a
~30-line streaming client, not a 100+-provider abstraction over one
localhost endpoint). The Sources footer (design III.5) is a separate,
non-streamed piece of text the caller (P6) emits as its own final SSE event
after the token stream ends -- build_sources_footer() renders it here since
it is a generation/serving-layer concern, not part of prompt-context
assembly (src/rag/context.py owns dedup + ordering of the sources list).
"""

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator

from src.config import Config
from src.gateway.contract import GenerationStream, Provider, Usage
from src.gateway.google import GoogleProvider
from src.gateway.groq import GroqProvider
from src.gateway.guard import SpendRateBreaker, guarded_generate
from src.gateway.retry import TransportError
from src.gateway.local import LOCAL_MODEL_ID, LocalProvider
from src.limits import ScopedRateLimiter, estimate_tokens
from src.observability import current_tenant
from src.pricing import cost_usd
from src.rag.ingest import BOOK_META

# Ceiling on conversation turns, passed to the guard's request-size check.
# mitya is single-turn today (`build_messages()` returns one system and one
# user message) and the Google adapter refuses assistant turns outright until
# P6 works out the interactions API's step_list format (R57), so this is a
# guard-rail against a caller that has not been written yet, not a live limit.
MAX_HISTORY_MESSAGES = 8

# Bounded transport attempts. `src.gateway.retry.retry_transport` used to be
# the other half of a parity claim here; Batch A0 item 3 deleted it (R88 --
# the two loops had already diverged on `retry_after`, so the claim was false
# before it was removed). Kept as a module constant rather than a versioned
# config knob:
# it is a deployment/resilience policy, not something that changes what a
# correct answer is.
TRANSPORT_ATTEMPTS = 3

logger = logging.getLogger(__name__)

# PERSONA_HEAD moved to the versioned serving config (v2 P2 Batch C step 7;
# C5 decided in favour of moving OWNERSHIP, not merely pinning the text).
# Read `Config.PERSONA_HEAD`, which resolves through the artifact facade -- see
# `_DEFAULT_PERSONA_HEAD` in src/config_artifact.py for the text and R54 for
# what was wrong before: nothing pinned it across time, so editing the persona
# did not churn `config_version` and a measurement could silently span two
# different prompts.
#
# Exposed as a FUNCTION, not a constant. A constant would snapshot the value at
# import time and defeat the versioning that was the whole point of moving it.
# Named `active_persona_head` rather than `persona_head` so it cannot be
# confused with `build_messages()`'s parameter of that name just below.
def active_persona_head() -> str:
    """The active persona system prompt, from the versioned serving config."""
    return Config.PERSONA_HEAD


class LlamaServerUnavailable(Exception):
    """llama-server refused the connection -- P6 maps this to HTTP 503."""


class LlamaServerTimeout(Exception):
    """llama-server did not respond within budget -- P6 maps this to HTTP 504."""


def build_messages(
    persona_head: str,
    passages_block: str,
    question: str,
    mode: str | None = None,
    memory_block: str = "",
) -> list[dict]:
    """Compose the chat messages sent to llama-server.

    `mode` selects the prompt-composition A/B (P7, ruling R19); defaults to
    `Config.PROMPT_COMPOSITION` ("A") when not given explicitly, so every
    existing caller (P6's `src/api/app.py`) keeps using composition A without
    a code change.

    * **A (design default):** system = persona head + passages framed as the
      author's own recollections; user turn carries the question only.
      Context never enters the user turn -- keeps the prompt closer to the
      fine-tune's training distribution (which never saw context at all).
    * **B (A/B candidate):** system = persona head, unmodified; passages are
      appended to the user turn instead, ahead of the question. This is the
      out-of-distribution seam RR ("persona may ignore or parrot injected
      context") predicts could go either way -- P7's answer-quality eval
      judges A vs B on persona retention + groundedness; the loser gets
      deleted from this file once a human has read the transcripts (see
      plans/mitya-rag/05-impl/phase-7.md), not decided here.

    `memory_block` (v2 P6 step 2(a)) is the allocator's memory tiers, already
    rendered and already fenced by `src/memory.py` -- this function does not
    frame it, because the fence is a security property and it must live in one
    place, beside the store that produces it. Empty string when the request is
    memory-free, which is every turn-1 and every sessionless request; the
    default keeps every pre-P6 caller (`scripts/eval_quality.py`,
    `test/test_no_rag_arm.py`) unchanged.

    **Placement: after the passages, at the tail of the system message.** Two
    reasons, one of which is not settled here. The settled one is that context
    never enters the user turn under composition A (R19) and memory is context.
    The unsettled one is R52/CP-6d's ordering question -- the transcript is the
    part that changes every turn, so putting it last keeps the longest possible
    byte-identical prefix, but whether that prefix ever reaches a provider
    minimum is R124's unrun measurement. This is the order that does not
    foreclose it, not a claim that reuse was achieved.
    """
    mode = mode or Config.PROMPT_COMPOSITION
    if mode == "B":
        system = persona_head
        user = (
            "Here are some passages from your own novels; let their substance "
            "inform your answer, but never mention passages, sources, or "
            "quotations:\n\n"
            + passages_block
            + ("\n\n" + memory_block if memory_block else "")
            + "\n\n"
            + question
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    if mode != "A":
        raise ValueError(f"unknown prompt composition mode: {mode!r} (expected 'A' or 'B')")

    system = (
        persona_head
        + "\n\nRecall these passages from your own novels; let their substance "
          "inform your answer, but never mention passages, sources, or quotations:\n\n"
        + passages_block
    )
    if memory_block:
        system += "\n\n" + memory_block
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]


# --- Batch C step 6: the gateway cutover -------------------------------
#
# `stream_answer()` used to open its own httpx client and POST to
# `Config.LLAMA_SERVER_URL`. It is now a GATEWAY CLIENT: it selects a provider
# from the versioned `Config.SERVING_MODEL`, runs it through
# `guarded_generate()` so spend enforcement sits before the provider call, and
# coerces the provider's `Usage` back into the plain dict its callers already
# read.
#
# The signature is unchanged on purpose. `src/api/app.py` gets no knowledge of
# providers: it calls `stream_answer(messages, usage)`, iterates strings, and
# reads a dict -- all provider coercion lives here, in sub-functions, and
# `/chat` stays generic. That is the operator's constraint, and it is why this
# is a rewrite of one function rather than surgery across the request path.


def _resolve_adapter(model: str) -> Provider:
    """Pick the bare vendor adapter for `model`, before any fault-injection
    wrapping. The ONLY place a model id becomes a vendor, so adding a
    provider does not touch the request path."""
    limits = Config.limits_for(model)
    provider_name = limits.get("provider")
    if model == LOCAL_MODEL_ID or provider_name is None:
        # A3: local is not the default and nothing in the request path starts
        # llama-server. Reaching here means someone set SERVING_MODEL back to
        # it deliberately -- log it, because a silently-local arm would make
        # every hosted cost row read as $0.00.
        logger.warning(
            "generation is pointed at %r -- the LOCAL arm. A3 makes local-q4km a "
            "frozen chart line, not a live arm; hosted cost/latency rows measured "
            "in this state are not comparable with the hosted arms.", model,
        )
        return LocalProvider(model=model)
    if provider_name == "google":
        return GoogleProvider(model=model)
    if provider_name == "groq":
        return GroqProvider(model=model)
    raise ValueError(
        f"no adapter for provider {provider_name!r} (model {model!r}). Add one "
        f"under src/gateway/ and a branch here."
    )


def _provider_for(model: str) -> Provider:
    """`_resolve_adapter(model)`, wrapped in `FaultProvider` when
    `Config.FAULT_INJECT` is set (v2 P4 Batch C step 7, D8). One branch, so a
    fault-injection run exercises the exact same resolution path a real
    request does -- the wrapping happens here rather than at the call site so
    nothing in `stream_answer()` has to know injection exists.
    """
    if Config.FAKE_PROVIDER:
        # Before the fault wrap, so a fault-injection run can be exercised over
        # the fake at zero spend; FaultProvider wraps whatever provider it is
        # handed, which is what makes the two flags compose rather than collide.
        from src.gateway.fake import FakeProvider

        logger.warning(
            "generation is running against the FAKE PROVIDER (MITYA_FAKE_PROVIDER set) "
            "-- every answer in this process is a replayed fixture and no vendor call "
            "is made. Latency and cost rows from this run measure the server, not a "
            "provider. This must never be set on a run whose answers are meant to be real.",
        )
        provider: Provider = FakeProvider()
    else:
        provider = _resolve_adapter(model)
    if Config.FAULT_INJECT:
        from src.gateway.fault import FaultProvider, parse_fault_spec

        logger.warning(
            "generation is running under FAULT INJECTION (MITYA_FAULT_INJECT=%r) "
            "-- every request in this process will have faults injected. This "
            "must never be set on a run whose answers are meant to be real.",
            Config.FAULT_INJECT,
        )
        return FaultProvider(provider, parse_fault_spec(Config.FAULT_INJECT))
    return provider


# Which credential each hosted provider needs, kept beside `_provider_for()`
# for the reason its own docstring gives: this module is the only place a model
# id becomes a vendor. A readiness check that resolved vendors on its own would
# be a second, parallel notion of what serves -- which is the defect Batch A0
# item 1 exists to remove, not to duplicate one layer up.
LOCAL_PROVIDER = "local"
_PROVIDER_CREDENTIAL = {"google": "GOOGLE_API_KEY", "groq": "GROQ_API_KEY"}


def serving_provider_name() -> str:
    """The provider `_provider_for(Config.SERVING_MODEL)` would return, named.

    A vendor key (`"google"`, `"groq"`), or `LOCAL_PROVIDER` for the
    llama-server adapter. Resolution mirrors `_provider_for()` exactly,
    including its provider-is-None fallback to local, so readiness cannot
    disagree with what the request path actually calls.
    """
    model = Config.SERVING_MODEL
    provider = Config.limits_for(model).get("provider")
    if model == LOCAL_MODEL_ID or provider is None:
        return LOCAL_PROVIDER
    return provider


def credential_status(provider: str) -> str:
    """`"ok"`, or why the serving provider cannot be called. Config-only.

    Deliberately NOT a live probe. `/health` is polled every few seconds by the
    SPA, and Gemini Flash allows 5 requests per MINUTE (R85) -- a probe would
    spend the quota the deployment exists to serve with, and on a token-bound
    provider it would spend the limit that actually binds. A missing key is the
    hosted failure mode that is knowable for free, and it is the one that
    happens; a provider outage surfaces at the request, as a 502, where it
    already has a code path (`TransportError`).
    """
    env = _PROVIDER_CREDENTIAL.get(provider)
    if env is None:
        return f"no credential mapping for provider {provider!r}"
    return "ok" if getattr(Config, env, None) else f"{env} is not set"


def _estimated_cost_usd(messages: list[dict], model: str) -> float:
    """Pre-flight spend estimate for the guard -- a CEILING, not a prediction.

    The guard has to decide before calling, and actual cost is only knowable
    after, so this prices the worst case: the estimated prompt plus a FULL
    `Config.MAX_TOKENS` completion. Over-rejecting is the correct direction for
    a spend guard.

    `MAX_TOKENS` really is an upper bound on both arms, and R56 is why: thinking
    counts against `max_output_tokens` on Google and against `max_tokens` on
    Groq, so a reasoning model cannot exceed it by thinking. An unpriced model
    estimates 0.0 -- `cost_usd()` already logs that the request is UNPRICED, and
    a guard must not invent a number to block on.
    """
    prompt_tokens = sum(estimate_tokens(m.get("content", "") or "") for m in messages)
    cost = cost_usd(model, prompt_tokens, Config.MAX_TOKENS)
    return cost.usd or 0.0


# One limiter object for the process, keyed by QUOTA SCOPE rather than by model
# -- Groq publishes org-wide limits, so its models share one window while
# Google's do not (see `src.limits.quota_scope`). R26's per-process split is
# untouched: N uvicorn workers still hold N sets of windows.
_limiter = ScopedRateLimiter()
_breaker = SpendRateBreaker(Config.SPEND_RATE_MAX_USD_PER_MINUTE)


async def stream_answer(
    messages: list[dict], usage: dict | None = None
) -> AsyncIterator[str]:
    """Stream answer text from whatever provider `Config.SERVING_MODEL` names.

    `usage` is the same caller-owned dict as before, filled in place, and it
    stays EMPTY when the provider reports no counts -- which is what makes the
    request UNPRICED rather than estimated. A fabricated token count that looks
    like a measurement is the one outcome charter II forbids outright.

    Two fields are new and additive, so existing readers are unaffected:
    `model` (the arm that actually served, which is no longer necessarily
    `Config.SERVING_MODEL` once routing lands) and `reasoning_tokens` (R55/R56 --
    already inside `completion_tokens`, carried separately so a cross-arm
    USD/answer comparison can subtract it).

    **The empty-completion retry is gone from this path.** `EMPTY_RETRIES`
    existed because the weak local q4 sampled EOS as a first token (R28/R29);
    whether hosted arms need anything like it is C4/step 8(b), decided on 11a's
    measured zero-token counts. R56 is the reason not to guess: on a thinking
    model an empty answer can mean the token budget was consumed by reasoning,
    and retrying that re-pays the full thinking cost and truncates identically.
    Batch B's `record_if_empty_completion()` counter is what will answer it.
    """
    model = Config.SERVING_MODEL
    provider = _provider_for(model)
    t_open = time.monotonic()
    # ONE attempt budget shared by both retry classes (step 8(b)). Transport
    # retries and quality retries must not compound: three of each would be
    # nine generations for one answer, and phase-2-gateway.md step 8 is
    # explicit that "retries are a cost multiplier here". A shared counter is
    # what makes the ceiling a ceiling rather than a per-class ceiling.
    #
    # Both classes retry only while NOTHING has been emitted. Once a token is
    # out, a retry would duplicate text mid-answer, so the window closes at the
    # first token -- the constraint the old EMPTY_RETRIES loop worked under,
    # and why TTFT is unaffected for a normal stream.
    #
    # The two classes are distinct and are counted separately (C4/CP-γ):
    #   * TRANSPORT -- no stream at all (429, 5xx, connect, timeout, or an
    #     in-band provider error). Always retried; the failure is ours or the
    #     network's, and a second attempt is the whole remedy.
    #   * QUALITY -- the stream succeeded and produced zero tokens. Retried
    #     ONLY where enabled, because a re-draw is a duplicated generation at
    #     full price. Measured at 11a: Google 0 of 200 draws, Groq 1 of 150
    #     (R70), so it defaults OFF everywhere and is opted into per model.
    #     R56 is the reason not to default it on: on a thinking model an empty
    #     answer can mean the budget was consumed by reasoning, and retrying
    #     re-pays the full thinking cost and truncates identically.
    attempts = 0
    retries = {"transport": 0, "quality": 0}
    retry_wait = 0.0
    quality_retry_on = model in Config.QUALITY_RETRY_MODELS

    while True:
        attempts += 1
        try:
            t_attempt = time.monotonic()
            stream = await _open_stream(provider, messages, model)
            first = await anext(aiter(stream.chunks), None)
        except TransportError as exc:
            if attempts >= Config.MAX_GENERATION_ATTEMPTS:
                raise
            retries["transport"] += 1
            # The provider's own retry-after wins when it sends one -- it knows
            # when capacity returns; our backoff is only a guess. Jitter is
            # still added so concurrent retries do not wake in lockstep.
            if exc.retry_after is not None:
                delay = exc.retry_after * (1.0 + 0.25 * random.random())
            else:
                delay = min(4.0, 0.25 * 2 ** (attempts - 1)) * (0.5 + random.random())
            logger.warning(
                "generation: TRANSPORT retry %d (attempt %d/%d) in %.2fs -- %s",
                retries["transport"], attempts, Config.MAX_GENERATION_ATTEMPTS, delay, exc,
            )
            await asyncio.sleep(delay)
            continue

        if first is not None:
            retry_wait = t_attempt - t_open
            break

        # Zero-token completion. NOT counted here: `src/api/app.py` already
        # increments the same instrument when this generator yields nothing
        # (its StopAsyncIteration path), and `record_if_empty_completion`
        # reuses that one counter on purpose -- one closed series across every
        # arm is what lets 11a compare local and hosted zero-token rates.
        # Counting in both places would double every event.
        if not quality_retry_on or attempts >= Config.MAX_GENERATION_ATTEMPTS:
            if not quality_retry_on:
                logger.info(
                    "generation: zero-token completion on %s; quality retry is OFF "
                    "for this model (CP-γ default) -- returning empty", model,
                )
            retry_wait = t_attempt - t_open
            break
        retries["quality"] += 1
        logger.warning(
            "generation: QUALITY retry %d (attempt %d/%d) -- zero-token completion on %s",
            retries["quality"], attempts, Config.MAX_GENERATION_ATTEMPTS, model,
        )

    if first is not None:
        yield first
        async for token in stream.chunks:
            yield token

    if usage is not None:
        _fill_usage_dict(usage, stream.usage)
        usage["retry_wait_s"] = round(retry_wait, 4)
        # Split by class so 11b's retry cost multiplier can be reported per
        # class rather than as one blended number -- a transport retry and a
        # duplicated generation have different causes and different fixes.
        usage["transport_retries"] = retries["transport"]
        usage["quality_retries"] = retries["quality"]
        usage["attempts"] = attempts


async def _open_stream(provider: Provider, messages: list[dict], model: str) -> GenerationStream:
    """One guarded attempt. Separate so the retry loop above reads as a retry
    rather than as a wall of keyword arguments."""
    return await guarded_generate(
        provider,
        messages,
        tier="default",
        tenant=current_tenant(),
        # No USD budget. R48: at the free tier there is no USD ceiling to
        # breach -- Google's rolling spend cap is literally $0 -- so the
        # binding constraint is QUOTA, enforced by the limiter above. Passing
        # an invented USD ceiling would make the guard look like it was
        # enforcing something it is not.
        budget=None,
        limiter=_limiter.limiter_for(model),
        breaker=_breaker,
        estimated_cost_usd=_estimated_cost_usd(messages, model),
        max_input_tokens=Config.CTX_SIZE,
        max_history_messages=MAX_HISTORY_MESSAGES,
        max_output_tokens=Config.MAX_TOKENS,
    )


def _fill_usage_dict(usage: dict, reported: Usage) -> None:
    """Coerce the gateway's `Usage` into the plain dict callers already read.

    Only non-None values are written: an absent count must stay absent so the
    request prices UNPRICED, rather than becoming a 0 that reads as free.
    """
    for field in ("prompt_tokens", "completion_tokens", "model"):
        value = getattr(reported, field, None)
        if value is not None:
            usage[field] = value
    reasoning = getattr(reported, "reasoning_tokens", None)
    if reasoning is not None:
        usage["reasoning_tokens"] = reasoning


def build_sources_footer(sources: list[dict]) -> str:
    """Render the deduped sources list (src.rag.context.build_context output)
    as the programmatic Sources footer, e.g.:

        Sources:
        * The Brothers Karamazov — Book 5, Chapter 4
        * Crime and Punishment — Part 3, Chapter 2

    Returns "" for an empty list (P6 omits the Sources event entirely when
    retrieval found no context, logging no_context=true per design IV.3).
    """
    if not sources:
        return ""
    lines = ["Sources:"]
    for s in sources:
        part_label = BOOK_META.get(s.get("book"), {}).get("part_label", "Part")
        lines.append(f"* {s.get('book')} — {part_label} {s.get('part')}, Chapter {s.get('chapter')}")
    return "\n".join(lines)
