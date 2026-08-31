#!/usr/bin/env python3
"""P5 step 8 -- attack the auth surface of a RUNNING server.

Eleven probes, sequential, zero spend on the fake arm. Each records the
evidence it saw, not merely pass/fail: R115 is this phase's example of an
assertion that failed while naming the wrong cause, and a probe that logs only
a verdict cannot be re-read later.

**Why this exists at all, rather than more in-process tests.** R108: the login
rate limiter passed review and 27 in-process tests while being completely
inert -- `connect()` rolled back the attempt rows on the way out of a raise.
Only execution against a live server found it. Probes here therefore assert on
BEHAVIOUR over HTTP, never on code shape.

**Ordering is load-bearing.** Probes 9-11 deliberately saturate the login
limiters, which locks out `tenant-a` and this host for
`Config.LOGIN_WINDOW_SECONDS`. They run last, and the server should be started
with `LOGIN_WINDOW_SECONDS=60` so step 9 can log in afterwards.

Usage:
    python scripts/probe_auth_surface.py --dry          # provenance + probes 1-2 only
    python scripts/probe_auth_surface.py --json out/p5-auth-probes.json
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
from src.harness.runmeta import load_credentials, logged_in, merge_observed, provenance
from src.observability import configure_logging

logger = logging.getLogger("probe_auth_surface")

RESULTS: list[dict] = []


def record(n: int, name: str, passed: bool | None, evidence: dict, note: str = "") -> None:
    """`passed=None` means OBSERVED, not judged -- the vacuous probes."""
    verdict = {True: "PASS", False: "FAIL", None: "RECORDED"}[passed]
    RESULTS.append({"n": n, "probe": name, "verdict": verdict, "evidence": evidence, "note": note})
    logger.info("probe %-2d %-34s %-8s %s", n, name, verdict, json.dumps(evidence, sort_keys=True)[:220])


async def _tenant_request_counts(base_url: str) -> dict[str, float]:
    """Per-tenant request counts from /metrics -- the only external way to see
    which tenant the server ATTRIBUTED a request to (P5's `tenant` label,
    R107)."""
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as c:
        text = (await c.get("/metrics")).text
    counts: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("mitya_request_duration_seconds_count") and 'tenant="' in line:
            tenant = line.split('tenant="', 1)[1].split('"', 1)[0]
            counts[tenant] = counts.get(tenant, 0.0) + float(line.rsplit(" ", 1)[1])
    return counts


# --- probes 1-8: the surface that does not touch the login limiters ---------

async def probe_unauthenticated(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as c:
        chat = await c.post("/chat", json={"query": "who is Raskolnikov"})
        books = await c.get("/books")
    record(1, "unauthenticated /chat", chat.status_code == 401,
           {"status": chat.status_code, "body_head": chat.text[:120],
            "sse_frames": chat.text.count("event:")})
    record(2, "unauthenticated /books", books.status_code == 401,
           {"status": books.status_code, "body_head": books.text[:120]})


async def probe_forged_header(base_url: str, creds: dict) -> None:
    """R102: the tenant must come from the session, and an X-Tenant-ID header
    must not be able to override it. P1 read that header unauthenticated; P5
    removed it. This proves the removal from outside."""
    before = await _tenant_request_counts(base_url)
    client = await logged_in(base_url, "tenant-a", creds["tenant-a"])
    try:
        resp = await client._client.post(
            "/chat", json={"query": "who is Raskolnikov"},
            headers={"X-Tenant-ID": "tenant-b"},
        )
        status = resp.status_code
        await resp.aread()
    finally:
        await client.aclose()
    after = await _tenant_request_counts(base_url)
    delta = {t: after.get(t, 0) - before.get(t, 0) for t in set(after) | set(before)}
    moved = {t: d for t, d in delta.items() if d > 0}
    record(3, "forged X-Tenant-ID header", moved.get("tenant-b", 0) == 0 and status == 200,
           {"status": status, "attribution_delta": moved},
           "header names tenant-b; attribution must stay on tenant-a")


async def probe_forged_session(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as c:
        forged = await c.post("/chat", json={"query": "x"},
                              cookies={"mitya_session": "not-a-real-token-" + "a" * 32})
        garbage = await c.post("/chat", json={"query": "x"},
                               headers={"Cookie": "=====;;;mitya_session"})
    record(4, "forged / unparseable session", forged.status_code == 401 and garbage.status_code == 401,
           {"forged_status": forged.status_code, "garbage_cookie_status": garbage.status_code},
           "an unparseable Cookie header must be 401, never 500")


async def probe_cache_cross_tenant(base_url: str, creds: dict) -> None:
    """A12(2): the answer cache key has NO tenant while the corpus is shared,
    so this probe is VACUOUS BY CONSTRUCTION and is recorded, never scored. It
    is written down so CP6 -- where per-tenant memory enters the prompt and the
    key must change -- has the P5 reading to compare against."""
    query = "what does Raskolnikov say about the axe"
    a = await logged_in(base_url, "tenant-a", creds["tenant-a"])
    b = await logged_in(base_url, "tenant-b", creds["tenant-b"])
    try:
        first = await a.chat(query)
        second = await b.chat(query)
    finally:
        await a.aclose()
        await b.aclose()
    record(5, "cross-tenant cache observation", None,
           {"a_cache_hit": first.cache_hit, "b_cache_hit": second.cache_hit,
            "b_served_from_a_entry": bool(second.cache_hit),
            "a_tail": first.answer_tail, "b_tail": second.answer_tail},
           "VACUOUS by A12(2): no tenant in the cache key at P5. Real test lands at CP6.")


async def probe_session_invalidation(base_url: str, creds: dict) -> None:
    """R114 case 1: a session that is no longer valid before /chat answers.

    DEVIATION, stated rather than hidden: the run plan said 'expire the row
    server-side', which the harness may not do -- nothing in this package
    reaches past HTTP (`src/harness/__init__.py`). Logout is the external
    equivalent and destroys the session server-side, so this probes
    INVALIDATION rather than clock expiry. Time-based expiry is covered
    in-process by `test_an_expired_session_does_not_resolve`."""
    client = await logged_in(base_url, "tenant-c", creds["tenant-c"])
    try:
        await client._client.post("/auth/logout")
        after = await client._client.post("/chat", json={"query": "x"})
        status = after.status_code
        await after.aread()
    finally:
        await client.aclose()
    record(6, "invalidated session before /chat", status == 401,
           {"status": status}, "logout, not clock expiry -- see docstring")


async def probe_expiry_mid_stream(base_url: str, creds: dict) -> None:
    """R114 case 2: invalidation AFTER the first token cannot be a 401 -- the
    200 is already committed (design §IV.2). The correct behaviour is that the
    stream in flight completes; the NEXT request is refused."""
    client = await logged_in(base_url, "tenant-d", creds["tenant-d"])
    killer = httpx.AsyncClient(base_url=base_url, timeout=30.0,
                               cookies=dict(client._client.cookies))
    frames, first_token_seen, killed = 0, False, False
    try:
        async with client._client.stream("POST", "/chat", json={"query": "who is Sonya"}) as resp:
            status = resp.status_code
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    frames += 1
                    if not first_token_seen:
                        first_token_seen = True
                        await killer.post("/auth/logout")
                        killed = True
        nxt = await client._client.post("/chat", json={"query": "x"})
        next_status = nxt.status_code
        await nxt.aread()
    finally:
        await killer.aclose()
        await client.aclose()
    record(7, "invalidation mid-stream", status == 200 and killed and next_status == 401,
           {"stream_status": status, "frames_after_kill": frames,
            "next_request_status": next_status},
           "the committed 200 must finish; only the NEXT request is refused (R114 case 2)")


async def probe_redaction(base_url: str) -> None:
    """P1's assertion, re-run with real accounts present. Any provider key in
    the environment must appear in neither /metrics nor any response body."""
    secrets = [v for k, v in os.environ.items()
               if ("KEY" in k.upper() or "TOKEN" in k.upper()) and len(v) > 12]
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as c:
        blob = (await c.get("/metrics")).text + (await c.get("/ready")).text
    leaked = [k for k, v in os.environ.items()
              if ("KEY" in k.upper() or "TOKEN" in k.upper()) and len(v) > 12 and v in blob]
    record(8, "secrets redaction", not leaked,
           {"env_secrets_checked": len(secrets), "leaked_names": leaked,
            "scraped_chars": len(blob)})


# --- probes 9-11: these SATURATE the login limiters. Always last. -----------

async def probe_enumeration(base_url: str, creds: dict) -> None:
    """No user enumeration: unknown account and wrong password must be
    indistinguishable by status, by body, and by timing -- the last one is why
    `login()` verifies against a decoy hash when the account does not exist.

    **Budget, learned the hard way (R116).** This probe MUST stay under both
    login caps, or it measures the rate limiter instead of the decoy. The
    first version fired 10+10 from one host: the known account crossed
    LOGIN_MAX_PER_ACCOUNT=5, the host crossed LOGIN_MAX_PER_IP=20, and the
    resulting 429s -- which skip the argon2id verify and so return in half
    the time -- made an inert comparison look like a timing oracle. Four
    attempts per side: 4 against one known account (< 5) and 4 against
    distinct unknown names (1 each), 8 from this host in total (< 20)."""
    unknown, wrong = [], []
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as c:
        for i in range(4):
            t = time.monotonic()
            r = await c.post("/auth/login",
                             json={"username": f"ghost-{os.getpid()}-{i}", "password": "x" * 20})
            unknown.append(((time.monotonic() - t) * 1000, r.status_code, r.text))
            t = time.monotonic()
            r = await c.post("/auth/login", json={"username": "tenant-e", "password": "x" * 20})
            wrong.append(((time.monotonic() - t) * 1000, r.status_code, r.text))
    u_ms = statistics.median(x[0] for x in unknown)
    w_ms = statistics.median(x[0] for x in wrong)
    # Compare statuses to statuses and bodies to bodies. The first version of
    # this line compared the status set to the BODY set, which can never be
    # equal, so the probe could only ever report FAIL -- on a run whose actual
    # measurement (one status, one body, ratio 1.006) was a clean pass. R116.
    same_shape = ({x[1] for x in unknown} == {x[1] for x in wrong} and
                  {x[2] for x in unknown} == {x[2] for x in wrong})
    ratio = max(u_ms, w_ms) / max(min(u_ms, w_ms), 0.001)
    saturated = 429 in ({x[1] for x in unknown} | {x[1] for x in wrong})
    record(9, "user enumeration", None if saturated else (same_shape and ratio < 1.5),
           {"unknown_median_ms": round(u_ms, 1), "wrong_password_median_ms": round(w_ms, 1),
            "timing_ratio": round(ratio, 3),
            "statuses": sorted({x[1] for x in unknown} | {x[1] for x in wrong}),
            "bodies": sorted({x[2][:80] for x in unknown} | {x[2][:80] for x in wrong})},
           "budget blown -- a 429 appeared, so this measured the limiter, not the decoy (R116)"
           if saturated else
           "identical status and body; timing within 1.5x (one argon2id verify on both branches)")


async def probe_account_rate_limit(base_url: str, creds: dict) -> None:
    """R108's regression probe, and the reason this file exists. The limiter
    must engage against a LIVE server -- and the correct password must be
    refused too, or the limiter is bypassable by guessing right."""
    statuses = []
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as c:
        for _ in range(6):
            r = await c.post("/auth/login", json={"username": "tenant-a", "password": "wrong" * 4})
            statuses.append(r.status_code)
        good = await c.post("/auth/login", json={"username": "tenant-a", "password": creds["tenant-a"]})
    record(10, "per-account login rate limit", 429 in statuses and good.status_code == 429,
           {"wrong_password_statuses": statuses, "correct_password_status": good.status_code,
            "retry_after": good.headers.get("retry-after")},
           "R108 regression: the correct password must ALSO be refused once the cap is reached")


async def probe_ip_rate_limit(base_url: str) -> None:
    """The second wall: an attacker spreading guesses across many usernames
    from one host must still hit a limit, with every account counter at zero."""
    statuses = []
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as c:
        for i in range(22):
            r = await c.post("/auth/login", json={"username": f"spray-{i}", "password": "x" * 20})
            statuses.append(r.status_code)
    record(11, "per-IP login rate limit", 429 in statuses,
           {"first_429_at_attempt": statuses.index(429) + 1 if 429 in statuses else None,
            "statuses_tail": statuses[-6:]},
           "distinct usernames, so only the IP counter can be what refuses")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--dry", action="store_true", help="provenance + probes 1-2 only")
    parser.add_argument("--json", help="write the artifact here")
    parser.add_argument("--only", type=int, help="re-run a single probe by number")
    args = parser.parse_args()

    configure_logging(logging.INFO)
    meta = await provenance(args.base_url)
    creds = load_credentials()

    if args.only:
        fn = {9: lambda: probe_enumeration(args.base_url, creds),
              10: lambda: probe_account_rate_limit(args.base_url, creds),
              11: lambda: probe_ip_rate_limit(args.base_url)}[args.only]
        await fn()
    elif True:
        await probe_unauthenticated(args.base_url)
    if args.only:
        pass
    elif args.dry:
        logger.info("DRY RUN: stopping after probe 2. READ THE EVIDENCE ABOVE before the real run.")
    else:
        await probe_forged_header(args.base_url, creds)
        await probe_forged_session(args.base_url)
        await probe_cache_cross_tenant(args.base_url, creds)
        await probe_session_invalidation(args.base_url, creds)
        await probe_expiry_mid_stream(args.base_url, creds)
        await probe_redaction(args.base_url)
        # Limiter-saturating probes last -- they lock tenant-a and this host out
        # for LOGIN_WINDOW_SECONDS.
        await probe_enumeration(args.base_url, creds)
        await probe_account_rate_limit(args.base_url, creds)
        await probe_ip_rate_limit(args.base_url)

    failed = [r for r in RESULTS if r["verdict"] == "FAIL"]
    recorded = [r for r in RESULTS if r["verdict"] == "RECORDED"]
    logger.info("probes: %d pass, %d fail, %d recorded-not-scored",
                len(RESULTS) - len(failed) - len(recorded), len(failed), len(recorded))
    for r in failed:
        logger.error("FAILED probe %d %s: %s", r["n"], r["probe"], json.dumps(r["evidence"]))

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"provenance": merge_observed(meta), "dry": args.dry, "probes": RESULTS}, f, indent=2)
        logger.info("wrote %s", args.json)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
