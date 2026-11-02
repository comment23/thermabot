"""Auth and roles (D§10): argon2id passwords, hashed session cookies, CSRF, lockout,
`require(cap)` dependency, BOB_TOKEN (only /healthz and /api/status), create-user CLI helper.
No secrets in code: BOB_TOKEN comes from env via app.state.bob_token."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request
from pydantic import BaseModel

from thermabot.adapters import db

Role = Literal["owner", "admin", "member"]
COOKIE = "tb_session"
CSRF_HEADER = "X-CSRF-Token"
FIRST_RUN_COOKIE = "tb_first_run"
FIRST_RUN_TTL = timedelta(minutes=15)
FIRST_RUN_KEY = "initial_owner_created"
MIN_PW = 8
USER_FAILS, USER_LOCK = 5, timedelta(minutes=15)
IP_FAILS, IP_WINDOW = 20, timedelta(hours=1)
SESSION_SHORT, SESSION_LONG = timedelta(hours=12), timedelta(days=30)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

_OWNER_ONLY = {"view_model", "manage_users", "edit_safety", "kill_on", "set_active"}
_ADMIN = {
    "view",
    "view_health",
    "kill_off",
    "override",
    "edit_comfort",
    "guest_toggle",
    "nudge_any",
    "nudge_own",
}
CAPS: dict[str, frozenset[str]] = {
    "owner": frozenset(_ADMIN | _OWNER_ONLY),
    "admin": frozenset(_ADMIN),
    "member": frozenset({"view", "nudge_own"}),
}
_ph = PasswordHasher()


class Principal(BaseModel):
    user_id: int | None
    username: str
    role: Role | Literal["service"]
    room: str | None = None
    csrf: str | None = None

    def can(self, cap: str) -> bool:
        return self.role != "service" and cap in CAPS[self.role]


class LoginError(Exception):
    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def hash_pw(pw: str) -> str:
    if len(pw) < MIN_PW:
        raise ValueError(f"password must be at least {MIN_PW} characters")
    return _ph.hash(pw)


def verify_pw(pw_hash: str, pw: str) -> bool:
    try:
        return _ph.verify(pw_hash, pw)
    except (VerificationError, InvalidHashError):
        return False


def create_user(
    conn: sqlite3.Connection,
    username: str,
    role: Role,
    password: str,
    room: str | None = None,
    now: datetime | None = None,
) -> int:
    if role not in CAPS:
        raise ValueError(f"bad role {role}")
    cur = conn.execute(
        "INSERT INTO users (username, role, room, pw_hash, created_at) VALUES (?,?,?,?,?)",
        (username, role, room, hash_pw(password), _iso(now or datetime.now(UTC))),
    )
    return int(cur.lastrowid or 0)


BOB_PATHS = frozenset({"/healthz", "/api/status"})  # GET only (D§10)
Clock = Callable[[], datetime]


class Auth:
    """Login/session/lockout service bound to one DB connection."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        bob_token: str | None = None,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self.conn, self.bob_token, self.clock = conn, bob_token, clock
        self._ip_fails: dict[str, deque[datetime]] = defaultdict(deque)
        self._first_run_key = secrets.token_bytes(32)

    def needs_first_account(self) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
            and self.conn.execute("SELECT 1 FROM bot_kv WHERE key=?", (FIRST_RUN_KEY,)).fetchone()
            is None
        )

    def first_run_token(self) -> str:
        expires = int((self.clock() + FIRST_RUN_TTL).timestamp())
        payload = f"{expires}.{secrets.token_urlsafe(24)}"
        signature = hmac.new(self._first_run_key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def valid_first_run_token(self, cookie: str, sent: str) -> bool:
        if not cookie or len(cookie) > 160 or len(sent) > 160:
            return False
        if not hmac.compare_digest(cookie.encode(), sent.encode()):
            return False
        payload, _, signature = cookie.rpartition(".")
        try:
            expires = int(payload.partition(".")[0])
        except ValueError:
            return False
        now = self.clock().timestamp()
        if not now < expires <= now + FIRST_RUN_TTL.total_seconds():
            return False
        expected = hmac.new(self._first_run_key, payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.encode(), expected.encode())

    def create_first_owner(self, username: str, password: str, ip: str) -> tuple[str, str]:
        # The write lock covers the empty-user check, account, audit, and session together.
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if not self.needs_first_account():
                raise LoginError(409, "Initial account setup is already complete. Sign in instead.")
            if self._ip_blocked(ip, self.clock()):
                raise LoginError(429, "too many attempts from this address")
            create_user(self.conn, username, "owner", password, now=self.clock())
            db.write_kv(self.conn, FIRST_RUN_KEY, "1", now=self.clock())
            self._audit(username, "initial_owner_created", ip, role="owner")
            return self.login(username, password, ip=ip)

    def _audit(self, actor: str, action: str, ip: str | None, **detail: object) -> None:
        db.audit(self.conn, actor, action, dict(detail), now=self.clock(), ip=ip)

    def _ip_blocked(self, ip: str, now: datetime) -> bool:
        q = self._ip_fails[ip]
        while q and now - q[0] > IP_WINDOW:
            q.popleft()
        return len(q) >= IP_FAILS

    def login(
        self, username: str, password: str, ip: str = "?", trusted: bool = False
    ) -> tuple[str, str]:
        """-> (session token for the cookie, csrf). Raises LoginError(401|423|429)."""
        now = self.clock()
        if self._ip_blocked(ip, now):
            raise LoginError(429, "too many attempts from this address")
        r = self.conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if r is not None and r["locked_until"] and datetime.fromisoformat(r["locked_until"]) > now:
            self._audit(username, "login_locked", ip)
            raise LoginError(423, "account locked")
        if r is None or not verify_pw(r["pw_hash"], password):
            self._ip_fails[ip].append(now)
            if r is not None:
                fails = int(r["failed_logins"]) + 1
                locked = _iso(now + USER_LOCK) if fails >= USER_FAILS else None
                self.conn.execute(
                    "UPDATE users SET failed_logins=?, locked_until=? WHERE id=?",
                    (0 if locked else fails, locked, r["id"]),
                )
            self._audit(username, "login_failed", ip)
            raise LoginError(401, "bad username or password")
        self.conn.execute(
            "UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?", (r["id"],)
        )
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        exp = now + (SESSION_LONG if trusted else SESSION_SHORT)
        self.conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf, created_at, expires_at, ip)"
            " VALUES (?,?,?,?,?,?)",
            (_sha(token), r["id"], csrf, _iso(now), _iso(exp), ip),
        )
        self._audit(username, "login", ip)
        return token, csrf

    def logout(self, token: str) -> None:
        self.conn.execute("DELETE FROM sessions WHERE token_hash=?", (_sha(token),))

    def end_other_sessions(self, user_id: int, keep_token: str | None) -> None:
        """After a password change: sign out every other device, keep this one."""
        self.conn.execute(
            "DELETE FROM sessions WHERE user_id=? AND token_hash<>?",
            (user_id, _sha(keep_token or "")),
        )

    def session(self, token: str | None) -> Principal | None:
        if not token:
            return None
        r = self.conn.execute(
            "SELECT u.id, u.username, u.role, u.room, s.csrf, s.expires_at FROM sessions s"
            " JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
            (_sha(token),),
        ).fetchone()
        if r is None or datetime.fromisoformat(r["expires_at"]) <= self.clock():
            return None
        return Principal(
            user_id=r["id"], username=r["username"], role=r["role"], room=r["room"], csrf=r["csrf"]
        )

    def bob(self, request: Request) -> bool:
        h = request.headers.get("authorization", "")
        if not self.bob_token or not h.lower().startswith("bearer "):
            return False
        return hmac.compare_digest(h[7:].strip(), self.bob_token)

    def principal(self, request: Request) -> Principal | None:
        """Session cookie, else BOB_TOKEN (only on BOB_PATHS GET)."""
        p = self.session(request.cookies.get(COOKIE))
        if p is not None:
            return p
        if self.bob(request):
            if request.method == "GET" and request.url.path in BOB_PATHS:
                return Principal(user_id=None, username="bob", role="service")
            raise HTTPException(403, "service token not valid here")
        return None


