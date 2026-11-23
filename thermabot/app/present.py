"""Display helpers shared by pages. Temperatures render in the saved display units."""

from __future__ import annotations

from thermabot.app.units import Units, format_temp
from thermabot.core.types import PlanStep


def action_sentence(step: PlanStep, units: Units = "F") -> str:
    """One line for a step. The mode word is not repeated in front of the setpoint."""
    if step.hvac_mode == "off":
        return "Off, fan on" if step.fan_mode == "on" else "Off"
    if step.hvac_mode == "heat" and step.heat_sp is not None:
        return f"Heat to {format_temp(step.heat_sp, units)}"
    if step.hvac_mode == "cool" and step.cool_sp is not None:
        return f"Cool to {format_temp(step.cool_sp, units)}"
    if step.hvac_mode in ("heat_cool", "auto") and (
        step.heat_sp is not None or step.cool_sp is not None
    ):
        return f"Hold {format_temp(step.heat_sp, units)}–{format_temp(step.cool_sp, units)}"
    return step.hvac_mode.replace("_", " ")
