"""The fake provider (v2 P2 step 2's verification instrument, schedule Batch A).

Per the schedule: "a contract with one implementation and no test double is
not verifiable; the fake is how step 2 is checked, not additional scope." It
is also load-bearing past this batch -- Batch B builds the budget guard,
retry policy, cache extension, and degradation ladder against this fake, with
no vendor chosen and no key provisioned (CP-alpha, which picks providers,
comes after Batch B).

Synthetic token counts are declared on the fixture below, not computed by a
tokenizer. Adding a tokenizer dependency to price a made-up reply is exactly
the overreach this batch must not do -- there is no real provider yet for a
real token count to describe, and a heuristic estimate would be a fabricated
number wearing a measurement's clothes (the same objection `pricing.py`
raises about estimating real usage).

`FakeProvider`'s price is computed from its own fixture-declared USD/Mtok
rates, never through `src.pricing.cost_usd()` / `Config.MODEL_RATES` -- this
model does not exist in that table and never will (CP-alpha chooses real
providers; adding a fake one to the versioned rate table would be a vendor
commitment this batch is explicitly not allowed to make).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from src.gateway.contract import GenerationStream, Usage
from src.pricing import TOKENS_PER_RATE_UNIT, Cost

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fixture:
    """One recorded reply to replay. Prices are per-token and declared here,
    not looked up -- see the module docstring."""

    chunks: tuple[str, ...]
    prompt_tokens: int
    completion_tokens: int
    # USD per MILLION tokens, matching Config.MODEL_RATES' unit and the unit
    # every provider publishes. Two units for one quantity in one codebase is
    # how a factor-of-1e6 slips into a budget guard.
    prompt_rate: float
    completion_rate: float


# One fixture is enough for Batch A/B's verification surface (contract shape,
# budget-guard ordering, cache attribution). Per-tier/per-scenario fixtures
# are a Batch C+ concern, once real arms exist for a fake to stand in for.
DEFAULT_FIXTURE = Fixture(
    chunks=(
        "Marvel ", "not ", "at ", "the ", "world's ", "evil, ",
        "but ", "at ", "man's ", "capacity ", "to ", "endure ", "it.",
    ),
    prompt_tokens=64,
    completion_tokens=13,
    # Round, made-up rates in USD/Mtok -- not a real vendor's price, and not
    # meant to resemble one. 1.0 and 4.0 are chosen only so the resulting
    # total is a non-zero, easily hand-checked number.
    prompt_rate=1.0,
    completion_rate=4.0,
)


class FakeProvider:
    """Replays `fixture` for every call, regardless of `messages`/`tier`.

    Records every call in `self.calls` -- Batch B's budget-guard and routing
    tests assert against this list (e.g. "a request over budget never reaches
    `generate()`" is checked by asserting `len(provider.calls) == 0`).
    """

    def __init__(self, model: str = "fake/replay-v1", fixture: Fixture = DEFAULT_FIXTURE) -> None:
        self.model = model
        self._fixture = fixture
        self.calls: list[dict] = []

    async def generate(
        self,
        messages: list[dict],
        tier: str,
        tenant: str,
        budget: float | None,
    ) -> GenerationStream:
        self.calls.append({"messages": messages, "tier": tier, "tenant": tenant, "budget": budget})
        logger.debug(
            "FakeProvider.generate: tier=%s tenant=%s budget=%s -- replaying fixture, no vendor call",
            tier, tenant, budget,
        )
        usage = Usage(model=self.model)
        fixture = self._fixture

        async def _chunks() -> AsyncIterator[str]:
            for chunk in fixture.chunks:
                yield chunk
            # Usage is filled only once every chunk has been yielded --
            # mirrors stream_answer()'s "usage chunk arrives last" shape, so a
            # caller that reads `usage` before draining `chunks` sees the
            # unfilled placeholder rather than a partial answer.
            usage.prompt_tokens = fixture.prompt_tokens
            usage.completion_tokens = fixture.completion_tokens
            usage.cost = Cost(
                usd=(
                    fixture.prompt_tokens * fixture.prompt_rate
                    + fixture.completion_tokens * fixture.completion_rate
                ) / TOKENS_PER_RATE_UNIT,
                metered=True,
            )

        return GenerationStream(chunks=_chunks(), usage=usage)
