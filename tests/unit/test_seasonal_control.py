from datetime import timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tests.conftest import local
from thermabot.app import forms
from thermabot.app.controller import diff
from thermabot.app.recovery import ConfigFile, checked_settings
from thermabot.config import Settings
from thermabot.core.comfort import block_band
from thermabot.core.fallback import fallback_plan, fallback_setpoints
from thermabot.core.guards import GuardCtx, WriteRecord, evaluate
from thermabot.core.planner import PlanInputs, plan
from thermabot.core.projection import project
from thermabot.core.safety import check
from thermabot.core.types import (
    Action,
    Forecast,
    ForecastPoint,
    HouseState,
    Season,
    Snapshot,
    ThermostatState,
)
from thermabot.core.weather import forecast_season


def seasonal_settings() -> Settings:
    return Settings.model_validate(
        {
            "thermostat": {"entity_id": "climate.example"},
            "seasonal_control": {
                "enabled": True,
                "home": [70, 73],
                "sleep": [68, 70],
            },
        }
    )


def forecast(now, temp):  # type: ignore[no-untyped-def]
    return Forecast(
        source="ha",
        fetched_at=now,
        points=[ForecastPoint(t=now + timedelta(hours=h), temp_f=temp) for h in range(49)],
    )


@pytest.mark.brd("FR-CMF-1")
def test_comfort_bands_are_independent_of_automatic_mode_deadband() -> None:
    s = seasonal_settings()
    seasons: tuple[Season, ...] = ("heat", "cool", "shoulder")
    for season in seasons:
        assert block_band(s, season, "home") == (70, 73)
        assert block_band(s, season, "sleep") == (68, 70)
    assert s.writes.deadband_f == 3
    assert fallback_setpoints(s, "home") == (70, 73)
    assert fallback_setpoints(s, "sleep") == (68, 71)


@pytest.mark.parametrize(
    ("outdoor", "mode", "home_setpoint", "sleep_setpoint"),
    [(45, "heat", 70, 68), (85, "cool", 73, 70)],
)
@pytest.mark.brd("FR-CMF-1")
def test_weather_selects_single_mode_without_early_sleep_preconditioning(
    outdoor: float, mode: str, home_setpoint: float, sleep_setpoint: float
) -> None:
    now = local(2026, 11, 10, 19, 5)
    sleep = local(2026, 11, 10, 22, 0)
    p = plan(
        PlanInputs(
            now=now,
            t0=71,
            current_mode="heat_cool",
            forecast=forecast(now, outdoor),
            settings=seasonal_settings(),
            off_allowed=False,
        )
    )
    assert p.kind == "optimised"
    assert all(step.hvac_mode == mode for step in p.steps)
    for step in p.steps:
        target = step.heat_sp if mode == "heat" else step.cool_sp
        if step.t < sleep:
            assert target == home_setpoint
        elif step.t < local(2026, 11, 11, 6, 30):
            assert target == sleep_setpoint
    assert not any(step.reason.startswith("precondition") for step in p.steps)


@pytest.mark.brd("FR-CMF-1")
def test_shoulder_sleep_uses_a_single_mode_at_the_requested_bound() -> None:
    now = local(2026, 11, 10, 23)
    p = plan(
        PlanInputs(
            now=now,
            t0=69,
            forecast=forecast(now, 64),
            settings=seasonal_settings(),
            off_allowed=False,
        )
    )
    step = p.steps[0]
    assert (step.hvac_mode, step.heat_sp, step.cool_sp) == ("heat", 68, None)


@pytest.mark.brd("FR-PLN-7")
def test_low_confidence_still_uses_forecast_season_and_requested_band() -> None:
    now = local(2026, 11, 10, 19)
    p = plan(
        PlanInputs(
            now=now,
            t0=71,
            current_mode="heat_cool",
            current_sp=(66, 69),
            forecast=forecast(now, 45),
            confidence="low",
            settings=seasonal_settings(),
        )
    )
    assert p.kind == "fallback"
    assert (p.steps[0].hvac_mode, p.steps[0].heat_sp, p.steps[0].cool_sp) == ("heat", 70, None)


