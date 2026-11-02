"""Generic thermostat + house fixtures in HA state shape (a Trane-like heat_cool climate)."""

from __future__ import annotations

from typing import Any

TSTAT = "climate.living_room"
FOREIGN = "climate.garage_heater"


def initial_states() -> dict[str, dict[str, Any]]:
    """entity_id -> {state, attributes}. Copied fresh per MockHA instance."""
    return {
        TSTAT: {
            "state": "heat_cool",
            "attributes": {
                "hvac_modes": ["off", "heat", "cool", "heat_cool"],
                "fan_modes": ["auto", "on", "circulate"],
                "preset_modes": ["none", "away", "hold"],
                "min_temp": 45,
                "max_temp": 95,
                "target_temp_step": 1,
                "current_temperature": 71,
                "current_humidity": 48,
                "temperature": None,
                "target_temp_low": 68,
                "target_temp_high": 76,
                "hvac_action": "idle",
                "fan_mode": "auto",
                "preset_mode": "none",
                "friendly_name": "Living Room",
            },
        },
        FOREIGN: {"state": "cool", "attributes": {"temperature": 74}},
        "sensor.living_room_compressor_speed": {
            "state": "0",
            "attributes": {"unit_of_measurement": "%"},
        },
        "binary_sensor.living_room_blower_active": {"state": "off", "attributes": {}},
        "sensor.outdoor_temperature": {
            "state": "58",
            "attributes": {"unit_of_measurement": "°F"},
        },
        "sensor.study_temperature": {"state": "70.5", "attributes": {}},
        "sensor.bedroom_1_temperature": {"state": "69.0", "attributes": {}},
        "sensor.bedroom_2_temperature": {"state": "70.0", "attributes": {}},
        "sensor.den_temperature": {"state": "66.0", "attributes": {}},
        "input_boolean.thermabot_enabled": {"state": "off", "attributes": {}},
        "input_boolean.thermabot_watchdog": {"state": "on", "attributes": {}},
        "input_datetime.thermabot_watchdog_last": {
            "state": "2026-10-05 14:00:00",
            "attributes": {},
        },
        "weather.home": {"state": "cloudy", "attributes": {"temperature": 58}},
        "zone.home": {"state": "3", "attributes": {"latitude": 40.0, "longitude": -75.0}},
        "person.alex": {"state": "home", "attributes": {}},
    }


def hourly_forecast(
    start_iso: str = "2026-10-05T18:00:00+00:00", n: int = 48
) -> list[dict[str, Any]]:
    from datetime import datetime, timedelta

    t0 = datetime.fromisoformat(start_iso)
    return [
        {
            "datetime": (t0 + timedelta(hours=i)).isoformat(),
            "temperature": 58 + (i % 24) / 3,
            "condition": "cloudy",
        }
        for i in range(n)
    ]
