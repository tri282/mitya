"""Google Gemini adapter (v2 P2 Batch C step 3), targeting the **native**
`/v1beta/interactions` endpoint -- never Google's OpenAI-compatible endpoint
and never `streamGenerateContent` (R53, CP-alpha's condition: reaching for
either collapses the divergence the pair was chosen for, and voids the
checkpoint).

Two wire facts measured live this session (R55) and enforced here, not just
documented:

1. `?alt=sse` alone does NOT stream. Without `"stream": true` in the request
   body, Google returns HTTP 200, `content-type: application/json`, one
   blocking JSON line -- no error, no warning. `generate()` asserts
   `content-type: text/event-stream` and raises loudly rather than silently
   returning a non-streaming reply that would pass a naive correctness test
   and invalidate every TTFT number downstream.
2. Google returns HTTP 200 carrying a REFUSAL. Content blocks live in their
   own code space (`safety`, `recitation`, `prohibited_content`, `spii`,
   `blocklist`, `content_blocked`, ...), not a status code. `generate()`
   raises `ContentRefused` (src/gateway/ladder.py) when it sees one -- a
   third outcome, distinct from success and from transport failure.

R56 (a later, corrective live measurement -- 7 non-streaming calls, not this
adapter's own probe below) overturns R55's claim that Google's reasoning
split is symmetric with Groq's: `gemini-3.5-flash-lite` spends ZERO thought
tokens; `gemini-3.5-flash`/`3.6-flash` spend 96-97%. It also found that
`max_output_tokens` includes thinking -- a 300-token budget on a thinking
model produced an 8-token ANSWER because thought consumed the rest, finish
state "truncated". That is a new empty/near-empty-completion cause, distinct
from the weak-local-model EOS-sampling one (R28/R29), and retrying does not
help it (every retry re-pays the full thinking cost and truncates
identically). This adapter records `finish_reason` and logs a warning on a
truncated finish (see `_fill_usage_from_completed`/`generate()` below) but
does not itself retry, raise the token budget, or otherwise decide policy --
that is C4/step 8(b)'s call.

**Live-verified end to end, 2026-08-28.** This adapter streams a real answer
and fills real token counts. Getting there corrected four things, each of
which had been written from `generateContent`'s documented shape and each of
which the API rejected or silently ignored:

  * `generation_config` / `max_output_tokens` are **snake_case**, not
    camelCase ("Unknown parameter 'generationConfig'. Did you mean
    'generation_config'?");
  * `system_instruction` is a plain **string**, not `{"parts": [...]}`;
  * the prompt goes in **`input`**, not `contents` -- "Unknown parameter
    'contents'." `contents` is the classic API's field and this endpoint has
    no such parameter;
  * on `interaction.completed` the usage object is nested at
    **`payload["interaction"]["usage"]`**, not at the payload root, and its
    input key is **`total_input_tokens`**. Reading the root returned an empty
    dict, so a successful streamed answer was reported with every token count
    None -- UNPRICED despite a perfectly good count sitting in the payload.
    This is the dangerous one: nothing raised, nothing logged, the answer was
    correct, and only the cost row was silently empty.

Still NOT established: multi-turn. `input` as a list of turns is refused with
"When using the steps-based API version, use step_list input format instead of
turn_list", and that format was not worked out here -- `_to_google_payload()`
raises on an assistant turn rather than dropping history. Single-turn is all
mitya needs today; P6 (memory) is where this has to be solved.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from src.config import Config
from src.gateway.contract import GenerationStream, Usage
from src.gateway.ladder import ContentRefused
from src.gateway.retry import TransportError
from src.pricing import cost_usd

logger = logging.getLogger(__name__)

INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
# Free GET, no key spend (R55 point 5) -- the cheapest available liveness
# check. Not wired into the ladder's readiness probe here: that bridge is
# src/api/app.py's `_dependency_status()`, out of this step's scope (step 6).
LIST_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# The refusal code space (R53/R55, from the provider survey's error-taxonomy
# axis): reached through a field in the body, never through an HTTP status.
_REFUSAL_CODES = frozenset({
    "safety", "recitation", "language", "prohibited_content",
    "spii", "blocklist", "image_safety", "content_blocked",
})

_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=5.0, pool=5.0)


class GoogleStreamingContractViolation(RuntimeError):
    """Must-hit #1 (R55): a 200 whose content-type is not text/event-stream.
    Deliberately NOT a TransportError -- retrying will not fix a request body
    that is missing "stream": true, so this must not enter retry.py's bounded
    retry loop; it is a configuration bug in the adapter or the caller, not a
    flaky call."""


@dataclass
class GoogleUsage(Usage):
    """Adds Google's reasoning-token split and finish state without touching
    the frozen contract (`src/gateway/contract.py` -- Batch A froze it, step
    3 does not reopen it). Subclassing satisfies `Provider`'s `->
    GenerationStream` contract exactly: any caller that only knows about the
    base `Usage` fields still reads them correctly; a caller that wants the
    split reads `usage.reasoning_tokens` when `isinstance(usage,
    GoogleUsage)`.

    `reasoning_tokens` here is R56's `total_thought_tokens` -- reported by
    Google as its OWN field, separate from `total_output_tokens`. This is the
    opposite convention from Groq (`src/gateway/groq.py`'s `GroqUsage`),
    which folds reasoning INTO `completion_tokens` and reports the split via
    a details sub-object. `base.completion_tokens` on THIS class is
    normalized to `total_output_tokens + total_thought_tokens` -- the sum --
    specifically so a caller comparing `usage.completion_tokens` across both
    providers reads the same thing (total billed completion tokens) either
    way; reading Google's raw `total_output_tokens` alone here would silently
    under-report the answer/reasoning split on any arm that is thinking.

    `finish_reason` (R56 point 3) is the raw string Google reports --
    `"truncated"` (thinking exhausted the budget before the answer did, R56's
    new empty/near-empty-completion cause -- retrying pays the full thinking
    cost again and truncates identically) is a DIFFERENT outcome from a
    stream that genuinely yielded nothing. This adapter only RECORDS it;
    deciding what to do about it (raise the budget? never retry a
    truncation?) is C4/step 8(b)'s call, not this step's."""

    reasoning_tokens: int | None = None


def _to_google_payload(
    messages: list[dict],
    model: str,
    *,
    max_tokens: int | None,
    temperature: float | None,
    top_p: float | None,
    safety_settings: list[dict] | None,
) -> dict:
    """Build the `/v1beta/interactions` request body.

    Every field here is live-confirmed against the real API (this session):

    * `generation_config` / `max_output_tokens` are **snake_case**, not
      camelCase -- "Unknown parameter 'generationConfig'. Did you mean
      'generation_config'?";
    * `system_instruction` takes a plain **string**, not `{"parts": [...]}`;
    * the user turn goes in **`input`**, not `contents`. `contents` is the
      classic `generateContent` field and the interactions API rejects it
      outright: "Unknown parameter 'contents'." A 200 with this function's
      output has now been observed.

    **Single-turn only, deliberately.** `input` as a list of turns is refused
    with "When using the steps-based API version, use step_list input format
    instead of turn_list", and that format is not established here. Mitya is
    single-turn today (`build_messages()` returns exactly one system and one
    user message), so this raises on an assistant turn rather than silently
    dropping conversation history -- a dropped turn would be invisible in the
    answer and fatal at P6, where multi-turn is the whole point.
    """
    system_texts: list[str] = []
    user_texts: list[str] = []
    for m in messages:
        role = m.get("role")
        text = m.get("content") or ""
        if role == "system":
            system_texts.append(text)
        elif role == "assistant":
            raise ValueError(
                "google adapter: assistant turns need the interactions API's "
                "step_list input format, which is not implemented. Single-turn "
                "only -- see P6 (memory) before sending conversation history."
            )
        else:
            user_texts.append(text)

    payload: dict = {
        "model": model,
        "input": "\n\n".join(user_texts),
        # Must-hit #1 (R55): this is the field that actually turns streaming
        # on. `?alt=sse` in the URL is necessary but not sufficient.
        "stream": True,
    }
    if system_texts:
        payload["system_instruction"] = "\n\n".join(system_texts)
    if safety_settings:
        payload["safety_settings"] = safety_settings
    generation_config = {}
    if max_tokens is not None:
        generation_config["max_output_tokens"] = max_tokens
    if temperature is not None:
        generation_config["temperature"] = temperature
    if top_p is not None:
        generation_config["top_p"] = top_p
    if generation_config:
        # Live-confirmed key: this session's first 400 was literally
        # "Unknown parameter 'generationConfig'. Did you mean
        # 'generation_config'?" -- the interactions API is snake_case where
        # generateContent is camelCase.
        payload["generation_config"] = generation_config
    return payload


async def _iter_sse_events(response: httpx.Response) -> AsyncIterator[tuple[str, dict]]:
    """Parse the named-event SSE machine (survey P-1 / R55): an `event:` line
    names the frame, one or more `data:` lines carry its JSON payload, a
    blank line ends the frame. Falls back to the payload's own `event_type`
    field when no `event:` line was sent (R53's text-delta example shows
    `event_type` inline too), so a change in which of the two carries the
    name does not silently stop dispatch."""
    event_name: str | None = None
    async for raw_line in response.aiter_lines():
        line = raw_line.rstrip("\n")
        if not line:
            event_name = None
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
            continue
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            yield (event_name or "done", {})
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("google adapter: unparseable SSE data: %r", data)
            continue
        yield (event_name or payload.get("event_type", "unknown"), payload)


def _refusal_reason(payload: dict) -> str | None:
    """Scan the plausible key spellings for a refusal code (see module
    docstring: exact key names inside `interaction.completed` are
    unverified). One level of nesting is checked because a terminal event
    plausibly wraps its content under an `interaction`/`output`/`result` key
    rather than at the top level."""
    for key in ("status", "finish_reason", "finishReason", "block_reason", "blockReason", "reason"):
        value = payload.get(key)
        if isinstance(value, str) and value.lower() in _REFUSAL_CODES:
            return value
    for nested_key in ("interaction", "output", "result"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            found = _refusal_reason(nested)
            if found:
                return found
    return None


def _finish_reason(payload: dict) -> str | None:
    """Whatever raw finish/status string Google reports on the terminal
    event, unfiltered (unlike `_refusal_reason`, which only matches the
    refusal code space). R56's new outcome -- `"truncated"`, thinking ate the
    token budget before the answer did -- lives in this same field space and
    must not be silently discarded just because it is not a refusal. Recorded
    on `GoogleUsage.finish_reason`; classifying it (retry? never retry?) is
    C4's job, not this adapter's."""
    for key in ("status", "finish_reason", "finishReason"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    for nested_key in ("interaction", "output", "result"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            found = _finish_reason(nested)
            if found:
                return found
    return None


def _fill_usage_from_completed(usage: GoogleUsage, payload: dict) -> None:
    """Usage arrives on `interaction.completed` (must-hit #5 / R53's survey),
    not per-chunk. R56 (live-measured this session, corrects R55): the
    thought-token field is `total_thought_tokens`, reported SEPARATELY from
    `total_output_tokens` -- checked first, ahead of the earlier guessed
    spellings kept as a fallback since the surrounding envelope shape is
    still unconfirmed (see module docstring).

    `completion_tokens` is the SUM of output + thought, not
    `total_output_tokens` alone -- see `GoogleUsage`'s docstring for why:
    Groq's `completion_tokens` already includes reasoning, and a cross-arm
    reader must see the same quantity in that field on both arms, or a
    USD/answer comparison silently favors whichever arm under-reports."""
    # Live-confirmed nesting: on `interaction.completed` the usage object sits
    # at payload["interaction"]["usage"], NOT at the payload root. Reading only
    # the root silently left every token count None on a successful 200 -- the
    # request streamed a real answer and was still reported UNPRICED.
    u = payload.get("usage") or (payload.get("interaction") or {}).get("usage") or {}
    # `total_input_tokens` is the live-confirmed key. The rest are earlier
    # guesses kept as fallbacks; without the first spelling, prompt_tokens
    # came back None on a real 200 and the request would have been UNPRICED
    # despite a perfectly good token count sitting in the payload.
    usage.prompt_tokens = (
        u.get("total_input_tokens") or u.get("prompt_tokens")
        or u.get("promptTokens") or u.get("input_tokens")
    )
    output_tokens = (
        u.get("total_output_tokens") or u.get("completion_tokens")
        or u.get("completionTokens") or u.get("output_tokens")
    )
    thought_tokens = (
        u.get("total_thought_tokens") or u.get("reasoning_tokens")
        or u.get("thoughts_tokens") or u.get("thoughtsTokenCount")
    )
    usage.reasoning_tokens = thought_tokens
    if output_tokens is not None or thought_tokens is not None:
        usage.completion_tokens = (output_tokens or 0) + (thought_tokens or 0)
    usage.finish_reason = _finish_reason(payload)
    # Real Config.MODEL_RATES lookup (unlike FakeProvider, which must never
    # touch the real table -- there is no vendor there to commit to). See
    # config_artifact.py's _DEFAULT_MODEL_RATES for this arm's rate row.
    usage.cost = cost_usd(usage.model, usage.prompt_tokens, usage.completion_tokens)


class GoogleProvider:
    """Native `/v1beta/interactions` adapter. One instance per model id --
    `tier`/routing choosing between models is a later step's (routing
    family, CP-beta) job; this adapter answers with whatever model it was
    constructed with, same shape as `FakeProvider`."""

    def __init__(
        self,
        model: str = "models/gemini-3.5-flash-lite",
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        safety_settings: list[dict] | None = None,
    ) -> None:
        self.model = model
        self._max_tokens = max_tokens if max_tokens is not None else Config.MAX_TOKENS
        self._temperature = temperature if temperature is not None else Config.GEN_TEMPERATURE
        self._top_p = top_p if top_p is not None else Config.GEN_TOP_P
        self._safety_settings = safety_settings

    async def generate(
        self,
        messages: list[dict],
        tier: str,
        tenant: str,
        budget: float | None,
    ) -> GenerationStream:
        logger.debug(
            "GoogleProvider.generate: model=%s tier=%s tenant=%s", self.model, tier, tenant
        )
        usage = GoogleUsage(model=self.model)
        payload = _to_google_payload(
            messages,
            self.model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            top_p=self._top_p,
            safety_settings=self._safety_settings,
        )

        async def _chunks() -> AsyncIterator[str]:
            headers = {"x-goog-api-key": Config.GOOGLE_API_KEY}
            url = f"{INTERACTIONS_URL}?alt=sse"
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    async with client.stream("POST", url, json=payload, headers=headers) as response:
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "")
                        if "text/event-stream" not in content_type:
                            # Must-hit #1: fail loudly, do not silently drain
                            # a blocking JSON reply as if it were a stream.
                            raise GoogleStreamingContractViolation(
                                f"google adapter: expected text/event-stream, got "
                                f"{content_type!r} -- 'stream': true likely did not "
                                f"reach the request body (R55)"
                            )
                        async for event_name, data in _iter_sse_events(response):
                            if event_name == "error":
                                err = data.get("error", {})
                                raise TransportError(f"google interaction error: {err}")
                            if event_name == "step.start":
                                step = data.get("step", {})
                                if step.get("type") == "thought":
                                    # Reasoning step -- never answer text
                                    # (must-hit #4, mirrors Groq's
                                    # delta.reasoning). No text to yield here;
                                    # Google's thought text (if any) would
                                    # arrive on a later step.delta tied to
                                    # this step's index, which this adapter
                                    # does not thread through -- see the
                                    # module's unverified-schema note.
                                    continue
                                continue
                            if event_name == "step.delta":
                                delta = data.get("delta", {})
                                if delta.get("type") == "text":
                                    text = delta.get("text")
                                    if text:
                                        yield text
                                continue
                            if event_name == "interaction.completed":
                                _fill_usage_from_completed(usage, data)
                                reason = _refusal_reason(data)
                                if reason:
                                    # Must-hit #3: HTTP 200, but this is not a
                                    # success. Distinct outcome (Rung.REFUSED).
                                    raise ContentRefused(f"google: content blocked ({reason})")
                                # R56: max_tokens is a THINKING+answer budget
                                # on a thinking model -- a truncated finish is
                                # not a tuning nicety, it is thinking eating
                                # the answer (measured: 300-token budget,
                                # 288 spent on thought, an 8-token answer).
                                # This adapter does not retry or raise the
                                # budget itself (C4's call) -- it only makes
                                # the truncation visible rather than letting
                                # it pass as an ordinary short reply.
                                if usage.finish_reason and "trunc" in usage.finish_reason.lower():
                                    logger.warning(
                                        "google adapter: answer truncated (finish_reason=%s, "
                                        "reasoning_tokens=%s of completion_tokens=%s) -- likely "
                                        "thinking exhausted the token budget, not a short answer "
                                        "(R56)",
                                        usage.finish_reason, usage.reasoning_tokens, usage.completion_tokens,
                                    )
                                continue
                            # interaction.created / interaction.status_update /
                            # step.stop / done: no action needed by this
                            # adapter today.
            except httpx.ConnectError as e:
                raise TransportError(f"google adapter: connect failed: {e}") from e
            except httpx.TimeoutException as e:
                raise TransportError(f"google adapter: timed out: {e}") from e
            except httpx.HTTPStatusError as e:
                # A real transport-level failure (429/5xx/etc.) -- distinct
                # from the 200-carrying-a-refusal case above, and the one
                # retry.py's bounded retry exists to absorb.
                raise TransportError(f"google adapter: HTTP {e.response.status_code}: {e}") from e

        return GenerationStream(chunks=_chunks(), usage=usage)
