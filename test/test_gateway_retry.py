# test/test_gateway_retry.py
"""P2 step 8(a) verify (schedule Batch B): the hosted zero-token-completion
counter -- src/gateway/retry.py.

Plain-assert script, not pytest. Offline: no network, no key.

**Three retry tests used to live here and were deleted at P4 Batch A0 item 3,
with the function they covered.** `retry_transport()` had zero request-path
call sites (R88); the live transport retry is `src/rag/generate.py`'s loop and
`test/test_retry_classes.py` is where it is asserted -- including, added by the
same batch, that it sleeps on the provider's own `retry_after`, which is the
assertion no test in this repo made before.
"""

import sys

sys.path.insert(0, ".")

from src import metrics  # noqa: E402
from src.gateway.contract import Usage  # noqa: E402
from src.gateway.retry import record_if_empty_completion  # noqa: E402

metrics.configure_metrics()


def test_hosted_zero_token_completion_is_counted() -> None:
    """v2 P2 step 8(a)'s ~3-line counter: a hosted completion with zero
    tokens increments the SAME instrument the local llama-server path uses
    (mitya_empty_completions_total)."""
    before = _empty_completions_total("STRUCTURAL")
    record_if_empty_completion("STRUCTURAL", Usage(completion_tokens=0, model="fake/replay-v1"))
    after = _empty_completions_total("STRUCTURAL")
    assert after == before + 1


def test_a_nonzero_completion_is_not_counted_as_empty() -> None:
    before = _empty_completions_total("DEFAULT")
    record_if_empty_completion("DEFAULT", Usage(completion_tokens=13, model="fake/replay-v1"))
    after = _empty_completions_total("DEFAULT")
    assert after == before


def test_an_undrained_stream_is_not_counted_as_empty() -> None:
    """completion_tokens is None before a stream is drained -- None must
    never be read as zero (Usage's own contract, contract.py)."""
    before = _empty_completions_total("DEFAULT")
    record_if_empty_completion("DEFAULT", Usage())  # completion_tokens defaults to None
    after = _empty_completions_total("DEFAULT")
    assert after == before


def _empty_completions_total(route: str) -> float:
    from prometheus_client import REGISTRY, generate_latest

    lines = generate_latest(REGISTRY).decode().splitlines()
    for line in lines:
        if line.startswith(f'mitya_empty_completions_total{{route="{route}"}}'):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
