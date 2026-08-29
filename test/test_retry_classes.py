# test/test_retry_classes.py
"""P2 step 8(b) verify: the two retry classes and their SHARED budget.

Plain-assert script, not pytest (matches test_gateway_contract.py's idiom).
Offline: fake providers, no network, no key.

The load-bearing property is the shared budget. `phase-2-gateway.md` step 8
requires the two classes not to "double up", because retries are a cost
multiplier: three transport attempts and three quality attempts would be nine
generations for one answer. A per-class ceiling is not a ceiling.

The second property is that the quality retry is OFF by default (CP-γ / R70),
so a zero-token completion costs one generation, not `MAX_GENERATION_ATTEMPTS`
of them.
"""

import asyncio
import sys

sys.path.insert(0, ".")

from src.config import Config  # noqa: E402
from src.gateway.contract import GenerationStream, Usage  # noqa: E402
from src.gateway.retry import TransportError  # noqa: E402
from src.rag import generate as gen  # noqa: E402

_MESSAGES = [{"role": "system", "content": "persona"}, {"role": "user", "content": "q"}]


class _CountingProvider:
    """Records how many generations were actually requested -- the number the
    cost multiplier is computed from."""

    def __init__(self, model: str, script: list) -> None:
        self.model = model
        self.script = list(script)
        self.calls = 0

    async def generate(self, messages, tier, tenant, budget) -> GenerationStream:
        self.calls += 1
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "transport":
            raise TransportError("fake transport failure")

        async def _chunks():
            if outcome == "empty":
                return
            yield "an answer"
            # `return` inside an async generator with a prior yield is fine;
            # this branch simply ends the stream.

        completion = 0 if outcome == "empty" else 2
        return GenerationStream(chunks=_chunks(), usage=Usage(model=self.model,
                                                              prompt_tokens=10,
                                                              completion_tokens=completion))


class _patched:
    """Point generate.py at a fake provider and a chosen policy."""

    def __init__(self, provider, quality_models=frozenset(), attempts=3):
        self.provider, self.quality_models, self.attempts = provider, quality_models, attempts

    def __enter__(self):
        self._orig_for = gen._provider_for
        self._orig_open = gen._open_stream
        self._orig_q = Config.QUALITY_RETRY_MODELS
        self._orig_a = Config.MAX_GENERATION_ATTEMPTS
        gen._provider_for = lambda model: self.provider
        # Bypass the guard: this test is about retry accounting, not spend.
        async def _open(provider, messages, model):
            return await provider.generate(messages, "default", "test", None)
        gen._open_stream = _open
        Config.QUALITY_RETRY_MODELS = self.quality_models
        Config.MAX_GENERATION_ATTEMPTS = self.attempts
        return self

    def __exit__(self, *exc):
        gen._provider_for = self._orig_for
        gen._open_stream = self._orig_open
        Config.QUALITY_RETRY_MODELS = self._orig_q
        Config.MAX_GENERATION_ATTEMPTS = self._orig_a


async def _run(provider, model="test/model"):
    usage: dict = {}
    text = ""
    orig = Config.SERVING_MODEL
    try:
        Config.SERVING_MODEL = model
        async for token in gen.stream_answer(_MESSAGES, usage):
            text += token
    finally:
        Config.SERVING_MODEL = orig
    return text, usage


def test_transport_failures_are_retried_then_succeed() -> None:
    p = _CountingProvider("test/model", ["transport", "transport", "ok"])
    with _patched(p):
        text, usage = asyncio.run(_run(p))
    assert text == "an answer", text
    assert p.calls == 3, p.calls
    assert usage["transport_retries"] == 2, usage
    assert usage["quality_retries"] == 0, usage


def test_quality_retry_is_off_by_default_so_an_empty_answer_costs_one_generation() -> None:
    """CP-γ's default. Google measured 0 zero-token completions over 200 draws,
    so paying for re-draws everywhere would buy nothing."""
    p = _CountingProvider("test/model", ["empty", "ok", "ok"])
    with _patched(p, quality_models=frozenset()):
        text, usage = asyncio.run(_run(p))
    assert text == "", f"expected the empty completion to be returned, got {text!r}"
    assert p.calls == 1, f"quality retry fired while disabled: {p.calls} generations"
    assert usage["quality_retries"] == 0, usage


def test_quality_retry_fires_only_for_an_opted_in_model() -> None:
    p = _CountingProvider("test/model", ["empty", "ok"])
    with _patched(p, quality_models=frozenset({"test/model"})):
        text, usage = asyncio.run(_run(p))
    assert text == "an answer", text
    assert p.calls == 2, p.calls
    assert usage["quality_retries"] == 1, usage


def test_the_two_classes_share_one_budget_and_cannot_double_up() -> None:
    """The property step 8 actually asks for.

    Three transport failures then three empties, with both classes enabled and
    a budget of 3: a PER-CLASS ceiling would permit up to six generations. The
    shared budget must stop at three.
    """
    p = _CountingProvider("test/model", ["transport", "empty", "transport", "empty", "ok", "ok"])
    with _patched(p, quality_models=frozenset({"test/model"}), attempts=3):
        try:
            _, usage = asyncio.run(_run(p))
        except TransportError:
            usage = {}
    assert p.calls <= 3, (
        f"the classes compounded: {p.calls} generations against a 3-attempt budget"
    )


def test_budget_is_exhausted_by_either_class_alone() -> None:
    """A budget only one class can spend is two budgets wearing one name."""
    p = _CountingProvider("test/model", ["empty"] * 10)
    with _patched(p, quality_models=frozenset({"test/model"}), attempts=3):
        text, usage = asyncio.run(_run(p))
    assert text == "", text
    assert p.calls == 3, f"expected exactly the 3-attempt budget, got {p.calls}"
    assert usage["quality_retries"] == 2, usage
    assert usage["attempts"] == 3, usage


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
