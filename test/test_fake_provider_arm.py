# test/test_fake_provider_arm.py
"""Verify: MITYA_FAKE_PROVIDER selects the fake arm (mitya-v2 P4 Batch C-run, R96).

Plain-assert script, not pytest. Offline, no server, no network, no provider.

R96's budget rests on the ramp that answers CP-4d costing nothing, and that
rests on the fake being SELECTABLE at the serving layer. It was not: the claim
"the FakeProvider seam is already built ... without a code change to the request
path" (schedule-p4.md §4) was asserted and never checked, and the correction to
R96 records it as the third such premise in this project. These checks exist so
the claim is true by test rather than by assertion.

    1. Off by default. A normal run reaches the real adapter.
    2. When set, `_provider_for()` returns the fake and NO vendor adapter is
       constructed.
    3. The two deployment flags COMPOSE: fault injection over the fake, so the
       injector can be exercised at zero spend.

Usage:
    python test/test_fake_provider_arm.py
"""

import importlib
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logger = logging.getLogger(__name__)


def _provider_with(**env):
    """Reimport Config and generate with `env` applied -- both read os.getenv at
    module import, so the flags cannot be toggled in place."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: v for k, v in env.items() if v is not None})
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
    try:
        import src.config
        import src.rag.generate
        importlib.reload(src.config)
        gen = importlib.reload(src.rag.generate)
        return gen._provider_for(src.config.Config.SERVING_MODEL)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import src.config
        import src.rag.generate
        importlib.reload(src.config)
        importlib.reload(src.rag.generate)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # The reload dance re-emits the module warnings each time; they are correct
    # and loud by design (that is the point of the flags), just noise here.
    for noisy in ("src.rag.generate", "src.config", "src.config_artifact"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    log = logging.getLogger("check")

    from src.gateway.fake import FakeProvider

    # 1. Off by default -- the real adapter, not the fake.
    provider = _provider_with(MITYA_FAKE_PROVIDER=None, MITYA_FAULT_INJECT=None)
    assert not isinstance(provider, FakeProvider), "fake selected without the flag"
    log.info("ok: off by default -- a normal run reaches the real adapter")

    # 2. Set -> the fake, and no vendor adapter behind it.
    provider = _provider_with(MITYA_FAKE_PROVIDER="1", MITYA_FAULT_INJECT=None)
    assert isinstance(provider, FakeProvider), f"flag set but got {type(provider).__name__}"
    log.info("ok: MITYA_FAKE_PROVIDER selects the fake arm")

    # "0"/"false" are off, so an unset-by-writing-0 deployment is not silently faked.
    for falsey in ("0", "false", ""):
        provider = _provider_with(MITYA_FAKE_PROVIDER=falsey, MITYA_FAULT_INJECT=None)
        assert not isinstance(provider, FakeProvider), f"{falsey!r} should read as off"
    log.info("ok: '0', 'false' and empty all read as off")

    # 3. The flags compose: fault injection wrapping the fake, zero spend.
    from src.gateway.fault import FaultProvider

    provider = _provider_with(MITYA_FAKE_PROVIDER="1", MITYA_FAULT_INJECT="429:1")
    assert isinstance(provider, FaultProvider), "fault wrap lost when the fake is on"
    inner = provider._inner
    assert isinstance(inner, FakeProvider), f"fault wrapped {type(inner).__name__}, not the fake"
    log.info("ok: fault injection composes over the fake -- injector testable at zero spend")

    log.info("ALL FAKE-ARM CHECKS PASSED")


if __name__ == "__main__":
    main()
