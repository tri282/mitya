"""P3 gate layers: invariants, cost, sampled quality (charter §III CP3, Q1/Q2).

Three layers, and their detection power is wildly unequal on purpose:

  invariants   exact, tolerance 0. No MDE exists -- 0 vs 1 route mismatch is not
               a sampling question. BOTH defects this eval set has ever caught
               (R18 cross-process nondeterminism, R19 route dispatch) were this
               class, and neither would have been visible in an aggregate.
  cost         exact arithmetic over the config artifact. 3x effects (CP2), no
               MDE problem. This is A13's objective.
  quality      paired McNemar non-inferiority, epsilon = the run's OWN MDE.

Nothing here is a hand-set threshold. Invariants compare against zero, cost
against production's own computed value, quality against production minus the
MDE this run computed. A constant would go stale as the system improves; none
is used (F6).

Said plainly, because Q2 requires it rather than allows it: at n=38 with the
effects this project actually produces (0.000-0.050 against an MDE of
0.121-0.185, R20/R72), the quality comparator fires on approximately no real
change. Its live role is catastrophic-break detection, around 5+ net row flips.
The gate's routine power lives in the invariant and cost layers. That is not
decoration BECAUSE those layers exist -- and the caption has to say so.
"""

import logging
import math
import random
import re
import statistics

logger = logging.getLogger(__name__)

# Verdicts are three-valued plus ERROR. `disposition` is derived separately and
# deliberately: a metric can be NOT-DETECTED while the run does not block (F5).
BREAK, DETECTED, NOT_DETECTED, ERROR = "BREAK", "DETECTED", "NOT-DETECTED", "ERROR"
NO_BASELINE = "NO-BASELINE"

# 80% power at alpha 0.05, two-sided -- R20's formula, reused rather than
# re-derived so the gate's MDE and the register's MDE are the same number.
_MDE_Z = 2.8


def _binom_tail_at_least(k: int, n: int) -> float:
    """P(X >= k) for X ~ Binomial(n, 0.5). Exact, stdlib only.

    Under McNemar's null the discordant rows split 50/50, so the one-sided
    exact test is a coin-flip tail. scipy for one tail probability would be a
    dependency for four lines of `math.comb`.
    """
    if n == 0:
        return 1.0
    return sum(math.comb(n, i) for i in range(k, n + 1)) / (2 ** n)


def mcnemar_noninferiority(
    prod_hits: dict[str, bool],
    cand_hits: dict[str, bool],
    alpha: float = 0.05,
) -> dict:
    """Paired non-inferiority on one family's per-row hit vectors.

    Joined on row id, never on position: two arms' "rows" lists are in harness
    order, and an index join silently produces fictional discordant counts if
    either arm skips a row. Mismatched id sets are an ERROR, not a delta.

    b = production hit, candidate missed.  c = the reverse.
    """
    if set(prod_hits) != set(cand_hits):
        missing = sorted(set(prod_hits) ^ set(cand_hits))[:5]
        return {"verdict": ERROR, "reason": f"arms cover different row ids, e.g. {missing}"}

    n = len(prod_hits)
    if n == 0:
        return {"verdict": ERROR, "reason": "empty family -- no rows to compare"}

    b = sum(1 for i in prod_hits if prod_hits[i] and not cand_hits[i])
    c = sum(1 for i in prod_hits if cand_hits[i] and not prod_hits[i])
    n_disc = b + c
    delta = (c - b) / n
    p_disc = n_disc / n
    mde = _MDE_Z * math.sqrt(p_disc / n) if p_disc else 0.0

    # Order matters: a real break is a break regardless of the MDE. The exact
    # test asks whether the candidate lost significantly more rows than it won.
    p_value = _binom_tail_at_least(b, n_disc)
    if n_disc and p_value < alpha and b > c:
        verdict = BREAK
    elif mde and abs(delta) >= mde:
        verdict = DETECTED
    else:
        verdict = NOT_DETECTED

    # No discordant rows means no variance estimate, so there is no MDE -- and
    # printing 0.0 would read as infinite sensitivity, which is the opposite of
    # what it means. Report it as undefined and say why in the caption.
    return {
        "n": n, "b": b, "c": c, "p_disc": round(p_disc, 4),
        "delta": round(delta, 4), "mde": round(mde, 4) if n_disc else None,
        "p_value": round(p_value, 4),
        "verdict": verdict,
        # Q2's rule lives here as a field, not as prose. P7's ranking reads it;
        # a NOT-DETECTED delta cannot enter a ranking as a gain.
        "recordable_as_improvement": verdict == DETECTED and delta > 0,
        "provenance": "measured",
    }


