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

    def __init__(self, model: str, script: list, retry_after: float | None = None) -> None:
        self.model = model
        self.script = list(script)
        self.calls = 0
        # What the provider itself says to wait, in seconds -- Groq sends it on
        # every 429, Google sends nothing (R53). None means the loop must fall
        # back to its own backoff.
        self.retry_after = retry_after

    async def generate(self, messages, tier, tenant, budget) -> GenerationStream:
        self.calls += 1
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "transport":
            raise TransportError("fake transport failure", retry_after=self.retry_after)

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
        self._orig_open = gen.open_guarded_stream
        self._orig_q = Config.QUALITY_RETRY_MODELS
        self._orig_a = Config.MAX_GENERATION_ATTEMPTS
        gen._provider_for = lambda model: self.provider
        # Bypass the guard: this test is about retry accounting, not spend.
        async def _open(provider, messages, model):
            return await provider.generate(messages, "default", "test", None)
        gen.open_guarded_stream = _open
        Config.QUALITY_RETRY_MODELS = self.quality_models
        Config.MAX_GENERATION_ATTEMPTS = self.attempts
        return self

    def __exit__(self, *exc):
        gen._provider_for = self._orig_for
        gen.open_guarded_stream = self._orig_open
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


class _recorded_sleeps:
    """Record every delay the retry loop asks for, without waiting it out.

    Swaps `asyncio.sleep` for a recorder that delegates to the real one with a
    zero delay, so a test asserting a 30-second wait takes no seconds. Restored
    unconditionally -- a leaked patch would silently disarm every later test's
    timing.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __enter__(self):
        self._orig = asyncio.sleep
        recorder = self

        async def _sleep(delay, *args, **kwargs):
            recorder.delays.append(delay)
            return await recorder._orig(0, *args, **kwargs)

        asyncio.sleep = _sleep
        return self

    def __exit__(self, *exc):
        asyncio.sleep = self._orig


def test_the_live_loop_sleeps_on_the_providers_own_retry_after() -> None:
    """Batch A0's price for deleting `retry_transport()` (D1, R88).

    Before this, NO test in the repo asserted that `exc.retry_after` is
    honoured, in either retry loop -- so the R68 fix (Groq's `Retry-After`
    latterly arriving in MINUTES, not the ~4s a jittered backoff would guess)
    was unguarded, and the deleted loop had already lost it silently
    (`retry.py` never read the field).

    30s is chosen to be unreachable by the fallback: the backoff is capped at
    `min(4.0, ...) * (0.5 + random())`, i.e. under 6.0s always. A delay at or
    above 30s can only have come from `retry_after`.
    """
    p = _CountingProvider("test/model", ["transport", "ok"], retry_after=30.0)
    with _patched(p), _recorded_sleeps() as slept:
        text, usage = asyncio.run(_run(p))
    assert text == "an answer", text
    assert usage["transport_retries"] == 1, usage
    assert len(slept.delays) == 1, slept.delays
    # Jitter is ADDITIVE on this branch (`* (1.0 + 0.25 * random())`): never
    # shorter than the provider asked for, at most 25% longer.
    assert 30.0 <= slept.delays[0] < 37.5, (
        f"the provider asked for 30s and the loop waited {slept.delays[0]}s -- "
        f"retry_after is not being honoured"
    )


def test_without_a_retry_after_the_loop_falls_back_to_its_own_backoff() -> None:
    """The other half of the branch. Google sends no rate-limit headers at all
    (R53), so `retry_after=None` must NOT become a zero wait or an infinity."""
    p = _CountingProvider("test/model", ["transport", "ok"], retry_after=None)
    with _patched(p), _recorded_sleeps() as slept:
        asyncio.run(_run(p))
    assert len(slept.delays) == 1, slept.delays
    assert 0.0 < slept.delays[0] <= 6.0, slept.delays


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
