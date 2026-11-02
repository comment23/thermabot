"""Setup-wizard entity discovery (v0.3.0). Pure: HA state dicts in, ranked suggestions out.

Keyword/device_class heuristics only; every suggestion carries a confidence (0..1) and a
reason so the Owner can judge it, and the wizard always allows a manual override.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel

TEMP_UNITS = {"°F", "°C", "F", "C", "\u2109", "\u2103"}
_OUTDOOR = re.compile(r"outdoor|outside|exterior|external")
# Temperature sensors that are clearly not a room (hardware, appliances, derived values).
_NOT_ROOM = re.compile(
    r"cpu|processor|battery|disk|gpu|router|chip|fridge|freezer|oven|water_heater|pool|probe"
    r"|dew|feels|apparent|forecast|thermabot|_max\b|_min\b|setpoint|target"
)
_FAN_RUN = re.compile(r"running|active|state|status|\bon\b")


class Entity(BaseModel):
    entity_id: str
    domain: str
    name: str
    state: str
    device_class: str | None = None
    unit: str | None = None
    hvac_modes: list[str] = []
    fan_modes: list[str] = []
    current_temperature: float | None = None
    temperature_unit: str | None = None
    min_temp: float | None = None
    max_temp: float | None = None
    target_temp_step: float | None = None
    target_temperature: float | None = None
    target_temp_low: float | None = None
    target_temp_high: float | None = None
    preset_modes: list[str] = []
    current_position: float | None = None


class Candidate(BaseModel):
    entity_id: str
    name: str
    state: str
    confidence: float
    reason: str
    room: str | None = None  # rooms only: suggested room key
    device_class: str | None = None


class Discovery(BaseModel):
    blower: list[Candidate]
    compressor: list[Candidate]
    outdoor: list[Candidate]
    humidity: list[Candidate]
    rooms: list[Candidate]
    people: list[Candidate]
    fans: list[Candidate]
    covers: list[Candidate]
    supplemental_heat: list[Candidate]
    openings: list[Candidate]


class SunPosition(BaseModel):
    state: str
    azimuth: float | None = None
    elevation: float | None = None
    next_rising: str | None = None
    next_setting: str | None = None


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n and abs(n) != float("inf") else None


def _list(v: Any) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else []


def summarise(st: Mapping[str, Any]) -> Entity:
    eid = str(st.get("entity_id", ""))
    a = st.get("attributes") or {}
    return Entity(
        entity_id=eid,
        domain=eid.split(".", 1)[0],
        name=str(a.get("friendly_name") or eid),
        state=str(st.get("state", "")),
        device_class=a.get("device_class"),
        unit=a.get("unit_of_measurement"),
        hvac_modes=_list(a.get("hvac_modes")),
        fan_modes=_list(a.get("fan_modes")),
        current_temperature=_num(a.get("current_temperature")),
        temperature_unit=str(a.get("temperature_unit") or a.get("unit_of_measurement") or "")
        or None,
        min_temp=_num(a.get("min_temp")),
        max_temp=_num(a.get("max_temp")),
        target_temp_step=_num(a.get("target_temp_step")),
        target_temperature=_num(a.get("temperature")),
        target_temp_low=_num(a.get("target_temp_low")),
        target_temp_high=_num(a.get("target_temp_high")),
        preset_modes=_list(a.get("preset_modes")),
        current_position=_num(a.get("current_position")),
    )


def entities(states: Iterable[Mapping[str, Any]], domains: Iterable[str]) -> list[Entity]:
    want = set(domains)
    out = [summarise(s) for s in states]
    return sorted((e for e in out if e.domain in want), key=lambda e: e.entity_id)


def _text(e: Entity) -> str:
    return f"{e.entity_id} {e.name}".lower()


def _is_temp(e: Entity) -> bool:
    return e.device_class == "temperature" or (e.unit or "").strip() in TEMP_UNITS


def room_key(e: Entity) -> str:
    """sensor.bedroom_1_temperature -> bedroom_1."""
    obj = e.entity_id.split(".", 1)[-1]
    obj = re.sub(r"(_current)?_(temperature|temp)$|^(temperature|temp)_", "", obj)
    return re.sub(r"[^a-z0-9_]", "_", obj.lower()).strip("_") or "room"


def _c(e: Entity, conf: float, reason: str, room: str | None = None) -> Candidate:
    return Candidate(
        entity_id=e.entity_id,
        name=e.name,
        state=e.state,
        confidence=round(conf, 2),
        reason=reason,
        room=room,
        device_class=e.device_class,
    )


def sun_position(states: Iterable[Mapping[str, Any]]) -> SunPosition | None:
    """Return HA's sun entity data only when its numeric angles are in valid ranges."""
    for state in states:
        if state.get("entity_id") != "sun.sun":
            continue
        attrs = state.get("attributes") or {}
        azimuth, elevation = _num(attrs.get("azimuth")), _num(attrs.get("elevation"))
        if azimuth is not None and not 0 <= azimuth <= 360:
            azimuth = None
        if elevation is not None and not -90 <= elevation <= 90:
            elevation = None
        return SunPosition(
            state=str(state.get("state", "unknown")),
            azimuth=azimuth,
            elevation=elevation,
            next_rising=str(attrs["next_rising"]) if attrs.get("next_rising") else None,
            next_setting=str(attrs["next_setting"]) if attrs.get("next_setting") else None,
        )
    return None


