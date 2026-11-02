"""Plain-schedule fallback plan (D§4.3, FR-PLN-7). Pure. Never chooses `off`.

v0.1.9: `planner.fallback_source` picks what the fallback holds when there is no confident
model. 'thermostat' (default) holds the thermostat's current manual setpoints when they are
a valid heat_cool pair ("do what the household does"), else the band edges; 'bands' = band
edges only.
The current setpoints are passed in (core stays pure).

v0.6.2 (presence/away-mode fix): `house`/`away_until` mirror planner.PlanInputs/build_timeline.
When house is EMPTY, every fallback step inside the away window uses the away band edges
(fallback_setpoints(s, 'away')) instead of holding the current setpoint or a home/sleep band.
Away always takes priority over fallback_source='thermostat' hold-current: holding an
occupant-chosen manual setpoint while genuinely away defeats the purpose of away mode and the
documented away-savings intent (D§4.3/FR-PLN-7); 'never off' and the hard limits still apply.
After the away window (a given away_until, or the next schedule boundary when none is given)
the step reverts to the normal hold-current/schedule-band behaviour, same as before.

v0.6.5 (stale away-band residue fix): confirmed live (Bob's direct DB query + Sam's code
trace) that `fallback_source='thermostat'` has a stable bad fixed point when the house
transitions EMPTY -> OCCUPIED but the thermostat is still physically sitting at the away
band edges (the bot's own prior write, not touched by anyone since): `valid_current()` can
only check physical legality (right mode, deadband, hard limits) -- it has no way to tell
"household genuinely chose this" apart from "this is just the bot's own away-band leftover".
So fallback_plan kept re-emitting the away band with reason "fallback: holding current
62-80" forever, because each replan read back exactly what it had just written. Fix: when
`house` is OCCUPIED (not EMPTY -- away already has its own branch above) and `current`
matches the away band edges (fallback_setpoints(s, 'away')) within float tolerance, `hold`
is treated as invalid and the step falls through to the normal schedule-block band
(block_at/fallback_setpoints) instead, breaking the loop. This is evaluated once up front
from the `current` passed in, not re-derived per step. A genuine manual setpoint that
differs from the away band edges is still held exactly as before -- only the exact-away-band
match changes. fallback_source='bands' is unaffected (hold was already always None there).
Known accepted tradeoff: a household that manually sets setpoints that happen to exactly
equal the away band edges while occupied will have that choice overridden by the schedule
block instead of held -- narrow and unlikely enough that adding new state (e.g. tracking
"who wrote this setpoint") to disambiguate would be overengineering for this fix.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from thermabot.config import Settings
from thermabot.core.limits import COOL_SP_MAX, COOL_SP_MIN, HEAT_SP_MIN
from thermabot.core.schedule import block_at, next_boundary
from thermabot.core.types import HouseState, Plan, PlanStep

_AWAY_BAND_TOL = 0.05  # °F; absorbs HA/SQLite float round-trip noise, not real manual offsets

# (hvac_mode, heat_sp, cool_sp) as read from the thermostat
Current = tuple[str | None, float | None, float | None]


def fallback_setpoints(s: Settings, block: str) -> tuple[float, float]:
    """heat_cool at the block's band edges: heat = heating-band low, cool = cooling-band high.
    Config validation guarantees this pair respects the deadband and hard limits."""
    heat_lo = getattr(s.bands.heat, block)[0]
    cool_hi = getattr(s.bands.cool, block)[1]
    return float(heat_lo), float(cool_hi)


def valid_current(s: Settings, current: Current | None) -> tuple[float, float] | None:
    """The thermostat's own heat_cool pair, if it is one the bot could legally write:
    mode heat_cool, deadband >= writes.deadband_f, inside the hard limits. Else None."""
    if current is None:
        return None
    mode, heat, cool = current
    if mode != "heat_cool" or heat is None or cool is None:
        return None
    if cool - heat < s.writes.deadband_f:
        return None
    if not (HEAT_SP_MIN <= heat <= COOL_SP_MAX and COOL_SP_MIN <= cool <= COOL_SP_MAX):
        return None
    return float(heat), float(cool)


def fallback_plan(
    s: Settings,
    now: datetime,
    reason: str,
    horizon_h: float | None = None,
    current: Current | None = None,
    house: HouseState = HouseState.OCCUPIED,
    away_until: datetime | None = None,
) -> Plan:
    """One step per planner step over the horizon; fan auto, heat_cool, no `off`. When
    `house` is EMPTY, steps inside the away window (away_until, else the next schedule
    boundary) use the away band edges regardless of fallback_source. Elsewhere, holds the
    current setpoints (fallback_source='thermostat' and valid), else schedule band edges.
    Exception (v0.6.5): while OCCUPIED, a `current` that exactly matches the away band edges
    is treated as stale away-band residue (the bot's own prior write), not a genuine
    household hold -- see module docstring.
    """
    step = timedelta(minutes=s.planner.step_min)
    n = int((horizon_h or s.planner.horizon_h) * 60 // s.planner.step_min)
    hold = valid_current(s, current) if s.planner.fallback_source == "thermostat" else None
    stale_away_residue = False
    if hold is not None and house != HouseState.EMPTY:
        away_heat, away_cool = fallback_setpoints(s, "away")
        if math.isclose(hold[0], away_heat, abs_tol=_AWAY_BAND_TOL) and math.isclose(
            hold[1], away_cool, abs_tol=_AWAY_BAND_TOL
        ):
            hold = None
            stale_away_residue = True
    away_end = (away_until or next_boundary(s.schedule, now)) if house == HouseState.EMPTY else None
    steps: list[PlanStep] = []
    prev: tuple[float, float] | None = None
    for k in range(n):
        t = now + k * step
        if away_end is not None and t < away_end:
            heat, cool = fallback_setpoints(s, "away")
            why = f"fallback: away band {heat:g}-{cool:g}" if (heat, cool) != prev else ""
        elif hold is not None:
            heat, cool = hold
            why = f"fallback: holding current {heat:g}-{cool:g}" if k == 0 else ""
        else:
            block = block_at(s.schedule, t)
            heat, cool = fallback_setpoints(s, block)
            if stale_away_residue and k == 0:
                why = (
                    f"fallback: stale away-band residue cleared, {block} band {heat:g}-{cool:g}"
                    if (heat, cool) != prev
                    else ""
                )
            else:
                why = f"fallback: {block} band {heat:g}-{cool:g}°F" if (heat, cool) != prev else ""
        prev = (heat, cool)
        steps.append(
            PlanStep(
                t=t, hvac_mode="heat_cool", heat_sp=heat, cool_sp=cool, fan_mode="auto", reason=why
            )
        )
    return Plan(kind="fallback", created_at=now, reason_banner=reason, steps=steps)
