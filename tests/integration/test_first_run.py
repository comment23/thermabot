"""First Owner bootstrap, CSRF, atomic completion, and configuration-aware routing."""

from __future__ import annotations

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.house import HOUSE
from tests.integration.test_api import NOW, PW, Clock
from thermabot.adapters import db
from thermabot.app.auth import (
    COOKIE,
    CSRF_HEADER,
    FIRST_RUN_COOKIE,
    FIRST_RUN_KEY,
    FIRST_RUN_TTL,
    Auth,
    LoginError,
    Role,
    create_user,
    verify_pw,
)
from thermabot.app.routes_api import Ctx, _kv_put, create_app, load_settings

pytestmark = pytest.mark.brd("FR-USR-1")


def fresh(tmp: Path) -> tuple[Ctx, Clock, TestClient]:
    clock = Clock()
    conn = db.open_db(tmp / "first-run.db", now=NOW)
    auth = Auth(conn, clock=clock)
    ctx = Ctx(conn=conn, auth=auth, clock=clock, last_tick=NOW, ha_health="ok")
    return ctx, clock, TestClient(create_app(ctx))


def form(client: TestClient, path: str = "/first-run") -> dict[str, str]:
    response = client.get(path)
    assert response.status_code == 200
    token = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
    assert token is not None
    return {CSRF_HEADER: token[1]}


def account(username: str = "alex") -> dict[str, str]:
    return {"username": username, "password": PW, "confirm": PW}


def test_fresh_install_creates_owner_then_opens_wizard(tmp_path: Path) -> None:
    ctx, _, client = fresh(tmp_path)
    response = client.get("/")
    assert response.url.path == "/first-run"
    headers = form(client)
    before = db.read_bot_state(ctx.conn)
    response = client.post("/api/first-run", headers=headers, json=account())
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.cookies.get(COOKIE)
    assert response.cookies.get(FIRST_RUN_COOKIE) is None
    assert FIRST_RUN_COOKIE not in client.cookies
    row = ctx.conn.execute("SELECT * FROM users").fetchone()
    assert (row["username"], row["role"]) == ("alex", "owner")
    assert row["pw_hash"] != PW and verify_pw(row["pw_hash"], PW)
    assert client.get("/first-run").url.path == "/setup"
    assert client.get("/login").url.path == "/setup"
    assert client.get("/api/users").status_code == 200
    assert db.read_kv(ctx.conn, FIRST_RUN_KEY) == "1"
    assert db.read_bot_state(ctx.conn) == before
    assert ctx.eff().state == "DISABLED"
    assert ctx.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0
    audit = [dict(r) for r in ctx.conn.execute("SELECT * FROM audit")]
    assert any(r["action"] == "initial_owner_created" for r in audit)
    assert PW not in json.dumps(audit) and row["pw_hash"] not in json.dumps(audit)
    assert client.post("/api/first-run", headers=headers, json=account("sam")).status_code == 409


@pytest.mark.parametrize("role", ["owner", "admin", "member"])
def test_any_existing_user_closes_registration(tmp_path: Path, role: Role) -> None:
    ctx, _, client = fresh(tmp_path)
    headers = form(client)
    create_user(ctx.conn, "existing", role, PW)
    assert client.get("/login").url.path == "/login"
    assert client.get("/first-run").url.path == "/login"
    response = client.post("/api/first-run", headers=headers, json=account())
    assert response.status_code == 409
    assert ctx.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


@pytest.mark.parametrize(
    "kind",
    ["missing", "no-cookie", "wrong", "forged", "tampered", "expired", "restart", "cross-site"],
)
def test_setup_requires_current_signed_csrf(tmp_path: Path, kind: str) -> None:
    ctx, clock, client = fresh(tmp_path)
    headers = form(client)
    if kind == "missing":
        headers = {}
    elif kind == "no-cookie":
        client.cookies.clear()
    elif kind == "wrong":
        headers[CSRF_HEADER] = "wrong"
    elif kind == "forged":
        client.cookies.clear()
        client.cookies.set(FIRST_RUN_COOKIE, "forged")
        headers[CSRF_HEADER] = "forged"
    elif kind == "tampered":
        tampered = headers[CSRF_HEADER][:-1] + "!"
        client.cookies.clear()
        client.cookies.set(FIRST_RUN_COOKIE, tampered)
        headers[CSRF_HEADER] = tampered
    elif kind == "expired":
        clock.t += FIRST_RUN_TTL + timedelta(seconds=1)
    elif kind == "restart":
        ctx.auth = Auth(ctx.conn, clock=clock)
        client = TestClient(create_app(ctx), cookies=client.cookies)
    else:
        headers["Sec-Fetch-Site"] = "cross-site"
    response = client.post("/api/first-run", headers=headers, json=account())
    assert response.status_code == 403
    assert ctx.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert db.read_kv(ctx.conn, FIRST_RUN_KEY) is None


def test_existing_auth_rate_limit_also_blocks_registration(tmp_path: Path) -> None:
    ctx, _, client = fresh(tmp_path)
    headers = form(client)
    for _ in range(20):
        with pytest.raises(LoginError):
            ctx.auth.login("missing", PW, ip="testclient")
    response = client.post("/api/first-run", headers=headers, json=account())
    assert response.status_code == 429
    assert ctx.auth.needs_first_account()


