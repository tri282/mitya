"""Multi-turn session driver (v2 P6 Batch B, the traffic half of step 9).

The driver the harness did not have. `replay.py` drives eval ROWS (one query,
one record, no session), `ramp.py` drives LOAD (concurrency, cycled queries) --
neither walks a conversation. `04b-schedule/schedule-p6.md` §7 Batch B names the
gap and this module closes it: **one session's turns, in order, under a single
`session_id`, turn N+1 issued only after turn N's response has been consumed.**

**Why the ordering is a hard property and not an implementation detail.** A
driver that fires a session's turns concurrently produces a clean run, a full
record set and a plausible summary -- and measures single-shot traffic, because
turn 2 reaches the server before turn 1 has written anything into
`SessionTurns`. Every request is then memory-free and the whole phase measures
the arm it was trying to compare against. That is the R97 shape (a run that
completes cleanly and measures the wrong thing looks identical to success), and
`test/test_multiturn_driver.py` asserts against it rather than trusting this
docstring.

**The recorded plan is the SERVER's.** `meta.context_plan` (`src/api/app.py`,
the generate path) carries `ContextPlan.attributes()` verbatim. This module
imports nothing from `src.memory` and never calls an allocator: a
client-recomputed plan agrees with the server exactly until the moment it
matters. `context_plan: null` in a record therefore means *the server served no
plan on that path* -- the cache-hit and retrieval-only metas compose no prompt
and emit none -- never "the driver did not look".

Same posture as the rest of `src/harness`: public API only, over
`HarnessClient`. Scoring lives in `src/memory_metrics.py`, not here -- this
module produces behaviour, exactly as `replay.py`'s docstring requires.
"""

import logging
import time
import uuid

from src.harness.client import HarnessClient

logger = logging.getLogger(__name__)


def run_id() -> str:
    """A short id that makes this run's `session_id`s unique.

    Two arms driven in one process would otherwise share `SessionTurns` entries
    under the same corpus session id, and the second arm would open with the
    first arm's transcript already in place -- a starved arm quietly carrying
    the honest arm's memory is the one contamination that would look like a
    result.
    """
    return uuid.uuid4().hex[:8]


async def drive_session(
    client: HarnessClient,
    session: dict,
    *,
    run: str,
    session_id: str | None = None,
) -> list[dict]:
    """Walk ONE session's turns in order. Returns one record per turn.

    `await` per turn, sequentially, with no `gather` anywhere in this function
    -- that IS the mechanism, so it is stated where it lives.
    """
    sid = session_id or f"{run}-{session['id']}"
    records: list[dict] = []
    for index, turn in enumerate(session["turns"], start=1):
        t0 = time.monotonic()
        result = await client.chat(turn["query"], session_id=sid)
        records.append({
            "session": session["id"],
            "family": session.get("family"),
            "session_id": sid,
            "turn_index": index,
            "query": turn["query"],
            "status_code": result.status_code,
            "terminal_event": result.terminal_event,
            "fault_phase": result.fault_phase,
            "route": result.route,
            "cache_hit": result.cache_hit,
            "model": result.model,
            "no_context": result.no_context,
            "answer_chars": result.answer_chars,
            # Kept per CLAUDE.md's batch rule: a run that completes cleanly and
            # answers with an empty string is indistinguishable from success by
            # `answer_chars` alone.
            "answer_tail": result.answer_tail,
            # Server-sourced, both of them. See the module docstring.
            "sources": result.sources,
            "context_plan": result.context_plan,
            "server_total_ms": result.server_total_ms,
            "client_total_ms": (time.monotonic() - t0) * 1000,
            "request_id": result.request_id,
        })
        logger.info(
            "multiturn: %s turn %d/%d status=%s plan=%s memory=%s sources=%d chars=%d tail=%r",
            session["id"], index, len(session["turns"]), result.status_code,
            (result.context_plan or {}).get("plan.blocks", "-"),
            (result.context_plan or {}).get("plan.has_memory", "-"),
            len(result.sources or []), result.answer_chars, result.answer_tail,
        )
    return records


async def drive(client: HarnessClient, sessions: list[dict], *, run: str) -> list[dict]:
    """Every session, one after another. Sessions are independent; TURNS are
    not, and only the inner loop's ordering is load-bearing."""
    records: list[dict] = []
    for session in sessions:
        records.extend(await drive_session(client, session, run=run))
    logger.info("multiturn: drove %d sessions, %d turns", len(sessions), len(records))
    return records
