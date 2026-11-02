"""T1.32: auth (D§10) - argon2, sessions, CSRF, lockout, require(cap), BOB_TOKEN scope."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from thermabot.adapters import db
from thermabot.app.auth import (
    CAPS,
    COOKIE,
    CSRF_HEADER,
    Auth,
    LoginError,
    Principal,
    cli_create_user,
    create_user,
    hash_pw,
    require,
    verify_pw,
)

NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)
BOB = "bob-test-token-not-a-secret-0123456789"  # fake token (tests only)
PW = "correct horse"


class Clock:
    def __init__(self) -> None:
        self.t = NOW

    def __call__(self) -> datetime:
        return self.t


def mk(tmp: Path) -> tuple[Auth, Clock, TestClient]:
    conn = db.open_db(tmp / "a.db", now=NOW)
    clk = Clock()
    auth = Auth(conn, bob_token=BOB, clock=clk)
    for name, role in (("alex", "owner"), ("ann", "admin"), ("mel", "member")):
        create_user(conn, name, role, PW, now=NOW)  # type: ignore[arg-type]
    app = FastAPI()
    app.state.auth = auth

    @app.get("/api/status")
    async def status(p: Annotated[Principal, Depends(require("view"))]) -> dict[str, str]:
        return {"who": p.username}

    @app.get("/healthz")
    async def healthz(p: Annotated[Principal, Depends(require("view"))]) -> dict[str, str]:
        return {"who": p.username}

    @app.get("/api/plan")
    async def plan(p: Annotated[Principal, Depends(require("view"))]) -> dict[str, str]:
        return {"who": p.username}

    @app.post("/api/kill")
    async def kill(p: Annotated[Principal, Depends(require("kill_off"))]) -> dict[str, str]:
        return {"who": p.username}

    return auth, clk, TestClient(app)


def login(auth: Auth, c: TestClient, user: str) -> str:
    tok, csrf = auth.login(user, PW, ip="10.0.0.2")
    c.cookies.set(COOKIE, tok)
    return csrf


@pytest.mark.brd("NFR-SEC-2")
def test_argon2_and_min_length() -> None:
    h = hash_pw(PW)
    assert h.startswith("$argon2id$") and verify_pw(h, PW) and not verify_pw(h, "nope1234")
    assert not verify_pw("garbage", PW)
    with pytest.raises(ValueError):
        hash_pw("short")


@pytest.mark.brd("NFR-SEC-2")
def test_lockout_after_5_failures(tmp_path: Path) -> None:
    auth, clk, _ = mk(tmp_path)
    for _ in range(5):
        with pytest.raises(LoginError) as e:
            auth.login("alex", "wrong-pass", ip="1.1.1.1")
        assert e.value.status == 401
    with pytest.raises(LoginError) as e:
        auth.login("alex", PW, ip="1.1.1.2")  # right password, still locked
    assert e.value.status == 423
    clk.t += timedelta(minutes=15, seconds=1)
    assert auth.login("alex", PW, ip="1.1.1.2")[0]
    n = auth.conn.execute("SELECT COUNT(*) FROM audit WHERE action='login_failed'").fetchone()[0]
    assert n == 5


@pytest.mark.brd("NFR-SEC-2")
def test_ip_rate_limit_429(tmp_path: Path) -> None:
    auth, clk, _ = mk(tmp_path)
    for i in range(20):
        with pytest.raises(LoginError):
            auth.login(f"ghost{i}", "x" * 8, ip="9.9.9.9")
    with pytest.raises(LoginError) as e:
        auth.login("alex", PW, ip="9.9.9.9")
    assert e.value.status == 429
    clk.t += timedelta(hours=1, seconds=1)
    assert auth.login("alex", PW, ip="9.9.9.9")[0]


@pytest.mark.brd("NFR-SEC-1")
def test_session_expiry_and_logout(tmp_path: Path) -> None:
    auth, clk, _ = mk(tmp_path)
    tok, _ = auth.login("mel", PW)
    assert auth.session(tok) is not None
    clk.t += timedelta(hours=12, seconds=1)
    assert auth.session(tok) is None
    tok2, _ = auth.login("mel", PW, trusted=True)
    clk.t += timedelta(days=29)
    assert auth.session(tok2) is not None
    auth.logout(tok2)
    assert auth.session(tok2) is None
    stored = auth.conn.execute("SELECT token_hash FROM sessions").fetchall()
    assert all(r[0] != tok2 for r in stored)  # only hashes stored


@pytest.mark.brd("NFR-SEC-3")
def test_csrf_missing_or_wrong_is_403(tmp_path: Path) -> None:
    auth, _, c = mk(tmp_path)
    csrf = login(auth, c, "ann")
    assert c.post("/api/kill").status_code == 403
    assert c.post("/api/kill", headers={CSRF_HEADER: "wrong"}).status_code == 403
    assert c.post("/api/kill", headers={CSRF_HEADER: csrf}).status_code == 200


@pytest.mark.brd("FR-USR-1")
def test_require_cap_and_401(tmp_path: Path) -> None:
    auth, _, c = mk(tmp_path)
    assert c.get("/api/plan").status_code == 401
    csrf = login(auth, c, "mel")
    assert c.get("/api/plan").status_code == 200
    assert c.post("/api/kill", headers={CSRF_HEADER: csrf}).status_code == 403
    assert CAPS["owner"] >= CAPS["admin"] >= CAPS["member"]


@pytest.mark.brd("NFR-SEC-4")
def test_bob_token_scope(tmp_path: Path) -> None:
    _, _, c = mk(tmp_path)
    h = {"Authorization": f"Bearer {BOB}"}
    assert c.get("/healthz", headers=h).status_code == 200
    assert c.get("/api/status", headers=h).json() == {"who": "bob"}
    assert c.get("/api/plan", headers=h).status_code == 403
    assert c.post("/api/kill", headers=h).status_code == 403
    assert c.get("/api/plan", headers={"Authorization": "Bearer nope"}).status_code == 401


@pytest.mark.brd("FR-USR-1")
def test_create_user_cli(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "c.db", now=NOW)
    answers = iter([PW, PW])
    uid = cli_create_user(["alex", "--role", "owner"], conn, lambda _: next(answers))
    r = conn.execute("SELECT username, role, pw_hash FROM users WHERE id=?", (uid,)).fetchone()
    assert (r[0], r[1]) == ("alex", "owner") and verify_pw(r[2], PW)
    bad = iter([PW, "different!"])
    with pytest.raises(SystemExit):
        cli_create_user(["x", "--role", "member"], conn, lambda _: next(bad))
