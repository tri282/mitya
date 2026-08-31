# test/test_auth.py
"""P5 Batch A verify: every clause of the charter §IV auth row (D7) has a test.

Plain-assert script, not pytest (matches test_gateway_guard.py's idiom).
Offline: no network, no server, no uvicorn. The database is a temp sqlite file
per run; `Config.DB_PATH` is repointed at it before `src.api.app` is imported
so the route-level tests hit the same file as the unit-level ones.

Clause -> test map (the same table is in plans/mitya-v2/05-impl/phase-5.md):

  argon2id                      -> test_argon2id_is_the_hasher
                                   test_argon2id_salts_are_unique_per_hash
                                   test_verify_rejects_a_tampered_or_foreign_hash
  opaque server-side tokens     -> test_session_token_is_opaque_not_a_jwt
                                   test_the_token_itself_is_never_stored
                                   test_no_jwt_library_is_imported
  HttpOnly/Secure/SameSite=Lax  -> test_cookie_carries_all_four_flags
  rotation on login             -> test_login_rotates_the_presented_session
  per-account rate limit        -> test_per_account_login_rate_limit
  per-IP rate limit             -> test_per_ip_login_rate_limit
  generic failure messages      -> test_unknown_user_and_wrong_password_are_identical
  no user enumeration           -> test_unknown_user_pays_the_same_hash
                                   test_rate_limit_is_reachable_for_an_unknown_user
  registration closed           -> test_no_registration_route_exists
  TLS in front                  -> test_secure_flag_is_on_by_default
                                   (the terminator itself is deployment, not code)

Step 3 (identity on every request) is covered by:
  test_resolver_runs_exactly_once_per_request
  test_tenant_reaches_request_state_and_the_contextvar
  test_the_x_tenant_id_header_is_ignored
  test_chat_and_books_require_a_session
"""

import asyncio
import inspect
import os
import sys
import tempfile
import time

sys.path.insert(0, ".")

from src.config import Config  # noqa: E402

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="mitya-auth-test-"), "test.sqlite3")
Config.DB_PATH = _TMP_DB
# Route tests drive the login handler directly and read Set-Cookie off the
# returned response; the Secure flag must stay ON here, because asserting it is
# one of the clauses.
Config.SESSION_COOKIE_SECURE = True
Config.TRUST_FORWARDED_FOR = False

from fastapi import HTTPException, Request  # noqa: E402

import src.auth as auth  # noqa: E402
from src.auth import (  # noqa: E402
    COOKIE_NAME,
    LoginFailed,
    RateLimited,
    create_account,
    hash_password,
    login,
    logout,
    resolve_session,
    verify_password,
)
from src.db import connect, init_schema  # noqa: E402
from src.observability import (  # noqa: E402
    RequestIdMiddleware,
    TENANT_HEADER,
    current_tenant,
    is_valid_tenant,
)

import src.api.app as app_mod  # noqa: E402
from src.api.app import (  # noqa: E402
    LoginRequest,
    _resolve_tenant,
    auth_login_route,
    auth_logout_route,
    require_tenant,
)

PASSWORD = "correct horse battery staple"
OTHER_PASSWORD = "a completely different passphrase"


def _fresh_db() -> None:
    """Empty every table. Cheaper and less surprising than a new file per test:
    the module-level `Config.DB_PATH` is already bound into imported code."""
    init_schema()
    with connect() as conn:
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM login_attempts")
        conn.execute("DELETE FROM accounts")


def _seed(tenant: str = "alice", password: str = PASSWORD) -> None:
    create_account(tenant, password)


def _request(headers=None, cookies=None, client=("10.0.0.1", 1234), state=None) -> Request:
    raw = list(headers or [])
    if cookies:
        raw.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "headers": raw,
        "client": client,
        "query_string": b"",
    }
    if state is not None:
        scope["state"] = state
    return Request(scope)


def _cookie_attrs(response) -> dict[str, str]:
    """Parse the Set-Cookie header into {lowercased attribute: value}. A
    valueless attribute (HttpOnly, Secure) maps to the empty string."""
    raw = response.headers["set-cookie"]
    attrs: dict[str, str] = {}
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        attrs[name.lower()] = value
    return attrs


# ---------------------------------------------------------------------------
# argon2id
# ---------------------------------------------------------------------------

