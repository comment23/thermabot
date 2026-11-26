"""All researched room tools: read-only evidence, explicit approvals and persistence."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Literal

import pytest

from tests.house import house_settings
from tests.integration.test_api import NOW, login, make
from thermabot.adapters import db
from thermabot.app import jobs as J
from thermabot.app.room_insights import observations, report, setup_readiness
from thermabot.core.thermal import ThermalParams
from thermabot.core.types import Forecast, ForecastPoint, RoomReading, Snapshot, ThermostatState


def doc() -> dict[str, object]:
    return {
        "tstat": {"temp": 73, "age_min": 0, "hvac_action": "idle"},
        "rooms": {
            "bedroom_1": {
                "temp": 73,
                "age_min": 0,
                "humidity": {"value": 50, "age_min": 0, "source": "sensor.bedroom_1_humidity"},
            }
        },
        "house": "EMPTY",
        "block": "home",
    }


def seed_model(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO models(version,fitted_at,params_json,mae_json,n_samples,active)"
        " VALUES(1,?,?,?,?,1)",
        (
            NOW.isoformat(),
            ThermalParams(a=0.03, s=0).model_dump_json(),
            json.dumps({"thermostat": 0.5}),
            2000,
        ),
    )


@pytest.mark.brd("FR-UI-1")
def test_setup_readiness_reuses_simulation_gates_without_running_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from thermabot.core import insights as I

    ctx, _, client = make(tmp_path, settings=house_settings())
    headers = login(client, "ann")
    ctx.snapshot = Snapshot(
        t=NOW,
        thermostat=ThermostatState(hvac_mode="heat", current_temp=73, last_reported=NOW),
        rooms={"bedroom_1": RoomReading(key="bedroom_1", temp=74, age_min=0)},
    )
    ctx.forecast = Forecast(
        source="ha",
        fetched_at=NOW,
        points=[
            ForecastPoint(t=NOW + timedelta(hours=i), temp_f=45)
            for i in range(26)
            if i not in (9, 10)
        ],
    )
    seed_model(ctx.conn)

    def no_simulation(*args: object, **kwargs: object) -> None:
        raise AssertionError("readiness must not run a simulation")

    monkeypatch.setattr(I, "simulate", no_simulation)
    before = ctx.conn.total_changes
    ready = setup_readiness(
        ctx.conn,
        NOW,
        "bedroom_1",
        ctx.settings,
        ctx.snapshot,
        ctx.forecast,
        report(ctx.conn, NOW, "bedroom_1", ctx.settings),
    )
    assert ready["setback_ready"] and not ready["tariff_ready"] and not ready["cost_ready"]
    assert ready["current_f"] == 74
    assert ctx.conn.total_changes == before
    checks = {c["key"]: c for c in ready["checks"]}
    assert checks["model"]["ready"] and checks["forecast"]["ready"]
    assert not checks["meter"]["ready"] and checks["meter"]["optional"]
    long = client.post("/api/rooms/bedroom_1/what-if", json={"hours": 8}, headers=headers).json()
    assert not long["available"] and "Forecast gaps" in long["reason"]
    ctx.snapshot.rooms["bedroom_1"].estimated = True
    ctx.conn.execute("UPDATE models SET fitted_at=?", ((NOW - timedelta(days=31)).isoformat(),))
    before = ctx.conn.total_changes
    blocked = setup_readiness(
        ctx.conn,
        NOW,
        "bedroom_1",
        ctx.settings,
        ctx.snapshot,
        ctx.forecast,
        report(ctx.conn, NOW, "bedroom_1", ctx.settings),
    )
    checks = {c["key"]: c for c in blocked["checks"]}
    assert not blocked["setback_ready"] and blocked["current_f"] is None
    assert not checks["room"]["ready"] and not checks["model"]["ready"]
    assert ctx.conn.total_changes == before


@pytest.mark.brd("NFR-SEC-1")
def test_insights_auth_room_isolation_and_read_only_counterfactuals(tmp_path: Path) -> None:
    ctx, _, client = make(tmp_path)
    ctx.conn.execute("UPDATE users SET room='bedroom_1' WHERE username='jordan'")
    J.save_sample(ctx.conn, NOW, doc(), "SHADOW", 90, [])
    assert client.get("/api/rooms/bedroom_1/insights").status_code == 401
    headers = login(client, "jordan")
    before = ctx.conn.total_changes
    result = client.get("/api/rooms/bedroom_1/insights")
    assert result.status_code == 200
    assert result.json()["ideal_f"] == 73 and result.json()["humidity"]["mean_rh"] == 50
    assert client.get("/api/rooms/study/insights").status_code == 403
    endpoint = "/api/rooms/bedroom_1/what-if"
    assert client.post(endpoint, json={}).status_code == 403  # CSRF, even read-only POST
    assert not client.post(endpoint, json={}, headers=headers).json()["available"]
    ctx.snapshot = Snapshot(
        t=NOW,
        thermostat=ThermostatState(hvac_mode="heat", current_temp=73, last_reported=NOW),
        rooms={"bedroom_1": RoomReading(key="bedroom_1", temp=73, age_min=0)},
    )
    ctx.forecast = Forecast(
        source="ha",
        fetched_at=NOW,
        points=[ForecastPoint(t=NOW + timedelta(hours=i), temp_f=45) for i in range(49)],
    )
    seed_model(ctx.conn)
    before = ctx.conn.total_changes
    simulated = client.post(endpoint, json={"scenario": "setback"}, headers=headers).json()
    assert simulated["available"] and simulated["simulated"]
    assert simulated["baseline"]["comfort_pct"] == 100
    assert simulated["proposed"]["comfort_pct"] < 100 and not simulated["recommended"]
    assert simulated["saved_min"] > 0
    assert "constant" in simulated["note"]
    assert not client.post(endpoint, json={"scenario": "tariff"}, headers=headers).json()[
        "available"
    ]
    ctx.settings = ctx.settings.model_copy(
        update={
            "insights": ctx.settings.insights.model_copy(
                update={"off_peak_rate": 0.1, "peak_rate": 0.4, "rated_power_kw": 3}
            )
        }
    )
    tariff = client.post(endpoint, json={"scenario": "tariff"}, headers=headers).json()
    assert tariff["available"] and tariff["simulated"]
    assert tariff["baseline"]["cost"] > 0 and tariff["proposed"]["cost"] > 0
    assert tariff["cost_saving"] == pytest.approx(
        tariff["baseline"]["cost"] - tariff["proposed"]["cost"]
    )
    assert client.post(endpoint, json={"setback_f": 10}, headers=headers).status_code == 422
    assert ctx.conn.total_changes == before
    ctx.conn.execute("UPDATE models SET params_json='{}'")
    assert "invalid" in client.post(endpoint, json={}, headers=headers).json()["reason"]


@pytest.mark.brd("FR-MET-1")
@pytest.mark.parametrize("age", [0, 100, None])
def test_equipment_and_thermostat_humidity_need_source_freshness(
    tmp_path: Path, age: float | None
) -> None:
    ctx, _, _ = make(tmp_path)
    reading = doc()
    reading["tstat"] = {
        "temp": 73,
        "age_min": age,
        "hvac_action": "heating",
        "humidity": 50,
        "humidity_reading": {"value": 50, "age_min": age, "source": "climate.thermostat"},
    }
    reading["cs"] = 80
    reading["compressor_age_min"] = age
    J.save_sample(ctx.conn, NOW, reading, "SHADOW", 90, [])
    observed = observations(ctx.conn, NOW, "thermostat", ctx.settings)[0]
    if age == 0:
        assert observed.humidity == 50 and observed.action == "heating" and observed.cs == 80
    else:
        assert observed.humidity is None and observed.action is None and observed.cs is None
    reading["tstat"] = {"temp": 73, "age_min": 0, "humidity": 50}
    reading.pop("compressor_age_min")
    J.save_sample(ctx.conn, NOW, reading, "SHADOW", 90, [])
    legacy = observations(ctx.conn, NOW, "thermostat", ctx.settings)[0]
    assert legacy.humidity is None and legacy.cs is None


@pytest.mark.brd("FR-MET-1")
@pytest.mark.parametrize("invalid_input", ["old_model", "future_forecast", "stale_thermostat"])
def test_simulations_reject_old_or_untrustworthy_inputs(tmp_path: Path, invalid_input: str) -> None:
    ctx, _, client = make(tmp_path)
    ctx.snapshot = Snapshot(
        t=NOW,
        thermostat=ThermostatState(hvac_mode="heat", current_temp=73, last_reported=NOW),
        rooms={"bedroom_1": RoomReading(key="bedroom_1", temp=73, age_min=0)},
    )
    ctx.forecast = Forecast(
        source="ha",
        fetched_at=NOW,
        points=[ForecastPoint(t=NOW + timedelta(hours=i), temp_f=45) for i in range(49)],
    )
    seed_model(ctx.conn)
    if invalid_input == "old_model":
        ctx.conn.execute("UPDATE models SET fitted_at=?", ((NOW - timedelta(days=31)).isoformat(),))
    elif invalid_input == "future_forecast":
        ctx.forecast.fetched_at = NOW + timedelta(minutes=1)
    else:
        ctx.snapshot.thermostat.last_reported = NOW - timedelta(minutes=11)
    headers = login(client, "ann")
    before = ctx.conn.total_changes
    result = client.post("/api/rooms/bedroom_1/what-if", json={}, headers=headers).json()
    assert not result["available"] and result["reason"]
    assert ctx.conn.total_changes == before


@pytest.mark.brd("FR-CMF-2")
def test_schedule_suggestion_requires_fresh_evidence_and_approval(tmp_path: Path) -> None:
    ctx, _, client = make(tmp_path)
    for d in (3, 4, 5, 6, 7):  # five completed weekdays at 10:00 local
        for m in range(60):
            J.save_sample(
                ctx.conn,
                NOW.replace(hour=14) - timedelta(days=d) + timedelta(minutes=m),
                doc(),
                "SHADOW",
                90,
                [],
            )
    headers = login(client, "ann")
    evidence = client.get("/api/rooms/bedroom_1/insights").json()
    candidate = next(s for s in evidence["suggestions"] if s["id"] == "weekday-10")
    assert candidate["days"] == 5
    original = ctx.settings
    replans: list[int] = []
    ctx.replan = lambda: replans.append(1)
    endpoint = "/api/rooms/bedroom_1/schedule-approval"
    body = {"suggestion_id": candidate["id"], "fingerprint": evidence["fingerprint"]}
    assert (
        client.post(endpoint, json={**body, "fingerprint": "0" * 64}, headers=headers).status_code
        == 409
    )
    assert client.post(endpoint, json=body, headers=headers).status_code == 200
    schedule = [(b.start, b.block) for b in ctx.settings.schedule.weekday]
    assert schedule == [("06:30", "home"), ("10:00", "away"), ("11:00", "home"), ("22:00", "sleep")]
    assert ctx.settings.bands == original.bands and ctx.settings.safety == original.safety
    assert replans == [1] and ctx.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0
    state = db.read_bot_state(ctx.conn)
    assert state is not None and state.run_mode == "shadow"
    assert client.post(endpoint, json=body, headers=headers).status_code == 409
    member = login(client, "jordan")
    assert client.post(endpoint, json=body, headers=member).status_code == 403


@pytest.mark.brd("FR-MET-1")
def test_experiments_persist_and_mark_settings_changes(tmp_path: Path) -> None:
    ctx, _, client = make(tmp_path)
    headers = login(client, "ann")
    endpoint = "/api/rooms/bedroom_1/experiments"
    body = {"name": "Window sealing", "change_day": NOW.date().isoformat()}
    assert client.post(endpoint, json=body, headers=headers).status_code == 200
    stored = report(ctx.conn, NOW, "bedroom_1", ctx.settings)["experiments"]
    assert len(stored) == 1 and stored[0]["name"] == "Window sealing"
    assert not stored[0]["comparison"]["available"] and not stored[0]["settings_changed"]
    assert ctx.conn.execute("SELECT action FROM audit ORDER BY id DESC LIMIT 1").fetchone()[0] == (
        "room_experiment_create"
    )
    updated = ctx.settings.model_dump()
    updated["insights"]["ideal_temp_f"] = 74
    assert client.put("/api/settings", json=updated, headers=headers).status_code == 200
    assert client.get("/api/rooms/bedroom_1/insights").json()["experiments"][0]["settings_changed"]
    assert (
        client.post(
            endpoint,
            json={**body, "change_day": (NOW + timedelta(days=1)).date().isoformat()},
            headers=headers,
        ).status_code
        == 422
    )


@pytest.mark.brd("FR-MET-1")
def test_weather_alert_and_completed_experiment_survive_report_window(tmp_path: Path) -> None:
    ctx, _, client = make(tmp_path)
    for delta, runtime in [(i, 100) for i in range(16, 21)] + [(i, 120) for i in range(5, 10)]:
        ctx.conn.execute(
            "INSERT INTO efficiency_daily(day,mean_out_f,hdd,cdd,heat_min,cool_min,"
            "n_samples,out_hours,coverage_pct,created_at)"
            " VALUES(?,45,20,0,?,0,1440,24,100,?)",
            ((NOW - timedelta(days=delta)).date().isoformat(), runtime, NOW.isoformat()),
        )
    headers = login(client, "ann")
    result = client.get("/api/rooms/bedroom_1/insights").json()
    assert result["weather"]["worse"] and result["weather"]["change_pct"] == 20
    assert any(a["priority"] == 75 for a in result["actions"])
    assert (
        client.post(
            "/api/rooms/bedroom_1/experiments",
            headers=headers,
            json={
                "name": "Filter change",
                "change_day": (NOW - timedelta(days=14)).date().isoformat(),
            },
        ).status_code
        == 200
    )
    for now in (NOW, NOW + timedelta(days=21)):
        experiment = report(ctx.conn, now, "bedroom_1", ctx.settings)["experiments"][0]
        assert experiment["comparison"]["available"]
        assert experiment["comparison"]["change_pct"] == 20
        assert not experiment["settings_changed"]


@pytest.mark.brd("FR-MET-1")
def test_dedicated_meter_and_stale_humidity_are_not_whole_home_allocations(tmp_path: Path) -> None:
    settings = house_settings()
    settings.rooms["bedroom_1"].energy = "sensor.bedroom_1_energy"
    ctx, _, _ = make(tmp_path, settings=settings)
    for m in (0, 1):
        reading = doc()
        reading["rooms"] = {
            "bedroom_1": {
                "temp": 73,
                "age_min": 0,
                "energy": {
                    "value": 10 + m * 0.1,
                    "age_min": 0,
                    "source": "sensor.bedroom_1_energy",
                },
                "humidity": {"value": 80, "age_min": 100, "source": "sensor.bedroom_1_humidity"},
            }
        }
        reading["energy"] = {"value": 1000, "age_min": 0, "source": "sensor.hvac_energy"}
        J.save_sample(ctx.conn, NOW - timedelta(minutes=1 - m), reading, "SHADOW", 90, [])
    result = report(ctx.conn, NOW, "bedroom_1", settings)
    assert result["energy"]["scope"] == "dedicated room meter"
    assert result["energy"]["kwh"] == pytest.approx(0.1)
    assert result["humidity"]["minutes"] == 0
    assert result["actions"][0]["priority"] == 100
    assert len(observations(ctx.conn, NOW, "bedroom_1", settings)) == 2


@pytest.mark.brd("FR-MET-1")
@pytest.mark.parametrize("source", [None, ""])
def test_unidentified_meter_never_reports_energy(tmp_path: Path, source: str | None) -> None:
    ctx, _, _ = make(tmp_path)
    for m in (0, 1):
        reading = doc()
        reading["energy"] = {"value": 10 + m * 0.1, "age_min": 0, "source": source}
        J.save_sample(ctx.conn, NOW - timedelta(minutes=1 - m), reading, "SHADOW", 90, [])
    energy = report(ctx.conn, NOW, "bedroom_1", ctx.settings)["energy"]
    assert energy["kwh"] is None and energy["covered_minutes"] == 0


@pytest.mark.brd("NFR-REL-2")
def test_forward_migration_preserves_a_real_shaped_predecessor_copy(tmp_path: Path) -> None:
    original = sqlite3.connect(tmp_path / "original.db")
    for file in sorted(db.MIGRATIONS.glob("*.sql")):
        if int(file.name.split("_")[0]) <= 4:
            original.executescript(file.read_text(encoding="utf-8"))
    original.execute("PRAGMA user_version=4")
    original.execute(
        "INSERT INTO samples(ts,snapshot_json,bot_state) VALUES (?,?,'SHADOW')",
        (NOW.isoformat(), json.dumps(doc())),
    )
    original.commit()
    copy = sqlite3.connect(tmp_path / "copy.db")
    original.backup(copy)
    copy.close()
    migrated = db.open_db(tmp_path / "copy.db", now=NOW)
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 5
    assert migrated.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    assert migrated.execute("SELECT COUNT(*) FROM room_experiments").fetchone()[0] == 0
    assert original.execute("PRAGMA user_version").fetchone()[0] == 4
    migrated.close()
    original.close()


@pytest.mark.brd("FR-CFG-1")
@pytest.mark.parametrize(
    "insights",
    [
        {"ideal_temp_f": float("nan")},
        {"off_peak_rate": 0.1},
        {"off_peak_rate": 0.3, "peak_rate": 0.1},
        {"peak_start_hour": 22, "peak_end_hour": 6},
    ],
)
def test_analysis_assumptions_are_validated(insights: dict[str, object]) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        house_settings(insights=insights)


@pytest.mark.brd("FR-MET-1")
@pytest.mark.parametrize(
    ("kind", "unit", "value", "expected"),
    [
        ("energy", "Wh", "1000", 1),
        ("power", "W", "3000", 3),
        ("humidity", "%", "50", 50),
        ("energy", "W", "1000", None),
    ],
)
def test_sensor_units_are_normalized_without_actuation(
    kind: Literal["energy", "power", "humidity", "aux"],
    unit: str,
    value: str,
    expected: float | None,
) -> None:
    measurement = J.sensor_measurement(
        NOW,
        {
            "entity_id": "sensor.study_temperature",
            "state": value,
            "attributes": {"unit_of_measurement": unit},
            "last_reported": NOW.isoformat(),
        },
        kind,
    )
    assert measurement["value"] == expected and measurement["age_min"] == 0
