# test/test_wiring_enums.py
"""v3 P0 B1 verify (OI27): the closed enums for finish reason, guard verdict and refusal layer,
the finish-reason normalization over recorded values, and the counted `other` bucket.

Plain-assert script. Offline: stub providers, no network. Run from the repo root.
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, ".")

from src import metrics  # noqa: E402
from src.db import init_schema  # noqa: E402
from src.gateway import guard as guard_mod  # noqa: E402
from src.gateway.contract import (  # noqa: E402
    INCOMPLETE_FINISHES,
    FinishReason,
    GenerationStream,
    Usage,
    normalize_finish_reason,
)
from src.gateway.fake import FakeProvider, Fixture  # noqa: E402
from src.gateway.google import _REFUSAL_CODES  # noqa: E402
from src.gateway.guard import SpendRateBreaker, guarded_generate  # noqa: E402
from src.gateway.ledger import RequestLedger, SpendLayer, SpendStore  # noqa: E402
from src.guard import GuardLayer  # noqa: E402
from src.limits import RateLimiter  # noqa: E402
from src.limits_config import SpendLimits  # noqa: E402
from src.pricing import Cost  # noqa: E402


def test_metric_label_sets_mirror_the_enums() -> None:
    assert metrics.FINISH_REASONS == tuple(m.value for m in FinishReason)
    assert metrics.SPEND_LAYERS == tuple(m.value for m in SpendLayer)
    assert metrics.GUARD_LAYERS == tuple(m.value for m in GuardLayer) + ("none",)


def test_recorded_values_normalize_as_the_findings_say() -> None:
    table = [
        # Google status values (R53, R56)
        ("completed", None, FinishReason.STOP),
        ("truncated", None, FinishReason.LENGTH),
        # OpenAI dialect on Groq, Baseten, Purdue, OpenRouter (F4, F23, F26, F28)
        ("stop", None, FinishReason.STOP),
        ("length", None, FinishReason.LENGTH),
        ("error", None, FinishReason.ERROR),
        ("STOP", None, FinishReason.STOP),
        # OpenRouter native_finish_reason (F24)
        ("stop", "STOP", FinishReason.STOP),
        ("error", "MALFORMED_FUNCTION_CALL", FinishReason.ERROR),
        (None, "length", FinishReason.LENGTH),
        # absent and unknown
        (None, None, FinishReason.NONE),
        ("", None, FinishReason.NONE),
        ("mystery", None, FinishReason.OTHER),
        (None, "mystery", FinishReason.OTHER),
        ("mystery", "stop", FinishReason.STOP),
    ]
    for raw, native, expected in table:
        assert normalize_finish_reason(raw, native) is expected, (raw, native)
    for code in _REFUSAL_CODES:
        assert normalize_finish_reason(code) is FinishReason.CONTENT_FILTER, code
    for member in FinishReason:
        assert normalize_finish_reason(member.value) is member, "normalization must be idempotent"
    assert INCOMPLETE_FINISHES == {FinishReason.LENGTH, FinishReason.ERROR, FinishReason.CONTENT_FILTER}


# --- the unknown bucket is counted, never dropped -------------------------------

_DB = os.path.join(tempfile.mkdtemp(prefix="mitya-enum-db-"), "t.sqlite3")
init_schema(_DB)
_SPEND = SpendLimits(
    per_call_usd=1.0, per_request_usd=1.0, tenant_day_usd=1e6, tenant_day_tokens=10**9,
    global_minute_usd=1e6, global_day_usd=1e6, global_day_tokens=10**9, global_month_usd=1e6,
)


class _Finishing:
    model = "stub/finish"

    def __init__(self, finish, native=None):
        self._finish, self._native = finish, native

    async def generate(self, messages, tier, tenant, budget):
        usage = Usage(model=self.model)

        async def _chunks():
            yield "x"
            usage.cost = Cost(0.0001, True)
            usage.finish_reason, usage.native_finish_reason = self._finish, self._native

        return GenerationStream(chunks=_chunks(), usage=usage)


def _drive(provider) -> tuple[list[str], Usage]:
    seen: list[str] = []
    saved = guard_mod.record_finish_reason
    guard_mod.record_finish_reason = seen.append
    ledger = RequestLedger(_SPEND, SpendStore(_SPEND, _DB))

    async def _go():
        stream = await guarded_generate(
            provider, [{"role": "user", "content": "q"}], "default", "default",
            limiter=RateLimiter(rpm=10**6, tpm=10**9, rpd=10**9, sleep=lambda s: None),
            breaker=SpendRateBreaker(1e6, 0.8), ledger=ledger, estimated_cost_usd=0.0001,
            max_input_tokens=10**6, max_history_messages=10, max_output_tokens=0,
        )
        [c async for c in stream.chunks]
        return stream.usage

    try:
        usage = asyncio.run(_go())
    finally:
        guard_mod.record_finish_reason = saved
    return seen, usage


def test_unknown_finish_reason_is_counted_as_other_and_the_raw_value_is_kept() -> None:
    seen, usage = _drive(_Finishing("mystery"))
    assert seen == ["other"]
    assert usage.finish_reason == "mystery", "the raw value is never dropped"


def test_known_values_are_counted_under_their_enum_value() -> None:
    assert _drive(_Finishing("completed"))[0] == ["stop"]
    assert _drive(_Finishing("error", "MALFORMED"))[0] == ["error"]
    assert _drive(_Finishing(None))[0] == ["none"]


def test_a_forced_truncation_on_the_fake_is_counted_as_length() -> None:
    provider = FakeProvider(fixture=Fixture(("a",), 10, 1, 1.0, 4.0, finish_reason="length"))
    seen, usage = _drive(provider)
    assert seen == ["length"] and usage.finish_reason == "length"


def test_a_failed_send_counts_no_finish_reason() -> None:
    from src.gateway.fault import FaultProvider, FaultSpec
    from src.gateway.retry import TransportError

    faulty = FaultProvider(FakeProvider(), FaultSpec(kind="timeout", attempts=1))
    seen: list[str] = []
    saved = guard_mod.record_finish_reason
    guard_mod.record_finish_reason = seen.append
    try:
        try:
            _drive_raw(faulty)
        except TransportError:
            pass
        else:
            raise AssertionError("the fault did not surface")
    finally:
        guard_mod.record_finish_reason = saved
    assert seen == []


def _drive_raw(provider) -> None:
    ledger = RequestLedger(_SPEND, SpendStore(_SPEND, _DB))

    async def _go():
        stream = await guarded_generate(
            provider, [{"role": "user", "content": "q"}], "default", "default",
            limiter=RateLimiter(rpm=10**6, tpm=10**9, rpd=10**9, sleep=lambda s: None),
            breaker=SpendRateBreaker(1e6, 0.8), ledger=ledger, estimated_cost_usd=0.0001,
            max_input_tokens=10**6, max_history_messages=10, max_output_tokens=0,
        )
        [c async for c in stream.chunks]

    asyncio.run(_go())


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
