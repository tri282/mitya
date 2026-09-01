"""The two PERSISTED memory tiers: profile rows and typed session digests.

v2 P6 Batch C, steps 2(b), 5 and 6, in the shape CP-6a settled
(`plans/mitya-v2/02-decide/decisions-p6.md`). The tables are in `src/db.py`'s
`SCHEMA`; this module is the only thing that reads or writes them.

**Why this is not in `src/memory.py`.** That module's structural test
(`test/test_memory.py`, `test_nothing_in_this_module_can_persist_a_turn`)
asserts it names no database, no SQL and no file. CP-6a O-A confirmed option
(i) -- recent turns are process memory and die with the session -- and kept the
tripwire **unamended**, so the durable half is a separate module. The value
objects (`ProfileFact`, `Episode`) still live over there, and this module
imports them rather than the reverse, which is what keeps that direction true.

**Retention and eviction are enforced lazily, never by a job.** One `DELETE`
on the write path plus one predicate at read, the `src/auth.py:252-256`
`_prune_attempts` precedent: this deployment has no scheduler, and a tenant who
never returns is bounded by the caps rather than by a background sweep. Cost is
a `DELETE` on a path that already holds the connection.

**Nothing here has been validated by a run and nothing may be reported as
though it had** (N-6). R122 `[measured]`: the frozen corpus carries no
timestamp, so no time-boxed window is crossed at any duration; sessions are 2-4
turns against caps of 10/20, so eviction never fires under replay. Both
mechanisms are exercised by deterministic unit tests with known answers
(`test/test_memory_store.py`), and "a clean run completed" is not evidence
about either -- that inference is the CP5 evidence record's six-instrument
failure.

**Clocks are injected.** Every function that needs the time takes `now` as a
parameter, the idiom `src/auth.py` already uses (`resolve_session`,
`_recent_failures`), because a retention window that can only be tested by
waiting is a window that does not get tested.
"""

import json
import logging
import re
import time
from collections import deque

from src.config import Config
from src.db import connect
from src.memory import KIND_FACT, KIND_PREFERENCE, KINDS, LEVELS, Episode, ProfileFact
from src.metrics import record_memory_write_skip

logger = logging.getLogger(__name__)

# --- step 5: write size and rate caps ----------------------------------------
#
# Deployment policy, NOT versioned config fields. They bound what one turn may
# push into the store; they do not change what reaches the prompt, so they do
# not change what a correct answer is (D9's split) and must not churn
# `config_version`. `Config.MEMORY_VALUE_MAX_CHARS` is the one that IS versioned,
# because it bounds the injected text.

# How many rows one extraction may create. R120(c) measured that 68 of 108
# corpus turns are context-free stubs carrying no user facts at all, so an
# extractor returning more than a couple of facts from one question is
# malfunctioning, not productive.
MAX_FACTS_PER_WRITE = 3
# Per-tenant write rate, sliding minute, process-local. The provider call is
# already bounded by the shared quota window (`src/limits.py`); this bounds the
# STORE against one tenant churning rows, which the quota window does not see.
# ponytail: one deque per process -- under A12(3) there is one process, and a
# second worker would hold a second window. Same caveat purge carries.
MAX_WRITES_PER_MINUTE_PER_TENANT = 12
# Episodic digest bounds. The digest is typed; these are what "bounded list"
# means in the schema comment, and SQLite cannot enforce them.
MAX_TOPICS = 6
MAX_CHAPTERS = 6
MAX_SLUG_CHARS = 32

_write_window: deque[tuple[float, str]] = deque()

# --- step 6: redaction before persistence ------------------------------------
#
# Applied BEFORE the value reaches the database, not after -- "redaction before
# persistence" is the charter §IV wording and the order is the whole point.
# Three shapes, each of which a user can put in a question and none of which
# belongs in a durable store keyed to their account. Not an exhaustive PII
# detector and not claimed to be one: it is the floor under the length bound,
# and the length bound is what does the structural work.
_REDACTIONS = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\+?\d[\d\s().-]{7,}\d"), "[number]"),
    (re.compile(r"\b[A-Za-z0-9_-]{24,}\b"), "[token]"),
)
_PUNCT = re.compile(r"[^\w\s.-]")
_SPACE = re.compile(r"\s+")


def normalize(text: str, max_chars: int | None = None) -> str:
    """Redact, lowercase, collapse whitespace, strip punctuation, truncate.

    This is the STRUCTURAL half of C-1, and its honest bound is CP-6a's: a
    length-bounded, punctuation-stripped field defeats **paragraph-scale**
    recovery by construction, and does **not** defeat **clause-scale** recovery
    -- an 80-character value can hold a verbatim clause. The clause-scale claim
    is a test's job (`test/test_memory_store.py`, the adversarial
    `refusal`-family turn), asserted against this normalized form as well as
    against the raw one, and it is never upgraded to "structural" in any report.
    """
    for pattern, placeholder in _REDACTIONS:
        text = pattern.sub(placeholder, text)
    text = _SPACE.sub(" ", _PUNCT.sub(" ", text.lower())).strip()
    limit = Config.MEMORY_VALUE_MAX_CHARS if max_chars is None else max_chars
    return text[:limit]


