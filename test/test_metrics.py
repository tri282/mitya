# test/test_metrics.py
"""P1 step 7 verify: the metric label allowlist (src/metrics.py).

The load-bearing assertion here is that `tenant` reaches NO metric label. It is
an unauthenticated header validated only for shape (~37^32 values), so one
Prometheus series per distinct value per metric is a memory-exhaustion path into
the operator plane through the front door. Shape validation guards log
injection; it does not guard cardinality.

Every exclusion check below is paired with a positive check, because a test that
asserts "tenant is absent" passes trivially when nothing was emitted at all --
the shape of the four defects this repo has already caught (R39, R41, R42, R43).

Plain-assert script, not pytest. Offline: no scrape, no container, no network.
"""

import sys

sys.path.insert(0, ".")
from prometheus_client import REGISTRY, generate_latest  # noqa: E402

from src import metrics  # noqa: E402
from src.metrics import (  # noqa: E402
    CACHE_OUTCOMES,
    REQUEST_OUTCOMES,
    ROUTES,
    STAGES,
    UNKNOWN,
    configure_metrics,
    record_answer_cost,
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


def _emit_one_of_everything() -> None:
    record_request_duration(9.1, "DEFAULT", "ok")
    record_ttft(7.79, "DEFAULT")
    record_stage_duration(2.24, "STRUCTURAL", "rerank")
    record_answer_cost(0.0004, "local-q4km")
    record_unmetered_answer("local-q4km")
    record_unpriced_answer("some/unknown-model")
    record_cache_lookup("DEFAULT", "hit")
    record_empty_completion("STRUCTURAL")


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
    ):
        assert name in lines, f"{name} missing from the scrape"


def _tenant_offenders(lines: list[str]) -> list[str]:
    return [line for line in lines if "tenant" in line]


def test_tenant_is_not_a_label_anywhere() -> None:
    """The security boundary. Asserted against the RENDERED scrape, not against
    the source, so it holds regardless of how a label got there."""
    _emit_one_of_everything()
    offenders = _tenant_offenders(_scrape())
    assert not offenders, f"tenant reached a metric label: {offenders[:3]}"


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
