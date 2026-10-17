"""SQLite persistence for the whole system (v2 P5 step 1; C10 / G8).

**System-wide, not auth-only.** C10 (`plans/mitya-v2/02-decide/decisions-p5.md`)
settles the *storage decision* for every concern that needs durable state:
stdlib `sqlite3`, one database file under `data/`, schema applied with
`CREATE TABLE IF NOT EXISTS` at startup. No ORM, no Alembic, no new service,
no new dependency. P5 is simply the first phase that needed it, so P5 paid for
it -- P6's memory tables and P1's telemetry retention append to `SCHEMA` below
and get the same connection helper, unchanged.

**Auth-only** is exactly the three tables in `SCHEMA` and nothing else in this
module.

Migration story, stated because C10's tell is "whatever is chosen has to
survive P6 adding tables": adding a table is a new `CREATE TABLE IF NOT
EXISTS` entry in `SCHEMA`; adding a column to an existing table is an
`ALTER TABLE ... ADD COLUMN` appended to `MIGRATIONS`, which is executed
after `SCHEMA` and swallows the "duplicate column name" error that a
re-run raises. That is the entire tooling, deliberately -- there is one
writer, one machine, and no rollback requirement that git does not already
cover (D8).

**Connection policy: one short-lived connection per operation**, via
`connect()`. Deliberately not a long-lived process-wide connection: sqlite3
connections are not shareable across threads without `check_same_thread=False`
plus a lock of our own, and `sqlite3.connect()` on an already-open WAL
database costs microseconds -- far below the ~2.1s retrieval stage this sits
next to. When that stops being true, the upgrade is a connection pool here and
nowhere else.

Calls are synchronous inside `async def` handlers. That blocks the event loop
for the duration of the query, which is correct-by-accident today for the same
reason R100 found retrieval to be safe: the request path is already fully
synchronous between the semaphore acquire and the first generation await.
A single indexed primary-key lookup is sub-millisecond; if that changes,
`asyncio.to_thread` around these helpers is the fix.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager

from src.config import Config

logger = logging.getLogger(__name__)

# Spend layers (v3 P0 B1, OI8). `scope` is 'global' or 'tenant:<id>'; `period` is
# 'day:YYYY-MM-DD' or 'month:YYYY-MM', both UTC. No FK to accounts: global rows have no
# tenant. Also executed by `SpendStore` before each operation, so a script that never ran
# `init_schema()` still finds the table.
SPEND_SCHEMA = """
    CREATE TABLE IF NOT EXISTS spend (
        scope  TEXT NOT NULL,
        period TEXT NOT NULL,
        usd    REAL NOT NULL DEFAULT 0,
        tokens INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (scope, period)
    )
    """

# Every table in the system, in one greppable place. P6 appends here.
#
# `tenant_id` is the account's identity AND the value that reaches the tenant
# dimension (spans, logs, and -- once P1 step 7 is revisited -- metric labels).
# It is constrained to `src/observability.py`'s `_TENANT_RE` shape at insert
# time by `src.auth.create_account`, so nothing downstream needs a second
# validation and the metric-label cardinality guard in `src/metrics.py` is
# satisfied by the accounts table being a bounded registry.
SCHEMA: tuple[str, ...] = (
    # --- auth (P5 step 2) ---
    """
    CREATE TABLE IF NOT EXISTS accounts (
        tenant_id     TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        created_at    REAL NOT NULL,
        disabled      INTEGER NOT NULL DEFAULT 0
    )
    """,
    # The TOKEN ITSELF IS NEVER STORED. `token_hash` is sha256(token) hex, so a
    # read of this file (backup, stray copy, `data/` in a tarball) does not
    # hand the reader a usable session -- the same reason the password column
    # holds a KDF output rather than a password. Lookup is by exact hash, so a
    # hash rather than a KDF is correct here: the token is 256 random bits and
    # is not guessable, so there is nothing for a slow KDF to protect.
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        tenant_id  TEXT NOT NULL REFERENCES accounts(tenant_id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS sessions_tenant_idx ON sessions(tenant_id)",
    # Failed logins only, one row each, for the per-account AND per-IP limits
    # (D7). `scope` is 'account:<submitted username>' or 'ip:<addr>'. The
    # account scope keys on the SUBMITTED string, not on a resolved account,
    # so a 429 is reachable for a username that does not exist -- otherwise
    # the rate limiter itself becomes a user-enumeration oracle.
    """
    CREATE TABLE IF NOT EXISTS login_attempts (
        scope TEXT NOT NULL,
        at    REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS login_attempts_scope_at_idx ON login_attempts(scope, at)",

    # --- memory, the two PERSISTED tiers (v2 P6 step 2(b), CP-6a) ---
    #
    # There is deliberately NO TABLE FOR RECENT TURNS. CP-6a O-A confirmed
    # option (i): recent turns live in process memory and die with the session
    # (`src/memory.py`'s `SessionTurns`), because a table of verbatim turns IS
    # persisted verbatim user text whatever its TTL, and charter IV forbids it.
    # `test/test_memory.py`'s `test_nothing_in_this_module_can_persist_a_turn`
    # is the tripwire that keeps it that way; it is unamended by this batch.
    #
    # NO `MIGRATIONS` ENTRY EITHER: these are new tables, not new columns.
    #
    # One table for both classes of profile row, with `kind` as its own column
    # rather than a prefix parsed out of `key` (CP-6a O-C). Two tables would
    # carry an identical column set and double every path that touches them --
    # read, upsert, eviction, retention, purge, and the storage-growth row of
    # step 10 -- for a divergence nobody has asked for.
    #
    # `confidence` is a COARSE ENUM and never a float (N-4): its source is a
    # model's self-report, and R63 already found that class of signal unable to
    # arbitrate. A float would imply a precision the source does not have.
    #
    # `permanent` is an EXEMPTION FLAG, not a large score (N-3): it skips the
    # eviction scan outright. Modelled as high importance it would still lose
    # once several facts are important; as a flag it always holds. It does NOT
    # exempt a row from the retention window -- see `src/memory_store.py`.
    #
    # THERE IS NO STORED SCORE (N-5). The eviction weight is computed inside
    # the eviction query from the stored primitives, so it cannot go stale and
    # needs no refresh job. The weighting itself is marked provisional where it
    # is written (A13's operator rider) -- nothing has chosen those weights.
    #
    # `writer_model` is NOT NULL on both tables (G-P14). The store is durable
    # and the writer can change -- via a `summarizer_model` edit across days, or
    # the deferred promotion ladder -- and two writers in one store with no
    # writer column is R51's arm-contamination shape in a new location. Added
    # now because a column added later is a MIGRATIONS entry plus a backfill of
    # rows whose writer is unknowable.
    #
    # `ON DELETE CASCADE` from `accounts`, the sessions-table idiom above, so
    # account deletion purges memory without a second code path.
    """
    CREATE TABLE IF NOT EXISTS memory_profile (
        tenant_id      TEXT NOT NULL REFERENCES accounts(tenant_id) ON DELETE CASCADE,
        kind           TEXT NOT NULL CHECK (kind IN ('fact', 'preference')),
        key            TEXT NOT NULL,
        value          TEXT NOT NULL,
        importance     TEXT NOT NULL CHECK (importance IN ('high', 'medium', 'low')),
        confidence     TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
        permanent      INTEGER NOT NULL DEFAULT 0,
        count          INTEGER NOT NULL DEFAULT 1,
        first_seen     REAL NOT NULL,
        last_confirmed REAL NOT NULL,
        writer_model   TEXT NOT NULL,
        PRIMARY KEY (tenant_id, kind, key)
    )
    """,
    # The typed episodic digest. NO FREE-TEXT COLUMN anywhere in this schema
    # (CP-6a's correction to `cp-6a-input.md` §3): a session summary produced by
    # a language model is free text, and free text can quote the user verbatim,
    # so an episodic tier with a `summary TEXT` column is option (ii) wearing
    # option (iii)'s name. `topics` is a bounded list of short normalized slugs
    # and `chapters` a bounded list of {book, part, chapter} in the same shape
    # as the corpus's own `targets` (R120); both are stored as a JSON array of
    # those bounded values, which is a serialization of typed fields, not a
    # place to put a paragraph. `src/memory_store.py` enforces the bounds --
    # SQLite cannot.
    """
    CREATE TABLE IF NOT EXISTS memory_episodic (
        tenant_id    TEXT NOT NULL REFERENCES accounts(tenant_id) ON DELETE CASCADE,
        session_id   TEXT NOT NULL,
        ended_at     REAL NOT NULL,
        turn_count   INTEGER NOT NULL,
        topics       TEXT NOT NULL,
        chapters     TEXT NOT NULL,
        writer_model TEXT NOT NULL,
        PRIMARY KEY (tenant_id, session_id)
    )
    """,

    SPEND_SCHEMA,
)

