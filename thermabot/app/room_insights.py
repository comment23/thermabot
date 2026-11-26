"""Room reports over existing samples; all calculations and proposals are read-only."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal

from pydantic import ValidationError

from thermabot.app.efficiency import _eff_days
from thermabot.app.jobs import num as finite_num
from thermabot.config import Settings
from thermabot.core import insights as I
from thermabot.core.schedule import active_zone
from thermabot.core.thermal import ThermalParams, in_range
from thermabot.core.types import Forecast, Snapshot
from thermabot.core.weather import is_stale

log = logging.getLogger(__name__)


def num(value: object) -> float | None:
    return (
        finite_num(float(value))
        if isinstance(value, int | float) and not isinstance(value, bool)
        else None
    )


def fingerprint(settings: Settings) -> str:
    return sha256(settings.model_dump_json().encode()).hexdigest()


def _object(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        log.warning("ignoring non-object room telemetry")
        return {}
    return value


def _fresh(value: dict[str, Any], limit: float) -> bool:
    age = num(value.get("age_min"))
    return age is not None and 0 <= age <= limit


def _meter(value: dict[str, Any], limit: float) -> I.Meter | None:
    number, source = num(value.get("value")), value.get("source")
    if (
        number is None
        or number < 0
        or not isinstance(source, str)
        or not source
        or not _fresh(value, limit)
    ):
        return None
    return I.Meter(number, source)


def observations(
    conn: sqlite3.Connection, now: datetime, room: str, settings: Settings
) -> list[I.Observation]:
    cfg = settings.rooms[room]
    rows = conn.execute(
        """SELECT ts, stale_rooms,
          json_extract(snapshot_json, '$.tstat') AS tstat,
          CASE WHEN :room = 'thermostat' THEN json_extract(snapshot_json, '$.tstat')
            ELSE (SELECT value FROM json_each(snapshot_json, '$.rooms') WHERE key = :room)
          END AS reading,
          json_extract(snapshot_json, '$.energy') AS energy,
          json_extract(snapshot_json, '$.power') AS power,
          json_extract(snapshot_json, '$.auxiliary_heat') AS aux,
          json_extract(snapshot_json, '$.cs') AS cs,
          json_extract(snapshot_json, '$.compressor_age_min') AS cs_age,
          json_extract(snapshot_json, '$.house') AS house,
          json_extract(snapshot_json, '$.block') AS block
        FROM samples WHERE ts >= :start AND ts <= :end ORDER BY ts""",
        {
            "room": room,
            "start": (now - timedelta(days=30)).astimezone(UTC).isoformat(),
            "end": now.astimezone(UTC).isoformat(),
        },
    )
    out = []
    for row in rows:
        reading, th = _object(row["reading"]), _object(row["tstat"])
        raw_temp = num(reading.get("temp"))
        stale = room in (row["stale_rooms"] or "").split(",") or not _fresh(reading, cfg.stale_min)
        quality: Literal["fresh", "stale", "missing"] = (
            "missing" if raw_temp is None else "stale" if stale else "fresh"
        )
        rh = reading.get("humidity_reading" if room == "thermostat" else "humidity")
        humidity = _meter(rh, cfg.stale_min) if isinstance(rh, dict) else None
        th_fresh = _fresh(th, settings.rooms.get("thermostat", cfg).stale_min)
        energy = reading.get("energy") if cfg.energy else _object(row["energy"])
        power = {} if cfg.energy else _object(row["power"])
        aux = _object(row["aux"])
        out.append(
            I.Observation(
                t=datetime.fromisoformat(row["ts"]),
                temp=raw_temp if quality == "fresh" else None,
                quality=quality,
                indoor=num(th.get("temp"))
                if _fresh(th, settings.rooms.get("thermostat", cfg).stale_min)
                else None,
                humidity=humidity.value if humidity is not None and humidity.value <= 100 else None,
                energy=_meter(energy, settings.insights.meter_stale_min)
                if isinstance(energy, dict)
                else None,
                power=_meter(power, settings.insights.meter_stale_min),
                aux=aux.get("value")
                if isinstance(aux.get("value"), bool)
                and _fresh(aux, settings.insights.meter_stale_min)
                else None,
                action=th.get("hvac_action")
                if th_fresh and isinstance(th.get("hvac_action"), str)
                else None,
                cs=num(row["cs"])
                if th_fresh
                and _fresh({"age_min": row["cs_age"]}, settings.insights.meter_stale_min)
                else None,
                house=row["house"],
                block=row["block"],
            )
        )
    return out


def report(
    conn: sqlite3.Connection, now: datetime, room: str, settings: Settings
) -> dict[str, Any]:
    rows = observations(conn, now, room, settings)
    week = [r for r in rows if r.t > now - timedelta(days=7)]
    quality = I.sensor_quality(week, 7 * 1440)
    humidity = I.humidity_comfort(week, settings.insights)
    equipment = I.heat_pump_diagnostics(week)
    energy = I.energy_usage(week, settings.insights)
    energy["scope"] = "dedicated room meter" if settings.rooms[room].energy else "central HVAC"
    today = now.astimezone(active_zone()).date()
    experiment_rows = conn.execute(
        "SELECT * FROM room_experiments WHERE room=? ORDER BY id DESC LIMIT 20", (room,)
    ).fetchall()
    baseline_start = (today - timedelta(days=28)).isoformat()
    day_start = min(
        [baseline_start]
        + [
            (date.fromisoformat(row["change_day"]) - timedelta(days=14)).isoformat()
            for row in experiment_rows
        ]
    )
    days = _eff_days(conn, day_start)
    split = (today - timedelta(days=14)).isoformat()
    weather = I.weather_comparison(
        [d for d in days if baseline_start <= d.day < split],
        [d for d in days if split <= d.day < today.isoformat()],
    )
    suggestions = []
    for proposal in I.schedule_suggestions(rows):
        blocks = getattr(settings.schedule, proposal["kind"])
        start, end = f"{proposal['hour']:02}:00", f"{proposal['hour'] + 1:02}:00"
        previous = [b for b in blocks if b.start <= start]
        current = previous[-1] if previous else blocks[-1]
        if current.block == "home" and not any(start < b.start < end for b in blocks):
            suggestions.append(proposal)
    experiments = []
    for row in experiment_rows:
        change = date.fromisoformat(row["change_day"])
        start, end = (
            (change - timedelta(days=14)).isoformat(),
            (change + timedelta(days=14)).isoformat(),
        )
        comparison = I.weather_comparison(
            [d for d in days if start <= d.day < change.isoformat()],
            [d for d in days if change.isoformat() <= d.day < min(end, today.isoformat())],
        )
        changed = row["settings_json"] != settings.model_dump_json()
        experiments.append(
            {
                "id": row["id"],
                "name": row["name"],
                "change_day": row["change_day"],
                "comparison": comparison,
                "settings_changed": changed,
            }
        )
    actions: list[dict[str, Any]] = []

    def action(priority: int, title: str, evidence: str) -> None:
        actions.append({"priority": priority, "title": title, "evidence": evidence})

    if quality["coverage_pct"] < 80:
        action(
            100,
            "Restore reliable sensor coverage",
            f"{quality['coverage_pct']:.1f}% of the week measured",
        )
    if quality["persistent_disagreement"]:
        action(
            90,
            "Check sensor placement and room airflow",
            f"{quality['paired_minutes']} paired minutes; persistent thermostat disagreement",
        )
    if equipment["cycling_alert"]:
        action(
            85,
            "Review short cycling with your HVAC professional",
            f"{equipment['short_runs']} completed runs shorter than 3 minutes",
        )
    if humidity["outside_rh_minutes"]:
        action(
            80,
            "Review humidity and ventilation",
            f"{humidity['outside_rh_minutes']} paired minutes outside 30-60% RH",
        )
    if weather.get("worse"):
        action(
            75,
            "Investigate weather-normalized runtime increase",
            f"{weather['change_pct']:.1f}% increase; check filters, schedule and envelope",
        )
    temps = [r.temp for r in week if r.temp is not None]
    ideal_pct = (
        100
        * sum(
            abs(t - settings.insights.ideal_temp_f) <= settings.insights.tolerance_f for t in temps
        )
        / len(temps)
        if temps
        else None
    )
    if ideal_pct is not None and ideal_pct < 80:
        action(
            70,
            "Review the room's ideal-temperature trade-offs",
            f"{ideal_pct:.1f}% of measured minutes inside the ideal reference band",
        )
    if equipment["aux_minutes"]:
        action(
            65,
            "Review auxiliary-heat use",
            f"{equipment['aux_minutes']} measured auxiliary-heat minutes",
        )
    if suggestions:
        action(
            60,
            "Review occupancy-based schedule suggestions",
            f"{len(suggestions)} repeatable time windows; approval required",
        )
    if energy["kwh"] is None:
        action(
            30,
            "Configure a dedicated HVAC or room energy meter",
            "No usable consecutive meter/power samples",
        )
    if settings.insights.peak_rate is None:
        action(
            20,
            "Configure your tariff for cost comparisons",
            "Peak/off-peak prices are not configured",
        )
    return {
        "ideal_f": settings.insights.ideal_temp_f,
        "tolerance_f": settings.insights.tolerance_f,
        "ideal_pct": ideal_pct,
        "quality": quality,
        "humidity": humidity,
        "energy": energy,
        "equipment": equipment,
        "weather": weather,
        "suggestions": suggestions,
        "fingerprint": fingerprint(settings),
        "experiments": experiments,
        "actions": sorted(actions, key=lambda a: a["priority"], reverse=True),
        "week_start": now - timedelta(days=7),
        "week_end": now,
    }


@dataclass(frozen=True)
class _SimulationInputs:
    indoor: float
    offset: float
    params: ThermalParams
    version: int
    times: list[datetime]
    outdoor: list[float]


def _simulation_inputs(
    conn: sqlite3.Connection,
    now: datetime,
    room: str,
    settings: Settings,
    snapshot: Snapshot | None,
    forecast: Forecast | None,
    hours: int,
) -> tuple[_SimulationInputs | None, list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []

    def check(key: str, label: str, reason: str | None, href: str, cap: str) -> None:
        checks.append(
            {
                "key": key,
                "label": label,
                "ready": reason is None,
                "detail": reason or "Ready",
                "href": href,
                "cap": cap,
                "optional": False,
            }
        )

    reading = snapshot.rooms.get(room) if snapshot is not None else None
    room_temp = num(reading.temp) if reading is not None else None
    room_reason = None
    if snapshot is None or not timedelta(0) <= now - snapshot.t <= timedelta(minutes=10):
        room_reason = "A fresh live snapshot is required."
    elif reading is None or room_temp is None or reading.stale or reading.estimated:
        room_reason = (
            "A fresh measured room temperature is required; estimates are not substituted."
        )
    else:
        age = reading.age_min
        if reading.last_reported is not None:
            age = (now - reading.last_reported).total_seconds() / 60
        if age is None or not 0 <= age <= settings.rooms[room].stale_min:
            room_reason = "Room reading age is unknown or stale."
    check("room", "Room temperature", room_reason, "settings/people", "edit_comfort")
    th = snapshot.thermostat if snapshot is not None else None
    indoor = num(th.current_temp) if th is not None else None
    th_reason = None
    if (
        th is None
        or indoor is None
        or th.last_reported is None
        or not 0 <= (now - th.last_reported).total_seconds() <= 600
    ):
        th_reason = "A fresh thermostat measurement is required."
    check("thermostat", "Central thermostat", th_reason, "settings/ha", "edit_safety")

    times = [now + timedelta(minutes=15 * i) for i in range((hours + 4) * 4)]
    outdoor: list[float] = []
    forecast_reason = None
    if forecast is None or forecast.fetched_at > now or is_stale(forecast, now, settings.weather):
        forecast_reason = "A fresh forecast with sufficient coverage is required."
    else:
        for t in times:
            candidates = [p for p in forecast.points if p.t <= t]
            point = max(candidates, key=lambda p: p.t) if candidates else None
            if point is None or t - point.t > timedelta(hours=1) or num(point.temp_f) is None:
                forecast_reason = "Forecast gaps prevent an honest simulation."
                break
            outdoor.append(point.temp_f)
    check("forecast", "Forecast coverage", forecast_reason, "settings/ha", "edit_safety")

    model = conn.execute(
        "SELECT params_json, mae_json, version, fitted_at FROM models"
        " WHERE active=1 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    params = None
    model_reason = None
    if model is None:
        model_reason = "Fit and validate the thermal model before running simulations."
    else:
        try:
            raw = json.loads(model["params_json"])
            errors = json.loads(model["mae_json"])
            if not isinstance(errors, dict):
                raise ValueError("invalid model validation data")
            mae = num(errors.get("thermostat"))
            if not isinstance(raw, dict) or not {"a", "s", "q_h", "q_c0", "q_c1"} <= raw.keys():
                raise ValueError("incomplete model")
            params = ThermalParams.model_validate(raw)
            fitted = datetime.fromisoformat(model["fitted_at"])
            if fitted.tzinfo is None:
                raise ValueError("model fit timestamp has no time zone")
            if not in_range(params) or num(params.sigma) is None or not 0 <= params.sigma <= 1:
                raise ValueError("invalid model coefficients")
            if mae is None or mae > settings.planner.mae_ok_tstat_f:
                model_reason = "Thermal-model validation confidence is too low."
            elif not timedelta(0) <= now - fitted <= timedelta(days=30):
                model_reason = (
                    "Thermal model is older than 30 days or future-dated; refit required."
                )
        except (ValueError, ValidationError) as exc:
            log.error("room simulation model is invalid: %s", type(exc).__name__)
            model_reason = "Stored thermal model is invalid; refit required."
    check("model", "Validated thermal model", model_reason, "model", "view_model")
    if any(not c["ready"] for c in checks):
        return None, checks
    if room_temp is None or indoor is None or params is None or model is None:
        raise ValueError("ready simulation inputs must include measured temperatures and model")
    return _SimulationInputs(
        indoor, room_temp - indoor, params, int(model["version"]), times, outdoor
    ), checks


def setup_readiness(
    conn: sqlite3.Connection,
    now: datetime,
    room: str,
    settings: Settings,
    snapshot: Snapshot | None,
    forecast: Forecast | None,
    insights: dict[str, Any],
) -> dict[str, Any]:
    """Use the simulation's actual gates, without running a model or changing state."""
    inputs, checks = _simulation_inputs(conn, now, room, settings, snapshot, forecast, 4)
    tariff = settings.insights.peak_rate is not None
    shift_tariff = tariff and settings.insights.peak_rate != settings.insights.off_peak_rate
    meter = insights["energy"]["kwh"] is not None
    for key, label, ready, detail, href in (
        (
            "humidity",
            "Room humidity (optional)",
            insights["humidity"]["minutes"] > 0,
            "Needs fresh paired temperature / humidity readings; checked over the last 7 days.",
            "settings/people",
        ),
        (
            "meter",
            f"Energy meter ({insights['energy']['scope']}; optional)",
            meter,
            "Needs usable consecutive meter / power intervals; checked over the last 7 days.",
            "settings/people" if settings.rooms[room].energy else "settings/ha",
        ),
        (
            "tariff",
            "Peak / off-peak tariff (for tariff shifting)",
            shift_tariff,
            "Configure distinct peak/off-peak tariffs before comparing load shifting.",
            "settings/advanced",
        ),
        (
            "power",
            "Rated-power assumption (for simulated costs)",
            settings.insights.rated_power_kw is not None,
            "Set rated HVAC kW to estimate simulation costs; this is an assumption, not a meter.",
            "settings/advanced",
        ),
    ):
        checks.append(
            {
                "key": key,
                "label": label,
                "ready": ready,
                "detail": "Ready" if ready else detail,
                "href": href,
                "cap": "edit_comfort",
                "optional": True,
            }
        )
    return {
        "checks": checks,
        "ready_count": sum(c["ready"] for c in checks),
        "total": len(checks),
        "setback_ready": inputs is not None,
        "tariff_ready": inputs is not None and shift_tariff,
        "cost_ready": tariff and settings.insights.rated_power_kw is not None,
        "current_f": num(snapshot.rooms[room].temp)
        if snapshot is not None and checks[0]["ready"]
        else None,
    }


