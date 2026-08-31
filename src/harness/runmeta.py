"""Shared run metadata for P5's two drivers: credentials in, provenance out.

Both `scripts/probe_auth_surface.py` (step 8) and `scripts/run_fairness.py`
(step 9) need the same two things before they may issue traffic -- the seeded
test credentials, and a record of WHICH SERVER answered. Neither belongs in a
script that only one of them imports, and duplicating the provenance block is
how two artifacts come to disagree about the run they describe.

The credential file is never in the repo: `data/*` is gitignored and the file
is mode 600. Passwords are read from it and are never logged, never written
into an artifact, and never passed on a command line where `ps` would show
them.
"""

import json
import logging
import os

from src.harness.client import HarnessClient

logger = logging.getLogger(__name__)

DEFAULT_CREDENTIALS = "data/p5-test-accounts.env"


def load_credentials(path: str = DEFAULT_CREDENTIALS) -> dict[str, str]:
    """`tenant-a=<password>` lines to a dict. Comments and blanks skipped."""
    creds: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, password = line.partition("=")
            if password:
                creds[name.strip()] = password
    if not creds:
        raise SystemExit(f"no credentials in {path} -- run scripts/seed_account.py first")
    logger.info("credentials: %d accounts loaded from %s", len(creds), path)
    return creds


async def provenance(base_url: str, **kwargs) -> dict:
    """Read `/ready` BEFORE any traffic and return it for the artifact.

    Which arm served a run is not recoverable afterwards, so it is captured
    rather than assumed -- the same argument `scripts/run_harness_ramp.py`
    makes, and the reason R96 insists a fake curve can never be mistaken for a
    metered one later.

    Also records the arm. Whether the session cookie carried `Secure` is NOT
    knowable here and is deliberately left `null` until a login is observed --
    `SESSION_COOKIE_SECURE` is set on the SERVER's environment, and reading it
    from this process would report the driver's own shell. That mistake would
    make an artifact claim the Secure clause was under test in a run where it
    was switched off, which is precisely the mislabelling R106 exists to stop.
    `logged_in()` fills it in from the real `Set-Cookie`.
    """
    # `**kwargs` reaches `HarnessClient` unchanged -- P6 Batch B's driver
    # passes `transport=httpx.ASGITransport(app=app)` so a run needs no
    # uvicorn (CLAUDE.md: uvicorn holds an exclusive lock on data/vector_db/).
    # `logged_in()` below already forwarded kwargs; this one did not, and the
    # two have to agree or the provenance read and the traffic would go to
    # different servers.
    async with HarnessClient(base_url=base_url, **kwargs) as client:
        body = client_ready = (await client.ready()).json()
    logger.info("provenance: %s", json.dumps(body, sort_keys=True)[:400])

    fake = bool(body.get("fake_provider")) if isinstance(body, dict) else None
    if fake:
        logger.warning("FAKE PROVIDER arm -- latency and cost rows describe the SERVER, not a vendor")

    return {
        "ready": client_ready,
        "arm": "fake" if fake else "metered",
        # Filled in by the first logged_in() call; see OBSERVED below.
        "secure_cookie_under_test": None,
        "credentials_file": DEFAULT_CREDENTIALS,
    }


# What only a real response can tell us. Merged into the artifact by the
# drivers after traffic, never guessed from this process's environment.
OBSERVED: dict = {}


def merge_observed(meta: dict) -> dict:
    """Fold post-traffic observations into the provenance block."""
    meta.update(OBSERVED)
    return meta


def require_cache_off(meta: dict) -> None:
    """Refuse to measure the cache and call it a load curve.

    **R97, and it is a permanent constraint on every run, not an incident.**
    `run_at_concurrency` cycles a fixed query list, so the second pass is
    warm and the whole curve degenerates into cache-lookup latency: P4
    measured 2100 ms cold against 1.7-2.6 ms warm, and `rps 246.7` on a
    server whose real throughput was 0.45. A fairness or admission run
    against a warm cache is worse than wrong -- contention never happens at
    3 ms of service time, so every rejection counter reads zero and the run
    looks like a clean pass of a control that was never exercised.

    `CACHE_ENABLED` is read at module level (`src/config.py:345`), so this
    needs a server restart, not a flag.
    """
    readiness = (meta.get("ready") or {}).get("readiness") or {}
    if readiness.get("cache_available"):
        raise SystemExit(
            "REFUSING TO RUN: the server has the answer cache ENABLED, so this would "
            "measure cache-lookup latency and report zero contention (R97). Restart it "
            "with CACHE_ENABLED=false -- it is read at import, so a restart is required."
        )
    logger.info("cache is off -- this run measures retrieval and generation (R97)")


async def logged_in(base_url: str, username: str, password: str, **kwargs) -> HarnessClient:
    """A `HarnessClient` that has authenticated, or a hard stop.

    The 200-but-no-cookie case is the R106 trap and is fatal here rather than
    a warning: every later assertion in both drivers would otherwise run
    unauthenticated and fail for a reason that has nothing to do with what is
    being probed.
    """
    client = HarnessClient(base_url=base_url, **kwargs)
    resp = await client.login(username, password)
    if resp.status_code != 200:
        await client.aclose()
        raise SystemExit(f"login failed for {username}: {resp.status_code} {resp.text[:200]}")
    set_cookie = resp.headers.get("set-cookie", "")
    if "secure_cookie_under_test" not in OBSERVED:
        secure = "secure" in set_cookie.lower()
        OBSERVED["secure_cookie_under_test"] = secure
        if not secure:
            logger.warning(
                "the server issued the session cookie WITHOUT Secure -- this run cannot "
                "report the Secure clause as tested (R106); artifact records it as such")

    if not client._client.cookies.get("mitya_session"):
        await client.aclose()
        raise SystemExit(
            f"login returned 200 for {username} but no session cookie was stored -- "
            "start the server with SESSION_COOKIE_SECURE=0 for local http runs (R106)"
        )
    return client
