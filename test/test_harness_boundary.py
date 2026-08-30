# test/test_harness_boundary.py
"""Verify: two standing rules that must stay true as `src/harness/` grows
(mitya-v2 P4 step 1's public-API-only rule; CP-4b/D8's default-off toggle).

Plain-assert script, not pytest. Offline, source-level only: no network, no
server, no import of the modules being checked (a static text/AST scan, so a
broken import elsewhere in `src/harness/` cannot mask this check).
"""

import ast
import glob
import json
import os
import sys

sys.path.insert(0, ".")

from src.config import Config  # noqa: E402


def _imported_module_roots(path: str) -> set[str]:
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_nothing_in_harness_reaches_into_the_application() -> None:
    """schedule-p4.md step 1: 'public API only, no in-process shortcuts.'
    Mechanically checkable: no module under `src/harness/` may import
    `src.rag`, `src.api`, or `src.gateway` -- the application internals a
    real external caller has no access to. `src.harness.*` imports of other
    harness modules (sessions.py/replay.py importing client.py) are the
    stated exception -- the rule is about reaching the APPLICATION, not
    about the harness package importing itself.
    """
    forbidden = {"src.rag", "src.api", "src.gateway"}
    for path in sorted(glob.glob("src/harness/*.py")):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    for bad in forbidden:
                        assert not (module == bad or module.startswith(bad + ".")), (
                            f"{path} imports {module!r} -- src/harness/ must stay "
                            f"public-API-only (step 1)"
                        )
                continue
            if module is None:
                continue
            for bad in forbidden:
                assert not (module == bad or module.startswith(bad + ".")), (
                    f"{path} imports from {module!r} -- src/harness/ must stay "
                    f"public-API-only (step 1)"
                )


def test_fault_inject_is_off_by_default() -> None:
    assert Config.FAULT_INJECT == "", (
        f"Config.FAULT_INJECT must default to empty (off); got {Config.FAULT_INJECT!r}. "
        f"This is a deployment field (config.py), not a versioned one -- it must never "
        f"be on unless MITYA_FAULT_INJECT is set deliberately."
    )


def test_no_serving_config_enables_fault_injection() -> None:
    """`Config.FAULT_INJECT` is a deployment env var, not a versioned field
    (D8) -- it should never even be a KEY in a `config/serving/*.json`
    artifact. Belt-and-suspenders: if a future edit ever adds a
    'fault_inject'-shaped key to a serving config by mistake, this catches it
    without needing to load every artifact through the full facade."""
    for path in sorted(glob.glob("config/serving/*.json")):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        flat = json.dumps(data).lower()
        assert "fault_inject" not in flat, f"{path} must not carry a fault-injection field (D8)"


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    test_nothing_in_harness_reaches_into_the_application()
    print("ok: nothing in src/harness/ imports from the application")
    test_fault_inject_is_off_by_default()
    print("ok: FAULT_INJECT is off by default")
    test_no_serving_config_enables_fault_injection()
    print("ok: no serving config under config/serving/ enables fault injection")
    print("ALL BOUNDARY CHECKS PASSED")