def test_argon2id_is_the_hasher() -> None:
    """D7 names argon2id specifically. Not bcrypt, not scrypt, not PBKDF2 --
    and not a plausible-looking wrapper that quietly falls back to one."""
    encoded = hash_password(PASSWORD)
    assert encoded.startswith("$argon2id$"), encoded[:40]
    # The cost parameters travel with the hash, so raising them later does not
    # invalidate stored rows.
    assert "m=19456" in encoded and "t=2" in encoded and "p=1" in encoded, encoded
    assert verify_password(PASSWORD, encoded) is True
    assert verify_password(OTHER_PASSWORD, encoded) is False


def test_argon2id_salts_are_unique_per_hash() -> None:
    """Two hashes of the same password must not be equal -- an unsalted or
    fixed-salt hasher makes the accounts table a rainbow-table lookup."""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_verify_rejects_a_tampered_or_foreign_hash() -> None:
    """A corrupt stored value fails the login; it does not raise, which would
    make it distinguishable from an ordinary wrong password."""
    assert verify_password(PASSWORD, "") is False
    assert verify_password(PASSWORD, "$argon2i$v=19$m=19456,t=2,p=1$AAAA$BBBB") is False
    assert verify_password(PASSWORD, "$argon2id$v=19$garbage") is False
    encoded = hash_password(PASSWORD)
    assert verify_password(PASSWORD, encoded[:-4] + "AAAA") is False


# ---------------------------------------------------------------------------
# Opaque server-side sessions, not JWT
# ---------------------------------------------------------------------------

def test_session_token_is_opaque_not_a_jwt() -> None:
    _fresh_db()
    _seed()
    token = login("alice", PASSWORD, "10.0.0.1")
    # A JWT is three base64 segments separated by dots and decodes to JSON.
    # An opaque token carries nothing at all.
    assert token.count(".") == 0, token
    assert len(token) >= 32, token
    session = resolve_session(token)
    assert session is not None and session.tenant_id == "alice"
    # Only the server's row says who it belongs to.
    assert "alice" not in token


def test_the_token_itself_is_never_stored() -> None:
    """A read of data/mitya.sqlite3 must not hand the reader a usable session."""
    _fresh_db()
    _seed()
    token = login("alice", PASSWORD, "10.0.0.1")
    with connect() as conn:
        rows = conn.execute("SELECT * FROM sessions").fetchall()
    assert len(rows) == 1
    stored = " ".join(str(v) for v in dict(rows[0]).values())
    assert token not in stored, "the raw session token reached the database"


def test_no_jwt_library_is_imported() -> None:
    """D7 rules JWT out permanently: single server, no federation, and a signed
    bearer token cannot be revoked before expiry without the server-side store
    a JWT exists to avoid."""
    source = inspect.getsource(auth)
    for banned in ("jwt", "jose", "itsdangerous"):
        assert f"import {banned}" not in source, banned


def test_logout_destroys_the_session_server_side() -> None:
    _fresh_db()
    _seed()
    token = login("alice", PASSWORD, "10.0.0.1")
    assert resolve_session(token) is not None
    logout(token)
    assert resolve_session(token) is None
    # Idempotent, and silent about whether there was anything to destroy.
    logout(token)
    logout(None)


def test_an_expired_session_does_not_resolve() -> None:
    _fresh_db()
    _seed()
    token = login("alice", PASSWORD, "10.0.0.1")
    assert resolve_session(token, now=time.time() + Config.SESSION_TTL_SECONDS + 1) is None


# ---------------------------------------------------------------------------
# Cookie flags and rotation
# ---------------------------------------------------------------------------

def test_cookie_carries_all_four_flags() -> None:
    """HttpOnly + Secure + SameSite=Lax + a scoped path, on the one response
    that ever issues a session."""
    _fresh_db()
    _seed()
    response = asyncio.run(
        auth_login_route(LoginRequest(username="alice", password=PASSWORD), _request())
    )
    assert response.status_code == 200
    attrs = _cookie_attrs(response)
    assert COOKIE_NAME.lower() in attrs, attrs
    assert "httponly" in attrs, "no HttpOnly -- the token is readable from JS"
    assert "secure" in attrs, "no Secure -- the token travels over plain HTTP"
    assert attrs.get("samesite", "").lower() == "lax", attrs
    assert attrs.get("path") == "/", attrs
    assert attrs.get("max-age") == str(Config.SESSION_TTL_SECONDS), attrs


def test_secure_flag_is_on_by_default() -> None:
    """TLS is terminated in front (D7). The Secure knob exists only because an
    HTTP client will not return a Secure cookie over plain http, which is every
    local harness run -- so the DEFAULT must be the safe one.

    Asserted against the source, not against the live value: this module
    pins `Config.SESSION_COOKIE_SECURE = True` at import so the cookie-flag
    test above is meaningful, which would make a live-value assertion
    self-fulfilling."""
    source = inspect.getsource(sys.modules["src.config"])
    assert 'os.getenv("SESSION_COOKIE_SECURE", "1")' in source, \
        "the Secure flag no longer defaults to on"
    assert 'os.getenv("TRUST_FORWARDED_FOR", "0")' in source, \
        "X-Forwarded-For no longer defaults to untrusted"