# `ALTER TABLE ... ADD COLUMN` statements, applied after SCHEMA. STILL EMPTY at
# v2 P6 Batch C: the two memory tables above are new tables, not new columns,
# so they belong in SCHEMA and nothing belongs here (CP-6a, explicitly).
# Re-running one raises OperationalError("duplicate column name: x"), which
# `init_schema` swallows -- that is what makes this idempotent without a
# version table.
MIGRATIONS: tuple[str, ...] = ()


@contextmanager
def connect(path: str | None = None):
    """One connection, committed on clean exit and rolled back on exception.

    `path` exists for tests, which point it at a temp file; production callers
    pass nothing and get `Config.DB_PATH`.
    """
    conn = sqlite3.connect(path or Config.DB_PATH)
    conn.row_factory = sqlite3.Row
    # Enforced per-connection in sqlite, not stored in the file -- so it has to
    # be set here rather than once at init, or `ON DELETE CASCADE` above is a
    # decoration.
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(path: str | None = None) -> None:
    """Create the database file and every table in `SCHEMA`. Idempotent.

    Called from `lifespan()` at startup and by `scripts/seed_account.py`, both
    of which may be the first to run.
    """
    db_path = path or Config.DB_PATH
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with connect(db_path) as conn:
        # WAL is a persistent property of the file, so this is set once and
        # survives; it is here rather than in `connect()` because it is a
        # one-time file property, not a per-connection setting. It matters for
        # the same reason the harness matters: a reader (a /chat request
        # resolving a session) must not block behind a writer (a login).
        conn.execute("PRAGMA journal_mode = WAL")
        for statement in SCHEMA:
            conn.execute(statement)
        for statement in MIGRATIONS:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
    logger.info("db: schema ready at %s (%d statements)", db_path, len(SCHEMA))