def _rank(cs: list[Candidate], limit: int | None = 10) -> list[Candidate]:
    out = sorted(cs, key=lambda c: (-c.confidence, c.entity_id))
    return out if limit is None else out[:limit]


def discover(states: Iterable[Mapping[str, Any]], thermostat: str = "") -> Discovery:
    es = [summarise(s) for s in states]
    blower: list[Candidate] = []
    comp: list[Candidate] = []
    out: list[Candidate] = []
    hum: list[Candidate] = []
    rooms: list[Candidate] = []
    people: list[Candidate] = []
    fans: list[Candidate] = []
    covers: list[Candidate] = []
    supplemental_heat: list[Candidate] = []
    openings: list[Candidate] = []
    for e in es:
        t = _text(e)
        if e.entity_id == thermostat or "thermabot" in t:
            continue
        if e.domain == "fan":
            fans.append(_c(e, 1.0, "Home Assistant fan entity; confirm room and supported modes"))
        elif e.domain == "cover":
            covers.append(
                _c(e, 1.0, "Home Assistant cover entity; confirm it is a motorized blind")
            )
        elif e.domain == "climate" and "heat" in e.hvac_modes:
            supplemental_heat.append(
                _c(e, 0.6, "climate entity supports heat; confirm its role and safe control")
            )
        if e.domain == "binary_sensor" and (
            e.device_class in ("door", "garage_door") or "door" in t
        ):
            openings.append(
                _c(
                    e,
                    0.9 if e.device_class in ("door", "garage_door") else 0.55,
                    "door contact candidate; confirm exterior exposure and active-state semantics",
                )
            )
        if e.domain == "person":
            people.append(_c(e, 0.9, "person"))
            continue
        if e.domain == "device_tracker":
            people.append(_c(e, 0.6, "device tracker"))
            continue
        if e.domain not in ("sensor", "binary_sensor"):
            continue
        if "blower" in t:
            blower.append(_c(e, 0.9, "name mentions blower"))
        elif e.domain == "binary_sensor" and "fan" in t and _FAN_RUN.search(t):
            blower.append(_c(e, 0.6, "binary fan running sensor"))
        if e.domain == "sensor" and "compressor" in t:
            conf = 0.95 if "speed" in t else 0.85
            comp.append(_c(e, conf, "name mentions compressor"))
        if e.domain != "sensor":
            continue
        outdoor = bool(_OUTDOOR.search(t))
        if _is_temp(e):
            if outdoor:
                conf = 0.9 if e.device_class == "temperature" else 0.8
                out.append(_c(e, conf, "outdoor temperature sensor"))
            elif not _NOT_ROOM.search(t):
                conf = 0.85 if e.device_class == "temperature" else 0.7
                why = "device_class temperature" if e.device_class else "°F/°C unit"
                rooms.append(_c(e, conf, why, room_key(e)))
        elif "temperature" in t and not outdoor and not _NOT_ROOM.search(t):
            rooms.append(_c(e, 0.4, "name mentions temperature", room_key(e)))
        if not outdoor:
            if e.device_class == "humidity":
                hum.append(_c(e, 0.8, "device_class humidity"))
            elif "humidity" in t and (e.unit or "").strip() == "%":
                hum.append(_c(e, 0.6, "name mentions humidity, unit %"))
    return Discovery(
        blower=_rank(blower),
        compressor=_rank(comp),
        outdoor=_rank(out),
        humidity=_rank(hum),
        rooms=_rank(rooms, None),
        people=_rank(people, None),
        fans=_rank(fans, None),
        covers=_rank(covers, None),
        supplemental_heat=_rank(supplemental_heat, None),
        openings=_rank(openings, None),
    )