def _slug(text: str) -> str:
    return normalize(text, MAX_SLUG_CHARS).replace(" ", "-").strip("-")


def write_allowed(tenant: str, now: float | None = None) -> bool:
    """Whether this tenant is under its per-minute store-write cap (step 5).

    Consulted BEFORE the extractor's provider call, so a tenant over the cap
    costs neither a draw nor a row.
    """
    now = time.time() if now is None else now
    while _write_window and now - _write_window[0][0] >= 60.0:
        _write_window.popleft()
    if sum(1 for _, t in _write_window if t == tenant) >= MAX_WRITES_PER_MINUTE_PER_TENANT:
        # COUNTED, not merely logged (CP-6d(a)). The caller returns 0 on a trip,
        # which is byte-identical to "the extractor found nothing" -- the
        # expected answer on most turns (O-E) -- so without this series a capped
        # run and a quiet corpus are the same artifact. The counter is here
        # rather than at the call site because this is where the trip is
        # detected and `write_allowed` has exactly one caller.
        record_memory_write_skip("rate_capped")
        logger.warning(
            "memory: tenant %s over the store write cap (%d/min) -- extraction skipped",
            tenant, MAX_WRITES_PER_MINUTE_PER_TENANT,
        )
        return False
    _write_window.append((now, tenant))
    return True


# --- retention -----------------------------------------------------------


def _cutoff(now: float) -> float:
    return now - Config.MEMORY_RETENTION_DAYS * 86400.0


def _prune(conn, tenant: str, now: float) -> None:
    """Drop rows outside the retention window, for one tenant, on a path that
    already holds the connection (`_prune_attempts`' shape).

    **`permanent` does NOT exempt a row from retention.** It exempts a row from
    the EVICTION scan (N-3) and nothing else: retention is the charter §IV
    user-data window, and a flag set from a model's self-report must not be able
    to pin a tenant's data in the store forever.
    """
    cutoff = _cutoff(now)
    conn.execute("DELETE FROM memory_profile WHERE tenant_id = ? AND last_confirmed < ?",
                 (tenant, cutoff))
    conn.execute("DELETE FROM memory_episodic WHERE tenant_id = ? AND ended_at < ?",
                 (tenant, cutoff))


# --- eviction ------------------------------------------------------------
#
# THE WEIGHTING BELOW IS PROVISIONAL (A13's operator rider, N-5). Nothing has
# chosen these weights -- not a measurement, not a checkpoint -- and they are a
# natural P7 search axis. They are computed INSIDE the query from the stored
# primitives rather than stored as a column, so the score cannot go stale on
# write and needs no refresh job.
#
# importance + confidence, each 1..3 from `LEVELS`; `count` contributes at most
# 5 so a repeatedly-reaffirmed triviality cannot outrank a high-importance fact
# on volume alone; recency subtracts one point per 30 days since
# `last_confirmed`, capped at 10. Ties break on `last_confirmed` then `key`, so
# the eviction ORDER is deterministic and a unit test can assert *which* row goes.
_WEIGHT_SQL = """
    (CASE importance WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END)
  + (CASE confidence WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END)
  + MIN("count", 5)
  - MIN((? - last_confirmed) / 2592000.0, 10.0)
"""


def _cap_for(kind: str) -> int:
    return (
        Config.MEMORY_PROFILE_MAX_FACTS if kind == KIND_FACT
        else Config.MEMORY_PROFILE_MAX_PREFS
    )


def _evict(conn, tenant: str, kind: str, now: float) -> int:
    """Drop the lowest-weighted non-permanent rows of one `kind` down to its cap.

    Per-kind (`WHERE kind = ?`) because the two caps exist so neither class can
    starve the other -- the property the owner's two numbers were for.
    """
    cap = _cap_for(kind)
    over = conn.execute(
        'SELECT COUNT(*) AS n FROM memory_profile WHERE tenant_id = ? AND kind = ?',
        (tenant, kind),
    ).fetchone()["n"] - cap
    if over <= 0:
        return 0
    doomed = [
        r["key"] for r in conn.execute(
            f'SELECT key FROM memory_profile WHERE tenant_id = ? AND kind = ? '
            f'AND permanent = 0 ORDER BY ({_WEIGHT_SQL}) ASC, last_confirmed ASC, key ASC '
            f'LIMIT ?',
            # Bind order follows the SQL TEXT, not the clause order a reader
            # expects: `?` in WHERE, then the `?` inside _WEIGHT_SQL's ORDER BY,
            # then LIMIT.
            (tenant, kind, now, over),
        )
    ]
    for key in doomed:
        conn.execute(
            "DELETE FROM memory_profile WHERE tenant_id = ? AND kind = ? AND key = ?",
            (tenant, kind, key),
        )
    if doomed:
        logger.info(
            "memory: evicted %d %s row(s) for tenant %s at cap %d -- %s",
            len(doomed), kind, tenant, cap, ", ".join(doomed),
        )
    return len(doomed)


