"""Prometheus metrics: the meter, the instrument definitions, and the label
allowlist (v2 P1 step 7, plan `step-7-operator-plane.md` steps 2-3).

R32 chose the OTel metrics SDK over a second, hand-rolled instrumentation of
the same seven -- now eight -- boundaries: every number this module exposes is
read off the SAME spans/attributes `src/observability.py` and `src/api/app.py`
already produce, through a second `MeterProvider` reader. Nothing here
measures anything twice.

THE LABEL RULE (plan Decision 2, this is a security boundary, not a style
choice): `tenant` NEVER appears in a metric label, on any instrument, ever, at
P1. `X-Tenant` is unauthenticated until P5 and is validated only for *shape*
(`^[a-z0-9][a-z0-9-]{0,31}\\Z`, ~37^32 values) -- that is a cardinality guard
for logs (bounds line length / injection), not for metrics (bounds nothing:
a caller sending distinct valid header values mints one Prometheus series per
value per metric, unboundedly -- a memory-exhaustion path into the operator
plane through the front door). `tenant` stays on spans and logs; it becomes a
metric label at P5 once identity is authenticated and the tenant set is
therefore bounded and known.

STATUS at v2 P5 Batch B: **the rule is now lifted, on four instruments, with a
budget that is enforced rather than asserted.** Both halves of the precondition
hold -- identity is authenticated (D7) and the tenant set is bounded to the
`accounts` table, which only `scripts/seed_account.py` can add a row to -- and
the owner has stated the maximum tenant count as **5** (`decisions-p5.md`),
which is the number R107 recorded as missing.

The budget, stated so it can be checked: `tenant` appears on exactly four
instruments -- `request_duration`, `answer_cost`, `quota_rejections` and
`admission_rejections` -- of the twelve defined here. (`pool_share_rejections`
was the fifth until G-P10 retired the guard it counted: R117 measured it at
zero because it could never fire, and a counter for a mechanism that does not
exist is an operator reading a line that can only ever say 0.) At 5 tenants the worst case is 5 x (2 routes x 4 outcomes)
= 40 histogram series on the largest of them; the rest are counters in the
single digits. Trivial, which is the whole reason it is now allowed.

It is enforced by `_tenant_key()`, not by trusting the account table: a value
that fails the tenant shape collapses to "unknown", and once
`Config.MAX_TENANT_LABELS` distinct values have been seen every further one
collapses to "other". The unbounded-series memory-exhaustion path the original
rule was written against is therefore closed by code even if the seeding
discipline is not kept. Every OTHER instrument stays tenant-free.

Every OTHER label is a closed set, enforced by routing every label value
through one of the `_*_key()` helpers below rather than letting a call site
pass a raw string -- an unknown id collapses to the literal "unknown" instead
of minting a new series, and there is exactly one place that mapping can be
got wrong.
"""

import logging
import time

from src.observability import (
    STAGE_CACHE,
    STAGE_CONTEXT,
    STAGE_ENTITIES,
    STAGE_GENERATE_FIRST,
    STAGE_GENERATE_STREAM,
    STAGE_RERANK,
    STAGE_RETRIEVE,
    STAGE_ROUTE,
    is_valid_tenant,
)

logger = logging.getLogger(__name__)

# --- closed label sets ------------------------------------------------------

ROUTES = ("DEFAULT", "STRUCTURAL")  # src/rag/router.py's two outputs (R30's dimension)
STAGES = (
    STAGE_CACHE, STAGE_ROUTE, STAGE_ENTITIES, STAGE_RETRIEVE,
    STAGE_RERANK, STAGE_CONTEXT, STAGE_GENERATE_FIRST, STAGE_GENERATE_STREAM,
)
# ok/error cover the request as a whole; empty is the EMPTY_RETRIES exhaustion
# path (a 200 with no answer, not an exception); quota is QuotaExhausted
# (src/limits.py) -- reserved here because it is the plan's stated closed set,
# though nothing on the live /chat path raises it yet (see metrics.py's
# module report / plans/mitya-v2/05-impl/phase-1.md for why).
REQUEST_OUTCOMES = ("ok", "error", "empty", "quota")
# The cache's outcome set stays TWO values (v2 P6 step 7(b)). A memory-bearing
# request is not a third outcome here: it is a different population, counted on
# `mitya_cache_bypassed_total` -- see `record_cache_bypass`.
CACHE_OUTCOMES = ("hit", "miss")
# Why a request was not admitted (v2 P5 step 6, src/limits.py's AtCapacity):
# "tenant" is the noisy neighbour held at its own cap, "total" is somebody else
# losing a request because the deployment is full. Two labels because they are
# the two halves of the starvation question.
ADMISSION_REASONS = ("tenant", "total", "fairness")
# Mirror the closed enums in src/gateway (FinishReason, SpendLayer) and src/guard.py
# (GuardLayer); test_wiring_enums.py asserts they stay equal. Literals here because
# importing the enums would make src/gateway/__init__ and this module circular.
FINISH_REASONS = ("stop", "length", "content_filter", "error", "other", "none")
SPEND_LAYERS = ("per_call", "per_request", "tenant_day", "global_minute", "global_day", "global_month")
GUARD_LAYERS = ("canary", "persona_break", "none")

