"""httpx streaming client for the local llama-server completion endpoint.

Prompt composition (design III.4, ruling R19): the fine-tune's training rows
never contained context in the prompt and its system prompt forbids
referencing external sources, so injected passages are out-of-distribution.
System = the verbatim fine-tuned persona head (stay near the training
distribution) + retrieved passages framed as the author's own recollections;
the user turn carries the question only.

Generation streams OpenAI-compatible SSE deltas from llama-server's
/v1/chat/completions (ruling R16 -- httpx, not LiteLLM: the whole need is a
~30-line streaming client, not a 100+-provider abstraction over one
localhost endpoint). The Sources footer (design III.5) is a separate,
non-streamed piece of text the caller (P6) emits as its own final SSE event
after the token stream ends -- build_sources_footer() renders it here since
it is a generation/serving-layer concern, not part of prompt-context
assembly (src/rag/context.py owns dedup + ordering of the sources list).
"""

import json
import logging
from collections.abc import AsyncIterator

import httpx

from src.config import Config
from src.rag.ingest import BOOK_META

logger = logging.getLogger(__name__)

# Verbatim fine-tune persona-head system prompt -- must match
# src/training.py SYSTEM_PROMPT word-for-word (design III.4). Inlined rather
# than imported: src/training.py pulls in unsloth/torch/trl/peft/transformers
# at module scope, which must never load into the lightweight serving path.
PERSONA_HEAD = (
    "You are Fyodor Dostoevsky. Answer the user's question in the first person, "
    "incorporating your deep literary, existential, and philosophical tone. "
    "Give no editorial comments and references to external sources."
)


class LlamaServerUnavailable(Exception):
    """llama-server refused the connection -- P6 maps this to HTTP 503."""


class LlamaServerTimeout(Exception):
    """llama-server did not respond within budget -- P6 maps this to HTTP 504."""


def build_messages(persona_head: str, passages_block: str, question: str) -> list[dict]:
    """Compose the chat messages sent to llama-server.

    System = persona head + passages framed as the author's own recollections
    (R19). User turn carries the question only -- context never enters the
    user turn, which would push the prompt further out of the fine-tune's
    training distribution than necessary.
    """
    system = (
        persona_head
        + "\n\nRecall these passages from your own novels; let their substance "
          "inform your answer, but never mention passages, sources, or quotations:\n\n"
        + passages_block
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]


async def stream_answer(messages: list[dict]) -> AsyncIterator[str]:
    """Stream answer token text from llama-server's OpenAI-compatible endpoint.

    Connect timeout 5s / read timeout 120s (design III.5, V). Raises
    LlamaServerUnavailable on connection failure and LlamaServerTimeout on a
    stalled connect/read -- both raised before or during iteration so P6 can
    catch them and surface 503/504 instead of a hung stream.
    """
    payload = {"messages": messages, "max_tokens": Config.MAX_TOKENS, "stream": True}
    timeout = httpx.Timeout(connect=5.0, read=120.0, write=5.0, pool=5.0)
    url = f"{Config.LLAMA_SERVER_URL}/v1/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload) as response:
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
                        logger.warning("stream_answer: unparseable SSE chunk: %r", data)
                        continue
                    choices = chunk.get("choices") or [{}]
                    token = choices[0].get("delta", {}).get("content")
                    if token:
                        yield token
    except httpx.ConnectError as e:
        logger.error("stream_answer: llama-server unreachable at %s: %s", url, e)
        raise LlamaServerUnavailable(str(e)) from e
    except httpx.TimeoutException as e:
        logger.error("stream_answer: llama-server timed out (%s): %s", url, e)
        raise LlamaServerTimeout(str(e)) from e


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
