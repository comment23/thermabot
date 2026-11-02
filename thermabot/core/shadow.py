"""Shadow vs actual (v0.1.9, FR-MET): what the house did vs what the bot would have done.
Pure: rows in, numbers out.

Actual = measured from 1-minute samples (runtime from hvac_action / compressor / blower;
comfort against the scheduled band of each block, D§5.2).

Bot = a thermal-model SIMULATION, not a measurement. Every 15-min step restarts from the
measured indoor temperature (resync, so model error cannot compound), uses the measured
outdoor temperature, and runs the bot's dry-run setpoints through planner.ctrl_step. The
same model also replays the thermostat's actual setpoints; runtime saved % is model(bot) vs
model(actual setpoints), so a constant model bias cancels. Bot comfort shifts every room by
(simulated - measured) thermostat temperature within the step.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from thermabot.config import BlockName, Settings
from thermabot.core.comfort import (
    apply_staleness,
    base_weights,
    block_band,
    house_score,
    is_violation,
    room_band,
    room_score,
    violation_minutes,
)
from thermabot.core.metrics import MinuteSample, runtime
from thermabot.core.planner import Choice, ctrl_step
from thermabot.core.schedule import TZ
from thermabot.core.thermal import ThermalParams
from thermabot.core.types import Season

STEP_MIN = 15
GAP = timedelta(minutes=2)  # a longer gap resets consecutive-violation counters
MODES = ("off", "heat", "cool", "heat_cool")


class Setpoints(BaseModel):
    mode: str | None = None
    heat_sp: float | None = None
    cool_sp: float | None = None


class MinuteRow(BaseModel):
    """One `samples` row, decoded by the app layer."""

    t: datetime
    block: BlockName = "home"
    occupied: bool = True
    t_in: float | None = None
    t_out: float | None = None
    rooms: dict[str, float] = Field(default_factory=dict)  # excludes the thermostat
    ages: dict[str, float | None] = Field(default_factory=dict)
    hvac_action: str | None = None
    blower: bool = False
    cs: float | None = None  # normalised 0-1
    actual: Setpoints = Field(default_factory=Setpoints)
    bot: Setpoints | None = None


class ShadowDay(BaseModel):
    actual_heat_min: float | None = None
    actual_cool_min: float | None = None
    actual_cool_effort: float | None = None
    actual_fan_only_min: float | None = None
    actual_comfort_pct: float | None = None
    actual_violation_min: float | None = None
    sim_actual_runtime_min: float | None = None
    bot_heat_min: float | None = None
    bot_cool_min: float | None = None
    bot_cool_effort: float | None = None
    bot_comfort_pct: float | None = None
    bot_violation_min: float | None = None
    saved_min: float | None = None
    saved_pct: float | None = None
    energy_saved_pct: float | None = None
    n_samples: int = 0
    bot_steps: int = 0
    coverage_pct: float = 0.0
    note: str = ""


def house(
    s: Settings,
    season: Season,
    block: BlockName,
    temps: dict[str, float],
    ages: dict[str, float | None],
    occupied: bool,
) -> tuple[float | None, dict[str, float], dict[str, float]]:
    """(house score, room scores, effective weights) for one minute (D§5.2, D§5.3)."""
    band = block_band(s, season, block)
    w, _ = apply_staleness(base_weights(s, block), ages, s.rooms)
    sc: dict[str, float] = {}
    for r, v in temps.items():
        tol = s.rooms[r].tol if r in s.rooms else 0.0
        lo, hi = room_band(band, tol, season=season, cfg=s.comfort)
        sc[r] = room_score(v, lo, hi, s.comfort.falloff_f)
    h = house_score(sc, w, occupied=occupied, floor_margin=s.comfort.floor_margin)
    return h, sc, w


def comfort(
    s: Settings,
    season: Season,
    rows: Sequence[MinuteRow],
    shifts: Sequence[float | None] | None = None,
) -> tuple[float | None, float]:
    """(comfort %, violation minutes). Comfort % = share of occupied minutes with house score
    >= target (D§5.2). Violation minute = a weighted occupied room at score 0 for more than
    violation_min consecutive minutes (FR-CMF-3). `shifts[i]` moves every temp of row i (the
    bot estimate); None skips that row."""
    occ = ok = 0
    viol = 0.0
    zero: dict[str, float] = {}
    prev: datetime | None = None
    for i, r in enumerate(rows):
        d = 0.0 if shifts is None else shifts[i]
        if d is None:
            prev = None
            continue
        temps = {k: v + d for k, v in r.rooms.items()}
        if r.t_in is not None:
            temps["thermostat"] = r.t_in + d
        if not temps:
            continue
        if prev is None or r.t - prev > GAP:
            zero = {}
        prev = r.t
        h, sc, w = house(s, season, r.block, temps, r.ages, r.occupied)
        for k, v in sc.items():
            zero[k] = violation_minutes(zero.get(k, 0.0), v, 1.0)
        if r.occupied and h is not None:
            occ += 1
            ok += h >= s.comfort.target_score
        if any(is_violation(zero[k], w.get(k, 0.0), r.occupied, s.comfort) for k in sc):
            viol += 1
    return (100.0 * ok / occ if occ else None), viol


def _choice(sp: Setpoints | None) -> Choice | None:
    if sp is None or sp.mode not in MODES:
        return None
    return Choice(sp.mode, sp.heat_sp, sp.cool_sp)  # type: ignore[arg-type]


class Sim(BaseModel):
    heat_min: float = 0.0
    cool_min: float = 0.0
    cool_effort: float = 0.0
    steps: int = 0
    shifts: list[float | None] = Field(default_factory=list)

    @property
    def runtime_min(self) -> float:
        return self.heat_min + self.cool_min


def buckets(rows: Sequence[MinuteRow]) -> list[list[int]]:
    """Row indexes grouped into 15-min steps aligned to :00/:15/:30/:45."""
    out: list[list[int]] = []
    key: datetime | None = None
    for i, r in enumerate(rows):
        t = r.t.replace(second=0, microsecond=0)
        k = t - timedelta(minutes=t.minute % STEP_MIN)
        if k != key:
            out.append([])
            key = k
        out[-1].append(i)
    return out


def simulate(rows: Sequence[MinuteRow], p: ThermalParams, which: str) -> Sim:
    """Replay `which` setpoints ('bot' or 'actual') step by step, resynced to the measured
    temperature at each step start. Duty minutes scale with the step's covered minutes."""
    sim = Sim(shifts=[None] * len(rows))
    for idx in buckets(rows):
        r0 = rows[idx[0]]
        c = _choice(r0.bot if which == "bot" else r0.actual)
        if c is None or r0.t_in is None or r0.t_out is None:
            continue
        lt = r0.t.astimezone(TZ)
        t_end, u = ctrl_step(r0.t_in, r0.t_out, lt.hour + lt.minute / 60, c, p)
        n = len(idx)
        sim.steps += 1
        if u > 0:
            sim.heat_min += u * n
        elif u < 0:
            sim.cool_min += -u * n
            sim.cool_effort += -u * n * p.sigma
        k0 = r0.t.replace(second=0, microsecond=0)
        k0 -= timedelta(minutes=k0.minute % STEP_MIN)
        for i in idx:
            r = rows[i]
            if r.t_in is None:
                continue
            m = min(STEP_MIN, (r.t - k0).total_seconds() / 60 + 1)
            sim.shifts[i] = r0.t_in + m / STEP_MIN * (t_end - r0.t_in) - r.t_in
    return sim


