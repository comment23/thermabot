"""v0.3.0 Settings page: one form per Settings section, generated from the pydantic model.

Every leaf value becomes an input whose `data-path` is its dotted JSON path (e.g.
`bands.heat.home.0`, `rooms.study.temp`). static/settings.js applies the inputs on top of the
current settings JSON and PUTs the whole object, so validation stays in the one pydantic
`Settings` model, and 422 error locations map back onto these paths.

°F values are rendered in the display units (settings.display.units); `kind` says how to
convert them back: `temp` is an absolute temperature, `delta` a temperature difference.
"""

from __future__ import annotations

import types
import typing
from dataclasses import dataclass, field
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel

from thermabot.app.units import Units, f_to_c
from thermabot.config import Settings

OWNER_SECTIONS = ("thermostat", "safety", "off")  # routes_api.SAFETY_SECTIONS

TITLES = {
    "thermostat": "Thermostat",
    "display": "Display",
    "bands": "Comfort bands",
    "season": "Season",
    "schedule": "Schedule",
    "rooms": "Rooms",
    "weights": "Room weights",
    "sensors": "HVAC sensors",
    "presence": "Presence",
    "comfort": "Comfort scoring",
    "aux_heat": "Aux heat detection",
    "nudge": "Nudges",
    "override": "Overrides",
    "off": "System off rules",
    "safety": "Safety limits",
    "writes": "Write limits",
    "fan": "Fan",
    "planner": "Planner",
    "weather": "Weather",
    "metrics": "Metrics",
    "alerts": "Alerts",
    "summary": "Weekly summary",
}
# `openings` stays in the setup wizard: entity IDs are dotted map keys, not form paths.
ORDER = list(TITLES)

# Absolute temperatures (°F) outside `bands` (which are all absolute).
ABS_TEMP = {
    "season.heat_below_f",
    "season.cool_above_f",
    "off.outdoor_min_f",
    "off.outdoor_max_f",
    "safety.floor_tstat_f",
    "safety.floor_room_f",
    "safety.ceil_tstat_f",
    "safety.ceil_room_f",
    "metrics.hdd_base_f",
}


@dataclass
class FormField:
    path: str
    label: str
    kind: str  # temp | delta | number | int | text | bool | select | list
    value: Any
    choices: list[str] = field(default_factory=list)
    f_value: float | None = None  # temp/delta: the stored °F value
    nullable: bool = False  # v0.4.0: optional value; an emptied input saves null

    @property
    def id(self) -> str:
        return "f-" + self.path.replace(".", "-")


@dataclass
class Section:
    name: str
    title: str
    locked: bool  # needs edit_safety and the user lacks it
    fields: list[FormField]


def _kind(path: str, ann: Any, value: Any) -> tuple[str, list[str]]:
    origin = get_origin(ann)
    if origin is Literal:
        return "select", [str(a) for a in get_args(ann)]
    if isinstance(value, bool):
        return "bool", []
    if isinstance(value, int | float):
        last = path.rsplit(".", 1)[-1]
        if path.startswith("bands.") or path in ABS_TEMP:
            return "temp", []
        if last.endswith("_f") or last == "tol":
            return "delta", []
        return ("int" if isinstance(value, int) and ann is int else "number"), []
    if isinstance(value, list):
        return "list", []
    if value is None and ann in (float, int):  # optional number, e.g. weather.lat (v0.4.0)
        return "number", []
    return "text", []


def _strip_optional(ann: Any) -> Any:
    if get_origin(ann) in (typing.Union, types.UnionType):
        args = [a for a in get_args(ann) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return ann


def _walk(path: str, ann: Any, value: Any, out: list[FormField]) -> None:
    nullable = _strip_optional(ann) is not ann
    ann = _strip_optional(ann)
    if isinstance(value, BaseModel):
        for name, f in type(value).model_fields.items():
            _walk(f"{path}.{name}", f.annotation, getattr(value, name), out)
        return
    origin = get_origin(ann)
    if isinstance(value, dict):
        vt = get_args(ann)[1] if origin is dict and len(get_args(ann)) == 2 else Any
        for k, v in value.items():
            _walk(f"{path}.{k}", vt, v, out)
        return
    if isinstance(value, tuple) or (isinstance(value, list) and value and not _scalar_list(ann)):
        args = get_args(ann)
        for i, v in enumerate(value):
            at = args[i] if origin is tuple and i < len(args) else (args[0] if args else Any)
            _walk(f"{path}.{i}", at, v, out)
        return
    if isinstance(value, list):  # list of literals/strings -> one comma-separated field
        inner = get_args(ann)[0] if get_args(ann) else Any
        choices = [str(a) for a in get_args(inner)] if get_origin(inner) is Literal else []
        out.append(FormField(path, _label(path), "list", ", ".join(map(str, value)), choices))
        return
    kind, choices = _kind(path, ann, value)
    out.append(FormField(path, _label(path), kind, value, choices, nullable=nullable))


def _scalar_list(ann: Any) -> bool:
    args = get_args(ann)
    if get_origin(ann) is not list or not args:
        return False
    inner = args[0]
    return inner in (str, int, float) or get_origin(inner) is Literal


def _label(path: str) -> str:
    parts = path.split(".")[1:]
    if path.startswith("bands.") and parts[-1] in ("0", "1"):
        parts[-1] = "low" if parts[-1] == "0" else "high"
    return " ".join(p.replace("_", " ") for p in parts)


def _display(f: FormField, units: Units) -> None:
    if f.kind not in ("temp", "delta") or not isinstance(f.value, int | float):
        return
    f.f_value = float(f.value)
    if units == "C":
        f.value = f_to_c(f.value) if f.kind == "temp" else round(f.value * 5 / 9, 1)


def sections(s: Settings, owner: bool) -> list[Section]:
    units: Units = s.display.units
    out: list[Section] = []
    for name in ORDER:
        fs: list[FormField] = []
        _walk(name, Settings.model_fields[name].annotation, getattr(s, name), fs)
        for f in fs:
            _display(f, units)
        out.append(
            Section(
                name=name,
                title=TITLES[name],
                locked=name in OWNER_SECTIONS and not owner,
                fields=fs,
            )
        )
    return out