@pytest.mark.brd("FR-PLN-7")
def test_missing_forecast_preserves_single_mode_and_uses_requested_band() -> None:
    now = local(2026, 11, 10, 19)
    p = plan(
        PlanInputs(
            now=now,
            t0=71,
            current_mode="heat",
            current_sp=(66, None),
            forecast=None,
            settings=seasonal_settings(),
        )
    )
    assert p.kind == "fallback"
    assert (p.steps[0].hvac_mode, p.steps[0].heat_sp, p.steps[0].cool_sp) == ("heat", 70, None)


@pytest.mark.brd("FR-PLN-7")
def test_missing_forecast_and_indoor_temperature_use_safe_widened_band() -> None:
    now = local(2026, 11, 10, 23)
    p = fallback_plan(
        seasonal_settings(),
        now,
        "no indoor reading",
        current=("heat_cool", 66, 69),
    )
    assert (p.steps[0].hvac_mode, p.steps[0].heat_sp, p.steps[0].cool_sp) == (
        "heat_cool",
        68,
        71,
    )


@pytest.mark.parametrize("confidence", ["ok", "low"])
@pytest.mark.parametrize(
    ("outdoor", "indoor", "mode", "target"),
    [(85, 69, "heat", 70), (45, 74, "cool", 73), (64, 69, "heat", 70), (64, 74, "cool", 73)],
)
@pytest.mark.brd("FR-CMF-1")
def test_indoor_comfort_takes_precedence_over_the_weather_preference(
    confidence: str, outdoor: float, indoor: float, mode: str, target: float
) -> None:
    now = local(2026, 11, 10, 19)
    p = plan(
        PlanInputs(
            now=now,
            t0=indoor,
            current_mode="heat_cool",
            forecast=forecast(now, outdoor),
            settings=seasonal_settings(),
            confidence=confidence,
            off_allowed=False,
        )
    )
    first = p.steps[0]
    assert first.hvac_mode == mode
    assert (first.heat_sp if mode == "heat" else first.cool_sp) == target


@pytest.mark.parametrize(("indoor", "mode", "target"), [(67, "heat", 68), (71, "cool", 70)])
@pytest.mark.brd("FR-CMF-1")
def test_sleep_responds_to_both_comfort_bounds_in_shoulder_weather(
    indoor: float, mode: str, target: float
) -> None:
    now = local(2026, 11, 10, 23)
    p = plan(
        PlanInputs(
            now=now,
            t0=indoor,
            forecast=forecast(now, 64),
            settings=seasonal_settings(),
            off_allowed=False,
        )
    )
    first = p.steps[0]
    assert first.hvac_mode == mode
    assert (first.heat_sp if mode == "heat" else first.cool_sp) == target


@pytest.mark.brd("FR-CMF-1")
def test_away_uses_energy_saving_legacy_bounds_not_heating_comfort_upper_edge() -> None:
    now = local(2026, 11, 10, 19)
    for season in ("heat", "cool", "shoulder"):
        assert block_band(seasonal_settings(), season, "away") == (62, 80)
    for confidence in ("ok", "low"):
        p = plan(
            PlanInputs(
                now=now,
                t0=71,
                forecast=forecast(now, 45),
                settings=seasonal_settings(),
                house=HouseState.EMPTY,
                confidence=confidence,
                off_allowed=False,
            )
        )
        assert (p.steps[0].hvac_mode, p.steps[0].heat_sp) == ("heat", 62)


@pytest.mark.brd("FR-PLN-7")
def test_missing_forecast_still_corrects_an_out_of_range_indoor_temperature() -> None:
    now = local(2026, 11, 10, 19)
    p = plan(
        PlanInputs(
            now=now,
            t0=74,
            current_mode="heat",
            forecast=None,
            settings=seasonal_settings(),
        )
    )
    assert (p.steps[0].hvac_mode, p.steps[0].cool_sp) == ("cool", 73)


@pytest.mark.brd("FR-CMF-1")
def test_off_is_not_chosen_to_leave_an_occupied_house_outside_its_band() -> None:
    now = local(2026, 11, 10, 19)
    p = plan(PlanInputs(now=now, t0=74, forecast=forecast(now, 64), settings=seasonal_settings()))
    assert (p.steps[0].hvac_mode, p.steps[0].cool_sp) == ("cool", 73)


