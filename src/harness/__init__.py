"""Traffic harness (v2 P4, charter §III CP4; schedule Batch A steps 1, 2).

Boundary, stated once so it does not get quietly violated: everything in this
package talks to the running API over HTTP, through `HarnessClient`, and
NOTHING ELSE. No module here imports `src.rag`, `src.api`, `src.gateway`, or
any other application internal -- the harness gets exactly the access a real
external caller gets. If a number this package needs is not observable from
outside plus P1's operator plane (`/metrics`, `/health`, `/ready`), that is a
finding about the API (recorded in `plans/mitya-v2/05-impl/phase-4.md`'s
observability gap list), not a reason to reach inside.

Batch A ships two of the four modes named in `04-plan/phase-4-harness.md`:
`client.py` (step 1, the external client) and `replay.py` (step 2, replay
mode). The session-corpus generator, load ramp, and fault injector are later
batches (schedule.md §6) and do not exist yet -- nothing here scaffolds for
them.
"""

from src.harness.client import ChatResult, HarnessClient

__all__ = ["ChatResult", "HarnessClient"]
