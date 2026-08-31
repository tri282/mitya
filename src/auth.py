"""Accounts, password hashing, opaque server-side sessions, login rate limits.

v2 P5 step 2. Every clause of the charter §IV auth row (D7) is implemented
here and has a named test in `test/test_auth.py`; the clause -> test table is
in `plans/mitya-v2/05-impl/phase-5.md`. Nothing in D7 is relaxed.

**argon2id with no new dependency.** `cryptography` (49.0.0, already installed
for the existing stack) ships `cryptography.hazmat.primitives.kdf.argon2.Argon2id`,
which is OpenSSL's Argon2 implementation -- so the D7 clause is satisfied
without adding `argon2-cffi`. The one string attached: OpenSSL 3.2+ is
required at runtime, and an older OpenSSL raises `UnsupportedAlgorithm` on the
first hash rather than at import. `argon2_available()` exists so startup can
say so loudly instead of failing on the first login attempt.

**Opaque tokens, not JWT.** D7's reasoning, restated because this module is
where it is enforced: single server, no federation, and a JWT costs revocation
-- there is no way to invalidate a signed bearer token before its expiry
without the server-side store that a JWT exists to avoid. A session here is a
row; logout is a `DELETE`.

**The stored form is a hash, never the token.** See `src/db.py`'s SCHEMA note.

**No user enumeration, and it is three separate mechanisms, not one:**
  1. one exception (`LoginFailed`) and one message for "no such account" and
     "wrong password" -- the caller cannot tell them apart from the response;
  2. an unknown username still pays a full argon2id verification against a
     fixed decoy hash, so the two cases cost comparable wall time;
  3. the per-account rate-limit scope keys on the SUBMITTED username rather
     than on a resolved account, so a 429 is reachable for a username that
     does not exist. Without (3) the limiter is itself the oracle that (1) and
     (2) were built to close.
"""

import base64
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidKey, UnsupportedAlgorithm
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from src.config import Config
from src.db import connect
from src.observability import is_valid_tenant

logger = logging.getLogger(__name__)

# An account id IS a tenant id: the value stored in `accounts.tenant_id`
# is what reaches log lines, span attributes and (once P1 step 7 is revisited)
# metric labels. `is_valid_tenant` is that shape check, imported rather than
# re-declared so there is exactly one definition of it in the codebase.

COOKIE_NAME = "mitya_session"

# OWASP Password Storage Cheat Sheet's argon2id row: m=19456 KiB (19 MiB),
# t=2, p=1. Encoded into every stored hash (see `_encode`), so raising these
# later does not invalidate existing hashes -- verification reads the
# parameters off the stored string, not from these constants.
_ARGON2_MEMORY_KIB = 19456
_ARGON2_ITERATIONS = 2
_ARGON2_LANES = 1
_ARGON2_LENGTH = 32
_SALT_BYTES = 16

_GENERIC_LOGIN_FAILURE = "invalid credentials"


class AuthError(Exception):
    """Base for everything a login can fail with."""


class LoginFailed(AuthError):
    """Wrong password, unknown account, or a disabled account.

    ONE exception for all three (D7: generic failure messages, no user
    enumeration). Callers map it to a single status and a single message.
    """

    def __init__(self, message: str = _GENERIC_LOGIN_FAILURE):
        super().__init__(message)


class RateLimited(AuthError):
    """Too many recent failed logins for this account id or this IP."""

    def __init__(self, retry_after: int):
        super().__init__("too many attempts")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Session:
    tenant_id: str
    expires_at: float


# ---------------------------------------------------------------------------
# argon2id
# ---------------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


def _kdf(salt: bytes, memory_kib: int, iterations: int, lanes: int, length: int) -> Argon2id:
    return Argon2id(
        salt=salt,
        length=length,
        iterations=iterations,
        lanes=lanes,
        memory_cost=memory_kib,
    )