def what_if(
    conn: sqlite3.Connection,
    now: datetime,
    room: str,
    settings: Settings,
    snapshot: Snapshot | None,
    forecast: Forecast | None,
    scenario: Literal["setback", "tariff"],
    setback_f: float,
    hours: int,
) -> dict[str, Any]:
    def unavailable(reason: str) -> dict[str, Any]:
        return {"available": False, "simulated": True, "reason": reason}

    if scenario == "tariff" and (
        settings.insights.peak_rate is None
        or settings.insights.peak_rate == settings.insights.off_peak_rate
    ):
        return unavailable(
            "Configure distinct peak/off-peak tariffs before comparing load shifting."
        )
    inputs, checks = _simulation_inputs(conn, now, room, settings, snapshot, forecast, hours)
    if inputs is None:
        return unavailable(next(c["detail"] for c in checks if not c["ready"]))
    mode: Literal["heat", "cool"] = (
        "heat" if inputs.outdoor[0] < settings.insights.ideal_temp_f else "cool"
    )
    baseline = I.simulate(
        inputs.times,
        inputs.outdoor,
        inputs.indoor,
        inputs.offset,
        inputs.params,
        settings,
        mode,
        "baseline",
    )
    proposed = I.simulate(
        inputs.times,
        inputs.outdoor,
        inputs.indoor,
        inputs.offset,
        inputs.params,
        settings,
        mode,
        scenario,
        setback_f,
        hours,
    )
    cost_saving = baseline["cost"] - proposed["cost"] if baseline["cost"] is not None else None
    return {
        "available": True,
        "simulated": True,
        "scenario": scenario,
        "model_version": inputs.version,
        "baseline": baseline,
        "proposed": proposed,
        "saved_min": baseline["runtime_min"] - proposed["runtime_min"],
        "cost_saving": cost_saving,
        "currency": settings.insights.currency,
        "recommended": proposed["safe"]
        and proposed["comfort_pct"] >= baseline["comfort_pct"]
        and (
            cost_saving is not None and cost_saving > 0
            if scenario == "tariff"
            else proposed["runtime_min"] < baseline["runtime_min"]
        ),
        "note": "SIMULATED central HVAC, not measured savings. Room projection assumes the current "
        "room-minus-thermostat offset stays constant. Cost assumes configured rated power, "
        "not measured variable-speed consumption. No settings or actuator changes.",
    }