def require(cap: str) -> Callable[[Request], Awaitable[Principal]]:
    """FastAPI dependency: 401 no session, 403 missing cap or bad CSRF on non-GET.
    `view` also accepts BOB_TOKEN on BOB_PATHS."""

    async def dep(request: Request) -> Principal:
        auth: Auth = request.app.state.auth
        p = auth.principal(request)
        if p is None:
            raise HTTPException(401, "login required")
        if p.role == "service":
            if cap == "view":
                return p
            raise HTTPException(403, "service token not valid here")
        if not p.can(cap):
            raise HTTPException(403, f"needs {cap}")
        if request.method not in SAFE_METHODS:
            sent = request.headers.get(CSRF_HEADER, "")
            if not p.csrf or not hmac.compare_digest(sent, p.csrf):
                raise HTTPException(403, "CSRF token missing or wrong")
        return p

    return dep


def cli_create_user(
    argv: list[str], conn: sqlite3.Connection, getpass: Callable[[str], str]
) -> int:
    """`python -m thermabot create-user NAME --role owner [--room R]`; password via prompt."""
    import argparse

    ap = argparse.ArgumentParser(prog="thermabot create-user")
    ap.add_argument("username")
    ap.add_argument("--role", choices=sorted(CAPS), required=True)
    ap.add_argument("--room")
    a = ap.parse_args(argv)
    pw = getpass("Password: ")
    if pw != getpass("Repeat: "):
        raise SystemExit("passwords differ")
    uid = create_user(conn, a.username, a.role, pw, a.room)
    db.audit(conn, "cli", "create_user", {"username": a.username, "role": a.role})
    return uid
