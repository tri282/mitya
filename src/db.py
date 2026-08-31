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
)

# `ALTER TABLE ... ADD COLUMN` statements, applied after SCHEMA. Empty today.
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
