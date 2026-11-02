"""T1.35: kill-switch API flow (FR-KILL-1/3/4/5, D§11.2) against the mock HA."""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.house import HOUSE
from tests.integration.mock_ha import BOT_USER_ID, GOOD_TOKEN, MockHA
from tests.integration.test_api import NOW, login, make
from tests.integration.test_controller import Sleeps
from thermabot.adapters import db
from thermabot.adapters.ha_rest import HAError, HARest
from thermabot.app.controller import Controller
from thermabot.app.routes_api import Ctx
from thermabot.core.statemachine import Persisted

TSTAT = "climate.living_room"


def wire(tmp: Path, hvac: str = "off") -> tuple[Ctx, MockHA, TestClient, list[str]]:
    ctx, _, c = make(tmp)
    ha = MockHA()
    ha.states[TSTAT]["state"] = hvac
    rest = HARest(
        "http://ha", GOOD_TOKEN, transport=httpx.ASGITransport(app=ha.app), writable_entity=TSTAT
    )
    ctl = Controller(ctx.conn, rest, settings=HOUSE, bot_user_id=BOT_USER_ID, sleep=Sleeps())
    order: list[str] = []

    async def restore(now: Any) -> str:
        p = db.read_bot_state(ctx.conn)
        order.append(f"restore(kill={p.kill_switch if p else None})")
        return await ctl.kill_restore(now)

    async def mirror(on: bool) -> None:
        order.append(f"mirror({on})")
        await rest.set_mirror(on)

    ctx.kill_restore, ctx.set_mirror = restore, mirror
    return ctx, ha, c, order


def climate_calls(ha: MockHA) -> list[dict[str, Any]]:
    return [x for x in ha.calls if x["domain"] == "climate"]


@pytest.mark.brd("FR-KILL-3")
def test_off_restore_band_one_write_then_off(tmp_path: Path) -> None:
    ctx, ha, c, order = wire(tmp_path, "off")
    h = login(c, "ann")
    t0 = time.monotonic()
    r = c.post("/api/kill", json={"enabled": False, "restore": "band"}, headers=h)
    assert time.monotonic() - t0 < 5
    j = r.json()
    assert r.status_code == 200 and j["state"] == "DISABLED" and j["restore"] == "sent"
    cc = climate_calls(ha)
    # T1.44: leaving OFF = explicit set_hvac_mode, then setpoints
    assert [x["service"] for x in cc] == ["set_hvac_mode", "set_temperature"]
    assert cc[0]["data"]["hvac_mode"] == "heat_cool"
    assert cc[1]["data"]["target_temp_low"] == 68 and cc[1]["data"]["target_temp_high"] == 76
    assert order == ["restore(kill=on)", "mirror(False)"]  # restore BEFORE OFF
    assert ha.states["input_boolean.thermabot_enabled"]["state"] == "off"
    p = db.read_bot_state(ctx.conn)
    assert p is not None and p.kill_switch == "off"
    rows = ctx.conn.execute("SELECT outcome, is_safety_restore FROM writes").fetchall()
    assert [tuple(x) for x in rows] == [("sent", 1)]
    a = ctx.conn.execute(
        "SELECT actor, action, detail_json FROM audit WHERE action='kill_off'"
    ).fetchone()
    assert a["actor"] == "ann" and '"restore": "sent"' in a["detail_json"]


@pytest.mark.brd("FR-KILL-3")
def test_off_leave_no_write(tmp_path: Path) -> None:
    ctx, ha, c, order = wire(tmp_path, "off")
    h = login(c, "ann")
    r = c.post("/api/kill", json={"enabled": False, "restore": "leave"}, headers=h)
    assert r.json()["restore"] == "skipped" and r.json()["state"] == "DISABLED"
    assert climate_calls(ha) == [] and order == ["mirror(False)"]


@pytest.mark.brd("FR-KILL-3")
def test_off_restore_not_needed_when_running(tmp_path: Path) -> None:
    _, ha, c, _ = wire(tmp_path, "heat")
    h = login(c, "ann")
    r = c.post("/api/kill", json={"enabled": False}, headers=h)
    assert r.json()["restore"] == "not_needed" and climate_calls(ha) == []


@pytest.mark.brd("FR-KILL-3")
def test_restore_failure_still_turns_off(tmp_path: Path) -> None:
    ctx, ha, c, _ = wire(tmp_path, "off")
    ha.climate_forbidden = True
    h = login(c, "ann")
    r = c.post("/api/kill", json={"enabled": False}, headers=h)
    assert r.json()["restore"] == "failed" and r.json()["kill_switch"] == "off"
    p = db.read_bot_state(ctx.conn)
    assert p is not None and p.kill_switch == "off"