def test_login_rotates_the_presented_session() -> None:
    """Session fixation: an identifier that existed before the privilege level
    changed must not survive the change."""
    _fresh_db()
    _seed()
    first = login("alice", PASSWORD, "10.0.0.1")
    second = login("alice", PASSWORD, "10.0.0.1", presented_token=first)
    assert second != first
    assert resolve_session(first) is None, "the pre-login token still works"
    assert resolve_session(second) is not None


def test_logout_route_clears_the_cookie() -> None:
    _fresh_db()
    _seed()
    token = login("alice", PASSWORD, "10.0.0.1")
    response = asyncio.run(auth_logout_route(_request(cookies={COOKIE_NAME: token})))
    assert response.status_code == 200
    assert resolve_session(token) is None
    attrs = _cookie_attrs(response)
    assert attrs.get(COOKIE_NAME.lower()) in ("", '""'), attrs


# ---------------------------------------------------------------------------
# Rate limits -- per account AND per IP, both required
# ---------------------------------------------------------------------------

def test_per_account_login_rate_limit() -> None:
    _fresh_db()
    _seed()
    for _ in range(Config.LOGIN_MAX_PER_ACCOUNT):
        try:
            login("alice", OTHER_PASSWORD, "10.0.0.1")
            raise AssertionError("wrong password must not succeed")
        except LoginFailed:
            pass
    # The cap is now reached: even the CORRECT password is refused, which is
    # the point -- otherwise the limiter is bypassable by guessing right.
    try:
        login("alice", PASSWORD, "10.0.0.1")
        raise AssertionError("rate limit did not engage")
    except RateLimited as exc:
        assert exc.retry_after > 0


def test_per_ip_login_rate_limit() -> None:
    """The per-account limit must not be the only one: an attacker spreading
    guesses across many usernames from one host has to hit a second wall."""
    _fresh_db()
    for i in range(Config.LOGIN_MAX_PER_IP):
        try:
            login(f"nobody-{i}", OTHER_PASSWORD, "10.0.0.9")
        except LoginFailed:
            pass
    # A different, never-tried username from the same IP is now refused, and
    # refused for the IP's sake -- its own account counter is at zero.
    try:
        login("nobody-fresh", OTHER_PASSWORD, "10.0.0.9")
        raise AssertionError("per-IP rate limit did not engage")
    except RateLimited:
        pass
    # A different IP is unaffected.
    try:
        login("nobody-fresh", OTHER_PASSWORD, "10.0.0.10")
        raise AssertionError("wrong password must not succeed")
    except LoginFailed:
        pass


def test_a_successful_login_clears_the_account_counter() -> None:
    _fresh_db()
    _seed()
    for _ in range(Config.LOGIN_MAX_PER_ACCOUNT - 1):
        try:
            login("alice", OTHER_PASSWORD, "10.0.0.1")
        except LoginFailed:
            pass
    login("alice", PASSWORD, "10.0.0.1")
    with connect() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM login_attempts WHERE scope = ?", ("account:alice",)
        ).fetchone()["n"]
    assert remaining == 0, remaining


# ---------------------------------------------------------------------------
# Generic failures, no user enumeration
# ---------------------------------------------------------------------------

def test_unknown_user_and_wrong_password_are_identical() -> None:
    """Same exception, same message, same HTTP shape. From outside, the two
    cases are one case."""
    _fresh_db()
    _seed()
    outcomes = []
    for username, password in (("alice", OTHER_PASSWORD), ("nosuchuser", OTHER_PASSWORD)):
        try:
            login(username, password, f"10.0.{len(outcomes)}.1")
            raise AssertionError("login must not succeed")
        except LoginFailed as exc:
            outcomes.append((type(exc).__name__, str(exc)))
    assert outcomes[0] == outcomes[1], outcomes

    # And through the route: identical status AND identical body.
    responses = []
    for username in ("alice", "nosuchuser"):
        try:
            asyncio.run(auth_login_route(
                LoginRequest(username=username, password=OTHER_PASSWORD),
                _request(client=("10.1.1.1", 1)),
            ))
            raise AssertionError("login must not succeed")
        except HTTPException as exc:
            responses.append((exc.status_code, exc.detail))
    assert responses[0] == responses[1] == (401, "invalid credentials"), responses


