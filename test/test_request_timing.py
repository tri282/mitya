# test/test_request_timing.py
"""P1 step 2 prerequisite: the /chat timing contract (R30 defect 4, R33).

Plain-assert script, not pytest. Offline: no GPU, no llama-server, no Qdrant.
Replays `src/api/app.py`'s timing logic against stub generators, because the
condition that broke it -- every EMPTY_RETRIES attempt returning zero tokens --
is ~2% per request against a real model (R29) and query-specific, so waiting for
it is not a test strategy.

The invariant under test: `ttft_ms` is the time a *token* first arrived, or None.
It is never the time the server gave up. Before R33, the empty path set
`t_first_token` on StopAsyncIteration, so a request that produced no tokens at
all reported ttft_ms == total_ms -- a number that looks like a measurement and
is not one. Step 2 wraps spans around exactly these timers; a lying timer with a
span around it is a wrong number with a better UI.
"""

import asyncio
import sys
import time

sys.path.insert(0, ".")

EMPTY_RETRIES = 4
ATTEMPT_S = 0.02  # stands in for one _stream_once round trip


async def _empty_stream():
    """Mirrors generate.stream_answer when every attempt yields nothing."""
    for _ in range(EMPTY_RETRIES + 1):
        await asyncio.sleep(ATTEMPT_S)
    return
    yield  # marks this an async generator


async def _late_stream():
    """First attempt empty, second yields real tokens -- the retry that works."""
    await asyncio.sleep(ATTEMPT_S)
    for text in ("Raskolnikov ", "walked ", "out."):
        await asyncio.sleep(ATTEMPT_S)
        yield text


async def _drive(gen):
    """app.py's priming + streaming + meta arithmetic, structure preserved."""
    t_start = time.monotonic()

    t_first_token: float | None = None
    try:
        first_token = await gen.__anext__()
        t_first_token = time.monotonic()
    except StopAsyncIteration:
        first_token = None

    answer_parts: list[str] = []
    if first_token:
        answer_parts.append(first_token)
    async for token in gen:
        answer_parts.append(token)

    total_ms = int((time.monotonic() - t_start) * 1000)
    ttft_ms = int((t_first_token - t_start) * 1000) if t_first_token is not None else None
    return ttft_ms, total_ms, "".join(answer_parts)


def test_empty_completion_reports_no_ttft() -> None:
    ttft_ms, total_ms, answer = asyncio.run(_drive(_empty_stream()))
    assert answer == "", answer
    assert ttft_ms is None, (
        f"ttft_ms={ttft_ms} on a stream that produced no tokens -- that is time-to-give-up, "
        "not time-to-first-token (R33)"
    )
    # The retry cost is real and must still be visible somewhere: it lands in
    # total_ms, and step 2's generation span attributes it properly.
    assert total_ms >= int((EMPTY_RETRIES + 1) * ATTEMPT_S * 1000 * 0.8), total_ms


def test_ttft_never_exceeds_total() -> None:
    """The R30 pairing (ttft_ms=2387 > total_ms=2036) must be impossible by construction."""
    for gen_factory in (_empty_stream, _late_stream):
        ttft_ms, total_ms, _ = asyncio.run(_drive(gen_factory()))
        if ttft_ms is not None:
            assert ttft_ms <= total_ms, f"{gen_factory.__name__}: {ttft_ms} > {total_ms}"


def test_retry_that_succeeds_reports_a_real_ttft() -> None:
    """A stream whose first attempt is empty still has a genuine first token."""
    ttft_ms, total_ms, answer = asyncio.run(_drive(_late_stream()))
    assert answer == "Raskolnikov walked out.", answer
    assert ttft_ms is not None and ttft_ms > 0
    assert ttft_ms < total_ms, (ttft_ms, total_ms)


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