def _pct(base: float, new: float) -> float | None:
    return (base - new) / base * 100.0 if base > 0 else None


def compare(
    s: Settings,
    season: Season,
    rows: Sequence[MinuteRow],
    params: ThermalParams | None,
    day_minutes: int = 1440,
) -> ShadowDay:
    """One day of shadow vs actual. No model -> bot columns stay None (never faked)."""
    out = ShadowDay(
        n_samples=len(rows),
        coverage_pct=round(100.0 * len(rows) / day_minutes, 1) if day_minutes else 0.0,
    )
    if not rows:
        out.note = "no samples"
        return out
    rt = runtime(
        [MinuteSample(hvac_action=r.hvac_action, blower_active=r.blower, cs=r.cs) for r in rows]
    )
    out.actual_heat_min = rt.heat_min
    out.actual_cool_min = rt.cool_min
    out.actual_cool_effort = rt.cool_effort
    out.actual_fan_only_min = rt.fan_only_min
    out.actual_comfort_pct, out.actual_violation_min = comfort(s, season, rows)
    if params is None:
        out.note = "no thermal model yet: bot estimate not available"
        return out
    bot = simulate(rows, params, "bot")
    act = simulate(rows, params, "actual")
    if bot.steps == 0:
        out.note = "no dry-run plan steps recorded"
        return out
    out.bot_steps = bot.steps
    out.sim_actual_runtime_min = round(act.runtime_min, 1)
    out.bot_heat_min = round(bot.heat_min, 1)
    out.bot_cool_min = round(bot.cool_min, 1)
    out.bot_cool_effort = round(bot.cool_effort, 1)
    out.bot_comfort_pct, out.bot_violation_min = comfort(s, season, rows, bot.shifts)
    out.saved_pct = _pct(act.runtime_min, bot.runtime_min)
    measured = rt.heat_min + rt.cool_min
    out.saved_min = None if out.saved_pct is None else measured * out.saved_pct / 100.0
    out.energy_saved_pct = _pct(
        act.heat_min + act.cool_effort, bot.heat_min + bot.cool_effort
    )  # runtime-based proxy (cool weighted by compressor speed); no kWh rate configured
    out.note = "bot = model simulation (resynced every 15 min), not a measurement"
    return out