def test_unknown_user_pays_the_same_hash() -> None:
    """Mechanism, then wall clock. The mechanism assertion is the real one --
    a timing assertion alone is flaky on a loaded machine, and a timing
    assertion that passes for the wrong reason is worse than none."""
    _fresh_db()
    _seed()

    calls: list[str] = []
    original = auth.verify_password

    def counting(password, encoded):
        calls.append(encoded[:20])
        return original(password, encoded)

    auth.verify_password = counting
    try:
        for username in ("alice", "nosuchuser"):
            calls.clear()
            try:
                login(username, OTHER_PASSWORD, "10.2.2.2")
            except LoginFailed:
                pass
            assert len(calls) == 1, (username, calls)
            assert calls[0].startswith("$argon2id$"), calls
    finally:
        auth.verify_password = original

    # Coarse wall-clock check with a wide margin: argon2id at m=19456 dominates
    # both branches, so the ratio should be near 1. 3x is deliberately loose --
    # this test must not fail because the machine was busy.
    _fresh_db()
    _seed()

    def timed(username: str) -> float:
        start = time.perf_counter()
        try:
            login(username, OTHER_PASSWORD, "10.3.3.3")
        except (LoginFailed, RateLimited):
            pass
        return time.perf_counter() - start

    known = min(timed("alice") for _ in range(3))
    _fresh_db()
    _seed()
    unknown = min(timed("nosuchuser") for _ in range(3))
    ratio = max(known, unknown) / max(min(known, unknown), 1e-9)
    assert ratio < 3.0, f"timing oracle: known={known:.4f}s unknown={unknown:.4f}s"


def test_rate_limit_is_reachable_for_an_unknown_user() -> None:
    """If the per-account limiter only counted real accounts, the 429 itself
    would answer "does this username exist?" -- so it keys on the SUBMITTED
    string."""
    _fresh_db()
    for _ in range(Config.LOGIN_MAX_PER_ACCOUNT):
        try:
            login("ghost", OTHER_PASSWORD, f"10.4.4.{_}")
        except LoginFailed:
            pass
    try:
        login("ghost", OTHER_PASSWORD, "10.4.4.99")
        raise AssertionError("no 429 for an unknown user -- that is the oracle")
    except RateLimited:
        pass


# ---------------------------------------------------------------------------
# Registration is closed
# ---------------------------------------------------------------------------

def test_no_registration_route_exists() -> None:
    """decisions-p5.md: closed, seeded by script. No invite tokens, no admin
    UI, and nothing on the HTTP surface that creates an account."""
    paths = [getattr(r, "path", "") for r in app_mod.app.routes]
    for path in paths:
        lowered = path.lower()
        for banned in ("register", "signup", "sign-up", "accounts"):
            assert banned not in lowered, f"{path} looks like a registration surface"
    assert "/auth/login" in paths and "/auth/logout" in paths, paths


def test_account_id_must_be_a_valid_tenant_id() -> None:
    """The account id IS the tenant id and reaches log lines verbatim."""
    _fresh_db()
    for bad in ("Alice", "a" * 33, "-alice", "alice\n", "al ice", "alice;DROP", ""):
        try:
            create_account(bad, PASSWORD)
            raise AssertionError(f"accepted invalid tenant id {bad!r}")
        except ValueError:
            pass
    # And the reserved anonymous name, which would otherwise authenticate and
    # then be treated as anonymous on every request.
    try:
        create_account(Config.DEFAULT_TENANT, PASSWORD)
        raise AssertionError("accepted the anonymous tenant name as an account")
    except ValueError:
        pass
    create_account("alice-2", PASSWORD)
    assert is_valid_tenant("alice-2")


def test_short_passwords_are_refused() -> None:
    _fresh_db()
    try:
        create_account("bob", "short")
        raise AssertionError("accepted a 5-character password")
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Step 3 -- identity on every request, resolved exactly once
# ---------------------------------------------------------------------------

