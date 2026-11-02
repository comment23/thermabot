"""v0.3.0 display units. The core and the stored settings are always °F; °C exists only at the
UI/API edge. Rule (mirrored in static/setup.js and static/settings.js): show °C to 0.1, and
store °F rounded to 0.5. Every 0.5 °F value survives F -> C -> F unchanged (the 0.05 °C display
rounding is under 0.1 °F, well inside the 0.25 °F half-step)."""

from __future__ import annotations

from typing import Literal

Units = Literal["F", "C"]


def f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9, 1)


def c_to_f(c: float) -> float:
    return round((c * 9 / 5 + 32) * 2) / 2


def to_display(f: float, units: Units) -> float:
    return f_to_c(f) if units == "C" else f


def from_display(v: float, units: Units) -> float:
    return c_to_f(v) if units == "C" else v
