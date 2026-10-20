"""Model gateway (v2 P2, charter D1/D10; schedule Batch A step 1).

Boundary: the gateway owns provider adapters, routing, the budget guard,
timeout/retry/circuit-breaker, and provider-side caching. It does NOT own
prompt construction -- `src/rag/generate.py`'s build_messages() stays exactly
where it is and keeps composing the messages list this package's contract
consumes.

Why the seam sits here and not inside one "generate" module: P7 needs prompts
and routing to be independent search axes (phase-2-gateway.md step 1). If a
provider adapter also decided what to say, an optimizer sweeping routing
could not tell whether a delta came from the arm or from a prompt change that
rode along with it. Two separate concerns kept in two separate places, so one
can change without touching the other.

Batch A shipped the seam: the `Provider` contract (`contract.py`) and its
first, vendor-free implementation, `FakeProvider` (`fake.py`).

Batch B (schedule steps 4, 8(a), 9, 10) adds everything buildable with no
vendor chosen: the spend guard (`guard.py` -- budget check, request size
limits, spend-rate breaker, all before the provider call, and
`RateLimiter.acquire_async`), the transport failure vocabulary (`retry.py` -- its
`retry_transport()` loop was deleted at P4 Batch A0 item 3, R88: zero
request-path call sites, and the live loop in `src/rag/generate.py` is the
implementation),
and the degradation ladder written as policy (`ladder.py`). Routing, real
provider adapters, and provider-side caching are still named by the boundary
above but not built -- CP-alpha has not picked a provider pair yet and
CP-beta has not picked a routing family, so building any of that now would be
code shaped for a decision that has not been made.
"""

from src.gateway.contract import (
    CallClass,
    DecideResult,
    Decider,
    FinishReason,
    GenerationStream,
    Provider,
    Usage,
)
from src.gateway.guard import (
    Degraded,
    RequestTooLarge,
    SpendRateBreaker,
    UnknownCallClass,
    guarded_decide,
    guarded_generate,
)
from src.gateway.ledger import BudgetExceeded, RequestLedger, SpendLayer
from src.gateway.ladder import Readiness, Rung, next_rung, readiness_from_status
from src.gateway.retry import TransportError, record_if_empty_completion

__all__ = [
    "CallClass", "DecideResult", "Decider", "FinishReason", "GenerationStream", "Provider", "Usage",
    "BudgetExceeded", "Degraded", "RequestLedger", "RequestTooLarge", "SpendLayer",
    "SpendRateBreaker", "UnknownCallClass", "guarded_decide", "guarded_generate",
    "Readiness", "Rung", "next_rung", "readiness_from_status",
    "TransportError", "record_if_empty_completion",
]
