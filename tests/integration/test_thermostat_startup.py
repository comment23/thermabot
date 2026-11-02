"""FR-CFG-1: settings row persisted on first start; configured thermostat entity.
v0.2.0: a fresh install lands in SETUP NEEDED (no climate reads or writes)."""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.house import HOUSE
from tests.integration.mock_ha import BOT_USER_ID, GOOD_TOKEN, MockHA
from tests.integration.test_controller import LIVE, NOW, STEP, Sleeps, snap
from tests.integration.test_main import env
from thermabot.adapters import db
from thermabot.adapters.ha_rest import HARest
from thermabot.app.controller import Controller
from thermabot.app.main import (
    MIGRATION_BY,
    build,
    create_app,
    load_env,
    persist_effective_settings,
)
from thermabot.config import Settings, ThermostatCfg
from thermabot.core.statemachine import Persisted

OLD = "climate.living_room"
OTHER = "climate.hallway"
T0 = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)


def _row(tmp: Path) -> tuple[str, str, str] | None:
    conn = sqlite3.connect(tmp / "data" / "thermabot.db")
    try:
        r = conn.execute(
            "SELECT value_json, updated_at, updated_by FROM settings WHERE key='settings'"
        ).fetchone()
    finally:
        conn.close()
    return None if r is None else (r[0], r[1], r[2])


def _start(tmp: Path, t: datetime) -> None:
    ha = MockHA()
    app = create_app(
        load_env(env(tmp)), clock=lambda: t, ha_transport=httpx.ASGITransport(app=ha.app)
    )
    with TestClient(app) as c:
        assert c.get("/healthz").status_code in (200, 503)
    assert [x for x in ha.calls if x["domain"] == "climate"] == []


@pytest.mark.brd("FR-CFG-1")
def test_fresh_install_persists_no_generic_row(tmp_path: Path) -> None:
    # v0.2.0: generic (unconfigured) defaults are never written, so a wrong-order deploy onto
    # a row-less DB leaves it untouched and a rollback to v0.1.11 still finds no row.
    _start(tmp_path, T0)
    assert _row(tmp_path) is None
    _start(tmp_path, T0 + timedelta(hours=1))
    assert _row(tmp_path) is None


@pytest.mark.brd("FR-CFG-1")
def test_persist_effective_settings_once_when_configured(tmp_path: Path) -> None:
    from tests.integration.test_api import make

    ctx, _, _ = make(tmp_path)  # Ctx with the configured test house, no settings row
    assert ctx.conn.execute("SELECT 1 FROM settings WHERE key='settings'").fetchone() is None
    assert persist_effective_settings(ctx) is True
    r1 = ctx.conn.execute(
        "SELECT value_json, updated_at, updated_by FROM settings WHERE key='settings'"
    ).fetchone()
    assert r1["updated_by"] == MIGRATION_BY
    assert json.loads(r1["value_json"]) == HOUSE.model_dump(mode="json")
    assert persist_effective_settings(ctx) is False  # never overwrites
    r2 = ctx.conn.execute(
        "SELECT value_json, updated_at, updated_by FROM settings WHERE key='settings'"
    ).fetchone()
    assert tuple(r1) == tuple(r2)


@pytest.mark.brd("FR-CFG-1")
def test_existing_settings_row_is_never_touched(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    conn = db.open_db(tmp_path / "data" / "thermabot.db", now=T0)
    saved = HOUSE.model_dump(mode="json")
    del saved["thermostat"]  # a pre-v0.1.11 row
    saved["safety"]["floor_tstat_f"] = 63
    value = json.dumps(saved)
    conn.execute(
        "INSERT INTO settings (key, value_json, updated_at, updated_by) VALUES (?,?,?,?)",
        ("settings", value, "2026-10-01T00:00:00+00:00", "alex"),
    )
    conn.close()
    _start(tmp_path, T0)
    assert _row(tmp_path) == (value, "2026-10-01T00:00:00+00:00", "alex")


@pytest.mark.brd("FR-CFG-1")
async def test_build_wires_configured_entity_into_both_layers(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    conn = db.open_db(tmp_path / "data" / "thermabot.db", now=T0)
    conn.execute(
        "INSERT INTO settings (key, value_json, updated_at, updated_by) VALUES (?,?,?,?)",
        (
            "settings",
            Settings(thermostat=ThermostatCfg(entity_id=OTHER)).model_dump_json(),
            T0.isoformat(),
            "alex",
        ),
    )
    conn.close()
    rt = build(
        load_env(env(tmp_path)),
        clock=lambda: T0,
        ha_transport=httpx.ASGITransport(app=MockHA().app),
    )
    assert rt.ha.writable_entity == OTHER
    assert rt.controller.entity == OTHER == rt.controller.settings.thermostat.entity_id
    await rt.aclose()


def _controller(tmp: Path, settings: Settings | None, writable: str) -> tuple[Controller, MockHA]:
    conn = db.open_db(tmp / "t.db", now=NOW)
    db.write_bot_state(
        conn,
        Persisted(kill_switch="on", run_mode="active", shadow_started_at=NOW),
        by="test",
        now=NOW,
    )
    ha = MockHA()
    ha.states[OTHER] = copy.deepcopy(ha.states[OLD])  # a second climate entity
    rest = HARest(
        "http://ha",
        GOOD_TOKEN,
        transport=httpx.ASGITransport(app=ha.app),
        writable_entity=writable,
    )
    c = Controller(conn, rest, settings=settings, bot_user_id=BOT_USER_ID, sleep=Sleeps())
    return c, ha


@pytest.mark.brd("FR-CFG-1")
async def test_controller_writes_go_to_configured_entity(tmp_path: Path) -> None:
    c, ha = _controller(tmp_path, Settings(thermostat=ThermostatCfg(entity_id=OTHER)), OTHER)
    ds = await c.tick(snap(), STEP, LIVE, NOW)
    assert [d.allowed for d in ds] == [True]
    assert {x["data"]["entity_id"] for x in ha.calls} == {OTHER}
    assert ha.states[OTHER]["attributes"]["target_temp_low"] == 67
    assert ha.states[OLD]["attributes"]["target_temp_low"] == 68  # untouched
    row = c.conn.execute("SELECT entity_id, outcome FROM writes").fetchone()
    assert tuple(row) == (OTHER, "sent")


@pytest.mark.brd("FR-CFG-1")
async def test_adapter_refuses_when_layers_disagree(tmp_path: Path) -> None:
    # Core allows the default entity, but the adapter is wired for another: no HTTP at all.
    c, ha = _controller(tmp_path, HOUSE, OTHER)
    await c.tick(snap(), STEP, LIVE, NOW)
    assert ha.calls == []
    assert [r[0] for r in c.conn.execute("SELECT outcome FROM writes")] == ["failed"]


@pytest.mark.brd("FR-CFG-1")
def test_thermostat_section_needs_edit_safety(tmp_path: Path) -> None:
    from tests.integration.test_api import login, make

    _, _, c = make(tmp_path)
    h = login(c, "ann")
    s = c.get("/api/settings").json()
    assert s["thermostat"]["entity_id"] == OLD  # make() seeds the test house
    s["thermostat"]["entity_id"] = OTHER
    assert c.put("/api/settings", json=s, headers=h).status_code == 403  # admin
    s["thermostat"]["entity_id"] = "sensor.nope"
    assert c.put("/api/settings", json=s, headers=h).status_code == 422
    s["thermostat"]["entity_id"] = OTHER
    h = login(c, "alex")
    assert c.put("/api/settings", json=s, headers=h).status_code == 200
    assert c.get("/api/settings").json()["thermostat"]["entity_id"] == OTHER
