# Synthesized gate defects (P3 step 8)

Charter step 9 said "replay labelled historical changes -- v1's ablation arms are
the obvious source". R20 withdrew those arms as noise readings: they carry no
true labels, so calibrating against them would be calibrating against random
assignment (F2). The only ground-truth regressions this project has ever
observed are R18 (cross-process nondeterminism) and R19 (route dispatch), both
invariant-class. So the defects are synthesized.

**Nothing here is ever applied to `src/`.** Each defect is a `sitecustomize.py`
that Python auto-imports when its directory is on `PYTHONPATH`, so the patch
lives for exactly one subprocess and the working tree stays clean. A defect
injected into the tree and forgotten is a worse outcome than an uncalibrated
gate.

    PYTHONPATH=test/gate/defects/d2_route python scripts/gate.py --candidate <hash>