def _drive(app, headers=None):
    """Run one ASGI request against `app` (test_observability.py's idiom)."""
    scope = {"type": "http", "method": "GET", "path": "/books", "headers": headers or []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list = []

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return scope, sent


def test_tenant_reaches_request_state_and_the_contextvar() -> None:
    """C11: resolved at the cookie check, carried on request.state. Both the
    ContextVar (which feeds LOG_FORMAT and the root span) and
    `request.state.tenant` (which the endpoint reads) must carry it."""
    _fresh_db()
    _seed()
    token = login("alice", PASSWORD, "10.0.0.1")
    observed: dict = {}

    async def stub(scope, receive, send):
        observed["contextvar"] = current_tenant()
        observed["state"] = scope["state"]["tenant"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestIdMiddleware(stub, tenant_resolver=_resolve_tenant)
    _drive(middleware, headers=[(b"cookie", f"{COOKIE_NAME}={token}".encode())])
    assert observed == {"contextvar": "alice", "state": "alice"}, observed


def test_resolver_runs_exactly_once_per_request() -> None:
    """C11's stated discriminator: the resolved tenant must reach the cache key
    WITHOUT a second round trip per request."""
    _fresh_db()
    _seed()
    token = login("alice", PASSWORD, "10.0.0.1")
    calls: list[int] = []

    def counting(scope):
        calls.append(1)
        return _resolve_tenant(scope)

    async def stub(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestIdMiddleware(stub, tenant_resolver=counting)
    _drive(middleware, headers=[(b"cookie", f"{COOKIE_NAME}={token}".encode())])
    assert len(calls) == 1, calls


def test_the_x_tenant_id_header_is_ignored() -> None:
    """P1 read X-Tenant-ID off the wire and said so: unauthenticated, until P5.
    It is now an impersonation primitive if honoured, so it is not."""
    _fresh_db()
    _seed()
    token = login("alice", PASSWORD, "10.0.0.1")
    observed: list[str] = []

    async def stub(scope, receive, send):
        observed.append(current_tenant())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestIdMiddleware(stub, tenant_resolver=_resolve_tenant)
    _drive(middleware, headers=[
        (b"cookie", f"{COOKIE_NAME}={token}".encode()),
        (TENANT_HEADER, b"victim"),
    ])
    assert observed == ["alice"], observed

    # And with no cookie at all, the header cannot mint an identity either.
    observed.clear()
    _drive(middleware, headers=[(TENANT_HEADER, b"victim")])
    assert observed == [Config.DEFAULT_TENANT], observed


def test_an_unparseable_cookie_header_is_no_cookie_not_a_500() -> None:
    observed: list[str] = []

    async def stub(scope, receive, send):
        observed.append(current_tenant())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestIdMiddleware(stub, tenant_resolver=_resolve_tenant)
    for hostile in (b"", b"=====", b"mitya_session", b"\x00\x01"):
        observed.clear()
        _drive(middleware, headers=[(b"cookie", hostile)])
        assert observed == [Config.DEFAULT_TENANT], (hostile, observed)


def test_chat_and_books_require_a_session() -> None:
    """The tenant plane rejects an anonymous caller; the operator plane does
    not have the dependency at all, because the ladder and a liveness probe
    cannot depend on a session."""
    for route in app_mod.app.routes:
        path = getattr(route, "path", "")
        if path in ("/chat", "/books", "/auth/session"):
            dependencies = route.dependant.dependencies
            assert any(d.call is require_tenant for d in dependencies), \
                f"{path} is not auth-gated"
        if path in ("/health", "/ready", "/metrics"):
            deps = getattr(route, "dependant", None)
            assert not (deps and deps.dependencies), f"{path} must stay unauthenticated"

    # The dependency itself: anonymous is rejected, a real tenant is returned.
    try:
        require_tenant(_request(state={"tenant": Config.DEFAULT_TENANT}))
        raise AssertionError("anonymous caller was not rejected")
    except HTTPException as exc:
        assert exc.status_code == 401, exc.status_code
    try:
        require_tenant(_request(state={}))
        raise AssertionError("missing tenant was not rejected")
    except HTTPException as exc:
        assert exc.status_code == 401, exc.status_code
    assert require_tenant(_request(state={"tenant": "alice"})) == "alice"


def test_forwarded_for_is_not_trusted_by_default() -> None:
    """X-Forwarded-For is client-supplied. Honouring it unconditionally gives
    an attacker an unlimited supply of distinct "IPs" and voids the per-IP
    login limit entirely."""
    assert Config.TRUST_FORWARDED_FOR is False
    request = _request(headers=[(b"x-forwarded-for", b"1.2.3.4")], client=("10.9.9.9", 1))
    assert app_mod._client_ip(request) == "10.9.9.9"

    Config.TRUST_FORWARDED_FOR = True
    try:
        # When a trusted proxy IS in front, the LAST hop is taken -- every
        # earlier entry is whatever the client chose to send.
        spoofed = _request(
            headers=[(b"x-forwarded-for", b"9.9.9.9, 1.2.3.4")], client=("10.9.9.9", 1)
        )
        assert app_mod._client_ip(spoofed) == "1.2.3.4"
    finally:
        Config.TRUST_FORWARDED_FOR = False


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
