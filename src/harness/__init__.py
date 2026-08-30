"""Traffic harness (v2 P4, charter §III CP4; schedule Batch A steps 1, 2).

Boundary, stated once so it does not get quietly violated: everything in this
package talks to the running API over HTTP, through `HarnessClient`, and
NOTHING ELSE. No module here imports `src.rag`, `src.api`, `src.gateway`, or
any other application internal -- the harness gets exactly the access a real
external caller gets. If a number this package needs is not observable from
outside plus P1's operator plane (`/metrics`, `/health`, `/ready`), that is a
finding about the API (recorded in `plans/mitya-v2/05-impl/phase-4.md`'s
observability gap list), not a reason to reach inside.

Batch A shipped `client.py` (step 1, the external client) and `replay.py`
(step 2, replay mode). Batch B added `sessions.py` (step 3/4, the frozen
session corpus and its A5 repeat-rate dial). Batch C adds the measuring
machinery: `ramp.py` (step 5, the load-ramp driver), `saturation.py` (step 6,
the backpressure OBSERVATION per D2 -- not a verification), and `rungs.py`
(step 8, the ladder rung forcer). Step 7's fault injector is server-side by
CP-4b's own decision (D7/D8) -- `src/gateway/fault.py`, not here; nothing in
this package injects a fault, it only observes what the server did when one
was.
"""

from src.harness.client import ChatResult, HarnessClient

__all__ = ["ChatResult", "HarnessClient"]
