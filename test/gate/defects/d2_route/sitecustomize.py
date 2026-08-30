"""D2 -- route-table corruption. Reproduces R19's shape.

R19: the harness computed the route, asserted it, then ran the DEFAULT hybrid
path for every row -- so STRUCTURAL rows were scored on a pipeline production
never uses for them. This forces every route to DEFAULT, which is what a
corrupted route table or a broken dispatch would look like.

The gate's invariant layer should see route_matched < route_checked on the 16
rows that declare an expected_route. That count is EXACT -- there is no MDE and
no sampling story, which is precisely why this class of defect is the one the
gate can actually catch.
"""

import os
import sys

# sitecustomize runs BEFORE the repo root is on sys.path, so `import src` fails
# here unless we put it there first -- and a failing sitecustomize is reported as
# one swallowed warning line, which looks exactly like "the gate did not catch
# the defect". That false negative is the reason this block exists.
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

import src.rag.router as _router

_real_route = _router.route


def _always_default(query, filters, structure, summaries):
    return "DEFAULT", None


_router.route = _always_default