@pytest.mark.parametrize(
    "change",
    [
        {"username": ""},
        {"username": " "},
        {"username": "../alex"},
        {"username": "x" * 65},
        {"password": "short"},
        {"password": "x" * 129},
        {"confirm": "different password"},
        {"role": "owner"},
        {"password": {"secret": "do-not-echo-this-password"}},
    ],
)
def test_invalid_accounts_are_rejected_without_echoing_passwords(
    tmp_path: Path, change: dict[str, object]
) -> None:
    ctx, _, client = fresh(tmp_path)
    headers = form(client)
    response = client.post("/api/first-run", headers=headers, json={**account(), **change})
    assert response.status_code == 422
    assert PW not in response.text and "do-not-echo-this-password" not in response.text
    assert ctx.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert ctx.eff().state == "DISABLED"


def test_body_type_size_and_json_errors_are_explicit(tmp_path: Path) -> None:
    _, _, client = fresh(tmp_path)
    headers = form(client)
    assert client.post("/api/first-run", headers=headers, data=account()).status_code == 415
    headers["Content-Type"] = "application/json"
    response = client.post("/api/first-run", headers=headers, content='{"password":"secret"')
    assert response.status_code == 422 and "secret" not in response.text
    assert client.post("/api/first-run", headers=headers, json="x" * 4097).status_code == 413


@pytest.mark.parametrize("stored", [True, False])
def test_existing_configuration_skips_wizard_and_is_preserved(tmp_path: Path, stored: bool) -> None:
    ctx, _, client = fresh(tmp_path)
    if stored:
        _kv_put(ctx, "settings", HOUSE.model_dump(mode="json"), "existing")
    else:
        ctx.settings = HOUSE  # legacy effective configuration without a saved row
    before = load_settings(ctx)
    headers = form(client)
    assert client.post("/api/first-run", headers=headers, json=account()).status_code == 201
    assert client.get("/first-run").url.path == "/"
    assert client.get("/login").url.path == "/"
    assert load_settings(ctx) == before
    assert ctx.eff().state == "DISABLED"


def test_saved_partial_configuration_does_not_restart_wizard(tmp_path: Path) -> None:
    ctx, _, client = fresh(tmp_path)
    _kv_put(ctx, "settings", ctx.settings.model_dump(mode="json"), "existing")
    assert client.post("/api/first-run", headers=form(client), json=account()).status_code == 201
    assert client.get("/first-run").url.path == "/"
    assert not load_settings(ctx).thermostat.configured


def test_cli_owner_without_configuration_also_gets_wizard(tmp_path: Path) -> None:
    ctx, _, client = fresh(tmp_path)
    create_user(ctx.conn, "alex", "owner", PW)
    assert client.post("/login", json={"username": "alex", "password": PW}).status_code == 200
    assert client.get("/login").url.path == "/setup"
    _kv_put(ctx, "settings", HOUSE.model_dump(mode="json"), "alex")
    assert client.get("/login").url.path == "/"
    assert client.get("/setup").status_code == 200  # still available explicitly


def test_setup_page_cookie_security_and_ingress_paths(tmp_path: Path) -> None:
    ctx, _, _ = fresh(tmp_path)
    prefix = "/api/hassio_ingress/test"
    client = TestClient(
        create_app(ctx), base_url="https://testserver", headers={"X-Ingress-Path": prefix}
    )
    path = prefix + "/first-run"
    response = client.get(path)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert response.headers["referrer-policy"] == "no-referrer"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert f"Path={prefix}" in cookie and "Max-Age=900" in cookie
    assert 'data-api="POST api/first-run"' in response.text
    headers = form(client, path)
    response = client.post(prefix + "/api/first-run", headers=headers, json=account())
    assert response.status_code == 201
    assert FIRST_RUN_COOKIE not in client.cookies
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "setup"


def test_registration_cannot_reopen_after_restart_or_account_removal(tmp_path: Path) -> None:
    ctx, _, client = fresh(tmp_path)
    headers = form(client)
    assert client.post("/api/first-run", headers=headers, json=account()).status_code == 201
    ctx.conn.execute("DELETE FROM sessions")
    ctx.conn.execute("DELETE FROM users")
    ctx.conn.close()
    ctx, _, client = fresh(tmp_path)
    assert not ctx.auth.needs_first_account()
    assert client.get("/first-run").url.path == "/login"
    assert client.post("/api/first-run", headers=headers, json=account()).status_code == 409


def test_creation_rolls_back_if_audit_fails(tmp_path: Path) -> None:
    ctx, _, client = fresh(tmp_path)
    headers = form(client)
    with patch("thermabot.app.auth.db.audit", side_effect=sqlite3.OperationalError("disk full")):
        with pytest.raises(sqlite3.OperationalError):
            client.post("/api/first-run", headers=headers, json=account())
    assert ctx.auth.needs_first_account()
    for table in ("users", "sessions", "audit"):
        assert ctx.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert db.read_kv(ctx.conn, FIRST_RUN_KEY) is None
    assert client.post("/api/first-run", headers=headers, json=account()).status_code == 201


def test_two_connections_cannot_create_two_initial_owners(tmp_path: Path) -> None:
    ctx, clock, _ = fresh(tmp_path)
    other_conn = db.open_db(tmp_path / "first-run.db", now=NOW)
    other = Auth(other_conn, clock=clock)
    barrier = Barrier(2)

    def create(auth: Auth, name: str) -> int:
        barrier.wait(timeout=5)
        try:
            auth.create_first_owner(name, PW, "127.0.0.1")
        except LoginError as exc:
            return exc.status
        return 201

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(create, ctx.auth, "alex")
            two = pool.submit(create, other, "sam")
            assert sorted([one.result(timeout=10), two.result(timeout=10)]) == [201, 409]
        assert ctx.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert ctx.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    finally:
        other_conn.close()
