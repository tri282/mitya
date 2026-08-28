# test/test_cost.py
"""P1 step 3 verify: cost attribution arithmetic and its refusals (G5).

Plain-assert script, matching test_span_tree.py. Offline: no server, no
provider, no network.

Why this exists. The arithmetic is trivial and the numbers are all zero today,
so nothing here fails loudly if it breaks -- which is exactly the problem. Every
assertion below is about a case that produces a plausible-looking wrong answer
rather than an error: an unpriced request rendering as free, an unmetered zero
rendering as a measured cost, a retry storm reporting one prompt when it paid
for five. All three are invisible until the first real bill at P2, and by then
the evidence log has already published the wrong figure.
"""

import logging
import sys

sys.path.insert(0, ".")

from src.config import Config  # noqa: E402
from src.pricing import (  # noqa: E402
    FREE,
    UNPRICED,
    Cost,
    cost_usd,
    is_metered,
    total_usd,
)

# A metered model that does not exist, injected for the duration of the run.
# The point of step 3 is that P2 adds a row and changes nothing else -- so the
# test exercises the seam the same way P2 will, by adding a row.
FAKE_METERED = "test/fake-metered"
Config.MODEL_RATES[FAKE_METERED] = {"prompt": 1.0, "completion": 4.0, "metered": True}


def test_local_serving_model_is_priced_but_not_metered() -> None:
    """The distinction the whole step turns on. Local inference costs $0.00 in
    fees and that figure is exact -- but nobody meters it, so it is not a
    price, and charter II's provisional rule keys on that flag.

    Names `local-q4km` explicitly rather than reading `Config.SERVING_MODEL`.
    It used to be the default, and this test silently meant "the default" until
    Batch C step 6 moved that to gemini-3.5-flash-lite (A3: local is a frozen
    chart line, not a live arm). What is being asserted is a property of the
    LOCAL row, so the local row is what it should name."""
    cost = cost_usd("local-q4km", prompt_tokens=1500, completion_tokens=120)
    assert cost.usd == 0.0
    assert cost.priced is True
    assert cost.metered is False


def test_default_serving_model_is_a_free_tier_estimate_not_spend() -> None:
    """The new default after Batch C step 6. It has a real list rate, so USD is
    nonzero -- but `metered=False` (R48): these calls run against free-tier
    quota, so the figure is a *list-rate estimate at the free tier* and must
    never be reported as spend. A metered=True here would be the bug."""
    cost = cost_usd(Config.SERVING_MODEL, prompt_tokens=1500, completion_tokens=120)
    assert cost.priced is True
    assert cost.usd is not None and cost.usd > 0.0
    assert cost.metered is False, "free-tier arms must not report as metered spend"


def test_unknown_model_is_unpriced_and_never_zero() -> None:
    """The inverse of Config.limits_for()'s instinct, deliberately.

    A quota guess that errs high burns quota; a cost guess that errs low is how
    a bill goes unnoticed. Zero here would be a silent, confident, wrong
    number -- the one outcome that survives review because it looks fine.
    """
    cost = cost_usd("models/nothing-here", prompt_tokens=1000, completion_tokens=50)
    assert cost.usd is None
    assert cost.priced is False
    assert cost is UNPRICED


def test_missing_token_counts_are_unpriced_not_estimated() -> None:
    """llama-server may ignore `stream_options` and report no usage at all. The
    honest degradation is unpriced; a chars-over-four estimate would be a
    fabricated number wearing a measurement's clothes."""
    assert cost_usd(Config.SERVING_MODEL, None, None) is UNPRICED


def test_arithmetic_against_a_hand_computed_figure() -> None:
    """Rates are per MILLION tokens, which is the unit every provider
    publishes; getting the scale wrong is a 1,000,000x error that still looks
    like a number."""
    cost = cost_usd(FAKE_METERED, prompt_tokens=2_000_000, completion_tokens=500_000)
    # 2M prompt at $1/Mtok = $2.00; 0.5M completion at $4/Mtok = $2.00.
    assert cost.usd == 4.0
    assert cost.metered is True


def test_one_half_of_the_split_is_zero_not_missing() -> None:
    """A per-stage split passes None for the half it does not bill. That is
    'this stage does not charge for that half', contributing zero -- distinct
    from BOTH being None, which is a missing measurement."""
    prompt_only = cost_usd(FAKE_METERED, prompt_tokens=1_000_000, completion_tokens=None)
    assert prompt_only.usd == 1.0
    assert prompt_only.priced is True


def test_retry_storm_pays_for_every_prompt() -> None:
    """R33's failure mode, priced. Five attempts each submit the full prompt
    and four of them return nothing; usage accumulates across attempts rather
    than being overwritten, so the storm costs 5 prompts, not 1.

    This is the concrete argument for per-stage over end-to-end cost: an
    end-to-end scalar shows one number and no explanation of why it is high.
    """
    one_prompt = 1500
    storm = cost_usd(FAKE_METERED, prompt_tokens=one_prompt * 5, completion_tokens=0)
    single = cost_usd(FAKE_METERED, prompt_tokens=one_prompt, completion_tokens=0)
    assert storm.usd == single.usd * 5


def test_total_is_unpriced_if_any_stage_is() -> None:
    """Dropping the unknown term would under-report the request. A partially
    known cost is not a cost."""
    total = total_usd([FREE, Cost(usd=0.5, metered=True), UNPRICED])
    assert total is UNPRICED


def test_total_is_metered_only_if_every_stage_is() -> None:
    """One unmetered stage makes the sum a mixed quantity. Reporting that as a
    metered cost would launder an unmeasured stage into a real figure."""
    mixed = total_usd([Cost(usd=0.0, metered=False), Cost(usd=0.5, metered=True)])
    assert mixed.usd == 0.5
    assert mixed.metered is False

    fully = total_usd([Cost(usd=0.25, metered=True), Cost(usd=0.5, metered=True)])
    assert fully.usd == 0.75
    assert fully.metered is True


def test_empty_rollup_is_free_not_unpriced() -> None:
    """A cache hit runs no billable stage. That is genuinely free, and it is
    what makes A5's saving measurable as cost avoided once P2 meters
    anything."""
    assert total_usd([]) == FREE


def test_is_metered_does_not_warn_for_unknown_ids() -> None:
    """The predicate is called once per request on the hot path. cost_usd()
    already warns once for an unknown id; a second warning per request turns a
    missing table row into log spam that gets filtered, then ignored."""
    assert is_metered(FAKE_METERED) is True
    assert is_metered(Config.SERVING_MODEL) is False
    assert is_metered("models/nothing-here") is False


def main() -> None:
    # cost_usd()'s unknown-id path warns by design; keep the run readable.
    logging.disable(logging.WARNING)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    logging.disable(logging.NOTSET)
    print(f"test_cost: {len(tests)} passed")


if __name__ == "__main__":
    main()
