"""T1.34: table-driven (route x role) allow/deny for every D§10 / BRD §5.11 cell, through
the API, plus "can't delete the last Owner"."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.integration.test_api import BOB, login, make
from thermabot.app.auth import CAPS

ROLES = ("owner", "admin", "member")
USER = {"owner": "alex", "admin": "ann", "member": "jordan"}

# (method, path, json body, cap). Bodies are valid so "allowed" never ends in 401/403.
ROUTES: list[tuple[str, str, dict[str, Any] | None, str]] = [
    ("GET", "/api/status", None, "view"),
    ("GET", "/api/plan", None, "view"),
    ("GET", "/api/history?days=7", None, "view"),
    ("POST", "/api/nudge", {"delta_f": 1, "hours": 1}, "nudge_own"),
    ("POST", "/api/nudge", {"room": "loft", "delta_f": 1, "hours": 1}, "nudge_any"),
    ("GET", "/api/health", None, "view_health"),
    ("GET", "/api/audit", None, "view_health"),
    ("POST", "/api/kill", {"enabled": False, "restore": "leave"}, "kill_off"),
    ("POST", "/api/kill", {"enabled": True, "mode": "shadow"}, "kill_on"),
    ("POST", "/api/override", {"target_f": 70, "duration": "1h"}, "override"),
    ("DELETE", "/api/override", None, "override"),
    ("POST", "/api/guest", {"on": True}, "guest_toggle"),
    ("GET", "/api/settings", None, "edit_comfort"),
    ("GET", "/api/model", None, "view_model"),
    ("POST", "/api/model/refit", None, "view_model"),
    ("POST", "/api/alerts/test", None, "view_model"),
    ("POST", "/api/fan-experiment", {"override_id": 1}, "view_model"),
    ("GET", "/api/users", None, "manage_users"),
    (
        "POST",
        "/api/users",
        {"username": "zed", "role": "member", "password": "x" * 8},
        "manage_users",
    ),
    ("PATCH", "/api/users/3", {"room": "jordan"}, "manage_users"),
    ("DELETE", "/api/users/2", None, "manage_users"),
    ("POST", "/api/me/password", {"current": "x", "new": "y" * 8, "confirm": "y" * 8}, "view"),
]

# BRD §5.11 matrix, written out independently of auth.CAPS so a CAPS edit is caught.
MATRIX = {
    "view": ROLES,
    "nudge_own": ROLES,
    "view_health": ("owner", "admin"),
    "kill_off": ("owner", "admin"),
    "override": ("owner", "admin"),
    "edit_comfort": ("owner", "admin"),
    "guest_toggle": ("owner", "admin"),
    "nudge_any": ("owner", "admin"),
    "view_model": ("owner",),
    "manage_users": ("owner",),
    "edit_safety": ("owner",),
    "kill_on": ("owner",),
    "set_active": ("owner",),
}
CELLS = [(m, p, b, cap, role) for (m, p, b, cap) in ROUTES for role in ROLES]


@pytest.mark.brd("FR-USR-1")
def test_matrix_matches_caps() -> None:
    for cap, roles in MATRIX.items():
        for role in ROLES:
            assert (cap in CAPS[role]) == (role in roles), (cap, role)
    assert {cap for *_, cap in ROUTES} | {"edit_safety", "set_active"} == set(MATRIX)


@pytest.mark.brd("FR-USR-1")
@pytest.mark.parametrize(
    ("method", "path", "body", "cap", "role"),
    CELLS,
    ids=[f"{m} {p} {c} {r}" for m, p, _, c, r in CELLS],
)
def test_route_role(
    tmp_path: Path, method: str, path: str, body: dict[str, Any] | None, cap: str, role: str
) -> None:
    ctx, _, c = make(tmp_path)
    ctx.conn.execute(
        "INSERT INTO overrides (kind, source, payload_json, started_at,"
        " expires_at) VALUES ('ui_pause','t','{}','2026-01-01','2026-01-02')"
    )
    h = login(c, USER[role])
    r = c.request(method, path, json=body, headers=h)
    if role in MATRIX[cap]:
        assert r.status_code not in (401, 403), r.text
    else:
        assert r.status_code == 403, r.text


@pytest.mark.brd("FR-USR-1")
@pytest.mark.parametrize(("method", "path", "body", "cap"), ROUTES)
def test_anonymous_401(
    tmp_path: Path, method: str, path: str, body: dict[str, Any] | None, cap: str
) -> None:
    _, _, c = make(tmp_path)
    assert c.request(method, path, json=body).status_code == 401


@pytest.mark.brd("NFR-SEC-4")
@pytest.mark.parametrize(("method", "path", "body", "cap"), ROUTES)
def test_bob_token_only_status(
    tmp_path: Path, method: str, path: str, body: dict[str, Any] | None, cap: str
) -> None:
    _, _, c = make(tmp_path)
    r = c.request(method, path, json=body, headers={"Authorization": f"Bearer {BOB}"})
    assert r.status_code == (200 if path == "/api/status" else 403)


@pytest.mark.brd("FR-USR-1")
def test_cant_delete_or_demote_last_owner(tmp_path: Path) -> None:
    _, _, c = make(tmp_path)
    h = login(c, "alex")
    assert c.delete("/api/users/1", headers=h).status_code == 409
    assert c.patch("/api/users/1", json={"role": "member"}, headers=h).status_code == 409
    c.patch("/api/users/2", json={"role": "owner"}, headers=h)  # second owner
    assert c.delete("/api/users/1", headers=h).status_code == 200
