#!/usr/bin/env python3
"""P5 step 9 -- fairness, starvation and admission under multi-tenant load.

Three run shapes over one driver, because they share a body of traffic and a
set of logged-in clients:

  9a  baseline   1 tenant, a concurrency sweep. Subtracted from P4's
                 `out/ramp-fake-d7.json` at the same levels on the same arm,
                 the difference is the per-request AUTH overhead and nothing
                 else.
  9b  fairness   5 tenants, uneven load (one heavy, four light). Measures the
                 share of the pool the heaviest tenant captures and how badly
                 the lightest is starved -- A12(1)'s shape, which R104
                 corrected this phase from "per-tenant cap overspend".
  9c  admission  5 tenants all heavy, offering far more than
                 CHAT_MAX_INFLIGHT_TOTAL. Measures the rejection rate and
                 proves the queue is BOUNDED: P4 measured 0 rejections and a
                 62 s p99 because the system queued without limit (R98).

**The USD column will read $0.00 and that is a true statement, not a missing
number.** R109: every hosted arm estimates zero because `MODEL_RATES` has one
row, and R48 recorded that the free tier has no USD ceiling. It is labelled
`free_tier_no_usd_ceiling` in the artifact so step 10 cannot transcribe it as
"no overspend" -- the fairness numbers carry this step's result, not the USD.

Usage:
    python scripts/run_fairness.py --dry                       # 2 requests, prints what they produced
    python scripts/run_fairness.py --shape 9a --json out/p5-baseline.json
    python scripts/run_fairness.py --shape 9b --json out/p5-fairness.json
    python scripts/run_fairness.py --shape 9c --json out/p5-admission.json
"""

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx

from src.harness.client import DEFAULT_BASE_URL, HarnessClient
from src.harness.runmeta import load_credentials, logged_in, merge_observed, provenance, require_cache_off
from src.harness.sessions import load as load_corpus
from src.observability import configure_logging

logger = logging.getLogger("run_fairness")