UNKNOWN = "unknown"
# Overflow bucket for the tenant label -- see _tenant_key(). Distinct from
# UNKNOWN, which means "not a valid tenant id at all": "other" means "a real
# tenant, past the cardinality budget", and collapsing the two would hide a
# budget breach behind what looks like malformed input.
OTHER = "other"

# Tenant label values seen so far this process. Module-level and never pruned:
# it IS the cardinality budget's accounting, and a prune would let the same
# tenant re-enter the budget repeatedly.
_seen_tenants: set[str] = set()


def _tenant_key(tenant: str | None) -> str:
    """The tenant label value, bounded (v2 P5, R107; see the module docstring).

    Three outcomes, and the boundary is enforced here rather than assumed from
    the accounts table:

      * a valid, already-seen or within-budget tenant id -> itself;
      * anything failing the tenant shape (or absent) -> "unknown";
      * a valid id arriving after `Config.MAX_TENANT_LABELS` distinct ones have
        been seen -> "other", logged once per breach so the budget being hit is
        an operator-visible event and not a silent relabel.

    `is_valid_tenant` is imported from src.observability so the shape has one
    definition across logs, spans, account ids and now metric labels.
    """
    from src.config import Config

    # isinstance before the shape check, not for tidiness: `chat()` takes its
    # tenant through `Depends(require_tenant)`, and the offline tests that call
    # it directly (test_chat_semaphore.py) get the unresolved `Depends` object
    # in that parameter. A label helper that raises there would turn a metric
    # into a request-path failure.
    if not tenant or not isinstance(tenant, str) or not is_valid_tenant(tenant):
        return UNKNOWN
    if tenant in _seen_tenants:
        return tenant
    if len(_seen_tenants) >= Config.MAX_TENANT_LABELS:
        logger.warning(
            "metrics: tenant label budget of %d exhausted -- %r collapses to %r. "
            "Either more accounts exist than this deployment claims to support, "
            "or MAX_TENANT_LABELS needs raising deliberately.",
            Config.MAX_TENANT_LABELS, tenant, OTHER,
        )
        return OTHER
    _seen_tenants.add(tenant)
    return tenant


def _model_key(model: str) -> str:
    """Collapse an unrecognized model id to "unknown" rather than minting a new
    series. `Config.MODEL_RATES` is the closed set -- imported inside the
    function, matching `configure_tracing()`'s discipline of not pulling
    `src.config` in at module scope."""
    from src.config import Config

    return model if model in Config.MODEL_RATES else UNKNOWN


def _provider_key(model: str) -> str:
    """Provider label, closed set, derived rather than stored: at P1
    `Config.MODEL_RATES` rows (the serving-cost table) carry no `provider`
    field -- there is exactly one served model, the local GGUF build. A
    `local-`-prefixed id (the `SERVING_MODEL` naming convention -- see
    `Config.SERVING_MODEL`'s default `"local-q4km"`) maps to `"local"`.
    Anything else falls back to `Config.MODEL_LIMITS`, which DOES carry a
    provider column for the teacher/judge/gateway ids (google, openrouter,
    groq). Unknown to both -> "unknown", same reasoning as `_model_key`."""
    from src.config import Config

    if model.startswith("local-"):
        return "local"
    limits = Config.MODEL_LIMITS.get(model)
    if limits and limits.get("provider"):
        return str(limits["provider"])
    return UNKNOWN