def hash_password(password: str) -> str:
    """argon2id -> a PHC-format string, parameters and salt included.

    PHC (`$argon2id$v=19$m=...,t=...,p=...$salt$hash`) rather than a private
    encoding so the column is readable by any argon2 tool and so a later move
    to `argon2-cffi` -- if OpenSSL ever proves to be the wrong bet -- reads
    existing rows without a migration.
    """
    salt = os.urandom(_SALT_BYTES)
    key = _kdf(salt, _ARGON2_MEMORY_KIB, _ARGON2_ITERATIONS, _ARGON2_LANES,
               _ARGON2_LENGTH).derive(password.encode("utf-8"))
    return (
        f"$argon2id$v=19$m={_ARGON2_MEMORY_KIB},t={_ARGON2_ITERATIONS},"
        f"p={_ARGON2_LANES}${_b64(salt)}${_b64(key)}"
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time-ish verification against a stored PHC string.

    Returns False rather than raising on a malformed stored value: a corrupt
    row must fail the login, not 500 the endpoint and thereby distinguish
    itself from an ordinary wrong password.
    """
    try:
        _, algorithm, _version, params, salt_b64, key_b64 = encoded.split("$")
        if algorithm != "argon2id":
            return False
        parsed = dict(kv.split("=", 1) for kv in params.split(","))
        expected = _unb64(key_b64)
        kdf = _kdf(
            _unb64(salt_b64),
            int(parsed["m"]), int(parsed["t"]), int(parsed["p"]), len(expected),
        )
        kdf.verify(password.encode("utf-8"), expected)
        return True
    except (InvalidKey, ValueError, KeyError):
        return False


def argon2_available() -> bool:
    """Whether this build of OpenSSL actually provides Argon2 (3.2+).

    Called once at startup. Without it the first failure is a 500 on a user's
    login, at which point the cause is an `UnsupportedAlgorithm` buried in a
    traceback rather than a refusal to start.
    """
    try:
        hash_password("probe")
        return True
    except UnsupportedAlgorithm:
        return False


# The decoy. Hashing an arbitrary constant at import would cost ~40ms of
# startup and, worse, would run before `argon2_available()` could report a
# missing OpenSSL feature cleanly -- so it is built on first use and kept.
_DECOY_HASH: str | None = None


def _decoy() -> str:
    global _DECOY_HASH
    if _DECOY_HASH is None:
        _DECOY_HASH = hash_password(secrets.token_urlsafe(32))
    return _DECOY_HASH


# ---------------------------------------------------------------------------
# Accounts (registration is CLOSED -- decisions-p5.md. This is the only
# creation path, and it has no HTTP surface; `scripts/seed_account.py` is it.)
# ---------------------------------------------------------------------------

def create_account(tenant_id: str, password: str, *, db_path: str | None = None) -> None:
    if not is_valid_tenant(tenant_id):
        raise ValueError(
            f"account id {tenant_id!r} is not a valid tenant id "
            f"(lowercase alnum + hyphen, 1-32 chars) -- it reaches log lines "
            f"and span attributes verbatim"
        )
    if tenant_id == Config.DEFAULT_TENANT:
        # `Config.DEFAULT_TENANT` is what the middleware binds for an
        # UNAUTHENTICATED caller, and `require_tenant` rejects exactly that
        # value. An account allowed to take the name would authenticate
        # successfully and then be treated as anonymous on every request --
        # a confused-deputy bug with no error message anywhere.
        raise ValueError(
            f"{tenant_id!r} is reserved -- it is the anonymous tenant"
        )
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO accounts (tenant_id, password_hash, created_at) VALUES (?, ?, ?)",
            (tenant_id, hash_password(password), time.time()),
        )
    logger.info("auth: account created tenant=%s", tenant_id)


# ---------------------------------------------------------------------------
# Login rate limits -- per account AND per IP (D7, both, not either)
# ---------------------------------------------------------------------------

def _record_failure(conn, scope: str, now: float) -> None:
    conn.execute("INSERT INTO login_attempts (scope, at) VALUES (?, ?)", (scope, now))


def _recent_failures(conn, scope: str, now: float) -> int:
    cutoff = now - Config.LOGIN_WINDOW_SECONDS
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM login_attempts WHERE scope = ? AND at >= ?",
        (scope, cutoff),
    ).fetchone()
    return row["n"]


def _check_rate_limits(conn, account_scope: str, ip_scope: str, now: float) -> None:
    """Raise `RateLimited` if either limiter is over its cap.

    Deliberately NOT `src.limits.RateLimiter`: that one *sleeps* until capacity
    frees up (`acquire_async`), which is the right shape for a provider quota
    and exactly the wrong shape here -- a login limiter that sleeps converts a
    password-guessing attempt into a connection-holding denial of service.
    This one rejects.
    """
    if _recent_failures(conn, account_scope, now) >= Config.LOGIN_MAX_PER_ACCOUNT:
        raise RateLimited(int(Config.LOGIN_WINDOW_SECONDS))
    if _recent_failures(conn, ip_scope, now) >= Config.LOGIN_MAX_PER_IP:
        raise RateLimited(int(Config.LOGIN_WINDOW_SECONDS))


def _prune_attempts(conn, now: float) -> None:
    """Drop attempt rows outside every window. Cheap, and it runs on the login
    path only -- the table is otherwise unbounded and nothing else would ever
    read it."""
    conn.execute("DELETE FROM login_attempts WHERE at < ?", (now - Config.LOGIN_WINDOW_SECONDS,))


# ---------------------------------------------------------------------------
# Sessions -- opaque, server-side, rotated on login
# ---------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_session(conn, tenant_id: str, now: float) -> str:
    """Mint 256 bits of opaque token, store only its hash, return the token.

    The value is a bare random string: it carries no claims, no encoding, and
    nothing derivable about the account. That is the whole point of "opaque"
    -- the server's row is the only thing that says who it belongs to.
    """
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, tenant_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (_token_hash(token), tenant_id, now, now + Config.SESSION_TTL_SECONDS),
    )
    return token


def login(
    username: str,
    password: str,
    client_ip: str,
    *,
    presented_token: str | None = None,
    db_path: str | None = None,
    now: float | None = None,
) -> str:
    """Authenticate and return a NEW opaque session token.

    `presented_token` is whatever session cookie the caller already had. It is
    destroyed here whether or not it was valid, and whoever it belonged to --
    that is D7's **rotation on login**: the identifier that existed before the
    privilege level changed must not survive the change, which is what closes
    session fixation (an attacker who plants a known cookie value in a victim's
    browser holds a dead token the moment the victim logs in).

    Raises `RateLimited` or `LoginFailed`; never anything that distinguishes an
    unknown account from a wrong password.
    """
    now = time.time() if now is None else now
    account_scope = f"account:{username.lower()[:64]}"
    ip_scope = f"ip:{client_ip}"

    with connect(db_path) as conn:
        _check_rate_limits(conn, account_scope, ip_scope, now)

        row = conn.execute(
            "SELECT tenant_id, password_hash, disabled FROM accounts WHERE tenant_id = ?",
            (username.lower(),),
        ).fetchone()

        # Unknown account: verify against the decoy anyway. Both branches run
        # exactly one argon2id verification, so the timing difference between
        # them is a dict lookup, not a 40ms KDF.
        if row is None:
            verify_password(password, _decoy())
            ok = False
        else:
            ok = verify_password(password, row["password_hash"]) and not row["disabled"]

        if not ok:
            _record_failure(conn, account_scope, now)
            _record_failure(conn, ip_scope, now)
            # Commit BEFORE raising. `connect()` rolls back on exception, and
            # LoginFailed is an exception -- so without this the attempt rows
            # vanish on the way out and the limiter never counts anything. The
            # only writes in flight here are the two rows above.
            conn.commit()
            logger.warning("auth: login failed for %r from %s", username, client_ip)
            raise LoginFailed()

        # Rotation, and a clean slate for the limiter on success.
        if presented_token:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(presented_token),))
        conn.execute("DELETE FROM login_attempts WHERE scope = ?", (account_scope,))
        _prune_attempts(conn, now)
        token = _issue_session(conn, row["tenant_id"], now)

    logger.info("auth: login ok tenant=%s", row["tenant_id"])
    return token


def resolve_session(token: str | None, *, db_path: str | None = None,
                    now: float | None = None) -> Session | None:
    """One indexed lookup: token -> tenant. `None` for absent, unknown, or expired.

    This is the ONE round trip C11 permits per request. Its result is bound to
    `request.state` by the middleware, so nothing downstream -- the cache key
    included -- resolves the tenant a second time.
    """
    if not token:
        return None
    now = time.time() if now is None else now
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT tenant_id, expires_at FROM sessions WHERE token_hash = ?",
            (_token_hash(token),),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= now:
            # Expired rows are dropped on sight rather than by a sweeper: this
            # is the only code that ever looks at them, so it is also the only
            # place that can know one is dead.
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
            return None
    return Session(tenant_id=row["tenant_id"], expires_at=row["expires_at"])


def logout(token: str | None, *, db_path: str | None = None) -> None:
    """Destroy one session. Idempotent, and silent about whether it existed."""
    if not token:
        return
    with connect(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
