"""llama-server adapter (v2 P2 Batch C step 6).

`local-q4km` behind the same `Provider` contract as the hosted arms, so the
local model stops being a special case in the request path and becomes one more
provider that the gateway may or may not be pointed at.

**A3 governs when this may be used.** `local-q4km` is *frozen recorded numbers,
a chart line, not a live arm*: nothing in the `/chat` request path may start
llama-server, and the frozen quality row must never be re-measured. This adapter
therefore exists for two things only -- keeping `scripts/eval_quality.py` and
the offline eval path able to talk to a local model at all, and leaving a
deliberate, opt-in escape hatch if the hosted arms are unreachable. It is not
the default: `ServingConfig.serving_model` defaults to
`models/gemini-3.5-flash-lite` from Batch C step 6 onward.

Deleting the direct call instead of adapting it was the alternative, and was not
taken: `stream_answer()`'s llama-server client is the thing that produced the
frozen baseline, and stranding the eval path to enforce a *default* would trade
a real capability for a rule that the default already enforces.

Subclassing `GroqProvider` is a statement about the wire format, not about the
vendor. llama-server exposes the same OpenAI-compatible
`/v1/chat/completions` streaming shape, including the `stream_options`
usage-chunk extension that `src/rag/generate.py` already relied on, so only the
endpoint and the auth header differ. A third copy of that parser in this repo
is how three copies drift apart.

One real difference is inherited harmlessly: `GroqProvider` warns on
`finish_reason == "length"` about reasoning exhausting the token budget (R56).
`local-q4km` is not a reasoning model, so `reasoning_tokens` stays `None` and
that warning simply reports an ordinary truncation. The empty-completion
behaviour that `EMPTY_RETRIES` exists for (R28/R29 -- the weak q4 sampling EOS
as its first token) is *not* handled here: retry policy is C4/step 8(b)'s call,
and this adapter deliberately does not decide it.
"""

from __future__ import annotations

import logging

from src.config import Config
from src.gateway.groq import GroqProvider

logger = logging.getLogger(__name__)

LOCAL_MODEL_ID = "local-q4km"


class LocalProvider(GroqProvider):
    """llama-server on `Config.LLAMA_SERVER_URL`, OpenAI-compatible."""

    def __init__(self, model: str = LOCAL_MODEL_ID, **kwargs) -> None:
        super().__init__(model=model, **kwargs)

    def _endpoint(self) -> str:
        return f"{Config.LLAMA_SERVER_URL}/v1/chat/completions"

    def _headers(self) -> dict:
        # No auth: llama-server is local and unauthenticated. Returning {} here
        # rather than inheriting is the point -- inheriting would put
        # GROQ_API_KEY in an Authorization header on a plaintext localhost
        # request, which is a key leak to a process that has no use for it.
        return {}
