# test/test_call_class.py
"""v3 P0 B2 verify (G-P8, schedule-p0 III.6 item 5): the gateway refuses a call class it does not
know before anything is touched (limiter, ledger, store, provider), and the call-class and
provider metric labels are closed.

Plain-assert script. Offline: FakeProvider and an in-memory transport, temp SQLite. Run from the
repo root.
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, ".")
sys.path.insert(0, "test")

import provider_replay as pr  # noqa: E402

from src import metrics  # noqa: E402
from src.db import init_schema  # noqa: E402
from src.gateway.contract import CallClass, DecideResult, Usage  # noqa: E402
from src.gateway.fake import FakeProvider  # noqa: E402
from src.gateway.guard import (  # noqa: E402
    SpendRateBreaker,
    UnknownCallClass,
    admit_call_class,
    guarded_decide,
    guarded_generate,
)
from src.gateway.ledger import RequestLedger, SpendStore  # noqa: E402
from src.limits import RateLimiter  # noqa: E402
from src.limits_config import SpendLimits  # noqa: E402

WIDE = SpendLimits(
    per_call_usd=1000.0, per_request_usd=1000.0, tenant_day_usd=1e6, tenant_day_tokens=10**9,
    global_minute_usd=1e6, global_day_usd=1e6, global_day_tokens=10**9, global_month_usd=1e6,
    unpriced_call_tokens=10**9,
)
MESSAGES = [{"role": "user", "content": "who are you?"}]
UNKNOWN = ("embed", "rerank", "tool_call", "", "GENERATION", " generation", None, 7)


class StubDecider:
    """A `Decider` that records its calls."""

    model = "stub/decider"

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def decide(self, state, questions, tenant, budget):
        self.calls.append((state, questions, tenant, budget))
        return DecideResult(answers={}, usage=Usage(model=self.model))


class TripwireLimiter(RateLimiter):
    """Fails the test if the gateway reaches the limiter."""

    def __init__(self) -> None:
        super().__init__(rpm=10**6, tpm=10**9, rpd=10**9, sleep=lambda s: None)
        self.touched = 0

    async def acquire_async(self, tokens):
        self.touched += 1
        return await super().acquire_async(tokens)


def _parts():
    path = os.path.join(tempfile.mkdtemp(prefix="mitya-class-db-"), "t.sqlite3")
    init_schema(path)
    store = SpendStore(WIDE, path)
    return TripwireLimiter(), RequestLedger(WIDE, store), store


def _store_touched(*args, **kwargs):
    raise AssertionError("the spend store was reached by a refused call class")


def _generate(provider, limiter, ledger, call_class):
    return guarded_generate(
        provider, MESSAGES, "default", "tenant-a", call_class=call_class, limiter=limiter,
        breaker=SpendRateBreaker(1e6, 0.8), ledger=ledger, estimated_cost_usd=0.0001,
        max_input_tokens=10**6, max_history_messages=10, max_output_tokens=10,
    )


def test_an_unknown_call_class_is_refused_before_anything_is_touched() -> None:
    for call_class in UNKNOWN:
        limiter, ledger, store = _parts()
        store.reserve = _store_touched
        provider = FakeProvider()
        try:
            asyncio.run(_generate(provider, limiter, ledger, call_class))
        except UnknownCallClass as exc:
            assert exc.status == 400
        else:
            raise AssertionError(f"{call_class!r} was admitted")
        assert provider.calls == [], f"{call_class!r} reached the provider"
        assert limiter.touched == 0 and ledger.entries == []


def test_embed_and_rerank_are_not_members_until_p2() -> None:
    assert {m.value for m in CallClass} == {"generation", "decide"}
    for name in ("embed", "rerank"):
        try:
            admit_call_class(name, CallClass.GENERATION)
        except UnknownCallClass:
            continue
        raise AssertionError(f"{name} admitted")


def test_each_entry_point_serves_only_its_own_class() -> None:
    limiter, ledger, _ = _parts()
    provider = FakeProvider()
    try:
        asyncio.run(_generate(provider, limiter, ledger, "decide"))
    except UnknownCallClass:
        pass
    else:
        raise AssertionError("decide was served by the generation entry")
    decider = StubDecider()

    async def go():
        return await guarded_decide(
            decider, "state", {}, "tenant-a", call_class="generation",
            limiter=limiter, breaker=SpendRateBreaker(1e6, 0.8), ledger=ledger,
            estimated_cost_usd=0.0001, max_input_tokens=10**6, max_output_tokens=10,
        )

    try:
        asyncio.run(go())
    except UnknownCallClass:
        pass
    else:
        raise AssertionError("generation was served by the decide entry")
    assert provider.calls == [] and decider.calls == []
    assert limiter.touched == 0 and ledger.entries == []


def test_known_classes_are_admitted() -> None:
    limiter, ledger, _ = _parts()

    async def go():
        stream = await _generate(FakeProvider(), limiter, ledger, "generation")
        return [c async for c in stream.chunks]

    assert asyncio.run(go())
    assert limiter.touched == 1 and len(ledger.entries) == 1
    decider, limiter, ledger = StubDecider(), TripwireLimiter(), _parts()[1]

    async def decide():
        return await guarded_decide(
            decider, "state", {}, "tenant-a", call_class="decide", limiter=limiter,
            breaker=SpendRateBreaker(1e6, 0.8), ledger=ledger, estimated_cost_usd=0.0001,
            max_input_tokens=10**6, max_output_tokens=10,
        )

    asyncio.run(decide())
    assert len(decider.calls) == 1 and limiter.touched == 1 and len(ledger.entries) == 1


def test_call_class_labels_are_closed() -> None:
    assert metrics.CALL_CLASSES == tuple(m.value for m in CallClass)
    assert set(metrics.GATEWAY_ADMISSIONS) == {"admitted", "refused"}

    class Recorder:
        def __init__(self) -> None:
            self.labels: list[dict] = []

        def add(self, n, labels) -> None:
            self.labels.append(labels)

    rec, saved = Recorder(), metrics._instruments.get("gateway_calls")
    metrics._instruments["gateway_calls"] = rec
    try:
        metrics.record_gateway_call("generation", True)
        metrics.record_gateway_call("embed", False)
        metrics.record_gateway_call("x" * 500, False)
    finally:
        if saved is None:
            metrics._instruments.pop("gateway_calls", None)
        else:
            metrics._instruments["gateway_calls"] = saved
    assert rec.labels == [
        {"call_class": "generation", "outcome": "admitted"},
        {"call_class": "unknown", "outcome": "refused"},
        {"call_class": "unknown", "outcome": "refused"},
    ]


def test_provider_and_model_labels_are_closed() -> None:
    from src.config import Config

    for surface in pr.SEED["adapter_profiles"]:
        assert surface in metrics.PROVIDERS, surface
    # Ids with no MODEL_LIMITS / MODEL_RATES row collapse to unknown instead of minting a series.
    for model in ("typesafe/jev-1.14", "typesafe/jev-1.14-20261014", "gpt-oss:120b", "deepinfra/x"):
        assert metrics._model_key(model) == metrics.UNKNOWN, model
        assert metrics._provider_key(model) == metrics.UNKNOWN, model
    saved = Config.MODEL_LIMITS
    try:
        Config.MODEL_LIMITS = {**saved, "stub/odd": {"provider": "not-a-provider"}}
        assert metrics._provider_key("stub/odd") == metrics.UNKNOWN
    finally:
        del Config.MODEL_LIMITS


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
