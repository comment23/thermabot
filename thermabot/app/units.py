"""Temperature units.

The core, stored settings, safety limits and plans are always °F. Two edges convert:

- Display (v0.3.0): show °C to 0.1 and store °F rounded to 0.5. Every 0.5 °F value
  survives F -> C -> F unchanged (mirrored in static/setup.js and static/settings.js).
- Live Home Assistant readings: convert °C to °F exactly (no half-degree rounding) and
  convert setpoints back to the device unit on the write path. An unknown unit is not
  treated as °F.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

Units = Literal["F", "C"]
Scale = Literal["F", "C"]

_F = frozenset({"f", "°f", "℉", "fahrenheit"})
_C = frozenset({"c", "°c", "℃", "celsius"})


def f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9, 1)


def c_to_f(c: float) -> float:
    return round((c * 9 / 5 + 32) * 2) / 2


def to_display(f: float, units: Units) -> float:
    return f_to_c(f) if units == "C" else f


def from_display(v: float, units: Units) -> float:
    return c_to_f(v) if units == "C" else v


def format_temp(value: float | None, units: Units = "F", *, delta: bool = False) -> str:
    """A temperature for the UI. Stored values are °F; `delta` is a difference, not a point."""
    if value is None:
        return "—"
    v = float(value)
    if units == "C":
        v = round(v * 5 / 9, 1) if delta else f_to_c(v)
        num = f"{v:.1f}"
    else:
        num = f"{v:g}"
    if delta and v > 0:
        num = "+" + num
    return f"{num}°{'C' if units == 'C' else 'F'}"


def temperature_scale(unit: str | None) -> Scale | None:
    """'F' or 'C' for a Home Assistant temperature unit, else None (unknown)."""
    if unit is None:
        return None
    key = unit.strip().casefold()
    if key in _F:
        return "F"
    if key in _C:
        return "C"
    return None


def c_to_f_exact(c: float) -> float:
    """°C → °F without the display half-degree rounding."""
    return float(Decimal(str(c)) * Decimal(9) / Decimal(5) + Decimal(32))


def f_to_c_exact(f: float) -> float:
    """°F → °C without display rounding. Inverse of `c_to_f_exact` for short decimals."""
    return float((Decimal(str(f)) - Decimal(32)) * Decimal(5) / Decimal(9))


def delta_c_to_f(step_c: float) -> float:
    """A temperature increment in °C, expressed in °F (0.5 °C → 0.9 °F)."""
    return float(Decimal(str(step_c)) * Decimal(9) / Decimal(5))