# Fixed seed, and it is load-bearing rather than tidy: an unseeded bootstrap
# makes two gate runs on identical inputs disagree, which would trip the very
# determinism invariant this module asserts (R18's tripwire) and make the gate
# non-reproducible from its own report.
_BOOTSTRAP_SEED = 20260829
_BOOTSTRAP_DRAWS = 10000


def paired_bootstrap(prod_scores: dict[str, float], cand_scores: dict[str, float],
                     draws: int = _BOOTSTRAP_DRAWS) -> dict:
    """Paired bootstrap CI for a CONTINUOUS per-row metric (MRR, dchit).

    McNemar does not apply here. It conditions on discordant pairs, which only
    exist for binary outcomes: a row whose rank moves 4 -> 2 is a real change
    that no hit/miss table can see, so every row carries information and the
    concordant/discordant split is meaningless. MRR is a mean of 1/rank with no
    clean closed-form SE, so `notes/measurement-power.md` prescribes the paired
    bootstrap -- resample ROWS, take both arms' scores for each resampled row,
    which preserves pairing and gives a CI on the difference.

    The MDE reported here is 2.8 x the bootstrap SE of the paired difference,
    the same 2.8 (z_0.975 + z_0.80) used everywhere else in this project, so the
    number is commensurable with the McNemar MDE beside it.
    """
    if set(prod_scores) != set(cand_scores):
        return {"verdict": ERROR, "reason": "arms cover different row ids"}
    ids = sorted(prod_scores)
    n = len(ids)
    if n == 0:
        return {"verdict": ERROR, "reason": "empty family -- no rows to compare"}

    diffs = [cand_scores[i] - prod_scores[i] for i in ids]
    delta = sum(diffs) / n

    rng = random.Random(_BOOTSTRAP_SEED)
    means = []
    for _ in range(draws):
        total = 0.0
        for _ in range(n):
            total += diffs[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    se = statistics.pstdev(means)
    mde = 2.8 * se if se else None
    lo, hi = means[int(0.025 * draws)], means[int(0.975 * draws)]

    # se == 0 means every row moved by the SAME amount. That is not "no
    # information" -- it is a perfectly consistent effect, the most detectable
    # thing this test can see. Treating it as NOT-DETECTED (an earlier bug here)
    # would hide exactly the changes that move every row identically, e.g. a
    # rerank that shifts every hit up one place.
    if se == 0:
        verdict = DETECTED if delta != 0 else NOT_DETECTED
    elif lo > 0 or hi < 0:
        verdict = DETECTED
    else:
        verdict = NOT_DETECTED
    return {
        "n": n, "delta": round(delta, 4), "ci95": [round(lo, 4), round(hi, 4)],
        "se": round(se, 5), "mde": round(mde, 4) if mde else None,
        "note": ("paired differences have zero variance -- the effect is constant "
                 "across all rows, so no interval is needed") if se == 0 and delta else None,
        "draws": draws, "seed": _BOOTSTRAP_SEED,
        "verdict": verdict,
        "recordable_as_improvement": verdict == DETECTED and delta > 0,
        "provenance": "computed",
    }


def flips(prod_rows: list[dict], cand_rows: list[dict]) -> list[dict]:
    """Which rows moved, and which way. The report's PRIMARY artifact (F3).

    Retrieval hit/miss is deterministic given config (A8b, verified on three
    occasions), so what changed ON THIS SET is exact -- there is no sampling
    story about it at all. The MDE governs only the generalization claim. A
    developer needs to see the three rows that moved, not "delta -0.025".
    """
    prod = {r["id"]: r for r in prod_rows}
    cand = {r["id"]: r for r in cand_rows}
    out = []
    for row_id in sorted(set(prod) & set(cand)):
        p, c = prod[row_id], cand[row_id]
        if p["hit"] == c["hit"]:
            continue
        out.append({
            "id": row_id,
            "family": p["family"],
            "direction": "prod_hit_cand_miss" if p["hit"] else "prod_miss_cand_hit",
            "prod_rank": p["rank"],
            "cand_rank": c["rank"],
        })
    return out


def invariants(prod: dict, cand: dict | None, baseline: dict | None) -> list[dict]:
    """Exact assertions, tolerance 0. The layer that actually catches things."""
    checks: list[dict] = []

    def check(metric: str, expected, actual, note: str = "") -> None:
        record = {"metric": metric, "expected": expected, "actual": actual,
                  "verdict": BREAK if actual != expected else "PASS", "provenance": "measured"}
        if note:
            record["note"] = note
        checks.append(record)

    for label, arm in (("prod", prod), ("cand", cand)):
        if arm is None:
            continue
        # R19: the harness computed the route, asserted it, then ran the DEFAULT
        # path anyway. A mismatch count is exact -- there is nothing to sample.
        check(f"route_match_{label}", arm["route_checked"], arm["route_matched"])
        # A family whose denominator has silently emptied reports a perfect
        # score over zero rows. R13's failure mode, mechanised.
        for bucket in arm["buckets"]:
            if bucket["rows"] == 0:
                check(f"family_denominator_{label}:{bucket['bucket']}", ">0", 0,
                      "a family with no rows reports a vacuous score")

    if cand is not None:
        # R18: retrieval was bimodal across processes until the EntityMap
        # set-ordering fix. Two arms on the SAME config version must agree
        # bit-for-bit; if they do not, no paired test below is valid.
        if prod["config_version"] == cand["config_version"]:
            p = {r["id"]: r["hit"] for r in prod["rows"]}
            c = {r["id"]: r["hit"] for r in cand["rows"]}
            check("determinism_spread", 0, sum(1 for i in p if i not in c or p[i] != c[i]),
                  "same config version, two processes -- R18's tripwire")

    if baseline is None:
        checks.append({"metric": "determinism_vs_baseline", "expected": "recorded",
                       "actual": None, "verdict": NO_BASELINE, "provenance": "measured",
                       "note": "first run of this config version -- baseline recorded, not compared"})
    else:
        recorded = {r["id"]: r["hit"] for r in baseline["rows"]}
        live = {r["id"]: r["hit"] for r in prod["rows"]}
        drift = sum(1 for i in recorded if i in live and recorded[i] != live[i])
        check("determinism_vs_baseline", 0, drift,
              "production drifted against its own recorded run at an unchanged config version")

    return checks


def computed_cost_usd(config, manifest, prompt_tokens_per_passage: int | None = None) -> dict:
    """Ceiling cost per answer, from the config artifact alone (gap G-E).

    CEILING, not realised cost, and the report prints that on every run rather
    than leaving it to be inferred. Completion is priced at the full
    `max_tokens`, exactly as `generate._estimated_cost_usd` prices the spend
    guard -- over-stating is the correct direction for a budget, and it is what
    makes this layer free and deterministic.

    Two consequences a reader must not have to derive:
      - a config that shortens ANSWERS without moving `max_tokens` shows no
        saving here;
      - a `max_tokens` change is a MEASUREMENT BREAK (18570f5 moved it
        400 -> 1024) and no cost figure compares across it. `max_tokens` is
        carried in the report so a comparison across a break is detectable
        rather than silent.
    """
    from src.pricing import cost_usd

    per_passage = prompt_tokens_per_passage or manifest.chunk_tokens
    passages = config.top_k_rerank if config.rerank_enabled else config.top_k_fused
    prompt_tokens = passages * per_passage + _rough_tokens(config.persona_head)
    cost = cost_usd(config.serving_model, prompt_tokens, config.max_tokens)
    return {
        "usd": cost.usd,
        "model": config.serving_model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": config.max_tokens,
        "passages": passages,
        "basis": "max_tokens ceiling; prompt = passages x chunk_tokens + persona_head",
        "provenance": "computed",
    }


def _rough_tokens(text: str) -> int:
    """~4 chars per token. Deliberately crude: this prices a CEILING, and a
    tokenizer load would make a free layer expensive for a second decimal."""
    return len(text or "") // 4


def cost_layer(prod_cost: dict, cand_cost: dict | None) -> list[dict]:
    """Relative regression against production's own value -- no hand-set floor."""
    if cand_cost is None:
        return [{"metric": "computed_cost_usd", "prod": prod_cost["usd"], "cand": None,
                 "verdict": NOT_DETECTED, "basis": prod_cost["basis"],
                 "provenance": "computed", "recordable_as_improvement": False}]

    if prod_cost["completion_tokens"] != cand_cost["completion_tokens"]:
        return [{"metric": "computed_cost_usd", "verdict": ERROR,
                 "reason": f"max_tokens differs ({prod_cost['completion_tokens']} vs "
                           f"{cand_cost['completion_tokens']}) -- a measurement break, "
                           f"not a cost delta. See 18570f5.",
                 "provenance": "computed", "recordable_as_improvement": False}]

    prod_usd, cand_usd = prod_cost["usd"], cand_cost["usd"]
    if not prod_usd:
        return [{"metric": "computed_cost_usd", "verdict": ERROR,
                 "reason": "production model is UNPRICED -- no cost claim is possible",
                 "provenance": "computed", "recordable_as_improvement": False}]

    delta_pct = round(100.0 * (cand_usd - prod_usd) / prod_usd, 2)
    verdict = BREAK if cand_usd > prod_usd else (DETECTED if cand_usd < prod_usd else NOT_DETECTED)
    return [{"metric": "computed_cost_usd", "prod": prod_usd, "cand": cand_usd,
             "delta_pct": delta_pct, "basis": cand_cost["basis"],
             "provenance": "computed", "verdict": verdict,
             "recordable_as_improvement": verdict == DETECTED}]


# --- schema rules, mechanised so prose cannot undo them ----------------------

# Matched as whole underscore-separated TOKENS, not substrings: "n_cells_scored"
# is a count of judged cells, not a collapsed quality scalar, and a substring
# match on "score" rejects it. A tripwire that fires on innocent keys gets
# deleted by the next person who hits it, which is how the rule dies.
_FORBIDDEN_KEY_TOKENS = frozenset({
    "overall", "score", "scores", "composite", "weighted", "aggregate",
})


def validate_report(report: dict) -> None:
    """Reject a report that breaks one of the four honesty rules.

    These are enforced structurally rather than in a docstring because every
    one of them is undone by a single convenient line somebody adds later.
    """
    if not report.get("caption"):
        raise ValueError("report has no caption -- a verdict without its bound is not reportable")
    if report.get("disposition") is None:
        raise ValueError("report has no disposition")

    for layer, records in report.get("layers", {}).items():
        if not isinstance(records, list):
            raise ValueError(f"layers.{layer} must be a list, never a total (A13 rider)")
        for record in records:
            if record.get("verdict") in (NOT_DETECTED, BREAK, ERROR) \
                    and record.get("recordable_as_improvement"):
                raise ValueError(
                    f"layers.{layer}: {record.get('metric')!r} is {record.get('verdict')} "
                    f"but claims recordable_as_improvement -- Q2 forbids it"
                )

    # A13's rider tripwire. The rider leaves the quality guard's COMPOSITION
    # open, so nothing may quietly pick weights by introducing a single number
    # to sort on. This catches the future convenience sort key.
    _assert_no_scalar_collapse(report, path="report")

    if "quality preserved" in report["caption"].lower():
        raise ValueError("caption claims 'quality preserved'; the guard certifies "
                         "only 'not detectably worse' -- see A13's second obligation")

    # R77, measured: the gate cannot detect a completely ungrounded answer. Any
    # caption implying otherwise is false, so the disclaimer is mandatory rather
    # than advisory -- a report is invalid without it.
    if "GROUNDING NOT CHECKED" not in report["caption"]:
        raise ValueError(
            "caption omits the grounding disclaimer. R77 measured that stripping every "
            "passage moves source_leak and assistant_tell by exactly zero while "
            "groundedness falls 4.022 -> 1.000 offline. A gate that cannot see grounding "
            "must say so on every report."
        )


def _assert_no_scalar_collapse(node, path: str) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            tokens = set(re.split(r"[_\W]+", key.lower()))
            if tokens & _FORBIDDEN_KEY_TOKENS:
                raise ValueError(
                    f"{path}.{key} collapses quality to a scalar. A13's rider leaves the "
                    f"guard's composition open; a component needing one number must state "
                    f"its weighting inline and mark it provisional."
                )
            _assert_no_scalar_collapse(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _assert_no_scalar_collapse(item, f"{path}[{i}]")


def disposition_of(layers: dict) -> tuple[str, str]:
    """Derive (disposition, caption fragment) from the layers. Separate from
    per-metric verdicts by design -- F5's conflation is fixed structurally."""
    # Non-blocking records are reported, never decisive. Making a second
    # quality metric decisive would silently WEIGHT ranking quality against hit
    # quality -- exactly the composition A13's rider leaves open. Reporting it
    # costs nothing and decides nothing.
    records = [r for group in layers.values() for r in group
               if r.get("blocking", True)]
    if any(r.get("verdict") == ERROR for r in records):
        return "error", "a layer could not be evaluated"
    if any(r.get("verdict") == BREAK for r in records):
        broken = [r.get("metric") for r in records if r.get("verdict") == BREAK]
        return "blocks", f"blocked on {', '.join(str(m) for m in broken)}"
    return "does_not_block", "invariants hold"


def _demo() -> None:
    """Self-check on hand-built vectors -- the three outcomes that must differ."""
    ids = [f"r-{i:04d}" for i in range(38)]

    identical = {i: True for i in ids}
    same = mcnemar_noninferiority(identical, dict(identical))
    assert same["delta"] == 0.0 and same["verdict"] == NOT_DETECTED, same
    assert same["recordable_as_improvement"] is False

    # one row lost: inside the MDE, so not detected -- and NOT recordable.
    one_worse = dict(identical); one_worse[ids[0]] = False
    r = mcnemar_noninferiority(identical, one_worse)
    assert r["b"] == 1 and r["c"] == 0 and r["verdict"] == NOT_DETECTED, r
    assert r["mde"] > abs(r["delta"]), "a 1-row deficit must sit inside the MDE at n=38"

    # six rows lost: the exact binomial rejects -- this is what BREAK is for.
    six_worse = dict(identical)
    for i in ids[:6]:
        six_worse[i] = False
    r = mcnemar_noninferiority(identical, six_worse)
    assert r["verdict"] == BREAK, r
    assert r["recordable_as_improvement"] is False

    # a gain large enough to clear the MDE is DETECTED and recordable.
    poor = {i: False for i in ids}
    r = mcnemar_noninferiority(poor, identical)
    assert r["verdict"] == DETECTED and r["recordable_as_improvement"] is True, r

    # mismatched id sets are an ERROR, never a delta.
    assert mcnemar_noninferiority({"a": True}, {"b": True})["verdict"] == ERROR

    # flips join on id, not position.
    prod_rows = [{"id": "r-0002", "family": "primary", "hit": True, "rank": 4},
                 {"id": "r-0001", "family": "primary", "hit": False, "rank": None}]
    cand_rows = [{"id": "r-0001", "family": "primary", "hit": True, "rank": 3},
                 {"id": "r-0002", "family": "primary", "hit": True, "rank": 4}]
    moved = flips(prod_rows, cand_rows)
    assert moved == [{"id": "r-0001", "family": "primary",
                      "direction": "prod_miss_cand_hit", "prod_rank": None, "cand_rank": 3}], moved

    # schema rules
    ok = {"caption": "invariants hold, retrieval not detectably worse (MDE 0.128 at n=38); "
                     "GROUNDING NOT CHECKED (R77: Tier 1 cannot see it)",
          "disposition": "does_not_block", "layers": {"quality_1a": [r]}}
    validate_report(ok)
    for bad, why in (
        ({**ok, "caption": ""}, "missing caption"),
        ({**ok, "caption": "quality preserved; GROUNDING NOT CHECKED"}, "forbidden claim"),
        ({**ok, "caption": "invariants hold"}, "missing the R77 grounding disclaimer"),
        ({**ok, "layers": {"quality_1a": [{"verdict": NOT_DETECTED,
                                           "recordable_as_improvement": True}]}}, "Q2"),
        ({**ok, "overall_score": 0.9}, "scalar collapse"),
        ({**ok, "layers": {"quality_1a": [{"verdict": DETECTED, "composite": 1}]}}, "scalar"),
        ({**ok, "quality_score": 0.8}, "scalar collapse"),
    ):
        try:
            validate_report(bad)
        except ValueError:
            continue
        raise AssertionError(f"validate_report accepted a report it must reject: {why}")

    # ...and the tripwire must NOT fire on innocent keys. A rule that rejects
    # "n_cells_scored" is a rule somebody deletes the first time it blocks them.
    validate_report({**ok, "counts": {"n_cells_scored": 49, "underscored": 1}})

    logger.info("src/rag/gate.py self-check passed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _demo()
