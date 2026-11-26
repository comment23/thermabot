"""Simple comfort preferences generate reviewed settings, never actuator commands."""

from __future__ import annotations

import math
from typing import Literal

from thermabot.config import Settings
from thermabot.core.comfort import hold_setpoints

Priority = Literal["comfort", "balanced", "efficiency"]
FLEXIBILITY: dict[Priority, float] = {"comfort": 1, "balanced": 2, "efficiency": 3}


def selected_rooms(settings: Settings, block: str) -> list[str]:
    return sorted(room for room, weight in settings.weights.get(block, {}).items() if weight > 0)


def saved_priority(settings: Settings) -> Priority | None:
    if not settings.seasonal_control.enabled:
        return None
    for priority, flexibility in FLEXIBILITY.items():
        band = (
            settings.insights.ideal_temp_f - flexibility,
            settings.insights.ideal_temp_f + flexibility,
        )
        if (
            settings.seasonal_control.home == band
            and settings.seasonal_control.sleep == band
            and settings.insights.tolerance_f == flexibility
            and all(
                weights and all(w == 1 for w in weights.values())
                for weights in (settings.weights.get("home"), settings.weights.get("sleep"))
            )
        ):
            return priority
    return None


def propose(
    settings: Settings,
    ideal_f: float,
    priority: Priority,
    home_rooms: list[str],
    sleep_rooms: list[str],
) -> Settings:
    if not settings.thermostat.configured or settings.sensors_missing:
        raise ValueError(
            "Confirm the thermostat and HVAC sensor mappings in Home Assistant settings."
        )
    if not math.isfinite(ideal_f):
        raise ValueError("Ideal temperature must be finite.")
    for label, rooms in (("Daytime", home_rooms), ("Nighttime", sleep_rooms)):
        if not rooms:
            raise ValueError(f"{label}: select at least one important room.")
        if len(rooms) != len(set(rooms)):
            raise ValueError(f"{label}: duplicate room selections are not allowed.")
        for room in rooms:
            if room not in settings.rooms:
                raise ValueError(f"{label}: unknown room {room}. Reload the room list.")
            sensor = settings.rooms[room].temp
            if not sensor or (room != "thermostat" and sensor.startswith("climate.")):
                raise ValueError(
                    f"{label}: confirm a temperature sensor for {room} in Home & rooms."
                )
            if room == "thermostat" and sensor != settings.thermostat.entity_id:
                raise ValueError("Confirm the thermostat room's temperature mapping.")
    flexibility = FLEXIBILITY[priority]
    band = (ideal_f - flexibility, ideal_f + flexibility)
    automatic = hold_setpoints(band, settings.writes.deadband_f)
    floor = max(settings.safety.floor_tstat_f, settings.safety.floor_room_f)
    ceiling = min(settings.safety.ceil_tstat_f, settings.safety.ceil_room_f)
    if not all(floor < low <= high < ceiling for low, high in (band, automatic)):
        raise ValueError(
            "This preference conflicts with saved safety limits or the thermostat deadband. "
            "Choose another ideal temperature or priority; safety limits are not changed."
        )
    data = settings.model_dump()
    data["seasonal_control"] = {"enabled": True, "home": band, "sleep": band}
    data["insights"].update(ideal_temp_f=ideal_f, tolerance_f=flexibility)
    data["weights"]["home"] = {room: 1.0 for room in sorted(home_rooms)}
    data["weights"]["sleep"] = {room: 1.0 for room in sorted(sleep_rooms)}
    return Settings.model_validate(data)