# --- bucket boundaries -------------------------------------------------------
# The SDK's default histogram buckets top out at 10s and are linear-ish below
# that -- far too coarse for TTFT (R30: DEFAULT ~7.79s, STRUCTURAL ~4.26s) and
# far too coarse for the sub-millisecond `route` stage. Each histogram below
# gets its own View with explicit boundaries sized to what this pipeline
# actually measures.

# Total /chat request: TTFT plus the rest of the token stream, so longer than
# TTFT alone.
REQUEST_DURATION_BUCKETS = (0.5, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 40, 60)
# Seconds, not milliseconds -- ttft_ms/1000 at the call site.
TTFT_BUCKETS = (0.25, 0.5, 1, 1.5, 2, 3, 4, 5, 6, 7, 8, 10, 15)
# Spans sub-millisecond (`route`) through ~2.24s (`rerank`, R34: 84% of the
# retrieval path) share one histogram (route+stage is the slice key), so the
# bucket set has to cover both ends.
# One histogram covers all eight stages, and they span five orders of magnitude:
# `route` is sub-millisecond, `generate.stream` is ~10s. The boundaries must
# therefore cover BOTH ends -- P1 step 10 measured this set topping out at 5s and
# found both generate stages reporting exactly 5.0000s at p50/p95/p99, which is
# the top bucket edge, not a latency. The two most expensive stages in the
# pipeline were the two it could not measure.
STAGE_DURATION_BUCKETS = (
    0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5,
    7.5, 10, 15, 20, 30,
)
# USD, fractions of a cent at this model's rates.
COST_BUCKETS = (0.00001, 0.00005, 0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5)

# --- meter / instruments -----------------------------------------------------
# Populated by configure_metrics(); module-level so the record_*() helpers
# below and the /metrics endpoint (src/api/app.py) can reach them without
# threading a meter object through every call site.
_instruments: dict = {}


class _NullInstrument:
    """What every record_*() helper resolves to when OTEL_ENABLED=false.

    A no-op rather than a raise: the uninstrumented arm serves real requests
    through the same handler, so the call sites are live and must do nothing
    instead of failing. The residual cost is one dict lookup and one empty call
    per record -- which is itself part of the honest overhead figure, since a
    real "instrumentation removed" build would not have the call site at all.
    """

    def record(self, *args, **kwargs) -> None:
        pass

    def add(self, *args, **kwargs) -> None:
        pass


_NULL = _NullInstrument()