@pytest.mark.brd("NFR-SAFE-6")
@pytest.mark.brd("FR-KILL-1")
@pytest.mark.parametrize(
    "failure", ["missing", "ha_error", "http_error", "timeout", "unknown", "unavailable", ""]
)
def test_unreadable_restore_never_writes_and_still_turns_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    ctx, ha, c, _ = wire(tmp_path, "off")
    if failure == "missing":
        del ha.states[TSTAT]
    elif failure in ("unknown", "unavailable", ""):
        ha.states[TSTAT]["state"] = failure
    else:
        original = HARest.get_state
        failed = False

        async def transient_failure(rest: HARest, entity_id: str) -> dict[str, Any] | None:
            nonlocal failed
            if entity_id == TSTAT and not failed:
                failed = True
                if failure == "ha_error":
                    raise HAError(503, "state read unavailable")
                if failure == "http_error":
                    raise httpx.ReadError("state read interrupted")
                raise TimeoutError("state read timed out")
            return await original(rest, entity_id)

        monkeypatch.setattr(HARest, "get_state", transient_failure)
    h = login(c, "ann")
    t0 = time.monotonic()
    r = c.post("/api/kill", json={"enabled": False, "restore": "band"}, headers=h)
    assert time.monotonic() - t0 < 5
    assert r.status_code == 200
    assert climate_calls(ha) == []
    assert r.json()["restore"] == "failed" and r.json()["state"] == "DISABLED"
    p = db.read_bot_state(ctx.conn)
    assert p is not None and p.kill_switch == "off"
    assert ha.states["input_boolean.thermabot_enabled"]["state"] == "off"
    audit = ctx.conn.execute("SELECT detail_json FROM audit WHERE action='kill_off'").fetchone()
    assert audit is not None and '"restore": "failed"' in audit["detail_json"]


@pytest.mark.brd("FR-KILL-4")
def test_on_requires_mode_and_owner(tmp_path: Path) -> None:
    ctx, _, c, _ = wire(tmp_path)
    h = login(c, "alex")
    c.post("/api/kill", json={"enabled": False, "restore": "leave"}, headers=h)
    assert c.post("/api/kill", json={"enabled": True}, headers=h).status_code == 422
    ha = login(c, "ann")
    assert (
        c.post("/api/kill", json={"enabled": True, "mode": "shadow"}, headers=ha).status_code == 403
    )
    h = login(c, "alex")
    r = c.post("/api/kill", json={"enabled": True, "mode": "shadow"}, headers=h)
    assert r.status_code == 200 and r.json()["state"] == "SHADOW"
    assert r.json()["mirror_ok"] is True


@pytest.mark.brd("FR-MODE-3")
def test_active_blocked_before_7_days(tmp_path: Path) -> None:
    ctx, _, c, _ = wire(tmp_path)
    h = login(c, "alex")
    r = c.post("/api/kill", json={"enabled": True, "mode": "active"}, headers=h)
    assert r.status_code == 409 and "7 d" in r.json()["detail"]
    p = db.read_bot_state(ctx.conn)
    assert p is not None and p.run_mode == "shadow"
    assert (
        ctx.conn.execute("SELECT COUNT(*) FROM audit WHERE action='kill_on_denied'").fetchone()[0]
        == 1
    )
    db.write_bot_state(
        ctx.conn,
        Persisted(
            kill_switch="on", run_mode="shadow", shadow_started_at=NOW - timedelta(days=7, hours=1)
        ),
        by="test",
        now=NOW,
    )
    r = c.post("/api/kill", json={"enabled": True, "mode": "active"}, headers=h)
    assert r.status_code == 200 and r.json()["state"] == "ACTIVE"


@pytest.mark.brd("FR-KILL-1")
def test_mirror_failure_still_disabled(tmp_path: Path) -> None:
    ctx, _, c, _ = wire(tmp_path, "heat")

    async def broken(on: bool) -> None:
        raise ConnectionError

    ctx.set_mirror = broken
    h = login(c, "ann")
    r = c.post("/api/kill", json={"enabled": False}, headers=h)
    assert r.json()["state"] == "DISABLED" and r.json()["mirror_ok"] is False


@pytest.mark.brd("FR-KILL-3")
def test_kill_restore_from_off_survives_trane_quirk(tmp_path: Path) -> None:
    ctx, ha, c, _ = wire(tmp_path, "off")
    ha.trane_off_quirk = True
    h = login(c, "ann")
    r = c.post("/api/kill", json={"enabled": False, "restore": "band"}, headers=h)
    assert r.status_code == 200 and r.json()["restore"] == "sent"
    cc = climate_calls(ha)
    assert [x["service"] for x in cc] == ["set_hvac_mode", "set_temperature"]
    assert ha.states[TSTAT]["state"] == "heat_cool"
    a = ha.states[TSTAT]["attributes"]
    assert a["target_temp_low"] == 68 and a["target_temp_high"] == 76
