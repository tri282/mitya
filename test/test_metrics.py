# test/test_metrics.py
"""P1 step 7 verify: the metric label allowlist (src/metrics.py).

**Flipped at v2 P5 Batch B, and the flip IS the security property.** At P1 the
load-bearing assertion was that `tenant` reached NO metric label: it came from
an unauthenticated header validated only for shape (~37^32 values), so one
Prometheus series per distinct value per metric was a memory-exhaustion path
into the operator plane through the front door. P5 authenticated identity (D7)
and bounded the tenant set to the `accounts` table, so the label is now allowed
-- on an explicit five-instrument allowlist, with the cardinality budget
ENFORCED by `_tenant_key()` rather than assumed from the seeding discipline.

The assertions therefore moved rather than relaxed: `tenant` must appear on
exactly the allowlisted instruments and nowhere else, a malformed value must
collapse to "unknown", and a value past `Config.MAX_TENANT_LABELS` must collapse
to "other". A test that only asserted "the label exists" would pass on the
unbounded implementation this rule was written against.

Every exclusion check below is paired with a positive check, because a test that
asserts "tenant is absent" passes trivially when nothing was emitted at all --
the shape of the four defects this repo has already caught (R39, R41, R42, R43).

Plain-assert script, not pytest. Offline: no scrape, no container, no network.
"""

import sys

sys.path.insert(0, ".")
from prometheus_client import REGISTRY, generate_latest  # noqa: E402

from src import metrics  # noqa: E402
from src.metrics import (
    record_quota_rejection,  # noqa: E402
    ADMISSION_REASONS,
    CACHE_OUTCOMES,
    OTHER,
    REQUEST_OUTCOMES,
    ROUTES,
    STAGES,
    UNKNOWN,
    configure_metrics,
    record_answer_cost,
    record_admission_rejection,
    record_cache_lookup,
    record_empty_completion,
    record_request_duration,
    record_stage_duration,
    record_ttft,
    record_unmetered_answer,
    record_unpriced_answer,
)

configure_metrics()


def _scrape() -> list[str]:
    return [
        line for line in generate_latest(REGISTRY).decode().splitlines()
        if line.startswith("mitya_") and not line.startswith("#")
    ]


# The four instruments P5 allows `tenant` on. Anything else carrying it is a
# leak, and the test below reads this list rather than a hand-copied one.
TENANT_LABELLED = (
    "mitya_request_duration_seconds",
    "mitya_answer_cost_usd",
    "mitya_quota_rejections_total",
    "mitya_admission_rejections_total",
)


def _emit_one_of_everything() -> None:
    record_request_duration(9.1, "DEFAULT", "ok", "tenant-a")
    record_ttft(7.79, "DEFAULT")
    record_stage_duration(2.24, "STRUCTURAL", "rerank")
    record_answer_cost(0.0004, "local-q4km", "tenant-a")
    record_unmetered_answer("local-q4km")
    record_unpriced_answer("some/unknown-model")
    record_cache_lookup("DEFAULT", "hit")
    record_empty_completion("STRUCTURAL")
    record_quota_rejection("local-q4km", "tenant-a")
    record_admission_rejection("tenant-a", "tenant")


def test_the_scrape_is_not_empty() -> None:
    """Non-vacuity for every exclusion test below: if this fails, the absence
    assertions are all passing for the wrong reason."""
    _emit_one_of_everything()
    lines = _scrape()
    assert len(lines) > 20, f"only {len(lines)} mitya_ series -- nothing was exercised"


def test_every_instrument_appears() -> None:
    # Emit first: main() runs tests in alphabetical order, so no test may
    # depend on another having run.
    _emit_one_of_everything()
    lines = "\n".join(_scrape())
    for name in (
        "mitya_request_duration_seconds", "mitya_ttft_seconds",
        "mitya_stage_duration_seconds", "mitya_answer_cost_usd",
        "mitya_unmetered_answers_total", "mitya_unpriced_answers_total",
        "mitya_cache_lookups_total", "mitya_empty_completions_total",
        "mitya_quota_rejections_total", "mitya_admission_rejections_total",
    ):
        assert name in lines, f"{name} missing from the scrape"


def _tenant_offenders(lines: list[str]) -> list[str]:
    """Series carrying a `tenant` label on an instrument not on the allowlist."""
    return [
        line for line in lines
        if "tenant=" in line and not any(name in line for name in TENANT_LABELLED)
    ]


def test_tenant_is_not_a_label_outside_the_allowlist() -> None:
    """The security boundary, in its post-P5 form. Asserted against the RENDERED
    scrape, not against the source, so it holds regardless of how a label got
    there."""
    _emit_one_of_everything()
    offenders = _tenant_offenders(_scrape())
    assert not offenders, f"tenant reached a non-allowlisted metric: {offenders[:3]}"