def configure_metrics():
    """Install the meter provider with the Prometheus reader.

    Mirrors `configure_tracing()`'s discipline (src/observability.py): the
    exporter package is imported inside this function, not at module scope,
    so offline scripts that never serve /metrics do not pay for pulling in
    opentelemetry.exporter.prometheus / prometheus_client at import time.

    Uses the prometheus_client DEFAULT registry (PrometheusMetricReader's own
    default) rather than a private one, so the standard process_*/python_*
    collectors are scraped too -- useful, and free.
    """
    from src.config import Config as _Config

    if not _Config.OTEL_ENABLED:
        # Same switch as configure_tracing(): the uninstrumented arm records
        # nothing and registers no collectors, so /metrics serves only the
        # prometheus_client process defaults. `_instruments` stays empty and
        # every record_*() helper falls through to `_NULL` -- the arm still
        # SERVES requests (that is the whole point of measuring it), so the
        # helpers must be no-ops rather than raising KeyError.
        logging.getLogger(__name__).warning(
            "metrics: OTEL_ENABLED=false -- no meter installed, no instruments "
            "registered. Uninstrumented arm."
        )
        return None

    from opentelemetry.exporter.prometheus import PrometheusMetricReader
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
    from opentelemetry.sdk.resources import Resource

    from src.config import Config

    views = [
        View(
            instrument_name="mitya_request_duration_seconds",
            aggregation=ExplicitBucketHistogramAggregation(REQUEST_DURATION_BUCKETS),
        ),
        View(
            instrument_name="mitya_ttft_seconds",
            aggregation=ExplicitBucketHistogramAggregation(TTFT_BUCKETS),
        ),
        View(
            instrument_name="mitya_stage_duration_seconds",
            aggregation=ExplicitBucketHistogramAggregation(STAGE_DURATION_BUCKETS),
        ),
        View(
            instrument_name="mitya_answer_cost_usd",
            aggregation=ExplicitBucketHistogramAggregation(COST_BUCKETS),
        ),
    ]

    provider = MeterProvider(
        # scope_info_enabled=False: otherwise every series carries three
        # constant otel_scope_* labels. They are cardinality-neutral (constant
        # values) but they bloat every stored series and every PromQL query in
        # the dashboard for no information -- there is one instrumentation
        # scope in this process.
        metric_readers=[PrometheusMetricReader(scope_info_enabled=False)],
        resource=Resource.create({"service.name": Config.OTEL_SERVICE_NAME}),
        views=views,
    )
    meter = provider.get_meter(__name__)

    _instruments["request_duration"] = meter.create_histogram(
        "mitya_request_duration_seconds", unit="s", description="Total /chat request duration",
    )
    _instruments["ttft"] = meter.create_histogram(
        "mitya_ttft_seconds", unit="s", description="Time to first token",
    )
    _instruments["stage_duration"] = meter.create_histogram(
        "mitya_stage_duration_seconds", unit="s", description="Per-stage span duration",
    )
    _instruments["answer_cost"] = meter.create_histogram(
        "mitya_answer_cost_usd", unit="usd", description="Priced, metered answer cost",
    )
    _instruments["unmetered_answers"] = meter.create_counter(
        "mitya_unmetered_answers_total", description="Answers priced on an unmetered model row",
    )
    _instruments["unpriced_answers"] = meter.create_counter(
        "mitya_unpriced_answers_total", description="Answers whose model had no rate row -- billed, amount unknown",
    )
    _instruments["quota_rejections"] = meter.create_counter(
        "mitya_quota_rejections_total", description="QuotaExhausted rejections (src/limits.py)",
    )
    _instruments["cache_lookups"] = meter.create_counter(
        "mitya_cache_lookups_total",
        description=(
            "Answer cache lookups over the MEMORY-FREE population only, by hit/miss "
            "(v2 P6 step 7(b), G-P12(b) denominator discipline)"
        ),
    )
    _instruments["cache_bypassed"] = meter.create_counter(
        "mitya_cache_bypassed_total",
        description=(
            "Memory-bearing requests that bypassed the answer cache entirely "
            "(v2 P6 step 7(b), G-P12(b)). A SEPARATE POPULATION from "
            "mitya_cache_lookups_total -- never a term in its denominator"
        ),
    )
    _instruments["empty_completions"] = meter.create_counter(
        "mitya_empty_completions_total", description="EMPTY_RETRIES exhausted -- no token ever arrived",
    )
    _instruments["admission_rejections"] = meter.create_counter(
        "mitya_admission_rejections_total",
        description="Requests not admitted by the concurrency bound, by reason (v2 P5, A12(3))",
    )
    _instruments["finish_reasons"] = meter.create_counter(
        "mitya_finish_reasons_total",
        description="Priced calls by normalized finish reason; 'other' counts unrecognised provider values",
    )
    _instruments["budget_refusals"] = meter.create_counter(
        "mitya_budget_refusals_total", description="Calls refused before send, by spend layer (OI8)",
    )
    _instruments["guard_verdicts"] = meter.create_counter(
        "mitya_guard_verdicts_total", description="Output-guard verdicts on served answers, by layer",
    )
    _instruments["memory_write_failures"] = meter.create_counter(
        "mitya_memory_write_failures_total",
        description="Memory writes that failed without failing the turn, by stage (v2 P6, G-P13)",
    )
    _instruments["memory_write_skips"] = meter.create_counter(
        "mitya_memory_write_skips_total",
        description=(
            "Memory writes SKIPPED before the extractor's draw, by reason "
            "(v2 P6 Batch E prerequisites, CP-6d(a)). Neither a failure nor an "
            "empty extraction -- a third thing, which is why it is a third series"
        ),
    )

    logger.info("metrics: meter configured, %d instruments registered", len(_instruments))
    return meter


# --- recording helpers --------------------------------------------------
# One helper per instrument, each called from exactly one call site in
# src/api/app.py (plan step 3: no metric is written from two places). Each
# takes only allowlisted label values -- callers pass raw strings for `route`/
# `stage`/`outcome` because those already come from closed-set constants
# (STAGE_*, route(), literal outcome strings); `model` always goes through
# `_model_key()`/`_provider_key()` here, and `tenant` always through
# `_tenant_key()` (v2 P5), so a caller can never accidentally mint a new series
# by passing a raw id through.


