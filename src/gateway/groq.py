"""Groq adapter (v2 P2 Batch C step 3), OpenAI-compatible chat-completions
streaming -- close kin to `src/rag/generate.py`'s `_stream_once()` (that
file's own docstring names this: "the whole need is a ~30-line streaming
client"), extended for two things llama-server never had to handle: an
opt-in trailing usage chunk and a reasoning-model delta shape.

Two wire facts measured live this session (R55) and enforced here:

1. GPT-OSS is a reasoning model. `delta.reasoning` / `delta.channel` chunks
   carry chain-of-thought, not answer text -- `delta.content` is `None` on
   those, so a plain `if token:` skips them correctly, but by accident.
   This adapter never yields anything but `delta.content`, on purpose.
2. `completion_tokens` on the trailing usage chunk INCLUDES reasoning tokens
   (measured: 45/60 = 75% on one call). Pricing the total is correct -- it is
   what is billed -- but `completion_tokens_details.reasoning_tokens` is kept
   on `GroqUsage` as its own field so a cross-arm USD/answer comparison can
   subtract it out; see R55 point 3 and `p2-findings.md`'s carried-consequence
   row for 11a's cost table.

`queue_time` / `prompt_time` / `completion_time` (R55 point 4) are recorded
where present -- Google has no equivalent and this is not dropped for
symmetry.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from src.config import Config
from src.gateway.contract import GenerationStream, Usage
from src.gateway.retry import TransportError
from src.pricing import cost_usd

logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"

_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=5.0, pool=5.0)


@dataclass
class GroqUsage(Usage):
    """Adds Groq's reasoning-token split, finish state, and server-side
    latency decomposition without touching the frozen contract (see
    `src/gateway/google.py`'s `GoogleUsage` for the identical rationale).

    `reasoning_tokens` here is a SUB-COUNT already folded into
    `base.completion_tokens` -- the opposite convention from `GoogleUsage`,
    which reports `total_thought_tokens` separately and sums it INTO
    `completion_tokens` to match this arm's shape. No arithmetic is needed on
    this side: Groq's raw `completion_tokens` already is the normalized
    total (R55/R56). `include_reasoning: false` does NOT zero this out
    (R56 point 1 -- it suppresses reasoning from the response text, not from
    the bill); this adapter neither sets that flag nor `reasoning_effort` --
    picking a default is 11a's decision, not this step's.

    `finish_reason` (`choices[0].finish_reason`, e.g. `"stop"`/`"length"`) is
    recorded for the same reason `GoogleUsage.finish_reason` is: a `"length"`
    finish on a reasoning model can mean the answer was truncated by thinking
    eating the token budget (R56), a different empty/near-empty-completion
    cause from the weak-local-model EOS-sampling one (R28/R29) that C4/step
    8(b) must not conflate when it decides whether hosted arms get a retry."""

    reasoning_tokens: int | None = None
    finish_reason: str | None = None
    queue_time: float | None = None
    prompt_time: float | None = None
    completion_time: float | None = None


def _fill_usage(usage: GroqUsage, reported: dict) -> None:
    usage.prompt_tokens = reported.get("prompt_tokens")
    usage.completion_tokens = reported.get("completion_tokens")
    details = reported.get("completion_tokens_details") or {}
    usage.reasoning_tokens = details.get("reasoning_tokens")
    usage.queue_time = reported.get("queue_time")
    usage.prompt_time = reported.get("prompt_time")
    usage.completion_time = reported.get("completion_time")
    usage.cost = cost_usd(usage.model, usage.prompt_tokens, usage.completion_tokens)


class GroqProvider:
    """OpenAI-compatible streaming adapter. One instance per model id, same
    shape as `FakeProvider`/`GoogleProvider` -- routing between models is a
    later step's job."""

    def __init__(
        self,
        model: str = "openai/gpt-oss-120b",
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> None:
        self.model = model
        self._max_tokens = max_tokens if max_tokens is not None else Config.MAX_TOKENS
        self._temperature = temperature if temperature is not None else Config.GEN_TEMPERATURE
        self._top_p = top_p if top_p is not None else Config.GEN_TOP_P

    # Endpoint and auth are methods, not constants inlined below, for exactly
    # one reason: llama-server speaks the same OpenAI-compatible dialect, so
    # `src/gateway/local.py` overrides these two and reuses the parser instead
    # of the repo carrying a third copy of it. That is a statement about the
    # WIRE FORMAT being shared, not a claim that llama-server is Groq.
    def _endpoint(self) -> str:
        return CHAT_COMPLETIONS_URL

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {Config.GROQ_API_KEY}"}

    async def generate(
        self,
        messages: list[dict],
        tier: str,
        tenant: str,
        budget: float | None,
    ) -> GenerationStream:
        logger.debug(
            "%s.generate: model=%s tier=%s tenant=%s",
            type(self).__name__, self.model, tier, tenant,
        )
        usage = GroqUsage(model=self.model)
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "top_p": self._top_p,
            "stream": True,
            # Must-hit #5: usage arrives only when explicitly requested, on a
            # trailing chunk with no `choices`, same OpenAI extension
            # generate.py already relies on for llama-server.
            "stream_options": {"include_usage": True},
        }

        async def _chunks() -> AsyncIterator[str]:
            headers = self._headers()
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    async with client.stream(
                        "POST", self._endpoint(), json=payload, headers=headers
                    ) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            data = line[len("data: "):].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                logger.warning("groq adapter: unparseable SSE chunk: %r", data)
                                continue
                            reported = chunk.get("usage")
                            if reported:
                                _fill_usage(usage, reported)
                            choices = chunk.get("choices") or [{}]
                            finish_reason = choices[0].get("finish_reason")
                            if finish_reason:
                                usage.finish_reason = finish_reason
                                # R56: "length" on a reasoning model can mean
                                # thinking ate the token budget before the
                                # answer did, not that the answer was simply
                                # cut short -- surface it rather than let a
                                # near-empty reply pass as an ordinary one.
                                # (Usage/reasoning_tokens are not yet filled
                                # here -- OpenAI-compat streams the usage
                                # chunk AFTER the finish_reason chunk -- so
                                # this logs the event, not the token split;
                                # the split is on `usage` once draining ends.)
                                if finish_reason == "length":
                                    logger.warning(
                                        "groq adapter: finish_reason=length -- may be thinking "
                                        "exhausting the token budget, not a short answer (R56); "
                                        "see usage.reasoning_tokens once the stream is drained"
                                    )
                            delta = choices[0].get("delta", {})
                            # Must-hit #4 (R55): delta.reasoning / delta.channel
                            # are not answer text. Only delta.content is ever
                            # yielded -- on purpose, not because it happens to
                            # be falsy on the other two.
                            token = delta.get("content")
                            if token:
                                yield token
            except httpx.ConnectError as e:
                raise TransportError(f"groq adapter: connect failed: {e}") from e
            except httpx.TimeoutException as e:
                raise TransportError(f"groq adapter: timed out: {e}") from e
            except httpx.HTTPStatusError as e:
                # Groq sends `retry-after` on 429 and it is authoritative: its
                # leaky bucket knows when capacity actually returns, which a
                # client-side sliding window cannot compute (see TransportError).
                retry_after = None
                raw = e.response.headers.get("retry-after")
                if raw:
                    try:
                        retry_after = float(raw)
                    except ValueError:
                        logger.warning("groq adapter: unparseable retry-after %r", raw)
                raise TransportError(
                    f"groq adapter: HTTP {e.response.status_code}: {e}",
                    retry_after=retry_after,
                ) from e

        return GenerationStream(chunks=_chunks(), usage=usage)
