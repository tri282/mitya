#!/usr/bin/env python3
"""P6 Batch B -- the multi-turn run: one arm of the frozen corpus, in process.

Two arms, both free, both on the fake provider (`MITYA_FAKE_PROVIDER=1`):

  honest    `ALLOCATION_POLICY=passages_first` -- the shipped allocation.
  starved   `ALLOCATION_POLICY=passages_only`  -- recent turns zeroed. Batch A
            already shipped this policy (`src/memory.py`); no third policy was
            written for the starved arm, and none should be.

One arm per invocation, one `config_version` per artifact, because R51 forbids
pooling two arms' records under one arm label. `--score` then compares the two
artifacts and applies CP-6b's decision rule mechanically.

**No server.** The app is driven **in process** over `httpx.ASGITransport`, with
its own lifespan, exactly as `test/test_cache_isolation.py` drives real `/chat`
turns. Two reasons, neither of them taste: `CLAUDE.md` records that uvicorn holds
an **exclusive file lock** on `data/vector_db/`, so a server left up blocks every
other script here; and Batch B is specified as the fake arm at $0, so no provider
call and no GPU are wanted. `--base-url` drives an already-running server instead,
for the case where the owner has one up deliberately.

**The cache must be off**, and this script refuses to run otherwise --
`require_cache_off()`, R97. Two P6-specific reasons on top of R97's: a cache hit
emits **no `context_plan`** (it composes no prompt), and a turn served from the
cache never reaches the allocator at all, so a warm run silently measures fewer
turns than it reports. `CACHE_ENABLED` is read at import, so it is an env var on
the command line, not a flag.

Usage (nothing here runs without the owner's approval):
    CACHE_ENABLED=false MITYA_FAKE_PROVIDER=1 python scripts/run_multiturn.py --arm honest --dry
    CACHE_ENABLED=false MITYA_FAKE_PROVIDER=1 python scripts/run_multiturn.py \
        --arm honest  --json out/p6-multiturn-honest.json
    CACHE_ENABLED=false MITYA_FAKE_PROVIDER=1 python scripts/run_multiturn.py \
        --arm starved --json out/p6-multiturn-starved.json
    python scripts/run_multiturn.py --score out/p6-multiturn-honest.json \
        out/p6-multiturn-starved.json --json out/p6-cp6b.json

**What a failure looks like, stated before the run** (CLAUDE.md's batch rule):

  * every turn 200, `answer_tail` the fake fixture's tail ("...endure it."), and
    `context_plan` present on every turn -> the traffic is real;
  * `plan.blocks` reading `passages,recent_turns` from turn 2 onward on the
    honest arm, and `passages` alone on the starved arm -> the arms differ;
  * `plan.has_memory=false` on every honest-arm turn -> **the driver is firing
    turns independently or losing the session id**, the R97 shape, and the whole
    run measures single-shot traffic while exiting 0;
  * `context_plan: null` on many turns -> the cache is on, or the ladder is
    serving retrieval-only, and those turns are not measuring allocation at all;
  * `sources: []` everywhere -> retrieval returned nothing and family (c)'s
    recall is a statement about an empty index, not about allocation.

`--dry` drives **s-0001 only** -- the decisive session, whose turn 2 ("why does
that matter later on?") is unanswerable without turn 1 -- and prints each turn's
plan, source chapters and answer tail. Read those lines before authorizing the
full run; a clean exit code proves none of the five points above.
"""

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx

from src.config_artifact import VERSION_ENV, active_serving_config, reset_active, save
from src.harness.client import DEFAULT_BASE_URL
from src.harness.multiturn import drive, drive_session, run_id
from src.harness.runmeta import (
    load_credentials,
    logged_in,
    merge_observed,
    provenance,
    require_cache_off,
)
from src.harness.sessions import load as load_corpus
from src.memory_metrics import report, separation
from src.observability import configure_logging

logger = logging.getLogger("run_multiturn")

ARMS = {"honest": "passages_first", "starved": "passages_only"}
# The session the schedule names as the decisive case: turn 2 is anaphoric and
# unanswerable without turn 1, so it is what a starved allocation must move.
DECISIVE_SESSION = "s-0001"


def select_arm(arm: str) -> str:
    """Materialize the arm's serving config and select it for THIS process.

    `Config.*` resolves through the versioned artifact and not through the
    environment (`CLAUDE.md`), so an `ALLOCATION_POLICY=` env var alone would
    NOT switch arms -- it is only read by `ServingConfig.from_env()`, which a
    running process never calls. The arm is therefore a real artifact, written
    under its own content hash and selected by version; `CURRENT` is not moved,
    so nothing outside this process changes arm.
    """
    policy = ARMS[arm]
    cfg = active_serving_config()
    if cfg.allocation_policy != policy:
        cfg = cfg.model_copy(update={"allocation_policy": policy})
    version = save(cfg)
    os.environ[VERSION_ENV] = version
    reset_active()  # drop the cached artifact so the app loads the arm's one
    logger.info("arm=%s policy=%s config_version=%s", arm, policy, version)
    return version


class _InProcess:
    """The ASGI app, its lifespan, and a transport onto it.

    Imported inside the function rather than at module scope: importing
    `src.api.app` triggers `Config.validate_rag()` at lifespan and loads Qdrant
    in local mode, and `--score` must not pay that (or take the `data/vector_db/`
    lock) merely to read two JSON files.
    """

    def __init__(self) -> None:
        self._ctx = None
        self.transport: httpx.ASGITransport | None = None

    async def __aenter__(self) -> "_InProcess":
        from src.api.app import app

        self._ctx = app.router.lifespan_context(app)
        await self._ctx.__aenter__()
        self.transport = httpx.ASGITransport(app=app)
        logger.info("app started IN PROCESS -- no uvicorn, so data/vector_db/'s lock is this process's")
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._ctx is not None
        await self._ctx.__aexit__(*exc)
        logger.info("app lifespan closed -- vector_db lock released")