# --- writes --------------------------------------------------------------


def remember(
    tenant: str,
    facts: list[ProfileFact],
    writer_model: str,
    *,
    now: float | None = None,
    db_path: str | None = None,
) -> int:
    """Upsert profile rows, prune, evict. Returns rows written.

    **Merge IS the upsert** (N-2): `ON CONFLICT(tenant_id, kind, key) DO UPDATE`
    increments `count` and refreshes `last_confirmed` in the same statement. No
    similarity judgment, no embedding, no second provider call -- which is also
    what bounds the store by construction and makes eviction rare rather than
    routine.

    `value` and `key` go through `normalize()` on the way in: redaction happens
    BEFORE persistence, per charter §IV and step 6.
    """
    now = time.time() if now is None else now
    rows = [f for f in facts if f.kind in KINDS and f.key][:MAX_FACTS_PER_WRITE]
    if not rows:
        return 0
    with connect(db_path) as conn:
        for f in rows:
            conn.execute(
                """
                INSERT INTO memory_profile
                    (tenant_id, kind, key, value, importance, confidence, permanent,
                     "count", first_seen, last_confirmed, writer_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(tenant_id, kind, key) DO UPDATE SET
                    value          = excluded.value,
                    importance     = excluded.importance,
                    confidence     = excluded.confidence,
                    permanent      = MAX(memory_profile.permanent, excluded.permanent),
                    "count"        = memory_profile."count" + 1,
                    last_confirmed = excluded.last_confirmed,
                    writer_model   = excluded.writer_model
                """,
                (
                    tenant, f.kind, _slug(f.key), normalize(f.value),
                    f.importance if f.importance in LEVELS else "medium",
                    f.confidence if f.confidence in LEVELS else "medium",
                    1 if f.permanent else 0, now, now, writer_model,
                ),
            )
        _prune(conn, tenant, now)
        for kind in {f.kind for f in rows}:
            _evict(conn, tenant, kind, now)
    return len(rows)