def record_request_duration(seconds: float, route: str, outcome: str, tenant: str | None = None) -> None:
    """`tenant` defaults to None -> "unknown" so an unauthenticated or
    pre-P5 call site still records a request rather than raising -- a request
    that goes unrecorded is invisible to BOTH RED panels, which is the defect
    P1 step 7 measured live."""
    _instruments.get("request_duration", _NULL).record(
        seconds, {"route": route, "outcome": outcome, "tenant": _tenant_key(tenant)},
    )


def record_ttft(seconds: float, route: str) -> None:
    _instruments.get("ttft", _NULL).record(seconds, {"route": route})


def record_stage_duration(seconds: float, route: str, stage: str) -> None:
    _instruments.get("stage_duration", _NULL).record(seconds, {"route": route, "stage": stage})


def record_answer_cost(usd: float, model: str, tenant: str | None = None) -> None:
    """Only for a PRICED and METERED cost -- see module docstring / G5. Caller
    (src/api/app.py) must not call this for an unmetered or unpriced Cost."""
    _instruments.get("answer_cost", _NULL).record(
        usd,
        {"provider": _provider_key(model), "model": _model_key(model), "tenant": _tenant_key(tenant)},
    )


def record_unmetered_answer(model: str) -> None:
    """A priced answer whose model row is `metered=False` (local inference).
    Increments this counter INSTEAD OF `record_answer_cost` -- never both, and
    never a 0.0 in the cost histogram standing in for this (G5: a zero there
    is indistinguishable from a real free-metered call)."""
    _instruments.get("unmetered_answers", _NULL).add(1, {"model": _model_key(model)})


def record_unpriced_answer(model: str) -> None:
    """A metered answer with no rate row -- somebody billed us and we cannot say
    how much. The third case beside metered-and-priced and unmetered, and it is
    genuinely distinct: unmetered means nothing bills us (true, publishable as
    "unmetered"), unpriced means something does and the figure is missing. Both
    make an aggregate PROVISIONAL (G5); only one of them is a cost of 0."""
    _instruments.get("unpriced_answers", _NULL).add(1, {"provider": _provider_key(model), "model": _model_key(model)})


def record_quota_rejection(model: str, tenant: str | None = None) -> None:
    """QuotaExhausted (src/limits.py). No live call site on /chat as of P1 --
    QuotaExhausted is currently raised only by the offline teacher/judge path
    (src/rag/teacher.py), which never serves this process's /metrics. Defined
    now so P2's gateway quota check has an instrument to call into without a
    second metrics-module change."""
    _instruments.get("quota_rejections", _NULL).add(
        1,
        {"provider": _provider_key(model), "model": _model_key(model), "tenant": _tenant_key(tenant)},
    )


def record_admission_rejection(tenant: str, reason: str) -> None:
    """A request the concurrency bound did not admit (src/limits.py, A12(3)).

    `reason` comes from `AtCapacity.reason` and is a closed set
    (ADMISSION_REASONS). This is the instrument the starvation number is read
    off: "tenant" counts the noisy neighbour being held back, "total" counts
    what everyone else loses, and "fairness" counts a tenant asked to yield the
    reserved tail of the pool because it is ahead of the others (G-P10). The
    third arrived with the least-served rule and had to be added HERE too: an
    unlisted reason is silently relabelled `unknown`, which is how a live run
    came back with nine rejections attributed to nothing."""
    _instruments.get("admission_rejections", _NULL).add(
        1, {"tenant": _tenant_key(tenant), "reason": reason if reason in ADMISSION_REASONS else UNKNOWN},
    )


def record_finish_reason(reason: str) -> None:
    _instruments.get("finish_reasons", _NULL).add(
        1, {"finish_reason": reason if reason in FINISH_REASONS else UNKNOWN},
    )


def record_budget_refusal(layer: str) -> None:
    _instruments.get("budget_refusals", _NULL).add(
        1, {"layer": layer if layer in SPEND_LAYERS else UNKNOWN},
    )


def record_guard_verdict(layer: str | None) -> None:
    layer = layer or "none"
    _instruments.get("guard_verdicts", _NULL).add(
        1, {"layer": layer if layer in GUARD_LAYERS else UNKNOWN},
    )


MEMORY_WRITE_STAGES = ("extract", "episodic")


