# test/test_gateway_fault.py
"""Verify: src/gateway/fault.py (v2 P4 Batch C step 7, CP-4b D7/D8/D9).

Plain-assert script, not pytest. Offline: fake inner provider, no network, no
key, no server.

Covers: the spec grammar (`<class>:<attempts|*>`), the absorbed-429 shape
(D9's only verified taxonomy row -- fail N attempts then pass through, with
`retry_after` set so the live retry loop's honouring of it is exercisable),
the exhausting shape (`*` never delegates), and that `_provider_for()` in
`src/rag/generate.py` returns the bare adapter -- no wrapping at all -- when
`Config.FAULT_INJECT` is unset, which is the default-off guarantee D8/the
schedule's standing gate depends on.
"""

import asyncio
import sys

sys.path.insert(0, ".")

from src.config import Config  # noqa: E402
from src.gateway.fault import FaultProvider, FaultSpec, parse_fault_spec  # noqa: E402
from src.gateway.ladder import ContentRefused  # noqa: E402
from src.gateway.retry import TransportError  # noqa: E402

_MESSAGES = [{"role": "system", "content": "persona"}, {"role": "user", "content": "q"}]


class _InnerProvider:
    model = "fake/inner-v1"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, tier, tenant, budget):
        self.calls += 1

        async def _chunks():
            yield "ok"

        from src.gateway.contract import GenerationStream, Usage
        return GenerationStream(chunks=_chunks(), usage=Usage(model=self.model))


def test_parse_fault_spec_grammar() -> None:
    assert parse_fault_spec("429:2") == FaultSpec(kind="429", attempts=2)
    assert parse_fault_spec("429:*") == FaultSpec(kind="429", attempts=None)
    assert parse_fault_spec("timeout:1") == FaultSpec(kind="timeout", attempts=1)
    assert parse_fault_spec("refused:*") == FaultSpec(kind="refused", attempts=None)

    for bad in ("429", "429:0", "429:-1", "unknown:1", "429:abc"):
        try:
            parse_fault_spec(bad)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError:
            pass


def test_absorbed_429_delegates_after_the_configured_attempts_with_retry_after() -> None:
    """D9's only verified taxonomy row: fail the first 2, then pass through."""
    inner = _InnerProvider()
    provider = FaultProvider(inner, FaultSpec(kind="429", attempts=2))

    async def run():
        results = []
        for _ in range(3):
            stream = await provider.generate(_MESSAGES, "default", "tenant", None)
            try:
                first = await anext(aiter(stream.chunks))
                results.append(("ok", first))
            except TransportError as exc:
                results.append(("429", exc.retry_after))
        return results

    results = asyncio.run(run())
    assert results[0] == ("429", 2.0), results
    assert results[1] == ("429", 2.0), results
    assert results[2] == ("ok", "ok"), results
    assert inner.calls == 1, "the inner provider is only reached once attempts are spent"


def test_exhausting_429_never_delegates() -> None:
    inner = _InnerProvider()
    provider = FaultProvider(inner, FaultSpec(kind="429", attempts=None))

    async def run():
        for _ in range(5):
            stream = await provider.generate(_MESSAGES, "default", "tenant", None)
            try:
                await anext(aiter(stream.chunks))
                raise AssertionError("expected TransportError on every attempt")
            except TransportError:
                pass

    asyncio.run(run())
    assert inner.calls == 0


def test_timeout_and_refused_classes_raise_the_right_exception() -> None:
    async def first_chunk(kind: str):
        provider = FaultProvider(_InnerProvider(), FaultSpec(kind=kind, attempts=None))
        stream = await provider.generate(_MESSAGES, "default", "tenant", None)
        return await anext(aiter(stream.chunks))

    try:
        asyncio.run(first_chunk("timeout"))
        raise AssertionError("expected TransportError")
    except TransportError as exc:
        assert exc.retry_after is None

    try:
        asyncio.run(first_chunk("refused"))
        raise AssertionError("expected ContentRefused")
    except ContentRefused:
        pass


def test_fault_inject_is_off_by_default_and_provider_for_returns_the_bare_adapter() -> None:
    """The standing gate: `Config.FAULT_INJECT` unset means `_provider_for()`
    performs no wrapping at all -- not a FaultProvider configured to never
    fire, an entirely different object."""
    assert Config.FAULT_INJECT == "", (
        "MITYA_FAULT_INJECT must default to empty (off) -- got %r" % Config.FAULT_INJECT
    )
    from src.rag.generate import _provider_for, _resolve_adapter

    model = next(iter(Config.MODEL_LIMITS)) if Config.MODEL_LIMITS else "local-q4km"
    bare = _resolve_adapter(model)
    wrapped = _provider_for(model)
    assert type(wrapped) is type(bare), (
        f"_provider_for() must return the bare adapter type when FAULT_INJECT is unset, "
        f"got {type(wrapped)} vs {type(bare)}"
    )


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    test_parse_fault_spec_grammar()
    print("ok: fault spec grammar")
    test_absorbed_429_delegates_after_the_configured_attempts_with_retry_after()
    print("ok: absorbed 429 delegates after configured attempts, retry_after carried")
    test_exhausting_429_never_delegates()
    print("ok: exhausting 429 never delegates")
    test_timeout_and_refused_classes_raise_the_right_exception()
    print("ok: timeout/refused raise the right exception classes")
    test_fault_inject_is_off_by_default_and_provider_for_returns_the_bare_adapter()
    print("ok: FAULT_INJECT off by default, _provider_for returns the bare adapter")
    print("ALL FAULT INJECTION CHECKS PASSED")
