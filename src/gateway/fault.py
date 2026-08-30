"""Fault injection wrapper (v2 P4 Batch C step 7, decided at CP-4b: D7/D8/D9,
`plans/mitya-v2/02-decide/decisions-p4-cp4b.md`).

One `FaultProvider` wraps the resolved adapter, selected in
`src/rag/generate.py`'s `_provider_for()` behind `Config.FAULT_INJECT` (D8).
It is NOT a new `Provider` implementation with its own vendor logic -- it
delegates to `inner` once its configured attempt count is spent, so from the
retry loop's point of view an injected fault is indistinguishable in shape
from a real one arriving from `inner` itself.

Per-class injection points were rejected as a single one (D7): cold cache and
app-side budget exhaustion need no injector at all (an existing dial /
existing env var), so this module only has to reproduce the three classes
that DO arise from a provider adapter: an absorbed or exhausting 429, a
timeout, and an in-band refusal. Only the absorbed-429 row has a genuine
client signature on record (R87); the taxonomy reports the rest as
**fidelity unverified** (D9) -- that label lives in the evidence log this
module's output feeds, not in this file.

Raises on the first `anext()` of the returned stream, not from `generate()`
itself -- matching the genuine site a real Groq 429 arose
(`src/gateway/groq.py`'s `_chunks()`, via `response.raise_for_status()`), so
`src/rag/generate.py`'s retry loop catches an injected fault exactly the way
it catches a real one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from src.gateway.contract import GenerationStream, Provider, Usage
from src.gateway.ladder import ContentRefused
from src.gateway.retry import TransportError

logger = logging.getLogger(__name__)

_CLASSES = ("429", "timeout", "refused")


@dataclass(frozen=True)
class FaultSpec:
    kind: str  # "429" | "timeout" | "refused"
    attempts: int | None  # None means "*" -- fail every attempt (the exhausting row)


def parse_fault_spec(raw: str) -> FaultSpec:
    """Parse `Config.FAULT_INJECT`'s grammar: `<class>:<attempts|*>`.

    `429:2` = fail the first two attempts of EVERY request then pass through
    (the absorbed row, R65/R68's shape). `429:*` = fail every attempt (the
    exhausting row -- never absorbed). `timeout:1`, `refused:*` follow the
    same grammar for the other two classes.
    """
    try:
        kind, attempts_str = raw.split(":", 1)
    except ValueError:
        raise ValueError(
            f"MITYA_FAULT_INJECT must be '<class>:<attempts|*>', got {raw!r}"
        ) from None
    if kind not in _CLASSES:
        raise ValueError(f"unknown fault class {kind!r} (expected one of {_CLASSES})")
    if attempts_str == "*":
        attempts = None
    else:
        try:
            attempts = int(attempts_str)
        except ValueError:
            raise ValueError(f"fault attempts must be an integer or '*', got {attempts_str!r}") from None
        if attempts < 1:
            raise ValueError(f"fault attempts must be >= 1, got {attempts}")
    return FaultSpec(kind=kind, attempts=attempts)


class FaultProvider:
    """Wraps `inner`, failing the first `spec.attempts` calls (or every call,
    for `attempts=None`) with `spec.kind`, then delegating to `inner`.

    Per-instance attempt counter, matching D8: `_provider_for()` is called
    once per request (`src/rag/generate.py`'s `stream_answer()`), so this
    object is never shared across requests -- no lock, no shared state.
    """

    def __init__(self, inner: Provider, spec: FaultSpec) -> None:
        self._inner = inner
        self._spec = spec
        self._attempt = 0
        # Mirrors the wrapped adapter's `model` attribute (guard.py reads
        # `getattr(provider, "model", "unknown")` for logging) so wrapping a
        # provider in fault injection does not blank out its identity there.
        self.model = getattr(inner, "model", None)

    async def generate(
        self,
        messages: list[dict],
        tier: str,
        tenant: str,
        budget: float | None,
    ) -> GenerationStream:
        self._attempt += 1
        should_fail = self._spec.attempts is None or self._attempt <= self._spec.attempts
        if not should_fail:
            return await self._inner.generate(messages, tier, tenant, budget)

        logger.warning(
            "FAULT INJECTION active: attempt %d raising %r -- this run's answers "
            "are not real (Config.FAULT_INJECT is set)",
            self._attempt, self._spec.kind,
        )
        kind = self._spec.kind

        async def _raise() -> AsyncIterator[str]:
            # Unreachable `yield` below is what makes this an async generator
            # function -- the raise happens before it, on the stream's first
            # `anext()`, matching the genuine 429's raise site (module
            # docstring).
            if kind == "429":
                raise TransportError("injected: provider quota exceeded", retry_after=2.0)
            if kind == "timeout":
                raise TransportError("injected: provider timeout")
            raise ContentRefused("injected: provider refused to generate")
            yield ""  # pragma: no cover -- unreachable

        return GenerationStream(chunks=_raise(), usage=Usage())