def remember_episode(
    tenant: str,
    episode: Episode,
    writer_model: str,
    *,
    now: float | None = None,
    db_path: str | None = None,
) -> None:
    """Upsert one typed session digest, prune, and cap the tenant's digests.

    **No caller wires this to a session end, because there is no session-end
    signal to wire it to -- G-P15.** `ChatRequest` carries `query`, `filters`
    and `session_id` and nothing marks a session finished; the auth session is a
    different object with a 12-hour TTL and no end event either. The candidates
    (an explicit end call from the SPA, piggybacking logout, or lazily
    summarizing session k-1 on the first turn of session k) are a write-path and
    cadence question, so they belong to **CP-6d**, and none is chosen here.
    Inventing a trigger in this batch is exactly the foreclosure the checkpoint
    ordering exists to prevent.

    Consequence, which must travel with every number this tier appears in:
    **until the trigger exists, this table is never written in any run**, so
    step 10's per-tier utilization row reads zero for `episodic` --
    zero-because-untriggered, which is not the same fact as zero-because-unused
    and must never be reported as it.
    """
    now = time.time() if now is None else now
    topics = json.dumps([_slug(t) for t in episode.topics][:MAX_TOPICS])
    chapters = json.dumps([
        {k: c.get(k) for k in ("book", "part", "chapter")}
        for c in episode.chapters[:MAX_CHAPTERS]
    ])
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO memory_episodic
                (tenant_id, session_id, ended_at, turn_count, topics, chapters, writer_model)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, session_id) DO UPDATE SET
                ended_at     = excluded.ended_at,
                turn_count   = excluded.turn_count,
                topics       = excluded.topics,
                chapters     = excluded.chapters,
                writer_model = excluded.writer_model
            """,
            (tenant, episode.session_id, episode.ended_at, episode.turn_count,
             topics, chapters, writer_model),
        )
        _prune(conn, tenant, now)
        conn.execute(
            """
            DELETE FROM memory_episodic
             WHERE tenant_id = ? AND session_id NOT IN (
                   SELECT session_id FROM memory_episodic WHERE tenant_id = ?
                    ORDER BY ended_at DESC LIMIT ?)
            """,
            (tenant, tenant, Config.MEMORY_EPISODIC_MAX_PER_TENANT),
        )


# --- reads ---------------------------------------------------------------


def profile(
    tenant: str, *, now: float | None = None, db_path: str | None = None,
) -> list[ProfileFact]:
    """Profile rows for one tenant, best-weighted first, retention applied.

    The window is enforced HERE as well as on the write path: a tenant who has
    not written since the window elapsed must not be served stale rows just
    because nothing has pruned them yet. Lazy enforcement means the row may
    still be on disk; it may not reach a prompt.
    """
    now = time.time() if now is None else now
    with connect(db_path) as conn:
        rows = conn.execute(
            f'SELECT * FROM memory_profile WHERE tenant_id = ? AND last_confirmed >= ? '
            f'ORDER BY ({_WEIGHT_SQL}) DESC, key ASC',
            (tenant, _cutoff(now), now),
        ).fetchall()
    return [
        ProfileFact(
            kind=r["kind"], key=r["key"], value=r["value"], importance=r["importance"],
            confidence=r["confidence"], permanent=bool(r["permanent"]), count=r["count"],
            first_seen=r["first_seen"], last_confirmed=r["last_confirmed"],
            writer_model=r["writer_model"],
        )
        for r in rows
    ]


def episodes(
    tenant: str, *, now: float | None = None, db_path: str | None = None,
) -> list[Episode]:
    """Session digests for one tenant, newest first, retention applied."""
    now = time.time() if now is None else now
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM memory_episodic WHERE tenant_id = ? AND ended_at >= ? "
            "ORDER BY ended_at DESC",
            (tenant, _cutoff(now)),
        ).fetchall()
    return [
        Episode(
            session_id=r["session_id"], ended_at=r["ended_at"], turn_count=r["turn_count"],
            topics=tuple(json.loads(r["topics"])), chapters=tuple(json.loads(r["chapters"])),
            writer_model=r["writer_model"],
        )
        for r in rows
    ]


def purge_tenant(tenant: str, turns=None, *, db_path: str | None = None) -> int:
    """Delete every memory row for one tenant, durable AND in-process.

    ONE entry point for both halves, because a purge that empties the tables and
    leaves the live transcript in RAM has not purged. `turns` is the process's
    `SessionTurns` (`src/api/app.py`'s `state.turns`); omitted, only the durable
    half runs -- which is the honest shape for an out-of-process caller, and it
    is exactly the A12(3) caveat below rather than a convenience.

    Returns rows deleted.

    **The return value is a statement about the CALL, not about the store**
    (R108's shape), so the test that matters reads the store back rather than
    trusting this number. Purge is a `DELETE` and never a metered rewrite
    because **no persisted row mixes two tenants' content** -- that property,
    not "per-tenant rows", is what decides it (CP-6a's restatement of (c)).

    Complete only under A12(3), single process: `SessionTurns.purge_tenant`
    (`src/memory.py`) empties this process's RAM, and a second worker would hold
    a second copy no `DELETE` reaches. Account deletion needs no call here at
    all -- both tables carry `ON DELETE CASCADE` from `accounts`.
    """
    with connect(db_path) as conn:
        n = conn.execute("DELETE FROM memory_profile WHERE tenant_id = ?", (tenant,)).rowcount
        n += conn.execute("DELETE FROM memory_episodic WHERE tenant_id = ?", (tenant,)).rowcount
    in_process = turns.purge_tenant(tenant) if turns is not None else 0
    logger.info(
        "memory: purged %d persisted row(s) and %d in-process turn(s) for tenant %s",
        n, in_process, tenant,
    )
    return n + in_process


def storage_bytes(tenant: str, *, db_path: str | None = None) -> tuple[int, int]:
    """`(rows, approximate bytes)` this tenant holds -- step 10's storage-growth
    row, which needs no provider and no metric backend."""
    with connect(db_path) as conn:
        p = conn.execute(
            'SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(key) + LENGTH(value)), 0) AS b '
            'FROM memory_profile WHERE tenant_id = ?', (tenant,),
        ).fetchone()
        e = conn.execute(
            'SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(topics) + LENGTH(chapters)), 0) AS b '
            'FROM memory_episodic WHERE tenant_id = ?', (tenant,),
        ).fetchone()
    return p["n"] + e["n"], p["b"] + e["b"]


__all__ = [
    "KIND_FACT", "KIND_PREFERENCE", "MAX_FACTS_PER_WRITE",
    "MAX_WRITES_PER_MINUTE_PER_TENANT", "episodes", "normalize", "profile",
    "purge_tenant", "remember", "remember_episode", "storage_bytes", "write_allowed",
]
