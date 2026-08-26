"""Cost arithmetic for the telemetry spine (v2 P1 step 3, charter G5).

Arithmetic only. The rate table lives in `Config.MODEL_RATES` so that step 5's
versioned config artifact has one place to sweep rather than two.

The contract this module exists to hold, in one line: **a cost is either a real
number, or None -- never a zero standing in for "we do not know".** Every path
that cannot produce a figure returns None, and callers stamp the span unpriced
rather than reporting free work as free.

Note the two distinct kinds of zero, which are NOT interchangeable:

  * 0.0 with metered=True   -- a metered provider genuinely charged nothing.
  * 0.0 with metered=False  -- nobody meters this. Local inference. The figure
                              is honest but it is not a price, and any
                              aggregate containing it is provisional (II).
  * None                    -- unpriced. Unknown model, or token counts the
                              serving path could not obtain. Unpublishable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.config import Config

logger = logging.getLogger(__name__)

# Rates are published per million tokens by every provider; keep the table in
# that unit and divide once, here, rather than storing pre-scaled floats that
# nobody can check against a pricing page.
TOKENS_PER_RATE_UNIT = 1_000_000


@dataclass(frozen=True)
class Cost:
    """A priced (or deliberately unpriced) stage cost.

    `usd is None` means unpriced -- see the module docstring. `metered` is
    carried alongside rather than derived, because the caller stamping a span
    needs both and re-looking-up the rate to get the second one is how the two
    drift apart.
    """

    usd: float | None
    metered: bool

    @property
    def priced(self) -> bool:
        return self.usd is not None


UNPRICED = Cost(usd=None, metered=False)

# Stages that run on hardware we own: genuinely free, and genuinely not
# metered. Separate constant from UNPRICED because "this cost nothing" and "we
# could not work out what this cost" must never render as the same span.
FREE = Cost(usd=0.0, metered=False)


def is_metered(model: str) -> bool:
    """Whether anyone bills for this model's tokens.

    Reads the table directly rather than through Config.rate_for() so that an
    unknown id does not log a second warning per request -- the real pricing
    call already warns once. An unknown id is not metered as far as this
    predicate is concerned; whether it is PRICED is a separate question and
    cost_usd() is what answers it.
    """
    rate = Config.MODEL_RATES.get(model)
    return bool(rate and rate["metered"])


def cost_usd(
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> Cost:
    """Price a generation stage.

    Either token count may be None, which is the normal case for a per-stage
    split: `generate.first_token` prices prompt tokens alone and passes
    completion_tokens=None, `generate.stream` does the reverse. A None count is
    treated as "this stage does not bill for that half", contributing zero --
    NOT as a missing measurement.

    Both being None is different: nothing was measured, so nothing can be
    priced, and the result is UNPRICED.
    """
    if prompt_tokens is None and completion_tokens is None:
        logger.warning(
            "cost: model %r has no token counts -- UNPRICED. The serving path did not "
            "report usage; see src/rag/generate.py's usage-chunk handling.",
            model,
        )
        return UNPRICED

    rate = Config.rate_for(model)
    if rate is None:
        # rate_for() has already logged; do not double-warn on the hot path.
        return UNPRICED

    usd = (
        (prompt_tokens or 0) * rate["prompt"]
        + (completion_tokens or 0) * rate["completion"]
    ) / TOKENS_PER_RATE_UNIT
    return Cost(usd=usd, metered=rate["metered"])


def total_usd(costs: list[Cost]) -> Cost:
    """Roll stage costs up into a request cost.

    Three rules, in priority order, and the order matters:

    1. Any unpriced stage makes the total unpriced. A request that paid for
       something we could not price does not have a known cost, and quietly
       dropping the unknown term would under-report it -- exactly the failure
       mode Config.rate_for() refuses to commit.
    2. The total is metered only if EVERY contributing stage is metered. One
       unmetered stage makes the sum a mixed quantity, and charter II's
       provisional rule keys on this flag.
    3. Otherwise it is the plain sum. There is deliberately no second,
       independently-measured request total to disagree with the sum of its
       stages.
    """
    if not costs:
        return FREE
    if any(not c.priced for c in costs):
        return UNPRICED
    return Cost(
        usd=sum(c.usd for c in costs),
        metered=all(c.metered for c in costs),
    )
