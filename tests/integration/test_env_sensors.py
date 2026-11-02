"""v0.6.1: no built-in house sensor ids. A pre-v0.3.0 settings row (no `sensors` key) gets its
HVAC sensors once from THERMABOT_SENSOR_* (updated_by migration-v0.6.1); without them the bot
runs DEGRADED(sensors) with no writes and no crash. A Settings save that changes nothing
writes no row."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests import house as H
from tests.house import house_dict, seed_house
from tests.integration.mock_ha import GOOD_TOKEN, MockHA
from tests.integration.test_api import login, make
from tests.integration.test_jobs import NOW, Clock
from thermabot.adapters import db
from thermabot.app.main import SENSORS_MIGRATION_BY, build, load_env
from thermabot.config import Settings
from thermabot.core.statemachine import Persisted

ENV_SENSORS = {
    "THERMABOT_SENSOR_BLOWER": H.BLOWER,
    "THERMABOT_SENSOR_COMPRESSOR": H.COMPRESSOR,
    "THERMABOT_SENSOR_OUTDOOR": H.OUTDOOR,
}


def _legacy_row(data: Path) -> str:
    """A row as v0.1.11-v0.2.0 stored it: no `sensors`, no `display`."""
    seed_house(data)
    d = Settings.model_validate(house_dict()).model_dump(mode="json")
    d.pop("sensors")
    d.pop("display")
    raw = json.dumps(d)
    conn = sqlite3.connect(data / "thermabot.db")
    conn.execute("UPDATE settings SET value_json=?, updated_by='adam' WHERE key='settings'", (raw,))
    conn.commit()
    conn.close()
    return raw


def _row(data: Path) -> tuple[str, str, str]:
    conn = sqlite3.connect(data / "thermabot.db")
    r = conn.execute(
        "SELECT value_json, updated_at, updated_by FROM settings WHERE key='settings'"
    ).fetchone()
    conn.close()
    return r[0], r[1], r[2]


def _build(data: Path, ha: MockHA, **env: str) -> Any:
    e = load_env({"HA_URL": "http://ha", "HA_TOKEN": GOOD_TOKEN, "DATA_DIR": str(data), **env})
    return build(e, clock=Clock(NOW), ha_transport=httpx.ASGITransport(app=ha.app))


def _shadow(rt: Any, ha: MockHA) -> None:
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    db.write_bot_state(
        rt.conn,
        Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=NOW),
        by="t",
        now=NOW,
    )


@pytest.mark.brd("FR-CFG-1")
async def test_env_sensors_seed_legacy_row_once(tmp_path: Path) -> None:
    raw = _legacy_row(tmp_path)
    ha = MockHA()
    rt = _build(tmp_path, ha, **ENV_SENSORS)
    v, at, by = _row(tmp_path)
    assert by == SENSORS_MIGRATION_BY
    stored = json.loads(v)
    assert stored["sensors"] == {**H.SENSORS, "humidity": ""}
    # only the `sensors` key was added; every other stored value is unchanged
    assert {k: x for k, x in stored.items() if k != "sensors"} == json.loads(raw)
    assert rt.ctx.settings.sensors.outdoor == H.OUTDOOR and not rt.ctx.settings.sensors_missing
    _shadow(rt, ha)
    await rt.tick(NOW)
    assert rt.ctx.eff().state == "SHADOW"
    snap = json.loads(rt.conn.execute("SELECT snapshot_json FROM samples").fetchone()[0])
    assert snap["t_out"] == 58 and snap["cs_raw"] == 0
    assert [c for c in ha.calls if c["domain"] == "climate"] == []
    await rt.aclose()
    # idempotent: a second start (even with other env values) changes nothing
    rt2 = _build(tmp_path, MockHA(), THERMABOT_SENSOR_OUTDOOR="sensor.elsewhere")
    assert _row(tmp_path) == (v, at, by)
    await rt2.aclose()


@pytest.mark.brd("FR-CFG-1")
async def test_forgot_env_is_degraded_no_writes_no_crash(tmp_path: Path) -> None:
    raw = _legacy_row(tmp_path)
    ha = MockHA()
    rt = _build(tmp_path, ha)
    assert _row(tmp_path)[0] == raw and _row(tmp_path)[2] == "adam"  # nothing written
    assert rt.ctx.settings.sensors_missing
    _shadow(rt, ha)
    await rt.tick(NOW)
    e = rt.ctx.eff()
    assert e.state == "DEGRADED" and e.degraded_reason == "sensors" and e.base == "SHADOW"
    assert not e.plan_writes and e.label != "SETUP NEEDED"
    assert [c for c in ha.calls if c["domain"] == "climate"] == []
    st = rt.conn.execute("SELECT kill_switch, run_mode FROM bot_state").fetchone()
    assert (st[0], st[1]) == ("on", "shadow")
    assert _row(tmp_path)[0] == raw
    await rt.aclose()


@pytest.mark.brd("FR-CFG-1")
async def test_row_with_sensors_is_never_touched(tmp_path: Path) -> None:
    seed_house(tmp_path, Settings.model_validate({**house_dict(), "sensors": {"outdoor": ""}}))
    before = _row(tmp_path)
    rt = _build(tmp_path, MockHA(), **ENV_SENSORS)
    assert _row(tmp_path) == before
    await rt.aclose()


@pytest.mark.brd("FR-CFG-1")
async def test_invalid_env_sensor_is_rejected_without_writing(tmp_path: Path) -> None:
    raw = _legacy_row(tmp_path)
    rt = _build(tmp_path, MockHA(), THERMABOT_SENSOR_OUTDOOR="not an entity")
    assert _row(tmp_path)[0] == raw
    await rt.aclose()


@pytest.mark.brd("FR-CMF-1")
def test_no_diff_settings_put_writes_no_row(tmp_path: Path) -> None:
    ctx, _, c = make(tmp_path)
    h = login(c, "alex")
    s = c.get("/api/settings").json()
    assert c.put("/api/settings", json=s, headers=h).status_code == 200  # first: creates row
    r1 = ctx.conn.execute("SELECT updated_at, updated_by FROM settings").fetchone()
    n_audit = ctx.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    ctx.clock = lambda: NOW.replace(year=2027)
    assert c.put("/api/settings", json=s, headers=h).status_code == 200
    assert ctx.conn.execute("SELECT updated_at, updated_by FROM settings").fetchone() == r1
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == n_audit
    s["bands"]["heat"]["home"] = [67, 70]
    assert c.put("/api/settings", json=s, headers=h).status_code == 200
    assert ctx.conn.execute("SELECT updated_at FROM settings").fetchone()[0] != r1[0]


@pytest.mark.brd("FR-CMF-1")
def test_no_diff_setup_save_writes_no_row(tmp_path: Path) -> None:
    ctx, _, c = make(tmp_path)
    h = login(c, "alex")
    s = c.get("/api/settings").json()
    assert c.post("/api/setup/save", json=s, headers=h).status_code == 200
    r1 = ctx.conn.execute("SELECT updated_at FROM settings").fetchone()[0]
    ctx.clock = lambda: NOW.replace(year=2027)
    r = c.post("/api/setup/save", json=s, headers=h)
    assert r.status_code == 200 and r.json()["restart_required"] is False
    assert ctx.conn.execute("SELECT updated_at FROM settings").fetchone()[0] == r1