@pytest.mark.parametrize(
    ("outdoor", "expected"), [(59, "heat"), (60, "shoulder"), (68, "shoulder"), (69, "cool")]
)
@pytest.mark.brd("FR-CMF-1")
def test_forecast_thresholds_include_shoulder_boundaries(outdoor: float, expected: str) -> None:
    now = local(2026, 11, 10, 19)
    s = seasonal_settings()
    assert forecast_season(forecast(now, outdoor), now, s.season, s.weather) == expected


@pytest.mark.brd("FR-WX-3")
def test_stale_or_no_near_term_forecast_has_no_weather_preference() -> None:
    now = local(2026, 11, 10, 19)
    s = seasonal_settings()
    stale = forecast(now, 45).model_copy(update={"fetched_at": now - timedelta(hours=4)})
    distant = Forecast(
        source="ha",
        fetched_at=now,
        points=[ForecastPoint(t=now + timedelta(hours=h), temp_f=85) for h in range(25, 50)],
    )
    assert forecast_season(stale, now, s.season, s.weather) is None
    assert forecast_season(distant, now, s.season, s.weather) is None


@pytest.mark.brd("FR-PLN-7")
def test_projection_uses_seasonal_fallback_and_indoor_correction() -> None:
    now = local(2026, 11, 10, 19)
    projection = project(
        seasonal_settings(),
        now,
        plan=None,
        fc=forecast(now, 85),
        t0=69,
        params=None,
        current=("cool", None, 73),
        hours=2,
    )
    assert projection.fallback
    assert (projection.hours[0].mode, projection.hours[0].heat_sp) == ("heat", 70)


@pytest.mark.brd("NFR-SAFE-1")
def test_seasonal_mode_changes_do_not_bypass_the_existing_rate_limit() -> None:
    now = local(2026, 11, 10, 19)
    s = seasonal_settings()
    snapshot = Snapshot(
        t=now,
        thermostat=ThermostatState(
            hvac_mode="cool", current_temp=69, cool_sp=73, last_reported=now
        ),
    )
    action = Action(
        service="set_temperature",
        entity_id=s.thermostat.entity_id,
        data={"hvac_mode": "heat", "temperature": 70},
    )
    previous = WriteRecord(
        t=now - timedelta(minutes=5),
        service="set_hvac_mode",
        data={"hvac_mode": "cool"},
    )
    ctx = GuardCtx(
        kill_switch="on",
        ha_mirror="on",
        run_mode="active",
        snapshot=snapshot,
        settings=s,
        now=now,
        recent_writes=[previous],
    )
    decision = evaluate(action, ctx)
    assert not decision.allowed and decision.guard == "rate_limit"
    assert "mode change" in decision.reason


@pytest.mark.brd("NFR-SAFE-1")
def test_seasonal_mode_changes_do_not_bypass_the_daily_flip_limit() -> None:
    now = local(2026, 11, 10, 19)
    s = seasonal_settings()
    snapshot = Snapshot(
        t=now,
        thermostat=ThermostatState(
            hvac_mode="heat", current_temp=74, heat_sp=70, last_reported=now
        ),
    )
    action = Action(
        service="set_temperature",
        entity_id=s.thermostat.entity_id,
        data={"hvac_mode": "cool", "temperature": 73},
    )
    writes = [
        WriteRecord(
            t=now - timedelta(hours=hours),
            service="set_hvac_mode",
            data={"hvac_mode": mode},
        )
        for hours, mode in ((6, "heat"), (4, "cool"), (2, "heat"))
    ]
    decision = evaluate(
        action,
        GuardCtx(
            kill_switch="on",
            ha_mirror="on",
            run_mode="active",
            snapshot=snapshot,
            settings=s,
            now=now,
            recent_writes=writes,
        ),
    )
    assert not decision.allowed and decision.guard == "rate_limit"
    assert "flip" in decision.reason


@pytest.mark.parametrize(("month", "outdoor", "mode"), [(1, 85, "cool"), (7, 45, "heat")])
@pytest.mark.brd("FR-CMF-1")
def test_forecast_wins_over_calendar(month: int, outdoor: float, mode: str) -> None:
    now = local(2026, month, 15, 12)
    p = plan(
        PlanInputs(
            now=now,
            t0=71,
            forecast=forecast(now, outdoor),
            settings=seasonal_settings(),
            off_allowed=False,
        )
    )
    assert p.steps[0].hvac_mode == mode