def _queries(limit: int) -> list[str]:
    """Turns from the frozen corpus (R91/R92), so this run's traffic is the
    same traffic every other P4/P5 number describes."""
    corpus = load_corpus()
    turns = [t["query"] for s in corpus["sessions"] for t in s["turns"]]
    return (turns * ((limit // max(len(turns), 1)) + 1))[:limit]


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(int(len(s) * p / 100), len(s) - 1)], 1)


async def _tenant_traffic(client: HarnessClient, tenant: str, queries: list[str],
                          concurrency: int, duration_s: float = 0.0,
                          retry_after_s: float = 1.0) -> list[dict]:
    """One tenant's offered load, `concurrency` requests in flight at a time.

    **`duration_s` > 0 is the mode that produces a meaningful fairness number,
    and the fixed-count mode below is kept only for 9a's latency curve.** R119:
    a driver that fires N attempts and never retries a 429 measures arrival
    luck, not fairness. Rejection is instant, so a tenant that arrives during a
    saturated window burns its whole offered load in milliseconds and finishes
    with zero served, while whoever holds a slot re-fires on completion and
    keeps it. That produced a 1.0/0.0 split between two tenants offering
    identical load -- a statement about this driver, not about the server.

    In duration mode each tenant keeps `concurrency` requests in flight for
    `duration_s` seconds, and a rejected request waits (honouring `Retry-After`
    when the server sends one) and comes back, which is what a real client
    does. Share of served over a fixed window is then a fairness measurement.
    """
    records: list[dict] = []
    deadline = time.monotonic() + duration_s

    def _record(r, waited: float) -> None:
        records.append({
            "tenant": tenant, "status": r.status_code, "total_ms": round(r.total_ms, 1),
            "ttft_ms": round(r.ttft_ms, 1) if r.ttft_ms else None,
            "cost_usd": r.cost_usd, "cache_hit": r.cache_hit, "route": r.route,
            "answer_chars": r.answer_chars, "answer_tail": r.answer_tail,
            "error_detail": r.error_detail, "request_id": r.request_id,
            "retry_wait_s": round(waited, 2),
        })

    if duration_s <= 0:
        sem = asyncio.Semaphore(concurrency)

        async def one(q: str) -> None:
            async with sem:
                _record(await client.chat(q), 0.0)

        await asyncio.gather(*(one(q) for q in queries))
        return records

    async def worker(slot: int) -> None:
        i = slot
        waited = 0.0
        while time.monotonic() < deadline:
            r = await client.chat(queries[i % len(queries)])
            i += concurrency
            _record(r, waited)
            if r.status_code == 429:
                # Back off like a real client. The server sends Retry-After on
                # the rejections that know when capacity frees; the rest get a
                # flat wait, which is still infinitely more realistic than the
                # zero-wait hammer that produced R119.
                waited += retry_after_s
                await asyncio.sleep(retry_after_s)
            else:
                waited = 0.0

    await asyncio.gather(*(worker(k) for k in range(concurrency)))
    return records


def _summarize(records: list[dict]) -> dict:
    """Per-tenant view plus the two numbers A12(1) actually asks for."""
    by_tenant: dict[str, list[dict]] = {}
    for r in records:
        by_tenant.setdefault(r["tenant"], []).append(r)

    served = {t: [r for r in rs if r["status"] == 200] for t, rs in by_tenant.items()}
    total_served = sum(len(v) for v in served.values()) or 1

    per_tenant = {}
    for tenant, rs in sorted(by_tenant.items()):
        ok = served[tenant]
        lat = [r["total_ms"] for r in ok]
        per_tenant[tenant] = {
            "offered": len(rs),
            "served": len(ok),
            "share_of_served": round(len(ok) / total_served, 4),
            "rejected_429": sum(1 for r in rs if r["status"] == 429),
            "rejected_other": sum(1 for r in rs if r["status"] not in (200, 429)),
            "p50_ms": _pct(lat, 50), "p95_ms": _pct(lat, 95), "p99_ms": _pct(lat, 99),
            "usd": round(sum(r["cost_usd"] or 0.0 for r in ok), 6),
        }

    shares = [v["share_of_served"] for v in per_tenant.values()]
    return {
        "per_tenant": per_tenant,
        "heaviest_share_of_pool": round(max(shares), 4) if shares else None,
        "lightest_share_of_pool": round(min(shares), 4) if shares else None,
        "starvation_ratio": round(max(shares) / min(shares), 2) if shares and min(shares) > 0 else None,
        "total_rejections": sum(v["rejected_429"] for v in per_tenant.values()),
        "usd_total": round(sum(v["usd"] for v in per_tenant.values()), 6),
        "usd_note": "free_tier_no_usd_ceiling -- $0.00 here is a true statement, NOT 'no overspend' (R109)",
    }


async def _admission_counters(base_url: str) -> dict[str, float]:
    """Server-side rejection counters, which the client cannot infer: a 429 is
    visible from outside, but WHY it was refused (per-tenant slot, total slot,
    or pool share) only /metrics distinguishes."""
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as c:
        text = (await c.get("/metrics")).text
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith(("mitya_admission_rejections_total", "mitya_pool_share_rejections_total")):
            out[line.rsplit(" ", 1)[0]] = float(line.rsplit(" ", 1)[1])
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--shape", choices=["9a", "9b", "9c"], default="9a")
    parser.add_argument("--levels", default="1,2,4,8", help="9a only")
    parser.add_argument("--per-level", type=int, default=20, help="9a only")
    parser.add_argument("--per-tenant", type=int, default=12, help="9b/9c requests per tenant (count mode)")
    parser.add_argument("--duration", type=float, default=90.0,
                        help="9b/9c: seconds of sustained offered load per tenant. "
                             "0 falls back to the fixed-count mode R119 showed is unfair to measure with.")
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--json")
    args = parser.parse_args()

    configure_logging(logging.INFO)
    meta = await provenance(args.base_url)
    creds = load_credentials()
    # Not gated on --dry: a dry pass against a warm cache reports 3 ms rows and
    # tells the operator the run is fine when it is not. That is the exact
    # shape of failure R97 caught, so the dry pass has to be the thing that
    # catches it.
    require_cache_off(meta)
    before = await _admission_counters(args.base_url)

    result: dict = {"provenance": meta, "shape": args.shape, "dry": args.dry}

    if args.dry:
        client = await logged_in(args.base_url, "tenant-a", creds["tenant-a"])
        try:
            records = await _tenant_traffic(client, "tenant-a", _queries(2), 1)
        finally:
            await client.aclose()
        for r in records:
            logger.info("DRY row: status=%s total_ms=%s route=%s chars=%d tail=%r cost=%s",
                        r["status"], r["total_ms"], r["route"], r["answer_chars"],
                        r["answer_tail"], r["cost_usd"])
        logger.info("DRY RUN: 2 requests. READ THE TAILS ABOVE -- a clean exit code with an "
                    "empty or refused answer looks identical to success.")
        result["records"] = records

    elif args.shape == "9a":
        levels = [int(x) for x in args.levels.split(",")]
        client = await logged_in(args.base_url, "tenant-a", creds["tenant-a"],
                                 limits=httpx.Limits(max_connections=max(levels) + 4,
                                                     max_keepalive_connections=max(levels) + 4))
        curve = {}
        try:
            for level in levels:
                recs = await _tenant_traffic(client, "tenant-a", _queries(args.per_level), level)
                lat = [r["total_ms"] for r in recs if r["status"] == 200]
                curve[level] = {"served": len(lat), "p50_ms": _pct(lat, 50),
                                "p95_ms": _pct(lat, 95), "p99_ms": _pct(lat, 99),
                                "rejections": sum(1 for r in recs if r["status"] == 429)}
                logger.info("9a level=%d %s", level, json.dumps(curve[level]))
        finally:
            await client.aclose()
        result["curve"] = curve
        result["compare_against"] = "out/ramp-fake-d7.json (same arm, pre-auth) -- the delta is auth"

    else:
        # 9b: one heavy tenant against four light ones. 9c: all five heavy.
        plan = ({"tenant-a": 4, "tenant-b": 1, "tenant-c": 1, "tenant-d": 1, "tenant-e": 1}
                if args.shape == "9b" else
                {t: 4 for t in ("tenant-a", "tenant-b", "tenant-c", "tenant-d", "tenant-e")})
        clients = {}
        for tenant, conc in plan.items():
            clients[tenant] = await logged_in(
                args.base_url, tenant, creds[tenant],
                limits=httpx.Limits(max_connections=conc + 2, max_keepalive_connections=conc + 2))
        try:
            batches = await asyncio.gather(*(
                _tenant_traffic(clients[t], t, _queries(args.per_tenant), c,
                                duration_s=args.duration)
                for t, c in plan.items()))
        finally:
            for c in clients.values():
                await c.aclose()
        records = [r for b in batches for r in b]
        result["offered_concurrency"] = plan
        result["duration_s"] = args.duration
        result["summary"] = _summarize(records)
        result["records"] = records
        logger.info("%s summary: %s", args.shape,
                    json.dumps({k: v for k, v in result["summary"].items() if k != "per_tenant"}))
        for tenant, v in result["summary"]["per_tenant"].items():
            logger.info("  %-9s offered=%2d served=%2d share=%.3f 429=%d p95=%s",
                        tenant, v["offered"], v["served"], v["share_of_served"],
                        v["rejected_429"], v["p95_ms"])

    after = await _admission_counters(args.base_url)
    result["rejection_counters"] = {
        "before": before, "after": after,
        "delta": {k: round(after.get(k, 0) - before.get(k, 0), 2)
                  for k in set(after) | set(before)
                  if after.get(k, 0) - before.get(k, 0) != 0},
    }
    logger.info("rejection counters delta: %s", json.dumps(result["rejection_counters"]["delta"]))

    result["provenance"] = merge_observed(meta)
    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        logger.info("wrote %s", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
