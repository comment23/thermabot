"""v0.3.0 setup wizard API: Owner only, read-only towards HA (no service call ever), short
cache, graceful timeout, save writes only the settings row (never run mode / kill switch),
restart flag on a thermostat change, X-Ingress-Path honoured, X-Remote-User-* ignored."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

import thermabot
from tests.house import HOUSE, TSTAT, house_dict
from tests.integration.mock_ha import GOOD_TOKEN, MockHA
from tests.integration.test_api import login, make
from thermabot.adapters import db
from thermabot.adapters.ha_rest import HARest
from thermabot.app import setup_api
from thermabot.config import Settings


def _setup(tmp: Path, ha: MockHA | None = None, **kw: Any) -> tuple[Any, Any, MockHA]:
    ha = ha or MockHA()
    ctx, _, c = make(tmp, **kw)
    ctx.ha_read = HARest("http://ha", GOOD_TOKEN, transport=httpx.ASGITransport(app=ha.app))
    ctx.ha_url = "http://ha"
    return ctx, c, ha


@pytest.mark.brd("FR-USR-2")
@pytest.mark.parametrize(
    "path",
    [
        "/api/setup/ha-test",
        "/api/setup/entities?domain=climate",
        "/api/setup/discover",
        "/api/setup/home-context",
    ],
)
@pytest.mark.parametrize(("user", "status"), [("ann", 403), ("jordan", 403), (None, 401)])
def test_setup_owner_only(tmp_path: Path, path: str, user: str | None, status: int) -> None:
    _, c, ha = _setup(tmp_path)
    if user:
        login(c, user)
    assert c.get(path).status_code == status
    assert ha.all_states_reads == 0 and ha.config_reads == 0


@pytest.mark.brd("FR-USR-2")
@pytest.mark.parametrize("user", ["ann", "jordan"])
def test_setup_save_owner_only(tmp_path: Path, user: str) -> None:
    ctx, c, _ = _setup(tmp_path)
    h = login(c, user)
    r = c.post("/api/setup/save", json=house_dict(), headers=h)
    assert r.status_code == 403
    assert ctx.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


@pytest.mark.brd("FR-CFG-1")
def test_ha_test_ok_reports_version_and_no_token(tmp_path: Path) -> None:
    _, c, ha = _setup(tmp_path)
    login(c, "alex")
    r = c.get("/api/setup/ha-test")
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True and b["version"] == "2026.10.0" and b["url"] == "http://ha"
    assert GOOD_TOKEN not in r.text
    assert ha.calls == []


@pytest.mark.brd("FR-CFG-1")
def test_ha_test_bad_token_and_unreachable(tmp_path: Path) -> None:
    ctx, c, ha = _setup(tmp_path)
    ha.token = "other"
    login(c, "alex")
    b = c.get("/api/setup/ha-test").json()
    assert b["ok"] is False and "401" in b["error"] and "HA_TOKEN" in b["error"]
    home = c.get("/api/setup/home-context")
    assert home.status_code == 502 and "401" in home.json()["detail"]

    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

    ctx.ha_read = HARest("http://ha", GOOD_TOKEN, transport=Down())
    b = c.get("/api/setup/ha-test").json()
    assert b["ok"] is False and "HA_URL" in b["error"]
    r = c.get("/api/setup/discover")
    assert r.status_code == 502 and "HA_URL" in r.json()["detail"]


@pytest.mark.parametrize(
    "value",
    [True, None, "40", float("nan"), float("inf"), -91, 91],
)
def test_coordinate_rejects_invalid_latitude(value: Any) -> None:
    assert setup_api._coordinate(value, -90, 90) is None


def test_coordinate_accepts_valid_limits() -> None:
    assert setup_api._coordinate(-90, -90, 90) == -90
    assert setup_api._coordinate(90, -90, 90) == 90


@pytest.mark.brd("FR-CFG-1")
async def test_ha_timeout_is_graceful(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    class Slow:
        async def get_config(self) -> dict[str, Any]:
            await asyncio.sleep(5)
            return {}

        async def get_all_states(self) -> list[dict[str, Any]]:
            await asyncio.sleep(5)
            return []

    monkeypatch.setattr(setup_api, "HA_TIMEOUT_S", 0.05)
    ctx, c, _ = _setup(tmp_path)
    ctx.ha_read = Slow()
    login(c, "alex")
    b = c.get("/api/setup/ha-test").json()
    assert b["ok"] is False and "did not answer" in b["error"]
    r = c.get("/api/setup/entities?domain=climate")
    assert r.status_code == 502 and "did not answer" in r.json()["detail"]


@pytest.mark.brd("FR-CFG-1")
def test_entities_and_discover_are_read_only_and_cached(tmp_path: Path) -> None:
    ctx, c, ha = _setup(tmp_path, settings=Settings())
    login(c, "alex")
    r = c.get("/api/setup/entities?domain=climate")
    assert r.status_code == 200
    climate = {e["entity_id"]: e for e in r.json()}
    assert set(climate) == {TSTAT, "climate.garage_heater"}
    assert climate[TSTAT]["hvac_modes"] == ["off", "heat", "cool", "heat_cool"]
    assert climate[TSTAT]["fan_modes"] == ["auto", "on", "circulate"]
    d = c.get(f"/api/setup/discover?thermostat={TSTAT}").json()
    assert d["thermostat"] == TSTAT
    s = d["suggestions"]
    assert s["blower"][0]["entity_id"] == "binary_sensor.living_room_blower_active"
    assert s["outdoor"][0]["entity_id"] == "sensor.outdoor_temperature"
    assert any(x["entity_id"] == "person.alex" for x in s["people"])
    assert any(x["entity_id"] == "sensor.study_temperature" for x in s["rooms"])
    assert ha.all_states_reads == 1  # second call served from the cache
    assert c.get("/api/setup/entities?domain=sensor").status_code == 200
    assert ha.all_states_reads == 1
    assert c.get("/api/setup/entities?domain=light").status_code == 422
    assert ha.calls == [] and ha.state_posts == []  # never a service call or state write
    assert ctx.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0  # nothing saved


@pytest.mark.brd("FR-CFG-1")
def test_home_context_and_future_device_discovery_are_read_only(tmp_path: Path) -> None:
    ctx, c, ha = _setup(tmp_path)
    ha._set(
        "sun.sun",
        "above_horizon",
        {"azimuth": 180, "elevation": 35, "next_rising": "2026-10-30T11:00:00+00:00"},
        None,
    )
    ha._set("fan.den", "off", {"friendly_name": "Den fan"}, None)
    ha._set("cover.office_blind", "closed", {"friendly_name": "Office blind"}, None)
    ha._set(
        "climate.garage_heater",
        "off",
        {"hvac_modes": ["off", "heat"], "friendly_name": "Garage heater"},
        None,
    )
    ha._set(
        "binary_sensor.garage_door",
        "on",
        {"device_class": "garage_door", "friendly_name": "Garage door"},
        None,
    )
    login(c, "alex")
    context = c.get("/api/setup/home-context")
    assert context.status_code == 200
    assert context.json()["location_available"] is True
    assert context.json()["latitude"] == 40 and context.json()["longitude"] == -75
    assert context.json()["time_zone"] == "America/New_York"
    assert context.json()["sun"]["azimuth"] == 180
    suggestions = c.get("/api/setup/discover").json()["suggestions"]
    assert [x["entity_id"] for x in suggestions["fans"]] == ["fan.den"]
    assert [x["entity_id"] for x in suggestions["covers"]] == ["cover.office_blind"]
    assert [x["entity_id"] for x in suggestions["supplemental_heat"]] == ["climate.garage_heater"]
    assert [x["entity_id"] for x in suggestions["openings"]] == ["binary_sensor.garage_door"]
    assert ha.calls == [] and ha.state_posts == []
    assert ctx.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


@pytest.mark.brd("FR-CFG-1")
def test_save_writes_row_by_owner_and_flags_restart(tmp_path: Path) -> None:
    ctx, c, ha = _setup(tmp_path, settings=Settings())
    assert ctx.running_entity == ""
    replans: list[int] = []
    ctx.replan = lambda: replans.append(1)
    before = db.read_bot_state(ctx.conn)
    raw_before = [tuple(r) for r in ctx.conn.execute("SELECT * FROM bot_state")]
    h = login(c, "alex")
    body = {**house_dict(), "display": {"units": "C"}}
    r = c.post("/api/setup/save", json=body, headers=h)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["restart_required"] is True and b["running_entity"] == ""
    assert b["settings"]["bands"]["heat"]["home"] == [68, 71]  # stored in °F
    row = ctx.conn.execute(
        "SELECT value_json, updated_by FROM settings WHERE key='settings'"
    ).fetchone()
    assert row["updated_by"] == "alex"
    saved = Settings.model_validate_json(row["value_json"])
    assert saved.thermostat.entity_id == TSTAT and saved.display.units == "C"
    after = db.read_bot_state(ctx.conn)
    assert before is not None and after is not None
    assert (after.kill_switch, after.run_mode) == (before.kill_switch, before.run_mode)
    assert [tuple(r) for r in ctx.conn.execute("SELECT * FROM bot_state")] == raw_before
    assert ctx.running_entity == ""  # the running controller is not re-bound
    assert replans == [1] and ha.calls == []
    acts = [r[0] for r in ctx.conn.execute("SELECT action FROM audit WHERE actor='alex'")]
    assert "setup_save" in acts
    # same thermostat as the running one -> no restart needed
    ctx.running_entity = TSTAT
    assert c.post("/api/setup/save", json=body, headers=h).json()["restart_required"] is False


@pytest.mark.brd("FR-CMF-1")
@pytest.mark.parametrize(
    ("bands", "msg"),
    [
        ({"heat": {"home": [70, 74]}}, "overlap"),
        ({"heat": {"home": [72, 71]}}, "inverted"),
        ({"heat": {"sleep": [71, 71]}, "cool": {"sleep": [71, 72]}}, "deadband"),
    ],
)
def test_save_validation_errors(tmp_path: Path, bands: dict[str, Any], msg: str) -> None:
    ctx, c, _ = _setup(tmp_path)
    h = login(c, "alex")
    body = HOUSE.model_dump(mode="json")
    for season, blocks in bands.items():
        body["bands"][season].update(blocks)
    r = c.post("/api/setup/save", json=body, headers=h)
    assert r.status_code == 422
    assert msg in r.text
    assert ctx.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


@pytest.mark.brd("FR-CFG-1")
def test_save_needs_csrf(tmp_path: Path) -> None:
    _, c, _ = _setup(tmp_path)
    login(c, "alex")
    assert c.post("/api/setup/save", json=house_dict()).status_code == 403


@pytest.mark.brd("FR-CFG-1")
def test_ingress_path_and_remote_user_headers(tmp_path: Path) -> None:
    """X-Ingress-Path becomes root_path; pages still render and the login redirect is
    relative. X-Remote-User-* headers grant nothing (stage 6 TODO)."""
    _, c, _ = _setup(tmp_path)
    hdr = {"X-Ingress-Path": "/api/hassio_ingress/abc123", "X-Remote-User-Name": "alex"}
    r = c.get("/setup", headers=hdr, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "login"  # not trusted
    assert c.get("/api/setup/ha-test", headers=hdr).status_code == 401
    login(c, "alex")
    r = c.get("/setup", headers=hdr)
    assert r.status_code == 200
    # Assets stay relative (ingress) and carry a content-based cache-buster.
    v = thermabot.__version__
    assert f'href="static/app.css?v={v}-' in r.text and f'src="static/setup.js?v={v}-' in r.text
    r = c.get("/login", headers=hdr, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "./"


@pytest.mark.brd("FR-CFG-1")
def test_settings_put_unchanged_semantics(tmp_path: Path) -> None:
    """The forms page saves via the existing PUT /api/settings; GET returns the new keys."""
    _, c, _ = _setup(tmp_path)
    h = login(c, "alex")
    s = c.get("/api/settings").json()
    assert s["sensors"]["humidity"] == "" and s["display"] == {"units": "F"}
    s["sensors"]["blower"] = ""  # optional: not read any more
    s["sensors"]["outdoor"] = "sensor.outdoor_temperature"
    assert c.put("/api/settings", json=s, headers=h).status_code == 200
    got = c.get("/api/settings").json()["sensors"]
    assert got["outdoor"].endswith("outdoor_temperature") and got["blower"] == ""
    # an explicitly emptied sensor stays empty after reload (no legacy fill-in)
    row = c.app.state.ctx.conn.execute("SELECT value_json FROM settings").fetchone()[0]  # type: ignore[attr-defined]
    assert Settings.model_validate_json(row).sensors.blower == ""
