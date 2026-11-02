"""v0.4.0: provider resolution in the runtime, GET /api/forecast, weather setup endpoints.
HTTP is mocked everywhere (MockHA, httpx.MockTransport)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.house import house_dict, house_settings, seed_house
from tests.integration.mock_ha import GOOD_TOKEN, MockHA
from tests.integration.test_jobs import NOW, Clock, nws_ok
from tests.integration.test_open_meteo import om_body
from thermabot.adapters import db
from thermabot.adapters.ha_rest import HARest
from thermabot.app import jobs as J
from thermabot.app.auth import Auth, create_user
from thermabot.app.main import build, load_env
from thermabot.app.routes_api import Ctx, create_app
from thermabot.core.statemachine import Live, Persisted
from thermabot.core.thermal import ThermalParams

PW = "correct horse"


def om_transport(log: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def h(req: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(req)
        return httpx.Response(200, json=om_body(start=NOW))

    return httpx.MockTransport(h)


def runtime(tmp: Path, settings: Any = None, om_log: list[httpx.Request] | None = None) -> Any:
    e = load_env({"HA_URL": "http://ha", "HA_TOKEN": GOOD_TOKEN, "DATA_DIR": str(tmp)})
    seed_house(tmp, settings)
    return build(
        e,
        clock=Clock(NOW),
        ha_transport=httpx.ASGITransport(app=MockHA().app),
        nws_transport=nws_ok(),
        open_meteo_transport=om_transport(om_log),
    )


@pytest.mark.brd("FR-WX-2")
async def test_legacy_row_replans_with_nws(tmp_path: Path) -> None:
    rt = runtime(tmp_path)  # house_dict has no weather key -> nws
    await rt.replan(NOW)
    assert rt.weather is not None and rt.weather.name == "nws"
    assert rt.ctx.forecast is not None and rt.ctx.forecast.source == "nws"
    src = rt.conn.execute("SELECT forecast_source FROM plans").fetchone()[0]
    assert src == "nws"
    await rt.aclose()


@pytest.mark.brd("FR-WX-2")
async def test_open_meteo_with_explicit_location(tmp_path: Path) -> None:
    log: list[httpx.Request] = []
    s = house_settings(weather={"provider": "open_meteo", "lat": 51.5, "lon": -0.12})
    rt = runtime(tmp_path, s, log)
    await rt.replan(NOW)
    assert rt.ctx.forecast is not None and rt.ctx.forecast.source == "open_meteo"
    assert log[0].url.params["latitude"] == "51.5"
    assert not rt.weather_stale
    await rt.aclose()


@pytest.mark.brd("FR-WX-2")
async def test_provider_switch_rebuilds_client(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    await rt.replan(NOW)
    assert rt.weather.name == "nws"
    d = {**house_dict(), "weather": {"provider": "open_meteo"}}
    rt.conn.execute("UPDATE settings SET value_json=? WHERE key='settings'", (json.dumps(d),))
    await rt.replan(NOW)
    assert rt.weather.name == "open_meteo"
    assert rt.weather_key == ("open_meteo", 40.0, -75.0)  # zone.home from MockHA
    await rt.aclose()


@pytest.mark.brd("FR-WX-2")
async def test_weather_down_never_crashes_replan(tmp_path: Path) -> None:
    e = load_env({"HA_URL": "http://ha", "HA_TOKEN": GOOD_TOKEN, "DATA_DIR": str(tmp_path)})
    seed_house(tmp_path, house_settings(weather={"provider": "open_meteo", "lat": 1, "lon": 1}))
    rt = build(
        e,
        clock=Clock(NOW),
        ha_transport=httpx.ASGITransport(app=MockHA().app),
        open_meteo_transport=httpx.MockTransport(lambda r: httpx.Response(503)),
    )
    rt.weather = None
    await rt.replan(NOW)
    assert rt.ctx.plan is not None and rt.ctx.plan.kind == "fallback"
    assert all(st.hvac_mode != "off" for st in rt.ctx.plan.steps)
    assert rt.ctx.weather_health == "stale"
    await rt.aclose()


# ---- API --------------------------------------------------------------------------------
def make(tmp: Path) -> tuple[TestClient, Ctx, MockHA]:
    now = NOW
    conn = db.open_db(tmp / "t.db", now=now)
    db.write_bot_state(conn, Persisted(kill_switch="on", run_mode="shadow"), by="t", now=now)
    for name, role in (("alex", "owner"), ("ann", "admin"), ("jordan", "member")):
        create_user(conn, name, role, PW, now=now)  # type: ignore[arg-type]
    ha = MockHA()
    ctx = Ctx(
        conn=conn,
        auth=Auth(conn),
        clock=Clock(now),
        settings=house_settings(),
        live=Live(ha_mirror="on", tstat_age_min=0),
        ha_read=HARest("http://ha", GOOD_TOKEN, transport=httpx.ASGITransport(app=ha.app)),
        weather_transports={"open_meteo": om_transport(), "nws": nws_ok()},
    )
    return TestClient(create_app(ctx)), ctx, ha


def login(c: TestClient, user: str) -> dict[str, str]:
    r = c.post("/login", json={"username": user, "password": PW})
    assert r.status_code == 200
    return {"X-CSRF-Token": r.json()["csrf"]}


@pytest.mark.brd("FR-WX-1")
@pytest.mark.parametrize("user", ["alex", "ann", "jordan"])
def test_api_forecast_any_role_unfitted(tmp_path: Path, user: str) -> None:
    from thermabot.core.types import Forecast, ForecastPoint

    c, ctx, _ = make(tmp_path)
    assert c.get("/api/forecast").status_code == 401
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    ctx.forecast = Forecast(
        source="nws",
        fetched_at=now,
        points=[ForecastPoint(t=now + timedelta(hours=h), temp_f=40) for h in range(60)],
    )
    login(c, user)
    d = c.get("/api/forecast").json()
    assert d["provider"] == "nws" and d["source"] == "nws" and d["stale"] is False
    pr = d["projection"]
    assert len(pr["hours"]) == 48 and pr["fallback"] is True and pr["model_fitted"] is False
    assert pr["banner"] == "Fallback plan, model not fitted yet"
    h = pr["hours"][0]
    assert {"t", "temp_f", "mode", "heat_sp", "cool_sp", "indoor_f", "heat_min", "cool_min"} <= set(
        h
    )
    assert all(x["mode"] != "off" for x in pr["hours"])


@pytest.mark.brd("FR-WX-1")
def test_api_forecast_fitted_model_and_no_forecast(tmp_path: Path) -> None:
    c, ctx, _ = make(tmp_path)
    J.save_model(ctx.conn, ThermalParams(), 0.4, 500, datetime.now(UTC))
    login(c, "jordan")
    d = c.get("/api/forecast").json()
    assert d["stale"] is True and d["source"] is None and d["model_version"] == 1
    assert d["projection"]["model_fitted"] is True and len(d["projection"]["hours"]) == 48


@pytest.mark.brd("FR-WX-2")
def test_ha_location_and_weather_test(tmp_path: Path) -> None:
    c, ctx, ha = make(tmp_path)
    h = login(c, "ann")  # admin may edit comfort/weather
    r = c.get("/api/setup/ha-location").json()
    assert r == {
        "ok": True,
        "lat": 40.0,
        "lon": -75.0,
        "location_name": "Test House",
        "error": None,
    }
    r = c.post(
        "/api/setup/weather-test",
        json={"provider": "open_meteo", "lat": 51.5, "lon": -0.12},
        headers=h,
    ).json()
    assert r["ok"] and r["points"] == 96 and len(r["preview"]) <= 12
    assert (
        c.post(
            "/api/setup/weather-test", json={"provider": "nws", "lat": 99, "lon": 0}, headers=h
        ).status_code
        == 422
    )
    assert ha.calls == []  # read-only towards HA
    assert ctx.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0  # saves nothing
    assert ctx.forecast is None  # running forecast untouched


@pytest.mark.brd("FR-WX-2")
def test_weather_test_reports_failure(tmp_path: Path) -> None:
    c, ctx, _ = make(tmp_path)
    ctx.weather_transports = {"nws": httpx.MockTransport(lambda r: httpx.Response(404))}
    h = login(c, "alex")
    r = c.post(
        "/api/setup/weather-test", json={"provider": "nws", "lat": 51.5, "lon": 0}, headers=h
    ).json()
    assert not r["ok"] and "404" in r["error"] and "US only" in r["error"]


@pytest.mark.brd("FR-USR-2")
def test_weather_endpoints_need_edit_comfort(tmp_path: Path) -> None:
    c, _, _ = make(tmp_path)
    h = login(c, "jordan")
    assert c.get("/api/setup/ha-location").status_code == 403
    body = {"provider": "open_meteo", "lat": 1, "lon": 1}
    assert c.post("/api/setup/weather-test", json=body, headers=h).status_code == 403


@pytest.mark.brd("FR-UI-1")
def test_forecast_page_renders(tmp_path: Path) -> None:
    c, _, _ = make(tmp_path)
    login(c, "jordan")
    r = c.get("/forecast")
    assert r.status_code == 200 and 'data-testid="fallback-banner"' in r.text
    assert "model not fitted yet" in r.text