def record_memory_write_failure(stage: str) -> None:
    """A memory write that failed while the turn succeeded (G-P13, Batch C).

    **Metered, not merely logged**, and that is a deliberate departure from the
    budget guard's precedent at `src/gateway/guard.py:210-218` ("logged, not
    metered, until a later phase asks"). A silently stale memory changes what
    the multi-turn metric measures, so staleness has to be loud.

    `stage` separates the per-turn EXTRACTOR from the session-end SUMMARIZER,
    per CP-6a's O-E residual risk (2): the extractor has no instrument
    distinguishing "extracted nothing" (correct on the 68 of 108 context-free
    corpus turns, R120(c)) from "failed", so pooling the two write paths under
    one series would make that already-blunt signal unreadable."""
    _instruments.get("memory_write_failures", _NULL).add(
        1, {"stage": stage if stage in MEMORY_WRITE_STAGES else UNKNOWN},
    )


MEMORY_WRITE_SKIP_REASONS = ("rate_capped",)


def record_memory_write_skip(reason: str) -> None:
    """A memory write that never reached the extractor (CP-6d(a)).

    **The seventh instance of the pattern this project keeps finding.**
    `write_allowed()` (`src/memory_store.py`) is consulted BEFORE the provider
    call, and on a trip `write_profile_from_turn` returns 0 -- the same return
    as "the extractor correctly found nothing", with no counter and only a
    WARNING line. In a metered run that reads as a memory system extracting less
    than expected, which is a claim about the model, not about the cap.

    `reason` is a closed set for the reason every other label here is
    (R107/R113's cardinality budget): the value is minted in this module, not
    passed through from a tenant id, a model id or a message. There is
    deliberately NO tenant label -- P5 put tenant on four instruments and this
    is not one of them, and the run it exists for is one tenant at a time.
    """
    _instruments.get("memory_write_skips", _NULL).add(
        1, {"reason": reason if reason in MEMORY_WRITE_SKIP_REASONS else UNKNOWN},
    )


def record_cache_lookup(route: str, outcome: str) -> None:
    """A lookup against the cache's ONE remaining population: memory-free
    requests. `outcome` stays the closed set `CACHE_OUTCOMES` -- "bypassed" is
    deliberately NOT a third value here (see `record_cache_bypass`)."""
    _instruments.get("cache_lookups", _NULL).add(1, {"route": route, "outcome": outcome})


def record_cache_bypass(route: str) -> None:
    """A memory-bearing request that never consulted the cache (step 7(b)).

    **A separate instrument, not a third `outcome` on `cache_lookups`, and that
    is the whole mechanism.** CP-6c chose outcome (b) -- no caching for
    memory-bearing requests -- so the cache now serves two populations, and its
    condition was that the split be reported so a pooled hit rate is never
    published across them. As a third outcome value, `sum(rate(
    mitya_cache_lookups_total[5m]))` -- the obvious denominator, and the one
    already written into `ops/grafana/dashboards/mitya-red-spend.json` -- would
    silently start including bypasses, and the published hit rate would sag as
    memory adoption grew while nothing about the cache had degraded. Under two
    metric names that sum cannot be written by accident; pooling now requires
    naming both series, which is a deliberate act a reviewer can see.

    `route` only: no `tenant` (memory-bearing is per-tenant, and P5's budget
    puts tenant on four instruments, R113/R107 -- this is not one of them), and
    no per-tier or per-session label, which is how an unbounded label gets
    minted. Two series, total.
    """
    _instruments.get("cache_bypassed", _NULL).add(1, {"route": route})


def record_empty_completion(route: str) -> None:
    _instruments.get("empty_completions", _NULL).add(1, {"route": route})


class StageTimer:
    """`with StageTimer(route, STAGE_X): ...` -- times a block and records it
    on `mitya_stage_duration_seconds`. Not a span: the span is already open
    (`tracer.start_as_current_span`); this only adds the metrics side of the
    same block, using wall time rather than reading the SDK span's internal
    timestamps, matching this file's existing `t_start`/`t_first_token`
    monotonic-clock idiom in src/api/app.py."""

    def __init__(self, route: str, stage: str):
        self.route, self.stage = route, stage

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def __exit__(self, *exc_info):
        record_stage_duration(time.monotonic() - self._t0, self.route, self.stage)
        return False
