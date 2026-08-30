"""D1 -- cross-process nondeterminism. Reproduces R18's shape.

R18: `EntityMap._scan()` iterated a set, so entity resolution order varied with
PYTHONHASHSEED and recall was bimodal 0.720/0.760 across processes. Nothing
errored; the number simply differed per run, which is the worst kind of defect
because every individual run looks fine.

This reintroduces per-process variation at the same seam: resolved entity order
is shuffled with a per-process seed. Two arms on the SAME config version then
disagree, and the gate's determinism invariant should fire with a nonzero
spread. If it does not, no paired test downstream is valid.
"""

import os
import sys

# sitecustomize runs BEFORE the repo root is on sys.path, so `import src` fails
# here unless we put it there first -- and a failing sitecustomize is reported as
# one swallowed warning line, which looks exactly like "the gate did not catch
# the defect". That false negative is the reason this block exists.
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

import random

import src.rag.entities as _entities

_real_resolve = _entities.EntityMap.resolve
_rng = random.Random(os.getpid())


def _shuffled_resolve(self, query, active_book=None):
    expanded, names = _real_resolve(self, query, active_book=active_book)
    names = list(names)
    _rng.shuffle(names)
    # Rebuild the expanded query in the shuffled order -- this is what R18 did
    # by accident, and it changes the dense embedding, hence the ranking.
    if names:
        expanded = query + " " + " ".join(names)
    return expanded, names


_entities.EntityMap.resolve = _shuffled_resolve