class Totals(BaseModel):
    days: int = 0
    actual_runtime_min: float = 0.0
    saved_min: float | None = None
    saved_pct: float | None = None
    actual_comfort_pct: float | None = None
    bot_comfort_pct: float | None = None


def totals(days: Sequence[ShadowDay]) -> Totals:
    """7-day roll-up. saved % is runtime-weighted over the days that have a bot estimate."""
    out = Totals(days=len(days))
    out.actual_runtime_min = sum((d.actual_heat_min or 0) + (d.actual_cool_min or 0) for d in days)
    with_bot = [d for d in days if d.saved_min is not None]
    if with_bot:
        base = sum((d.actual_heat_min or 0) + (d.actual_cool_min or 0) for d in with_bot)
        out.saved_min = sum(d.saved_min or 0 for d in with_bot)
        out.saved_pct = out.saved_min / base * 100 if base > 0 else 0.0
    ac = [d.actual_comfort_pct for d in days if d.actual_comfort_pct is not None]
    bc = [d.bot_comfort_pct for d in with_bot if d.bot_comfort_pct is not None]
    out.actual_comfort_pct = sum(ac) / len(ac) if ac else None
    out.bot_comfort_pct = sum(bc) / len(bc) if bc else None
    return out


def verdict(t: Totals, have_model: bool, steps: int, need: int) -> str:
    """Plain-language one-liner for the Shadow report."""
    if not have_model:
        return f"Not enough data yet: {steps} of {need} steps"
    if t.saved_pct is None:
        return "Model ready; the first bot-vs-actual comparison appears after the next 00:05 rollup"
    pct = t.saved_pct
    if abs(pct) < 1:
        run = "about the same runtime"
    elif pct > 0:
        run = f"run {pct:.0f}% less"
    else:
        run = f"run {-pct:.0f}% more"
    ca, cb = t.actual_comfort_pct, t.bot_comfort_pct
    if ca is None or cb is None or abs(cb - ca) <= 2:
        cmf = "the same comfort"
    elif cb > ca:
        cmf = f"better comfort (+{cb - ca:.0f} pts)"
    else:
        cmf = f"lower comfort ({cb - ca:.0f} pts)"
    return f"Bot would have {run} with {cmf}"