def _dry_report(records: list[dict]) -> None:
    """Print what the turns PRODUCED, not that they completed (CLAUDE.md)."""
    for r in records:
        plan = r.get("context_plan") or {}
        logger.info(
            "DRY turn %d: status=%s blocks=%s has_memory=%s plan_tokens=%s "
            "recent_prov=%r sources=%s chars=%d tail=%r",
            r["turn_index"], r["status_code"], plan.get("plan.blocks", "-"),
            plan.get("plan.has_memory", "-"), plan.get("plan.tokens", "-"),
            plan.get("plan.block.recent_turns.provenance"),
            [f"{s.get('book')} p{s.get('part')} c{s.get('chapter')}" for s in (r.get("sources") or [])],
            r["answer_chars"], r["answer_tail"],
        )
    logger.warning(
        "DRY RUN over %s only. READ THE LINES ABOVE. has_memory=false on turn 2 means the "
        "session id did not carry and the run would measure single-shot traffic while exiting 0 "
        "(R97); context_plan '-' means the server served no plan on that path (cache hit or "
        "retrieval-only), and those turns measure nothing about allocation.",
        DECISIVE_SESSION,
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", choices=sorted(ARMS), help="which allocation arm to drive")
    parser.add_argument("--dry", action="store_true",
                        help=f"drive {DECISIVE_SESSION} only and print what the turns produced")
    parser.add_argument("--score", nargs=2, metavar=("HONEST", "STARVED"),
                        help="score two run artifacts against CP-6b's rule; drives no traffic")
    parser.add_argument("--sessions", type=int, default=0,
                        help="cap on sessions driven (0 = the whole frozen corpus)")
    parser.add_argument("--base-url", default=None,
                        help="drive an already-running server instead of the in-process app")
    parser.add_argument("--tenant", default="tenant-a")
    parser.add_argument("--json", default=None, help="where to write the artifact")
    args = parser.parse_args()
    configure_logging(logging.INFO)

    if args.score:
        honest, starved = (json.load(open(p, encoding="utf-8")) for p in args.score)
        for name, art in (("honest", honest), ("starved", starved)):
            if art.get("arm") != name:
                raise SystemExit(
                    f"{name} artifact is labelled arm={art.get('arm')!r} -- refusing to compare "
                    f"two runs of the same arm and call it a separation")
        if honest["provenance"].get("config_version") == starved["provenance"].get("config_version"):
            raise SystemExit(
                "both artifacts carry the same config_version -- the arms were not actually "
                "switched, so any delta here is noise (R51)")
        out = {
            "honest": {"config_version": honest["provenance"].get("config_version")},
            "starved": {"config_version": starved["provenance"].get("config_version")},
            "separation": separation(honest["metrics"], starved["metrics"]),
            "honest_metrics": honest["metrics"],
            "starved_metrics": starved["metrics"],
        }
        logger.info("CP-6b: %s", json.dumps(out["separation"]["per_family"]))
        logger.warning("CP-6b verdict: %s", out["separation"]["verdict"])
        if args.json:
            os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2)
            logger.info("wrote %s", args.json)
        return 0

    if not args.arm:
        raise SystemExit("--arm is required (or --score two artifacts)")

    corpus = load_corpus()
    sessions = corpus["sessions"]
    if args.dry:
        sessions = [s for s in sessions if s["id"] == DECISIVE_SESSION]
        if not sessions:
            raise SystemExit(f"{DECISIVE_SESSION} is not in corpus {corpus['version']}")
    elif args.sessions:
        sessions = sessions[: args.sessions]

    version = select_arm(args.arm)
    creds = load_credentials()

    async def _run(transport: httpx.AsyncBaseTransport | None, base_url: str) -> dict:
        kwargs = {"transport": transport} if transport is not None else {}
        meta = await provenance(base_url, **kwargs)
        # R97, plus: a cache hit emits no context_plan and never reaches the
        # allocator, so a warm run measures fewer turns than it reports.
        require_cache_off(meta)
        meta["config_version"] = version
        meta["allocation_policy"] = ARMS[args.arm]
        meta["arm_label"] = args.arm
        meta["in_process"] = transport is not None
        run = run_id()
        client = await logged_in(base_url, args.tenant, creds[args.tenant], **kwargs)
        try:
            if args.dry:
                records = await drive_session(client, sessions[0], run=run)
            else:
                records = await drive(client, sessions, run=run)
        finally:
            await client.aclose()
        return {"provenance": merge_observed(meta), "run": run, "records": records}

    if args.base_url:
        result = await _run(None, args.base_url)
    else:
        async with _InProcess() as ip:
            # base_url is a label only: ASGITransport never opens a socket.
            result = await _run(ip.transport, DEFAULT_BASE_URL)

    result["arm"] = args.arm
    result["corpus_version"] = corpus["version"]
    result["dry"] = args.dry
    result["metrics"] = report(result["records"], corpus)

    if args.dry:
        _dry_report(result["records"])
    else:
        for family, row in result["metrics"]["families"]["c"]["result"]["per_family"].items():
            logger.info("(c) %-14s turns_scored=%-3d hits=%-3d recall=%s unmeasured=%s",
                        family, row["turns_scored"], row["hits"], row["recall"], row["unmeasured"])
        logger.warning("tier value is UNMEASURED by construction (CP-6b): %s",
                       json.dumps(result["metrics"]["tier_value"]["tiers"]))

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        logger.info("wrote %s", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
