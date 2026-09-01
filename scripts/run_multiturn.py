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

`--dry` drives **s-0001 and s-0002** -- the decisive session, whose turn 2 ("why
does that matter later on?") is unanswerable without turn 1, and its successor,
**because CP-6d's episodic trigger is only observable across a session
boundary**. That is a deliberate deviation from the standing "2-3 rows" gate and
it is named: a single-session dry run cannot see the thing CP-6d(a) decided.
Read the lines before authorizing the full run; a clean exit code proves none of
the five points above.

--------------------------------------------------------------------------
**BATCH E-run (CP-6d): the metered shape.** Two arms, both at 40 sessions / 108
turns, cache off, one tenant, per family, never pooled:

  `--arm honest`    `passages_first`, memory writes ON. Every-turn profile
                    extraction plus the lazy episodic digest (39 of 40 session
                    boundaries fire).
  `--arm baseline`  `passages_only`, **memory writes SUPPRESSED at this driver**
                    and recorded as `memory_writes_enabled: false` in the
                    artifact's provenance. This is the Verify table's named
                    no-memory arm -- v1's shape, not "arm 1 minus a policy" --
                    and the suppression is what makes "does memory pay for
                    itself" answerable: a baseline paying memory's write cost
                    answers nothing. Nothing in `src/` gates the write on the
                    policy, so the suppression is a DRIVER stub, never a
                    request-path edit (CP-6d(b)).

**Pace: `--pace-per-min 12`, and this driver refuses a metered run without it.**
The binder is not a provider limit. `MAX_WRITES_PER_MINUTE_PER_TENANT` is 12
(`src/memory_store.py`) against a 15 rpm serving window, and it is consulted
BEFORE the extractor's draw: on a trip the write returns 0, which is the same
return as "the extractor correctly found nothing". A full-pace run would
silently skip ~20% of extractions and the artifact could not tell you.

Usage (metered; nothing here runs without the owner's approval):
    CACHE_ENABLED=false .venv/bin/python scripts/run_multiturn.py \
        --arm honest --dry --pace-per-min 12
    CACHE_ENABLED=false .venv/bin/python scripts/run_multiturn.py \
        --arm honest --pace-per-min 12 --json out/p6-e-honest.json
    CACHE_ENABLED=false .venv/bin/python scripts/run_multiturn.py \
        --arm baseline --pace-per-min 12 --json out/p6-e-baseline.json

**The baseline's known-answer check, computed into its own artifact** (CP-6d(b)
-- an instrument that has not been checked is not evidence): zero summarizer
draws, `summarizer_cost_usd == 0`, and zero stored rows added over the run. If
any is nonzero the suppression did not take and the cost row is VOID. One
honest deviation from CP-6d's wording, recorded rather than papered over: the
check cannot count `memory.write` SPANS, because suppression is a driver stub
and the request path still opens that span (at ~0 ms, with no draw behind it).
The store delta is the stronger evidence and it is what is asserted.
"""

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx

from src import memory_store, summarizer
from src.config import Config
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
from src.pricing import cost_usd
from src.observability import configure_logging

logger = logging.getLogger("run_multiturn")

# arm -> (allocation policy, memory writes enabled). The write flag is NOT a
# config field on purpose (CP-6d(b)): a `memory_writes_enabled` field would
# churn `config_version` and mint a third arm identity for a property no
# promotion will ever carry, and `MEMORY_BUDGET_TOKENS=0` would not work either
# -- it removes memory from the PROMPT, not from the WRITE path, so the cost row
# would still be contaminated. It is a driver stub, recorded in the artifact.
ARMS = {
    "honest": ("passages_first", True),
    "starved": ("passages_only", True),
    # Batch E-run's no-memory baseline. Same policy as `starved` and a different
    # arm: `starved` was Batch B's fake-arm control, which still wrote memory.
    "baseline": ("passages_only", False),
}
# The session the schedule names as the decisive case: turn 2 is anaphoric and
# unanswerable without turn 1, so it is what a starved allocation must move.
DECISIVE_SESSION = "s-0001"
# CP-6d's dry run is TWO sessions, not two turns: the lazy episodic trigger
# fires on the FIRST TURN OF THE SECOND session and is invisible in one.
DRY_SESSIONS = (DECISIVE_SESSION, "s-0002")
# CP-6d(a)'s pace ceiling, in turns/min/tenant. Equal to
# MAX_WRITES_PER_MINUTE_PER_TENANT, which is BELOW the 15 rpm serving window --
# the store cap is the binder, not the provider.
MAX_PACE_PER_MIN = 12


def select_arm(arm: str) -> str:
    """Materialize the arm's serving config and select it for THIS process.

    `Config.*` resolves through the versioned artifact and not through the
    environment (`CLAUDE.md`), so an `ALLOCATION_POLICY=` env var alone would
    NOT switch arms -- it is only read by `ServingConfig.from_env()`, which a
    running process never calls. The arm is therefore a real artifact, written
    under its own content hash and selected by version; `CURRENT` is not moved,
    so nothing outside this process changes arm.
    """
    policy = ARMS[arm][0]
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


class _Draws:
    """Every summarizer draw this process made, with its RAW RESPONSE TEXT.

    Installed as `src.summarizer.draw_observer` for the duration of a run. The
    raw text is the point and it is why a counter would not do: CP-6d separates
    "the model found nothing" (`[]`, the expected answer on most turns under
    O-E) from "the model returned prose" from "the write was skipped", and a row
    count collapses all three. That collapse is exactly what made Batch D's
    fake-provider result -- `rows=0` on 4/4 attempts -- scan as a working
    defense when the log said the schema had never been exercised.

    Costed here rather than read back off the metrics: the driver needs the
    per-arm `summarizer_cost_usd` in its own artifact, and the baseline arm's
    known-answer check is an assertion that this number is zero.
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def __call__(self, draw: dict) -> None:
        usage = draw.get("usage")
        served = getattr(usage, "model", None) or draw["model"]
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        cost = cost_usd(served, prompt, completion)
        self.rows.append({
            "kind": draw["kind"],
            "model": served,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            # None means UNPRICED -- a missing rate row, not a free draw. Kept
            # as None so the sum below cannot quietly absorb it as 0.0.
            "cost_usd": cost.usd,
            "cost_metered": cost.metered,
            "wall_ms": round(draw["wall_ms"], 1),
            "text": draw["text"],
        })

    def summary(self) -> dict:
        return {
            "draws": len(self.rows),
            "extractor_draws": sum(1 for r in self.rows if r["kind"] == "extract"),
            "episodic_draws": sum(1 for r in self.rows if r["kind"] == "episodic"),
            "summarizer_cost_usd": sum(r["cost_usd"] or 0.0 for r in self.rows),
            "unpriced_draws": sum(1 for r in self.rows if r["cost_usd"] is None),
            "wall_ms_total": round(sum(r["wall_ms"] for r in self.rows), 1),
        }


def suppress_memory_writes() -> list[str]:
    """Replace the app's two memory writers with counting no-ops (CP-6d(b)).

    **Driver-level, in this process, never a request-path edit.** Nothing in
    `src/` gates the write on the allocation policy -- `write_profile_from_turn`
    is called unconditionally after the meta event -- and adding a gate would
    mint a config field for a property no promotion will ever carry.

    Returns the list the stubs append to, so the artifact can record that the
    patch was actually REACHED. A suppression that was never called and a
    suppression that worked look identical in a row count, which is the whole
    class of defect this batch keeps closing.

    What it cannot suppress, stated: the `memory.write` STAGE SPAN still opens
    around the stubbed call, at ~0 ms with no draw behind it. The store delta in
    the artifact is the stronger evidence and it is what the check asserts.
    """
    from src.api import app as app_module

    calls: list[str] = []

    async def _no_profile(tenant, question, **kwargs) -> int:
        calls.append("extract")
        return 0

    async def _no_episode(tenant, session_id, transcript, turn_count, **kwargs) -> bool:
        calls.append("episodic")
        return False

    app_module.write_profile_from_turn = _no_profile
    app_module.write_episode = _no_episode
    logger.warning(
        "MEMORY WRITES SUPPRESSED at the driver -- this is the no-memory baseline arm "
        "(CP-6d(b)); the artifact records memory_writes_enabled=false and checks it"
    )
    return calls


def _known_answer_check(writes_enabled: bool, draws: _Draws, calls: list[str],
                        rows_before: int, rows_after: int) -> dict:
    """CP-6d(b)'s check on the baseline arm, computed rather than asserted by eye.

    An instrument that has not been checked is not evidence (`schedule-p6.md`
    §5). On the memory arm this reports the mirror-image expectation, so the
    block is never absent and never reads as a pass by omission.
    """
    summary = draws.summary()
    if not writes_enabled:
        failures = []
        if summary["draws"]:
            failures.append(f"{summary['draws']} summarizer draw(s) were made")
        if summary["summarizer_cost_usd"]:
            failures.append(f"summarizer_cost_usd={summary['summarizer_cost_usd']}")
        if rows_after != rows_before:
            failures.append(f"the store grew {rows_before} -> {rows_after} rows")
        if not calls:
            failures.append("the suppression stub was never called -- it may not have been installed")
        return {
            "arm_expects": "zero summarizer draws, summarizer_cost_usd == 0, no stored rows added",
            "suppressed_calls": len(calls),
            "summarizer_draws": summary["draws"],
            "summarizer_cost_usd": summary["summarizer_cost_usd"],
            "rows_before": rows_before,
            "rows_after": rows_after,
            "verdict": "pass" if not failures else "VOID",
            "failures": failures,
            "note": (
                "memory.write stage spans are NOT counted here: suppression is a driver stub "
                "and the request path still opens that span at ~0 ms with no draw behind it. "
                "The store delta is the stronger evidence (CP-6d(b), recorded deviation)."
            ),
        }
    return {
        "arm_expects": "summarizer draws on every turn plus one episodic draw per session boundary",
        "summarizer_draws": summary["draws"],
        "extractor_draws": summary["extractor_draws"],
        "episodic_draws": summary["episodic_draws"],
        "summarizer_cost_usd": summary["summarizer_cost_usd"],
        "rows_before": rows_before,
        "rows_after": rows_after,
        "verdict": "pass" if summary["draws"] else "VOID",
        "failures": [] if summary["draws"] else [
            "the memory arm made no summarizer draw at all -- the write path never ran"
        ],
    }


def _print_profile(tenant: str) -> int:
    """Read the rows BACK OUT of `memory_profile` and print them (CP-6d item 1).

    Read back, not echoed from the extractor's parse: what matters is what the
    store holds after normalization, redaction, the echo check and the caps --
    every one of which can drop or rewrite a row the extractor returned.
    """
    rows = memory_store.profile(tenant)
    for row in rows:
        logger.info(
            "  memory_profile <- kind=%s key=%s value=%r importance=%s confidence=%s "
            "count=%d writer_model=%s",
            row.kind, row.key, row.value, row.importance, row.confidence,
            row.count, row.writer_model,
        )
    if not rows:
        logger.info("  memory_profile <- (no rows for %s)", tenant)
    return len(rows)


def _print_episodes(tenant: str) -> int:
    rows = memory_store.episodes(tenant)
    for row in rows:
        logger.info(
            "  memory_episodic <- session=%s turns=%d topics=%s chapters=%s writer_model=%s",
            row.session_id, row.turn_count, list(row.topics), list(row.chapters),
            row.writer_model,
        )
    if not rows:
        logger.info("  memory_episodic <- (no rows for %s)", tenant)
    return len(rows)


def _print_turn(record: dict, draws: list[dict], tenant: str) -> None:
    """Everything CP-6d's dry run must print, for ONE turn."""
    plan = record.get("context_plan") or {}
    # (3) the standing four, plus config_version -- Config.* resolves through
    # the promoted artifact and not through the environment (R64).
    logger.info(
        "TURN %s/%d status=%s config_version=%s model=%s prompt_tokens=%s "
        "completion_tokens=%s cost_usd=%s metered=%s server_total_ms=%s",
        record["session"], record["turn_index"], record["status_code"],
        record.get("config_version"), record.get("model"), record.get("prompt_tokens"),
        record.get("completion_tokens"), record.get("cost_usd"), record.get("cost_metered"),
        record.get("server_total_ms"),
    )
    logger.info("  question: %r", record["query"])
    logger.info("  answer tail (%d chars): %r", record["answer_chars"], record["answer_tail"])
    # (2) the full context plan, per block, with utilization against the budget.
    if not plan:
        logger.warning("  context_plan: NONE -- the server composed no prompt on this path "
                       "(cache hit or retrieval-only). This turn measures nothing about allocation.")
    else:
        budget = Config.MEMORY_BUDGET_TOKENS
        blocks = [b for b in str(plan.get("plan.blocks", "")).split(",") if b and b != "-"]
        logger.info("  plan: policy=%s has_memory=%s blocks=%s total_tokens=%s "
                    "memory_budget=%d",
                    plan.get("plan.policy"), plan.get("plan.has_memory"),
                    ",".join(blocks) or "-", plan.get("plan.tokens"), budget)
        for tier in blocks:
            tokens = int(plan.get(f"plan.block.{tier}.tokens", 0) or 0)
            util = "-" if tier == "passages" else f"{100.0 * tokens / budget:.1f}% of memory budget"
            logger.info("    block %-12s tokens=%-5d %s  provenance=%r",
                        tier, tokens, util, plan.get(f"plan.block.{tier}.provenance"))
        for tier in ("recent_turns", "profile", "episodic"):
            if tier not in blocks:
                logger.info("    block %-12s ABSENT (0%% of memory budget)", tier)
    # (1) + (4): the raw summarizer response, its wall time, and the rows read back.
    for draw in draws:
        logger.info(
            "  summarizer draw kind=%s model=%s wall_ms=%s prompt_tokens=%s "
            "completion_tokens=%s cost_usd=%s",
            draw["kind"], draw["model"], draw["wall_ms"], draw["prompt_tokens"],
            draw["completion_tokens"], draw["cost_usd"],
        )
        logger.info("    RAW RESPONSE %s: %r", draw["kind"], draw["text"])
    if not draws:
        logger.info("  summarizer draw: NONE on this turn")
    _print_profile(tenant)
    if any(d["kind"] == "episodic" for d in draws):
        _print_episodes(tenant)


_DRY_FAILURE_TABLE = """
READ THE LINES ABOVE. CP-6d's eight failure shapes and what surfaces each:
  1 empty summary          RAW RESPONSE reading `[]` is the model finding nothing (expected on
                           most turns, O-E); PROSE is a different failure. A row count cannot
                           tell them apart -- that was Batch D's confound.
  2 Batch D recurring      `profile extraction failed ... Expecting value: line 1 column 1` in
                           the log means the provider returned prose: the typed schema is STILL
                           unexercised and the injection set's extractor arm reports withheld.
  3 verbatim quote stored  a memory_profile value sharing a clause with the question above it.
                           Read by eye: `_echoes()` catches >=5-word spans, clause-scale
                           recovery is defended by test, not structurally (CP-6a).
  4 empty memory block     no recent_turns block on turn 2+, or a tier at 0% of the budget.
                           This is G-P18's own acceptance test: if it still fires at
                           ctx_size=6144 the raise was mis-sized and the sweep does not start.
  5 episodic untriggered   zero memory_episodic rows after the s-0001 -> s-0002 boundary and no
                           episodic draw. That is a DIFFERENT zero from Batch C's
                           zero-because-untriggered and must not be reported as it.
  6 silent extraction skip `over the store write cap (12/min) -- extraction skipped` in the log.
                           ZERO such lines is the pass condition; pace at <=12 turns/min/tenant.
  7 one upstream bucket    retry-shaped latency on interleaved serving/summarizer draws, 429s
                           absorbed into wall time. If it fires: treat both model ids as ONE
                           500-rpd window, re-budget to 77.6%, drop the re-run assumption.
  8 suppression not taken  baseline arm only, and it is checked in that arm's own artifact
                           (`known_answer_check`), not here.
A clean exit code proves none of these.
"""

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
        "DRY RUN over %s. READ THE LINES ABOVE. has_memory=false on turn 2 means the "
        "session id did not carry and the run would measure single-shot traffic while exiting 0 "
        "(R97); context_plan '-' means the server served no plan on that path (cache hit or "
        "retrieval-only), and those turns measure nothing about allocation.",
        " + ".join(DRY_SESSIONS),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", choices=sorted(ARMS), help="which allocation arm to drive")
    parser.add_argument("--dry", action="store_true",
                        help=f"drive {' + '.join(DRY_SESSIONS)} and print what every draw produced")
    parser.add_argument("--pace-per-min", type=int, default=0,
                        help=f"turns/min/tenant; required (1..{MAX_PACE_PER_MIN}) on a METERED arm, "
                             f"because MAX_WRITES_PER_MINUTE_PER_TENANT=12 silently skips writes above it")
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
        # Two sessions, in corpus order, because the episodic trigger fires on
        # the FIRST TURN OF THE SECOND one (CP-6d).
        sessions = [s for s in sessions if s["id"] in DRY_SESSIONS]
        missing = set(DRY_SESSIONS) - {s["id"] for s in sessions}
        if missing:
            raise SystemExit(f"{sorted(missing)} not in corpus {corpus['version']}")
    elif args.sessions:
        sessions = sessions[: args.sessions]

    version = select_arm(args.arm)
    policy, writes_enabled = ARMS[args.arm]
    creds = load_credentials()
    # Installed for the whole run, uninstalled in the `finally` below: this is a
    # process-global on `src.summarizer`, and leaving it set would make a later
    # script in the same process keep appending to a dead artifact's list.
    draws = _Draws()
    suppressed_calls: list[str] = []

    async def _run(transport: httpx.AsyncBaseTransport | None, base_url: str) -> dict:
        kwargs = {"transport": transport} if transport is not None else {}
        meta = await provenance(base_url, **kwargs)
        # R97, plus: a cache hit emits no context_plan and never reaches the
        # allocator, so a warm run measures fewer turns than it reports.
        require_cache_off(meta)
        metered = meta.get("arm") == "metered"
        # The pace binder is the STORE cap, not the provider (CP-6d(a)). Refused
        # rather than warned: an unpaced metered run silently drops ~20% of its
        # extractions and the artifact reads as a quiet corpus.
        if metered and not 0 < args.pace_per_min <= MAX_PACE_PER_MIN:
            raise SystemExit(
                f"REFUSING TO RUN: this is a METERED arm and --pace-per-min is "
                f"{args.pace_per_min}. MAX_WRITES_PER_MINUTE_PER_TENANT is "
                f"{MAX_PACE_PER_MIN} and is consulted BEFORE the extractor's draw, so a "
                f"faster run skips writes silently and returns 0 -- indistinguishable "
                f"from 'the extractor found nothing' (CP-6d(a))."
            )
        interval = 60.0 / args.pace_per_min if args.pace_per_min else 0.0
        meta["config_version"] = version
        meta["allocation_policy"] = policy
        meta["arm_label"] = args.arm
        meta["in_process"] = transport is not None
        # Provenance CP-6d requires the artifact to carry, so a reader can name
        # the run without the shell history: both model ids (two quota windows,
        # G-P11), the arm, the write-suppression flag, the pace, and the corpus.
        meta["memory_writes_enabled"] = writes_enabled
        meta["serving_model"] = Config.SERVING_MODEL
        meta["summarizer_model"] = Config.SUMMARIZER_MODEL
        meta["ctx_size"] = Config.CTX_SIZE
        meta["memory_budget_tokens"] = Config.MEMORY_BUDGET_TOKENS
        meta["pace_per_min"] = args.pace_per_min
        meta["corpus_version"] = corpus["version"]
        meta["tenant"] = args.tenant
        run = run_id()
        rows_before, bytes_before = memory_store.storage_bytes(args.tenant)
        client = await logged_in(base_url, args.tenant, creds[args.tenant], **kwargs)
        try:
            if args.dry:
                records = await _drive_dry(client, sessions, run, interval)
            else:
                records = await drive(client, sessions, run=run, min_interval_s=interval)
        finally:
            await client.aclose()
        rows_after, bytes_after = memory_store.storage_bytes(args.tenant)
        return {
            "provenance": merge_observed(meta),
            "run": run,
            "records": records,
            "summarizer": draws.summary(),
            # The Verify table's storage-growth row, per arm, measured rather
            # than estimated -- and the baseline's half of the known-answer check.
            "storage": {
                "rows_before": rows_before, "rows_after": rows_after,
                "bytes_before": bytes_before, "bytes_after": bytes_after,
            },
            "known_answer_check": _known_answer_check(
                writes_enabled, draws, suppressed_calls, rows_before, rows_after,
            ),
            "cost": {
                "serving_usd": sum(r.get("cost_usd") or 0.0 for r in records),
                "summarizer_usd": draws.summary()["summarizer_cost_usd"],
                "note": (
                    "serving and summarizer are SEPARATE model ids and separate quota "
                    "windows (G-P11); they are never summed into one row without the split."
                ),
            },
        }

    async def _drive_dry(client, sessions_, run: str, interval: float) -> list[dict]:
        """Drive the dry run ONE TURN AT A TIME so each turn's summarizer draws
        can be attributed to it.

        The turn ordering is `drive_session`'s, unchanged -- a single-turn
        session under a fixed `session_id` is the same request the full run
        makes. Nothing here recomputes a plan or a source list; both are read
        off the server's own events, per that module's docstring.
        """
        records: list[dict] = []
        for session in sessions_:
            sid = f"{run}-{session['id']}"
            for index, turn in enumerate(session["turns"], start=1):
                seen = len(draws.rows)
                record = (await drive_session(
                    client, {**session, "turns": [turn]}, run=run, session_id=sid,
                    min_interval_s=interval if records else 0.0,
                ))[0]
                record["turn_index"] = index
                records.append(record)
                _print_turn(record, draws.rows[seen:], args.tenant)
        return records

    summarizer.draw_observer = draws
    try:
        if args.base_url:
            if not writes_enabled:
                raise SystemExit(
                    f"--arm {args.arm} suppresses memory writes IN THIS PROCESS, and "
                    f"--base-url drives a server in another one. The baseline arm must be "
                    f"driven in process, or its cost row is contaminated by writes nobody "
                    f"can see from here (CP-6d(b))."
                )
            result = await _run(None, args.base_url)
        else:
            async with _InProcess() as ip:
                if not writes_enabled:
                    suppressed_calls = suppress_memory_writes()
                # base_url is a label only: ASGITransport never opens a socket.
                result = await _run(ip.transport, DEFAULT_BASE_URL)
    finally:
        summarizer.draw_observer = None

    result["arm"] = args.arm
    result["policy"] = policy
    result["memory_writes_enabled"] = writes_enabled
    result["corpus_version"] = corpus["version"]
    result["dry"] = args.dry
    result["metrics"] = report(result["records"], corpus)

    if args.dry:
        _dry_report(result["records"])
        logger.warning("%s", _DRY_FAILURE_TABLE)
        logger.warning(
            "summarizer: %s; storage rows %d -> %d; known-answer check: %s %s",
            json.dumps(result["summarizer"]), result["storage"]["rows_before"],
            result["storage"]["rows_after"], result["known_answer_check"]["verdict"],
            result["known_answer_check"].get("failures") or "",
        )
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
