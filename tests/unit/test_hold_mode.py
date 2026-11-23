"""Supported-mode holds: no assumed heat_cool, and °C stays on the device grid."""

from __future__ import annotations

import pytest

from tests.conftest import local
from tests.house import HOUSE
from thermabot.app.controller import thermostat
from thermabot.app.jobs import _temp_f
from thermabot.core.fallback import choose_hold_mode, fallback_plan
from thermabot.core.safety import check
from thermabot.core.schedule import active_zone, configure_timezone
from thermabot.core.types import Snapshot, ThermostatState

NOW = local(2026, 10, 5, 12, 0)


@pytest.mark.brd("FR-HA-4")
def test_choose_hold_mode_prefers_a_listed_range_mode() -> None:
    assert choose_hold_mode(["off", "heat", "auto"], 68, 76, 70) == ("auto", 68, 76)
    assert choose_hold_mode(["heat_cool", "heat"], 68, 76, 80) == ("heat_cool", 68, 76)
    assert choose_hold_mode(None, 68, 76, 70) == ("heat_cool", 68, 76)


@pytest.mark.brd("FR-HA-4")
def test_choose_hold_mode_uses_temp_versus_band_for_a_single_mode() -> None:
    assert choose_hold_mode(["off", "heat", "cool"], 68, 76, 60) == ("heat", 68, None)
    assert choose_hold_mode(["off", "heat", "cool"], 68, 76, 80) == ("cool", None, 76)
    assert choose_hold_mode(["off", "heat", "cool"], 68, 76, 71) == ("heat", 68, None)
    assert choose_hold_mode(["off"], 68, 76, 60) is None


@pytest.mark.brd("NFR-SAFE-3")
def test_safety_restore_direction_does_not_use_the_opposite_mode() -> None:
    assert choose_hold_mode(["cool"], 68, 76, 50, direction="heat") is None
    assert choose_hold_mode(["heat", "cool"], 68, 76, 50, direction="heat") == ("heat", 68, None)


@pytest.mark.brd("NFR-SAFE-3")
def test_safety_restore_on_a_heat_only_device() -> None:
    snap = Snapshot(
        t=NOW,
        thermostat=ThermostatState(
            hvac_mode="off",
            hvac_modes=["off", "heat"],
            current_temp=58,
            last_reported=NOW,
            temperature_unit="°F",
        ),
    )
    result = check(snap, HOUSE, "home", NOW)
    assert result is not None and result.kind == "heat"
    assert result.action.data == {"hvac_mode": "heat", "temperature": 68}


@pytest.mark.brd("NFR-SAFE-3")
def test_safety_restore_does_not_assume_heat_cool_without_modes() -> None:
    snap = Snapshot(
        t=NOW,
        thermostat=ThermostatState(hvac_mode="off", current_temp=58, last_reported=NOW),
    )
    assert check(snap, HOUSE, "home", NOW) is None


@pytest.mark.brd("FR-PLN-7")
def test_fallback_plan_follows_supported_modes() -> None:
    heat_only = fallback_plan(
        HOUSE, NOW, "x", horizon_h=1, supported_modes=["off", "heat"], indoor_f=60
    )
    assert heat_only.steps[0].hvac_mode == "heat"
    assert heat_only.steps[0].cool_sp is None
    legacy = fallback_plan(HOUSE, NOW, "x", horizon_h=1)
    assert legacy.steps[0].hvac_mode == "heat_cool"


@pytest.mark.brd("NFR-SAFE-3")
def test_thermostat_converts_celsius_and_clears_unknown_units() -> None:
    celsius = thermostat(
        {
            "state": "heat_cool",
            "attributes": {
                "current_temperature": 20,
                "target_temp_low": 18,
                "target_temp_high": 24,
                "min_temp": 7,
                "max_temp": 35,
                "target_temp_step": 0.5,
                "temperature_unit": "°C",
                "hvac_modes": ["heat_cool"],
            },
        }
    )
    assert celsius.current_temp == pytest.approx(68)
    assert celsius.heat_sp == pytest.approx(64.4)
    assert celsius.min_temp == pytest.approx(44.6)
    assert celsius.target_temp_step == pytest.approx(0.9)
    unknown = thermostat(
        {
            "state": "heat",
            "attributes": {"current_temperature": 10, "temperature_unit": "K", "min_temp": 5},
        }
    )
    assert unknown.current_temp is None and unknown.min_temp is None
    assert unknown.temperature_unit == "K"


@pytest.mark.brd("FR-MET-1")
def test_room_and_outdoor_samples_convert_or_drop() -> None:
    assert _temp_f({"state": "20", "attributes": {"unit_of_measurement": "°C"}}) == pytest.approx(
        68
    )
    assert _temp_f({"state": "70", "attributes": {"unit_of_measurement": "°F"}}) == 70
    assert _temp_f({"state": "20", "entity_id": "sensor.room", "attributes": {}}) is None


def _zone_name(configured: str | None, ha_time_zone: str | None = None) -> str:
    return configure_timezone(configured, ha_time_zone).key


@pytest.mark.brd("FR-CMF-2")
def test_timezone_falls_back_from_env_to_ha_to_utc() -> None:
    assert _zone_name("Europe/Paris") == "Europe/Paris"
    assert active_zone() is configure_timezone("Europe/Paris")
    assert _zone_name("", "America/Chicago") == "America/Chicago"
    assert _zone_name("Not/AZone", "America/Denver") == "America/Denver"
    assert _zone_name("", None) == "UTC"
    assert _zone_name("nope", "also-nope") == "UTC"
    configure_timezone("America/New_York")