@pytest.mark.parametrize(("outdoor", "mode", "temperature"), [(45, "heat", 70), (85, "cool", 73)])
@pytest.mark.brd("NFR-SAFE-1")
def test_single_mode_plan_passes_existing_write_guards(
    outdoor: float, mode: str, temperature: float
) -> None:
    now = local(2026, 11, 10, 19)
    s = seasonal_settings()
    snapshot = Snapshot(
        t=now,
        thermostat=ThermostatState(
            hvac_mode="heat_cool",
            current_temp=71,
            heat_sp=66,
            cool_sp=69,
            last_reported=now,
        ),
    )
    p = plan(
        PlanInputs(now=now, t0=71, forecast=forecast(now, outdoor), settings=s, off_allowed=False)
    )
    action = diff(p.steps[0], snapshot, s.thermostat.entity_id)[0]
    assert action.data == {"hvac_mode": mode, "temperature": temperature}
    ctx = GuardCtx(
        kill_switch="on", ha_mirror="on", run_mode="active", snapshot=snapshot, settings=s, now=now
    )
    assert evaluate(action, ctx).allowed


@pytest.mark.brd("NFR-SAFE-3")
def test_safety_restore_keeps_three_degree_deadband() -> None:
    now = local(2026, 11, 10, 23)
    snapshot = Snapshot(
        t=now, thermostat=ThermostatState(hvac_mode="off", current_temp=61, last_reported=now)
    )
    result = check(snapshot, seasonal_settings(), "sleep", now)
    assert result is not None
    assert result.action.data == {
        "hvac_mode": "heat_cool",
        "target_temp_low": 68,
        "target_temp_high": 71,
    }


@pytest.mark.parametrize(
    "band", [[74, 73], [59, 73], [68, 81], [65, 67], [float("nan"), 73], [70, float("inf")]]
)
@pytest.mark.brd("FR-CMF-1")
def test_invalid_seasonal_comfort_band_is_rejected(band: list[float]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"seasonal_control": {"enabled": True, "home": band}})


@pytest.mark.brd("FR-CFG-1")
def test_settings_form_exposes_comfort_temperatures_in_display_units() -> None:
    s = seasonal_settings()
    s.display.units = "C"
    section = next(x for x in forms.sections(s, owner=True) if x.name == "seasonal_control")
    fields = {f.path: f for f in section.fields}
    assert fields["seasonal_control.enabled"].kind == "bool"
    assert fields["seasonal_control.home.0"].kind == "temp"
    assert fields["seasonal_control.home.0"].value == 21.1
    assert fields["seasonal_control.home.0"].f_value == 70


@pytest.mark.brd("FR-CFG-1")
def test_legacy_config_export_can_be_imported_without_enabling_the_new_policy() -> None:
    now = local(2026, 11, 10)
    values = Settings().model_dump(mode="json")
    del values["seasonal_control"]
    document = ConfigFile(
        format="thermabot-config",
        schema_version=1,
        app_version="0.6.8",
        exported_at=now,
        settings=values,
    )
    assert checked_settings(document).seasonal_control.enabled is False


@pytest.mark.parametrize("problem", ["extra_section", "missing_other", "missing_both"])
@pytest.mark.brd("FR-CFG-1")
def test_legacy_import_still_rejects_unexpected_or_missing_sections(problem: str) -> None:
    values = Settings().model_dump(mode="json")
    if problem == "extra_section":
        values["unexpected"] = {}
    else:
        del values["weather"]
        if problem == "missing_both":
            del values["seasonal_control"]
    document = ConfigFile(
        format="thermabot-config",
        schema_version=1,
        app_version="0.6.8",
        exported_at=local(2026, 11, 10),
        settings=values,
    )
    with pytest.raises(HTTPException) as exc:
        checked_settings(document)
    assert exc.value.status_code == 422


@pytest.mark.brd("FR-CMF-1")
def test_disabled_weather_aware_settings_do_not_change_legacy_plans() -> None:
    now = local(2026, 11, 10, 19)
    legacy = Settings()
    configured = Settings.model_validate(
        {"seasonal_control": {"enabled": False, "home": [70, 73], "sleep": [68, 70]}}
    )
    inputs = {"now": now, "t0": 71, "forecast": forecast(now, 45), "off_allowed": False}
    old_plan = plan(PlanInputs(settings=legacy, **inputs))
    disabled_plan = plan(PlanInputs(settings=configured, **inputs))
    assert old_plan == disabled_plan