def test_the_tenant_check_would_catch_a_real_leak() -> None:
    """Non-vacuity for the test above: prove the predicate can fail.

    Deliberately NOT by emitting a tenant-labelled series into the real
    registry. prometheus_client's registry is process-global and a series
    cannot be un-added, so doing that would break the test above for the rest
    of the run and leave the order of the two tests load-bearing. The predicate
    is what is under test, so the predicate is what gets the fabricated input."""
    leaked = 'mitya_cache_lookups_total{outcome="hit",route="DEFAULT",tenant="acme"} 1.0'
    assert _tenant_offenders(_scrape() + [leaked]) == [leaked], (
        "the tenant predicate cannot detect a tenant label -- it proves nothing"
    )


def test_tenant_does_reach_the_allowlisted_instruments() -> None:
    """The other half, and it is not decoration: the whole point of P5 lifting
    the rule is that per-tenant fairness and starvation become readable off the
    operator plane. An allowlist that nothing writes to would pass every
    exclusion test above and deliver nothing."""
    _emit_one_of_everything()
    for name in TENANT_LABELLED:
        matching = [l for l in _scrape() if name in l and 'tenant="tenant-a"' in l]
        assert matching, f"{name} carries no tenant label -- the label was never wired"


def test_a_malformed_tenant_collapses_to_unknown() -> None:
    """Enforced, not assumed. `_tenant_key` is the only path a label value can
    take, so a value that never came from an account row cannot mint a series."""
    assert metrics._tenant_key(None) == UNKNOWN
    assert metrics._tenant_key("") == UNKNOWN
    assert metrics._tenant_key("Bad Tenant") == UNKNOWN
    assert metrics._tenant_key("x" * 200) == UNKNOWN
    assert metrics._tenant_key("newline\ninjected") == UNKNOWN


def test_the_cardinality_budget_is_enforced_not_documented() -> None:
    """R107's number, made real. Past MAX_TENANT_LABELS distinct values every
    further tenant collapses to "other" -- so even if the accounts table grows
    past what this deployment claims to support, the series count does not.

    Uses a private, restored copy of the seen-set: this is process-global
    accounting and leaking test tenants into it would eat the real budget."""
    from src.config import Config as _Config

    saved_seen = set(metrics._seen_tenants)
    saved_budget = _Config.MAX_TENANT_LABELS
    try:
        metrics._seen_tenants.clear()
        _Config.MAX_TENANT_LABELS = 3
        assert [metrics._tenant_key(f"tenant-{c}") for c in "abc"] == [
            "tenant-a", "tenant-b", "tenant-c",
        ]
        assert metrics._tenant_key("tenant-d") == OTHER
        assert metrics._tenant_key("tenant-e") == OTHER
        # An already-seen tenant is still itself after the budget is spent --
        # otherwise a burst of new tenants would relabel the real ones.
        assert metrics._tenant_key("tenant-a") == "tenant-a"
        assert OTHER != UNKNOWN, (
            "over-budget and malformed must stay distinguishable -- collapsing "
            "them hides a budget breach behind what looks like bad input"
        )
    finally:
        metrics._seen_tenants.clear()
        metrics._seen_tenants.update(saved_seen)
        _Config.MAX_TENANT_LABELS = saved_budget


def test_an_unknown_model_collapses_to_unknown() -> None:
    assert metrics._model_key("no/such-model") == UNKNOWN
    assert metrics._provider_key("no/such-model") == UNKNOWN


def test_a_known_model_does_not_collapse() -> None:
    """Non-vacuity for the test above: if _model_key returned UNKNOWN for
    everything, the collapse test would pass and the metric would be useless.

    `local-q4km` is the only id in MODEL_RATES today -- the Gemini rows are
    deliberately commented out there, since an unverified price in a table gets
    read as a measurement. So this is the one known id there is, and that is
    the correct state, not a gap."""
    assert metrics._model_key("local-q4km") == "local-q4km"
    assert metrics._provider_key("local-q4km") == "local"
    # The provider side has a second, independent source (MODEL_LIMITS carries
    # a provider column even where MODEL_RATES has no row).
    assert metrics._provider_key("models/gemini-3.1-flash-lite") == "google"


def test_label_sets_are_closed_and_small() -> None:
    assert set(ROUTES) == {"DEFAULT", "STRUCTURAL"}
    assert len(STAGES) == 8 and len(set(STAGES)) == 8
    assert set(CACHE_OUTCOMES) == {"hit", "miss"}
    assert set(REQUEST_OUTCOMES) == {"ok", "error", "empty", "quota"}
    assert set(ADMISSION_REASONS) == {"tenant", "total", "fairness"}


def test_unmetered_does_not_enter_the_cost_histogram() -> None:
    """G5: a 0.0 in the cost histogram is indistinguishable from a real
    metered-but-free call. An unmetered answer increments its own counter and
    contributes nothing to cost."""
    before = [l for l in _scrape() if l.startswith("mitya_answer_cost_usd_count")]
    record_unmetered_answer("local-q4km")
    record_unpriced_answer("local-q4km")
    after = [l for l in _scrape() if l.startswith("mitya_answer_cost_usd_count")]
    assert before == after, "an unmetered or unpriced answer moved the cost histogram"


def test_a_metered_answer_does_enter_the_cost_histogram() -> None:
    """Non-vacuity for the test above."""
    before = _scrape()
    record_answer_cost(0.0012, "local-q4km")
    after = _scrape()
    assert before != after, "a metered cost did not reach the cost histogram"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
