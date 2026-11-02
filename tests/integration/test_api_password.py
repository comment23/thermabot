"""v0.1.6: change own password (any role) + Owner resets another user's password."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.integration.test_api import BOB, PW, login, make
from thermabot.app.auth import CSRF_HEADER
from thermabot.app.routes_api import create_app

NEW = "a brand new pw"


def _acts(ctx, actor: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [r[0] for r in ctx.conn.execute("SELECT action FROM audit WHERE actor=?", (actor,))]


@pytest.mark.brd("FR-USR-1")
@pytest.mark.parametrize("user", ["alex", "ann", "jordan"])
def test_any_role_changes_own_password(tmp_path: Path, user: str) -> None:
    ctx, _, c = make(tmp_path)
    other = TestClient(create_app(ctx))  # a second device, signed in as the same user
    login(other, user)
    h = login(c, user)
    body = {"current": PW, "new": NEW, "confirm": NEW}
    r = c.post("/api/me/password", json=body, headers=h)
    assert r.status_code == 200, r.text
    assert c.get("/api/status").status_code == 200  # this device stays signed in
    assert other.get("/api/status").status_code == 401  # other devices are signed out
    assert c.post("/login", json={"username": user, "password": PW}).status_code == 401
    assert c.post("/login", json={"username": user, "password": NEW}).status_code == 200
    assert "password_changed" in _acts(ctx, user)
    row = ctx.conn.execute("SELECT detail_json FROM audit WHERE action='password_changed'")
    assert NEW not in str(row.fetchone()[0]) and PW not in str(
        ctx.conn.execute("SELECT * FROM audit").fetchall()
    )


@pytest.mark.brd("FR-USR-1")
@pytest.mark.parametrize(
    ("body", "status", "msg"),
    [
        ({"current": "wrong wrong", "new": NEW, "confirm": NEW}, 400, "current password is wrong"),
        ({"current": PW, "new": NEW, "confirm": NEW + "x"}, 422, "don't match"),
        ({"current": PW, "new": "short", "confirm": "short"}, 422, "at least 8"),
        ({"current": PW, "new": PW, "confirm": PW}, 422, "must differ"),
    ],
)
def test_change_password_rejects(
    tmp_path: Path, body: dict[str, str], status: int, msg: str
) -> None:
    ctx, _, c = make(tmp_path)
    h = login(c, "jordan")
    r = c.post("/api/me/password", json=body, headers=h)
    assert r.status_code == status and msg in r.text, r.text
    assert c.post("/login", json={"username": "jordan", "password": PW}).status_code == 200


@pytest.mark.brd("NFR-SEC-1")
def test_change_password_needs_session_and_csrf(tmp_path: Path) -> None:
    _, _, c = make(tmp_path)
    body = {"current": PW, "new": NEW, "confirm": NEW}
    assert c.post("/api/me/password", json=body).status_code == 401
    bob = {"Authorization": f"Bearer {BOB}"}
    assert c.post("/api/me/password", json=body, headers=bob).status_code == 403
    login(c, "jordan")
    assert c.post("/api/me/password", json=body).status_code == 403  # no CSRF header
    assert c.post("/api/me/password", json=body, headers={CSRF_HEADER: "nope"}).status_code == 403


@pytest.mark.brd("FR-USR-1")
def test_owner_resets_other_user_password(tmp_path: Path) -> None:
    ctx, _, c = make(tmp_path)
    jordan = TestClient(create_app(ctx))
    login(jordan, "jordan")
    h = login(c, "alex")
    assert c.patch("/api/users/3", json={"password": NEW}, headers=h).status_code == 200
    assert jordan.get("/api/status").status_code == 401  # her sessions were ended
    assert jordan.post("/login", json={"username": "jordan", "password": NEW}).status_code == 200
    assert "password_reset" in _acts(ctx, "alex")


@pytest.mark.brd("FR-USR-2")
@pytest.mark.parametrize("user", ["ann", "jordan"])
def test_only_owner_resets_others(tmp_path: Path, user: str) -> None:
    _, _, c = make(tmp_path)
    h = login(c, user)
    assert c.patch("/api/users/1", json={"password": NEW}, headers=h).status_code == 403
