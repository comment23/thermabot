"""T1.35: kill-switch API flow (FR-KILL-1/3/4/5, D§11.2) against the mock HA."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from tests.house import HOUSE, seed_house
from tests.integration.mock_ha import BOT_USER_ID, GOOD_TOKEN, MockHA
from tests.integration.test_api import NOW, login, make
from tests.integration.test_controller import STEP, Sleeps, snap
from tests.integration.test_main import env
from thermabot.adapters import db
from thermabot.adapters.ha_rest import HAError, HARest
from thermabot.app import jobs as J
from thermabot.app.auth import Principal
from thermabot.app.controller import Controller
from thermabot.app.main import build, load_env
from thermabot.app.routes_api import (
    Ctx,
    NoticeIn,
    _kill,
    acknowledge_notice,
    kill,
    load_settings,
    settings_put,
    shadow_evidence,
    stop_warning_of,
)
from thermabot.app.setup_api import save as setup_save
from thermabot.config import Settings, StopHandoffCfg
from thermabot.core.statemachine import Live, Persisted
from thermabot.core.thermal import ThermalParams
from thermabot.core.types import KillIn, Plan, RestoreStatus

TSTAT = "climate.living_room"


def wire(
    tmp: Path, hvac: str = "off", *, active: bool = False
) -> tuple[Ctx, MockHA, TestClient, list[str]]:
    ctx, _, c = make(tmp)
    ha = MockHA()
    ha.states[TSTAT]["state"] = hvac
    if active:
        db.write_bot_state(
            ctx.conn,
            Persisted(kill_switch="on", run_mode="active", shadow_started_at=NOW),
            by="test",
            now=NOW,
        )
        ctx.settings = HOUSE.model_copy(update={"stop_handoff": StopHandoffCfg(enabled=True)})
        ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    rest = HARest(
        "http://ha", GOOD_TOKEN, transport=httpx.ASGITransport(app=ha.app), writable_entity=TSTAT
    )
    ctl = Controller(ctx.conn, rest, settings=HOUSE, bot_user_id=BOT_USER_ID, sleep=Sleeps())
    order: list[str] = []

    async def restore(now: Any, settings: Settings) -> tuple[RestoreStatus, str]:
        p = db.read_bot_state(ctx.conn)
        order.append(f"restore(kill={p.kill_switch if p else None})")
        return await ctl.kill_restore(now, settings)

    async def mirror(on: bool) -> None:
        order.append(f"mirror({on})")
        await rest.set_mirror(on)

    ctx.kill_restore, ctx.set_mirror = restore, mirror
    return ctx, ha, c, order


def climate_calls(ha: MockHA) -> list[dict[str, Any]]:
    return [x for x in ha.calls if x["domain"] == "climate"]


def ready_for_control(ctx: Ctx) -> None:
    ctx.settings = HOUSE.model_copy(
        update={"stop_handoff": StopHandoffCfg(enabled=True, heat_f=66, cool_f=74)}
    )
    ctx.snapshot = snap()
    ctx.plan = Plan(kind="fallback", created_at=NOW, steps=[STEP])
    ctx.heartbeat_configured = True
    db.write_bot_state(
        ctx.conn,
        Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=NOW - timedelta(days=8)),
        by="test",
        now=NOW,
    )
    J.save_model(ctx.conn, ThermalParams(), 1.0, 100, NOW)
    sample = json.dumps({"_thermabot": {"state": "SHADOW", "degraded_reason": None}})
    start = NOW - timedelta(days=7)
    ctx.conn.execute("BEGIN")
    ctx.conn.executemany(
        "INSERT INTO samples (ts, snapshot_json, bot_state) VALUES (?, ?, 'SHADOW')",
        (
            ((start + timedelta(minutes=minute)).isoformat(), sample)
            for minute in range(7 * 24 * 60)
        ),
    )
    ctx.conn.execute("COMMIT")


CONTROL_APPROVAL = {
    "enabled": True,
    "mode": "active",
    "confirmation": "ACTIVE",
    "reviewed_shadow": True,
    "reviewed_safety_net": True,
}


@pytest.mark.brd("FR-KILL-3")
def test_off_restore_band_one_write_then_off(tmp_path: Path) -> None:
    ctx, ha, c, order = wire(tmp_path, "off", active=True)
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
    assert [tuple(x) for x in rows] == [("sent", 0)]
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


@pytest.mark.brd("FR-MODE-2")
def test_stopping_shadow_never_restores_the_thermostat(tmp_path: Path) -> None:
    ctx, ha, c, _ = wire(tmp_path, "off")
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    h = login(c, "ann")
    r = c.post("/api/kill", json={"enabled": False, "restore": "band"}, headers=h)
    assert r.status_code == 200 and r.json()["restore"] == "skipped"
    assert r.json()["state"] == "DISABLED"
    assert climate_calls(ha) == []
    state = db.read_bot_state(ctx.conn)
    assert state is not None and state.kill_switch == "off"
    assert state.shadow_started_at is None


@pytest.mark.brd("FR-KILL-3")
def test_stop_hands_running_thermostat_to_owner_band(tmp_path: Path) -> None:
    ctx, ha, c, _ = wire(tmp_path, "heat_cool")
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    owner = login(c, "alex")
    settings = c.get("/api/settings").json()
    settings["stop_handoff"] = {"enabled": True, "heat_f": 66, "cool_f": 74}
    assert c.put("/api/settings", json=settings, headers=owner).status_code == 200
    db.write_bot_state(
        ctx.conn,
        Persisted(kill_switch="on", run_mode="active", shadow_started_at=NOW),
        by="test",
        now=NOW,
    )
    r = c.post("/api/kill", json={"enabled": False, "restore": "band"}, headers=owner)
    assert r.status_code == 200 and r.json()["restore"] == "sent"
    assert r.json()["state"] == "DISABLED"
    assert [call["service"] for call in climate_calls(ha)] == ["set_temperature"]
    attrs = ha.states[TSTAT]["attributes"]
    assert attrs["target_temp_low"] == 66 and attrs["target_temp_high"] == 74


@pytest.mark.brd("FR-KILL-3")
@pytest.mark.brd("NFR-SAFE-5")
@pytest.mark.parametrize("endpoint", ["settings", "setup"])
async def test_settings_save_waits_for_active_stop_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    ctx, ha, _, _ = wire(tmp_path, "heat_cool", active=True)
    ha.states[TSTAT]["attributes"].update(target_temp_low=67, target_temp_high=75)
    body = ctx.settings.model_dump(mode="json")
    body["thermostat"]["entity_id"] = ""
    owner = Principal(user_id=1, username="alex", role="owner")
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    entered, release, save_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed_settings: list[str] = []
    get_state = HARest.get_state

    async def delayed_read(rest: HARest, entity_id: str) -> dict[str, Any] | None:
        if entity_id == TSTAT:
            if not entered.is_set():
                entered.set()
                await release.wait()
            observed_settings.append(load_settings(ctx).thermostat.entity_id)
        return await get_state(rest, entity_id)

    async def save() -> None:
        save_started.set()
        if endpoint == "settings":
            await settings_put(body, request, ctx, owner)
        else:
            await setup_save(body, request, ctx, owner)

    monkeypatch.setattr(HARest, "get_state", delayed_read)
    stop = asyncio.create_task(kill(KillIn(enabled=False), request, ctx, owner))
    await asyncio.wait_for(entered.wait(), 1)
    saving = asyncio.create_task(save())
    await asyncio.wait_for(save_started.wait(), 1)
    saved_during_handoff = load_settings(ctx).thermostat.entity_id
    release.set()
    stopped, _ = await asyncio.gather(stop, saving)

    assert saved_during_handoff == TSTAT
    assert observed_settings and all(entity == TSTAT for entity in observed_settings)
    assert stopped.restore == "sent" and stopped.kill_switch == "off"
    assert len(climate_calls(ha)) == 1
    assert load_settings(ctx).thermostat.entity_id == ""


@pytest.mark.brd("FR-KILL-3")
def test_stopping_active_attempts_the_band_even_for_legacy_leave_request(tmp_path: Path) -> None:
    _, ha, c, _ = wire(tmp_path, "heat_cool", active=True)
    ha.states[TSTAT]["attributes"].update(target_temp_low=67, target_temp_high=75)
    h = login(c, "ann")

    r = c.post("/api/kill", json={"enabled": False, "restore": "leave"}, headers=h)

    assert r.status_code == 200 and r.json()["restore"] == "sent"
    assert [call["service"] for call in climate_calls(ha)] == ["set_temperature"]
    assert ha.states[TSTAT]["attributes"]["target_temp_low"] == 68
    assert ha.states[TSTAT]["attributes"]["target_temp_high"] == 76


@pytest.mark.brd("NFR-SEC-1")
def test_stop_handoff_configuration_requires_owner(tmp_path: Path) -> None:
    _, _, c, _ = wire(tmp_path)
    admin = login(c, "ann")
    settings = c.get("/api/settings").json()
    settings["stop_handoff"] = {"enabled": True, "heat_f": 66, "cool_f": 74}
    assert c.put("/api/settings", json=settings, headers=admin).status_code == 403


@pytest.mark.brd("NFR-SAFE-5")
@pytest.mark.parametrize(
    ("method", "path"), [("PUT", "/api/settings"), ("POST", "/api/setup/save")]
)
@pytest.mark.parametrize("replacement", ["", "climate.other"])
def test_thermostat_cannot_be_changed_during_active_control(
    tmp_path: Path, method: str, path: str, replacement: str
) -> None:
    ctx, ha, c, _ = wire(tmp_path, "heat_cool", active=True)
    owner = login(c, "alex")
    settings = c.get("/api/settings").json()
    settings["thermostat"]["entity_id"] = replacement

    response = c.request(method, path, json=settings, headers=owner)

    assert response.status_code == 409
    assert "stop" in response.json()["detail"].lower()
    assert load_settings(ctx).thermostat.entity_id == TSTAT
    state = db.read_bot_state(ctx.conn)
    assert state is not None and state.kill_switch == "on" and state.run_mode == "active"
    assert climate_calls(ha) == []


@pytest.mark.brd("FR-KILL-3")
def test_owner_can_update_stop_band_during_active_control(tmp_path: Path) -> None:
    ctx, _, c, _ = wire(tmp_path, "heat_cool", active=True)
    owner = login(c, "alex")
    settings = c.get("/api/settings").json()
    settings["stop_handoff"] = {"enabled": True, "heat_f": 66, "cool_f": 74}

    assert c.put("/api/settings", json=settings, headers=owner).status_code == 200
    assert load_settings(ctx).stop_handoff.heat_f == 66


@pytest.mark.brd("FR-MODE-2")
@pytest.mark.parametrize(
    ("method", "path"), [("PUT", "/api/settings"), ("POST", "/api/setup/save")]
)
def test_thermostat_change_restarts_watch_evidence(tmp_path: Path, method: str, path: str) -> None:
    ctx, _, c, _ = wire(tmp_path)
    ready_for_control(ctx)
    owner = login(c, "alex")
    assert shadow_evidence(ctx, db.read_bot_state(ctx.conn), NOW)[0]
    settings = c.get("/api/settings").json()
    settings["thermostat"]["entity_id"] = "climate.other"

    assert c.request(method, path, json=settings, headers=owner).status_code == 200
    state = db.read_bot_state(ctx.conn)
    assert state is not None and state.shadow_started_at == NOW

    settings["thermostat"]["entity_id"] = TSTAT
    assert c.request(method, path, json=settings, headers=owner).status_code == 200
    assert not shadow_evidence(ctx, db.read_bot_state(ctx.conn), NOW)[0]
    assert c.post("/api/kill", json=CONTROL_APPROVAL, headers=owner).status_code == 409


@pytest.mark.brd("FR-MODE-2")
@pytest.mark.parametrize(
    ("method", "path"), [("PUT", "/api/settings"), ("POST", "/api/setup/save")]
)
def test_thermostat_change_rolls_back_if_watch_clock_cannot_be_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    ctx, _, c, _ = wire(tmp_path)
    ready_for_control(ctx)
    owner = login(c, "alex")
    settings = c.get("/api/settings").json()
    settings["thermostat"]["entity_id"] = "climate.other"
    original_state = db.read_bot_state(ctx.conn)

    def fail_clock(
        conn: sqlite3.Connection, state: Persisted, by: str, now: datetime | None = None
    ) -> None:
        raise sqlite3.OperationalError("simulated Watch clock failure")

    monkeypatch.setattr(db, "write_bot_state", fail_clock)
    with pytest.raises(sqlite3.OperationalError, match="simulated Watch clock failure"):
        c.request(method, path, json=settings, headers=owner)
    assert load_settings(ctx).thermostat.entity_id == TSTAT
    assert ctx.settings.thermostat.entity_id == TSTAT
    assert db.read_bot_state(ctx.conn) == original_state


@pytest.mark.brd("FR-KILL-3")
def test_off_restore_not_needed_when_already_on_band(tmp_path: Path) -> None:
    _, ha, c, _ = wire(tmp_path, "heat_cool", active=True)
    h = login(c, "ann")
    r = c.post("/api/kill", json={"enabled": False}, headers=h)
    assert r.json()["restore"] == "not_needed" and climate_calls(ha) == []


@pytest.mark.brd("FR-KILL-3")
def test_restore_failure_still_turns_off(tmp_path: Path) -> None:
    ctx, ha, c, _ = wire(tmp_path, "off", active=True)
    ha.climate_forbidden = True
    h = login(c, "ann")
    r = c.post("/api/kill", json={"enabled": False}, headers=h)
    assert r.json()["restore"] == "failed" and r.json()["kill_switch"] == "off"
    p = db.read_bot_state(ctx.conn)
    assert p is not None and p.kill_switch == "off"


@pytest.mark.brd("FR-KILL-3")
def test_failed_handoff_warning_persists_until_acknowledged(tmp_path: Path) -> None:
    _, ha, c, _ = wire(tmp_path, "off", active=True)
    ha.climate_forbidden = True
    h = login(c, "ann")

    stopped = c.post("/api/kill", json={"enabled": False}, headers=h)
    assert stopped.json()["restore"] == "failed"
    warning = stopped.json()["stop_warning"]
    assert warning and "thermostat" in warning.lower()
    assert c.get("/api/status").json()["stop_warning"] == warning
    assert warning in c.get("/").text

    c.post("/api/kill", json={"enabled": False}, headers=h)
    assert c.get("/api/status").json()["stop_warning"] == warning
    member = login(c, "jordan")
    assert (
        c.post(
            "/api/notices/acknowledge", json={"kind": "stop_handoff"}, headers=member
        ).status_code
        == 403
    )
    admin = login(c, "ann")
    assert (
        c.post("/api/notices/acknowledge", json={"kind": "stop_handoff"}, headers=admin).status_code
        == 200
    )
    assert c.get("/api/status").json()["stop_warning"] is None


@pytest.mark.brd("FR-KILL-1")
@pytest.mark.parametrize("cancel_during", ["handoff", "mirror"])
async def test_cancelled_stop_stays_off_and_preserves_auditable_warning(
    tmp_path: Path, cancel_during: str
) -> None:
    ctx, _, _, _ = wire(tmp_path, "off", active=True)
    entered = asyncio.Event()

    async def restore(now: datetime, settings: Settings) -> tuple[RestoreStatus, str]:
        if cancel_during == "handoff":
            entered.set()
            await asyncio.Event().wait()
        return "failed", "Handoff was not confirmed; check the thermostat."

    async def mirror(on: bool) -> None:
        entered.set()
        await asyncio.Event().wait()

    ctx.kill_restore = restore
    ctx.set_mirror = mirror
    request = Request({"type": "http", "method": "POST", "path": "/api/kill", "headers": []})
    principal = Principal(user_id=1, username="ann", role="admin")
    task = asyncio.create_task(_kill(KillIn(enabled=False), request, ctx, principal))
    await asyncio.wait_for(entered.wait(), 1)
    if cancel_during == "handoff":
        pending = db.read_kv(ctx.conn, "stop_handoff_warning")
        assert pending and "in progress" in pending.lower()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    state = db.read_bot_state(ctx.conn)
    assert state is not None and state.kill_switch == "off"
    warning = db.read_kv(ctx.conn, "stop_handoff_warning")
    assert warning and "handoff" in warning.lower()
    if cancel_during == "mirror":
        assert "mirror" in warning.lower()
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit WHERE action='kill_off'").fetchone()[0] == 1


@pytest.mark.brd("FR-KILL-3")
@pytest.mark.parametrize("previous", [None, "Previous stop needs checking."])
def test_confirmed_handoff_clears_only_its_pending_warning(
    tmp_path: Path, previous: str | None
) -> None:
    ctx, _, c, _ = wire(tmp_path, "off", active=True)
    if previous:
        db.write_kv(ctx.conn, "stop_handoff_warning", previous, now=NOW)

    async def restore(_: datetime, __: Settings) -> tuple[RestoreStatus, str]:
        pending = db.read_kv(ctx.conn, "stop_handoff_warning")
        assert pending and "in progress" in pending.lower()
        return "sent", "Confirmed."

    ctx.kill_restore = restore
    response = c.post("/api/kill", json={"enabled": False}, headers=login(c, "ann"))
    assert response.status_code == 200 and response.json()["restore"] == "sent"
    assert response.json()["stop_warning"] == previous


@pytest.mark.brd("FR-KILL-1")
def test_stop_disables_even_if_pending_warning_cannot_be_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, ha, c, _ = wire(tmp_path, "off", active=True)
    original = db.write_kv

    def fail_warning(
        conn: sqlite3.Connection, key: str, value: str, now: datetime | None = None
    ) -> None:
        if key == "stop_handoff_warning":
            raise RuntimeError("simulated notice failure")
        original(conn, key, value, now)

    monkeypatch.setattr(db, "write_kv", fail_warning)
    with pytest.raises(RuntimeError, match="simulated notice failure"):
        c.post("/api/kill", json={"enabled": False}, headers=login(c, "ann"))
    state = db.read_bot_state(ctx.conn)
    assert state is not None and state.kill_switch == "off"
    assert climate_calls(ha) == []


@pytest.mark.brd("FR-KILL-1")
@pytest.mark.parametrize("mirror_fails", [False, True])
async def test_failed_stop_persistence_blocks_writes_until_stop_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror_fails: bool
) -> None:
    seed_house(tmp_path / "data")
    ha = MockHA()
    ha.states[TSTAT]["state"] = "heat_cool"
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    rt = build(
        load_env(env(tmp_path)), clock=lambda: NOW, ha_transport=httpx.ASGITransport(app=ha.app)
    )
    db.write_bot_state(rt.conn, Persisted(kill_switch="on", run_mode="active"), by="test", now=NOW)
    request = Request({"type": "http", "method": "POST", "path": "/api/kill", "headers": []})
    owner = Principal(user_id=1, username="alex", role="owner")
    handoffs: list[int] = []
    mirrors: list[bool] = []

    async def handoff(_: datetime, __: Settings) -> tuple[RestoreStatus, str]:
        handoffs.append(1)
        return "not_needed", "Already matched."

    set_mirror = rt.ctx.set_mirror

    async def mirror(on: bool) -> None:
        mirrors.append(on)
        if mirror_fails:
            raise HAError(503, "simulated HA outage")
        await set_mirror(on)

    def fail_state_write(
        conn: sqlite3.Connection, state: Persisted, by: str, now: datetime | None = None
    ) -> None:
        raise sqlite3.OperationalError("simulated Stop database failure")

    rt.ctx.kill_restore = handoff
    rt.ctx.set_mirror = mirror
    try:
        with monkeypatch.context() as broken:
            broken.setattr(db, "write_bot_state", fail_state_write)
            with pytest.raises(HTTPException) as failed:
                await kill(KillIn(enabled=False), request, rt.ctx, owner)

            ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
            await rt.controller.tick(snap(), STEP, Live(ha_mirror="on"), NOW)
            assert climate_calls(ha) == []
            assert failed.value.status_code == 503
            assert mirrors == [False]
            assert rt.ctx.eff().state == "DISABLED"
            assert "Stop could not be saved" in (stop_warning_of(rt.ctx) or "")

            with pytest.raises(HTTPException) as blocked:
                await kill(KillIn(enabled=True, mode="shadow"), request, rt.ctx, owner)
            assert blocked.value.status_code == 409
            with pytest.raises(HTTPException) as unacknowledged:
                await acknowledge_notice(NoticeIn(kind="stop_handoff"), request, rt.ctx, owner)
            assert unacknowledged.value.status_code == 409

        stopped = await kill(KillIn(enabled=False), request, rt.ctx, owner)
        assert stopped.kill_switch == "off"
        assert handoffs == [1]
        assert "previous Stop could not be saved" in (stopped.stop_warning or "")
    finally:
        await rt.aclose()


@pytest.mark.brd("NFR-SAFE-6")
@pytest.mark.brd("FR-KILL-1")
@pytest.mark.parametrize(
    "failure", ["missing", "ha_error", "http_error", "timeout", "unknown", "unavailable", ""]
)
def test_unreadable_restore_never_writes_and_still_turns_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    ctx, ha, c, _ = wire(tmp_path, "off", active=True)
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
    assert r.status_code == 409
    assert db.read_bot_state(ctx.conn).run_mode == "shadow"


@pytest.mark.brd("FR-MODE-3")
def test_control_requires_owner_review_even_when_ready(tmp_path: Path) -> None:
    ctx, _, c, _ = wire(tmp_path)
    ready_for_control(ctx)
    h = login(c, "alex")

    for field in ("confirmation", "reviewed_shadow", "reviewed_safety_net"):
        incomplete = {**CONTROL_APPROVAL, field: None}
        r = c.post("/api/kill", json=incomplete, headers=h)
        assert r.status_code in (409, 422)
        state = db.read_bot_state(ctx.conn)
        assert state is not None and state.run_mode == "shadow"

    r = c.post("/api/kill", json=CONTROL_APPROVAL, headers=h)
    assert r.status_code == 200 and r.json()["state"] == "ACTIVE"
    state = db.read_bot_state(ctx.conn)
    assert state is not None and state.shadow_started_at is None


@pytest.mark.brd("FR-MODE-3")
def test_control_remains_in_watch_if_mirror_enable_fails(tmp_path: Path) -> None:
    ctx, ha, c, _ = wire(tmp_path)
    ready_for_control(ctx)

    async def broken(_: bool) -> None:
        state = db.read_bot_state(ctx.conn)
        assert state is not None and state.run_mode == "shadow"
        raise HAError(503, "mirror unavailable")

    ctx.set_mirror = broken
    r = c.post("/api/kill", json=CONTROL_APPROVAL, headers=login(c, "alex"))
    assert r.status_code == 503
    state = db.read_bot_state(ctx.conn)
    assert state is not None and state.kill_switch == "on" and state.run_mode == "shadow"
    assert climate_calls(ha) == []


@pytest.mark.brd("FR-MODE-3")
def test_control_rechecks_readiness_after_mirror_enable(tmp_path: Path) -> None:
    ctx, ha, c, _ = wire(tmp_path)
    ready_for_control(ctx)

    async def lose_readiness(_: bool) -> None:
        state = db.read_bot_state(ctx.conn)
        assert state is not None and state.run_mode == "shadow"
        ctx.heartbeat_configured = False

    ctx.set_mirror = lose_readiness
    r = c.post("/api/kill", json=CONTROL_APPROVAL, headers=login(c, "alex"))
    assert r.status_code == 409
    state = db.read_bot_state(ctx.conn)
    assert state is not None and state.run_mode == "shadow"
    assert climate_calls(ha) == []


@pytest.mark.brd("FR-MODE-3")
@pytest.mark.parametrize(
    "problem",
    [
        "missing_day",
        "sampling_gap",
        "untracked",
        "not_shadow",
        "effective_active",
        "unexplained_degradation",
        "sent_write",
        "unconfirmed_write",
        "interrupted",
        "restart",
        "stopped",
        "mirror_off",
        "stale_tick",
        "handoff_disabled",
        "handoff_off_grid",
        "handoff_celsius_deadband",
        "no_model",
        "corrupt_model",
        "no_plan",
        "no_safety_net",
        "unacknowledged_stop",
        "corrupt_stop_warning",
    ],
)
def test_control_rejects_untrustworthy_evidence_or_unready_state(
    tmp_path: Path, problem: str
) -> None:
    ctx, ha, c, _ = wire(tmp_path)
    ready_for_control(ctx)
    when = NOW - timedelta(days=6)
    ts = when.isoformat()
    if problem == "missing_day":
        ctx.conn.execute(
            "DELETE FROM samples WHERE ts>=? AND ts<?",
            (ts, (when + timedelta(days=1)).isoformat()),
        )
    elif problem == "sampling_gap":
        ctx.conn.execute(
            "DELETE FROM samples WHERE ts>=? AND ts<?",
            (ts, (when + timedelta(minutes=4)).isoformat()),
        )
    elif problem == "untracked":
        ctx.conn.execute("UPDATE samples SET snapshot_json='{}' WHERE ts=?", (ts,))
    elif problem == "not_shadow":
        ctx.conn.execute("UPDATE samples SET bot_state='ACTIVE' WHERE ts=?", (ts,))
    elif problem == "effective_active":
        ctx.conn.execute(
            "UPDATE samples SET snapshot_json=? WHERE ts=?",
            (json.dumps({"_thermabot": {"state": "ACTIVE"}}), ts),
        )
    elif problem == "unexplained_degradation":
        ctx.conn.execute(
            "UPDATE samples SET snapshot_json=? WHERE ts=?",
            (json.dumps({"_thermabot": {"state": "DEGRADED"}}), ts),
        )
    elif problem in ("sent_write", "unconfirmed_write"):
        outcome = "sent" if problem == "sent_write" else "failed"
        ctx.conn.execute(
            "INSERT INTO writes (ts, service, entity_id, data_json, reason, outcome)"
            " VALUES (?, 'set_temperature', ?, '{}', 'test', ?)",
            (ts, TSTAT, outcome),
        )
    elif problem in ("interrupted", "restart"):
        action = "kill_off" if problem == "interrupted" else "restart_to_shadow"
        db.audit(ctx.conn, "test", action, {}, now=when)
    elif problem == "stopped":
        db.write_bot_state(
            ctx.conn,
            Persisted(
                kill_switch="off",
                run_mode="shadow",
                shadow_started_at=NOW - timedelta(days=8),
                disabled_since=NOW,
            ),
            by="test",
            now=NOW,
        )
    elif problem == "mirror_off":
        ctx.live = ctx.live.model_copy(update={"ha_mirror": "off"})
    elif problem == "stale_tick":
        ctx.last_tick = NOW - timedelta(minutes=4)
    elif problem == "handoff_disabled":
        ctx.settings = HOUSE
    elif problem == "handoff_off_grid":
        ctx.settings = HOUSE.model_copy(
            update={"stop_handoff": StopHandoffCfg(enabled=True, heat_f=66.5, cool_f=74)}
        )
    elif problem == "handoff_celsius_deadband":
        ctx.settings = HOUSE.model_copy(
            update={"stop_handoff": StopHandoffCfg(enabled=True, heat_f=66, cool_f=69)}
        )
        assert ctx.snapshot is not None
        th = ctx.snapshot.thermostat.model_copy(
            update={
                "temperature_unit": "°C",
                "min_temp": 50,
                "max_temp": 95,
                "target_temp_step": 0.9,
            }
        )
        ctx.snapshot = ctx.snapshot.model_copy(update={"thermostat": th})
    elif problem == "no_model":
        ctx.conn.execute("DELETE FROM models")
    elif problem == "corrupt_model":
        ctx.conn.execute("UPDATE models SET params_json='not-json'")
    elif problem == "no_plan":
        ctx.plan = None
    elif problem == "no_safety_net":
        ctx.heartbeat_configured = False
    elif problem == "unacknowledged_stop":
        db.write_kv(ctx.conn, "stop_handoff_warning", "Check the thermostat.", now=NOW)
    elif problem == "corrupt_stop_warning":
        db.write_kv(ctx.conn, "stop_handoff_warning", "Check the thermostat.", now=NOW)
        ctx.conn.execute("UPDATE bot_kv SET checksum='invalid' WHERE key='stop_handoff_warning'")

    h = login(c, "alex")
    r = c.post("/api/kill", json=CONTROL_APPROVAL, headers=h)
    assert r.status_code == 409, (problem, r.json())
    state = db.read_bot_state(ctx.conn)
    assert state is not None and state.run_mode == "shadow"
    assert climate_calls(ha) == []


@pytest.mark.brd("FR-MODE-3")
@pytest.mark.parametrize(("missing", "ready"), [(504, True), (505, False)])
def test_watch_evidence_requires_95_percent_sampling(
    tmp_path: Path, missing: int, ready: bool
) -> None:
    ctx, _, c, _ = wire(tmp_path)
    ready_for_control(ctx)
    start = NOW - timedelta(days=7)
    minutes = list(range(5, 7 * 24 * 60, 20)) + [7 * 24 * 60 - 2]
    ctx.conn.executemany(
        "DELETE FROM samples WHERE ts=?",
        (((start + timedelta(minutes=m)).isoformat(),) for m in minutes[:missing]),
    )
    login(c, "alex")
    checks = c.get("/api/readiness").json()["checks"]
    assert next(ch for ch in checks if ch["id"] == "soak")["ok"] is ready


@pytest.mark.brd("FR-MODE-3")
@pytest.mark.parametrize(("missing", "ready"), [(2, True), (3, False)])
def test_watch_evidence_rejects_gaps_over_three_minutes(
    tmp_path: Path, missing: int, ready: bool
) -> None:
    ctx, _, c, _ = wire(tmp_path)
    ready_for_control(ctx)
    start = NOW - timedelta(days=6)
    ctx.conn.execute(
        "DELETE FROM samples WHERE ts>=? AND ts<?",
        (start.isoformat(), (start + timedelta(minutes=missing)).isoformat()),
    )
    login(c, "alex")
    checks = c.get("/api/readiness").json()["checks"]
    assert next(ch for ch in checks if ch["id"] == "soak")["ok"] is ready


@pytest.mark.brd("FR-MODE-3")
def test_watch_evidence_requires_a_timezone_for_start(tmp_path: Path) -> None:
    ctx, _, _, _ = wire(tmp_path)
    state = Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=datetime(2026, 9, 1))

    ok, detail = shadow_evidence(ctx, state, NOW)

    assert not ok and "7 d" in detail


@pytest.mark.brd("FR-MODE-3")
def test_owner_can_review_degraded_samples_and_pre_send_refusals(tmp_path: Path) -> None:
    ctx, _, c, _ = wire(tmp_path)
    ready_for_control(ctx)
    ts = (NOW - timedelta(days=6)).isoformat()
    ctx.conn.execute(
        "UPDATE samples SET snapshot_json=?, stale_rooms='study' WHERE ts=?",
        (json.dumps({"_thermabot": {"state": "DEGRADED", "degraded_reason": "weather"}}), ts),
    )
    ctx.conn.execute(
        "INSERT INTO writes (ts, service, entity_id, data_json, reason, outcome, guard)"
        " VALUES (?, 'set_temperature', ?, '{}', 'refused', 'failed', 'not_sent')",
        (ts, TSTAT),
    )
    h = login(c, "alex")
    readiness = c.get("/api/readiness").json()
    soak = next(ch for ch in readiness["checks"] if ch["id"] == "soak")
    assert readiness["ready"] and "1 degraded, 1 with stale rooms" in soak["detail"]
    assert c.post("/api/kill", json=CONTROL_APPROVAL, headers=h).status_code == 200


@pytest.mark.brd("FR-KILL-1")
def test_mirror_failure_still_disabled(tmp_path: Path) -> None:
    ctx, _, c, _ = wire(tmp_path, "heat")

    async def broken(on: bool) -> None:
        raise ConnectionError

    ctx.set_mirror = broken
    h = login(c, "ann")
    r = c.post("/api/kill", json={"enabled": False}, headers=h)
    assert r.json()["state"] == "DISABLED" and r.json()["mirror_ok"] is False
    warning = r.json()["stop_warning"]
    assert warning and "mirror" in warning.lower()
    again = c.post("/api/kill", json={"enabled": False}, headers=h)
    assert again.json()["stop_warning"] == warning


@pytest.mark.brd("FR-KILL-3")
def test_kill_restore_from_off_survives_trane_quirk(tmp_path: Path) -> None:
    ctx, ha, c, _ = wire(tmp_path, "off", active=True)
    ha.trane_off_quirk = True
    h = login(c, "ann")
    r = c.post("/api/kill", json={"enabled": False, "restore": "band"}, headers=h)
    assert r.status_code == 200 and r.json()["restore"] == "sent"
    cc = climate_calls(ha)
    assert [x["service"] for x in cc] == ["set_hvac_mode", "set_temperature"]
    assert ha.states[TSTAT]["state"] == "heat_cool"
    a = ha.states[TSTAT]["attributes"]
    assert a["target_temp_low"] == 68 and a["target_temp_high"] == 76
