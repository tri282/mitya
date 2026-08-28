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

Batch A (this commit) ships only the seam: the `Provider` contract
(`contract.py`) and its first, vendor-free implementation, `FakeProvider`
(`fake.py`). Routing, the budget guard, retry/circuit-breaker, real provider
adapters, and provider-side caching are all named by the boundary above, but
none of them is built here -- CP-alpha has not picked a provider pair yet and
CP-beta has not picked a routing family, so building any of that now would be
code shaped for a decision that has not been made.
"""

from src.gateway.contract import GenerationStream, Provider, Usage

__all__ = ["GenerationStream", "Provider", "Usage"]
