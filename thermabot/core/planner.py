"""Planner (D\x154.2, FR-PLN-1/4/5/6): per-block policy search over the thermal model. Pure.

Control is simulated as the 15-min duty cycle of a thermostat holding its setpoint (the
average of bang-bang with hysteresis), so runtime is fractional per step.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from thermabot.config import Settings
from thermabot.core.comfort import (
    base_weights,
    block_band,
    hold_setpoints,
    house_score,
    remote_rooms,
    room_band,
    room_score,
    seasonal_band,
    seasonal_mode,
)
from thermabot.core.fallback import fallback_plan
from thermabot.core.limits import OFF_ABS_CAP_H
from thermabot.core.schedule import TZ, block_at, season_from_forecast
from thermabot.core.thermal import DT_H, ThermalParams, room_offset, step
from thermabot.core.types import Forecast, HouseState, HvacMode, Plan, PlanStep, Season
from thermabot.core.weather import coverage_h, forecast_at, forecast_season, is_stale

Label = Literal["home", "sleep", "away"]
PRECOND_HOURS = (1, 2, 3, 4)
EPS = 1e-6


class PlanInputs(BaseModel):
    now: datetime
    t0: float  # current thermostat temperature
    current_mode: str = "heat_cool"
    forecast: Forecast | None
    params: ThermalParams = Field(default_factory=ThermalParams)
    confidence: Literal["ok", "low"] = "ok"
    settings: Settings = Field(default_factory=Settings)
    house: HouseState = HouseState.OCCUPIED
    away_until: datetime | None = None  # EMPTY: away until this, else until next block start
    room_offsets: dict[str, tuple[float, float]] = Field(default_factory=dict)  # (d, e)
    fan_gain: dict[str, float] = Field(default_factory=dict)  # proven remote-room gain, °F
    off_allowed: bool = True  # safety.off_allowed_now (hold, forced-on hour, cap)
    off_used_h: float = 0  # cumulative off hours in the current run
    # thermostat's current (heat_sp, cool_sp); the fallback may hold them (fallback_source)
    current_sp: tuple[float | None, float | None] | None = None


@dataclass
class Choice:
    mode: HvacMode
    heat_sp: float | None
    cool_sp: float | None
    reason: str = ""


@dataclass
class Sim:
    temps: list[float] = field(default_factory=list)  # T after each step
    us: list[float] = field(default_factory=list)  # +heat / -cool duty fraction
    runtime_min: float = 0.0


def ctrl_step(
    t: float, t_out: float, hour: float, c: Choice, p: ThermalParams
) -> tuple[float, float]:
    """One 15-min step under thermostat control. Returns (T_next, signed duty)."""
    idle = step(t, t_out, hour, "idle", p)
    if c.mode in ("heat", "heat_cool") and c.heat_sp is not None and idle < c.heat_sp:
        full = step(t, t_out, hour, "heat", p)
        f = min(1.0, (c.heat_sp - idle) / max(full - idle, EPS))
        return idle + f * (full - idle), f
    if c.mode in ("cool", "heat_cool") and c.cool_sp is not None and idle > c.cool_sp:
        full = step(t, t_out, hour, "cool", p)
        f = min(1.0, (idle - c.cool_sp) / max(idle - full, EPS))
        return idle + f * (full - idle), -f
    return idle, 0.0


def _hour(t: datetime) -> float:
    lt = t.astimezone(TZ)
    return lt.hour + lt.minute / 60


@dataclass
class Timeline:
    times: list[datetime]
    t_out: list[float]
    labels: list[Label]
    blocks: list[tuple[int, int, Label]]  # [i, j) step ranges


def build_timeline(inp: PlanInputs, fc: Forecast, horizon_h: float) -> Timeline:
    s = inp.settings
    dt = timedelta(minutes=s.planner.step_min)
    n = int(horizon_h * 60 // s.planner.step_min)
    times = [inp.now + k * dt for k in range(n)]
    first = min(fc.points, key=lambda p: p.t).temp_f
    t_out = [v if (v := forecast_at(fc, t)) is not None else first for t in times]
    away_end: datetime | None = None
    if inp.house == HouseState.EMPTY:
        from thermabot.core.schedule import next_boundary

        away_end = inp.away_until or next_boundary(s.schedule, inp.now)
    labels: list[Label] = [
        "away" if away_end is not None and t < away_end else block_at(s.schedule, t) for t in times
    ]
    blocks: list[tuple[int, int, Label]] = []
    i = 0
    for k in range(1, n + 1):
        if k == n or labels[k] != labels[i]:
            blocks.append((i, k, labels[i]))
            i = k
    return Timeline(times, t_out, labels, blocks)


class Ctx:
    """Per-plan precomputed lookups."""

    def __init__(self, inp: PlanInputs, tl: Timeline, season: Season) -> None:
        self.inp, self.tl, self.season, self.s = inp, tl, season, inp.settings
        self.bands = {lb: block_band(self.s, season, lb) for lb in ("home", "sleep", "away")}
        self.weights = {lb: base_weights(self.s, lb) for lb in ("home", "sleep", "away")}

    def rooms(self, k: int, t: float, fan: bool = False) -> dict[str, float]:
        to = self.tl.t_out[k]
        out = {"thermostat": t}
        for r, (d, e) in self.inp.room_offsets.items():
            v = room_offset(t, to, d, e)
            g = self.inp.fan_gain.get(r, 0.0) if fan else 0.0
            if g > 0:
                v = v - g if v > t else min(t, v + g)
            out[r] = v
        return out

    def score(self, k: int, t: float, fan: bool = False) -> tuple[float, bool]:
        """(house score, any weighted room at 0) for step k at thermostat temp t."""
        lb = self.tl.labels[k]
        band = self.bands[lb]
        w = self.weights[lb]
        sc: dict[str, float] = {}
        for r, v in self.rooms(k, t, fan).items():
            tol = self.s.rooms[r].tol if r in self.s.rooms else 0.0
            lo, hi = room_band(band, tol, season=self.season, cfg=self.s.comfort)
            sc[r] = room_score(v, lo, hi, self.s.comfort.falloff_f)
        h = house_score(sc, w, occupied=lb != "away", floor_margin=self.s.comfort.floor_margin)
        zero = any(sc[r] <= 0 for r in sc if w.get(r, 0) > 0)
        return (h if h is not None else 100.0), zero

    def in_band(self, k: int, t: float) -> bool:
        lo, hi = self.bands[self.tl.labels[k]]
        return lo - 0.01 <= t <= hi + 0.01

    def ok(self, k: int, t: float, strict: bool = False) -> bool:
        """Constraint at step k: within floor/ceiling; occupied steps (all steps if strict,
        used for `off`) need score >= target and no weighted room at 0."""
        sc = self.s.safety
        if not sc.floor_tstat_f < t < sc.ceil_tstat_f:
            return False
        if self.tl.labels[k] == "away" and not strict:
            return True
        h, zero = self.score(k, t)
        return h >= self.s.comfort.target_score and not zero


def run(cx: Ctx, t0: float, i: int, j: int, c: Choice, fan: bool = False) -> Sim:
    """Simulate steps [i, j) under one choice."""
    sim = Sim()
    t = t0
    p = cx.inp.params
    for k in range(i, j):
        t, u = ctrl_step(t, cx.tl.t_out[k], _hour(cx.tl.times[k]), c, p)
        sim.temps.append(t)
        sim.us.append(u)
        sim.runtime_min += abs(u) * DT_H * 60
    return sim


def candidates(
    cx: Ctx, label: Label, i: int, j: int, off_left_h: float, t0: float, prev_mode: str
) -> list[Choice]:
    db = cx.s.writes.deadband_f
    lo, hi = hold_setpoints(cx.bands[label], db)
    if cx.s.seasonal_control.enabled:
        band = seasonal_band(cx.s, label)
        mode = seasonal_mode(band, t0, cx.season, prev_mode)
        if mode == "heat":
            out = [Choice("heat", band[0], None, f"heat {label} at {band[0]:g}°F")]
        elif mode == "cool":
            out = [Choice("cool", None, band[1], f"cool {label} at {band[1]:g}°F")]
        else:
            lo, hi = hold_setpoints(band, db)
            out = [Choice("heat_cool", lo, hi, f"hold {label} {lo:g}-{hi:g}°F")]
    else:
        out = [Choice("heat_cool", lo, hi, f"hold {label} {lo:g}-{hi:g}°F")]
        if cx.season == "heat":
            out.append(Choice("heat", lo, None, f"coast {label}: heat-only at {lo:g}°F"))
        elif cx.season == "cool":
            out.append(Choice("cool", None, hi, f"coast {label}: cool-only at {hi:g}°F"))
    oc = cx.s.off
    window = cx.tl.t_out[i:j]
    hours = (j - i) * DT_H
    if (
        cx.inp.off_allowed
        and hours <= off_left_h
        and min(window) >= oc.outdoor_min_f
        and max(window) <= oc.outdoor_max_f
    ):
        out.append(Choice("off", None, None, ""))
    return out


def feasible(cx: Ctx, i: int, sim: Sim, strict: bool) -> bool:
    return all(cx.ok(i + n, t, strict) for n, t in enumerate(sim.temps))


def cost(cx: Ctx, sim: Sim, c: Choice, prev_mode: str) -> float:
    return sim.runtime_min + cx.s.planner.mode_change_penalty_min * (c.mode != prev_mode)


def off_reason(cx: Ctx, i: int, j: int, sim: Sim) -> str:
    t1 = cx.tl.times[i].astimezone(TZ).strftime("%H:%M")
    t2 = cx.tl.times[j - 1] + timedelta(minutes=cx.s.planner.step_min)
    w = cx.tl.t_out[i:j]
    ms = min(cx.score(i + n, t)[0] for n, t in enumerate(sim.temps))
    return (
        f"off {t1}–{t2.astimezone(TZ):%H:%M}: forecast {min(w):.0f}–{max(w):.0f}°F, "
        f"house coasts in band (min score {ms:.0f})"
    )


def _mode_cost(cx: Ctx, c: Choice, prev_mode: str) -> float:
    """Mode-change penalty applies to changes into a conditioning mode; a change into `off`
    is what off windows are for (ties then prefer `off`, see choose_block)."""
    if c.mode == "off" or c.mode == prev_mode:
        return 0.0
    return cx.s.planner.mode_change_penalty_min


def choose_block(
    cx: Ctx, t0: float, i: int, j: int, label: Label, prev_mode: str, off_left_h: float
) -> tuple[Choice, Sim]:
    """Cheapest feasible candidate; `off` must prove the score over every step (strict).
    No feasible candidate -> `hold` (D\x154.2)."""
    best: tuple[float, int, Choice, Sim] | None = None
    hold: tuple[Choice, Sim] | None = None
    for c in candidates(cx, label, i, j, off_left_h, t0, prev_mode):
        sim = run(cx, t0, i, j, c)
        if hold is None:
            hold = (c, sim)
        if cx.s.seasonal_control.enabled and c.mode == "off":
            lo, hi = seasonal_band(cx.s, label)
            if not all(lo <= temp <= hi for temp in (t0, *sim.temps)):
                continue
        if not feasible(cx, i, sim, strict=c.mode == "off"):
            continue
        key = (sim.runtime_min + _mode_cost(cx, c, prev_mode), 0 if c.mode == "off" else 1)
        if best is None or key < best[:2]:
            best = (key[0], key[1], c, sim)
    if best is None:
        assert hold is not None
        c, sim = hold
        return Choice(c.mode, c.heat_sp, c.cool_sp, c.reason + " (no feasible policy)"), sim
    c, sim = best[2], best[3]
    if c.mode == "off":
        c = Choice("off", None, None, off_reason(cx, i, j, sim))
    return c, sim


def precondition(
    cx: Ctx, temps: list[float], t_block_start: float, bi: int, i2: int, nxt: Label
) -> tuple[int, int, list[float], list[float]] | None:
    """Smallest n in 1..4 h such that holding the next band for the last n hours of the
    current block puts the thermostat in the next band at its start. Returns
    (start index, n, temps, duties) or None."""
    lo, hi = hold_setpoints(cx.bands[nxt], cx.s.writes.deadband_f)
    c = Choice("heat_cool", lo, hi)
    per_h = int(60 // cx.s.planner.step_min)
    for n in PRECOND_HOURS:
        p = max(bi, i2 - n * per_h)
        t_p = t_block_start if p == bi else temps[p - 1]
        sim = run(cx, t_p, p, i2, c)
        if cx.in_band(i2, sim.temps[-1]) and feasible(cx, p, sim, strict=False):
            return p, n, sim.temps, sim.us
        if p == bi:
            break
    return None


def fan_steps(cx: Ctx, temps: list[float], modes: list[HvacMode]) -> list[bool]:
    """+fan modifier (FR-FAN-1/2): circulate when an occupied step's predicted gap in a
    remote (sleep-weighted) room
    > gap_trigger_f and that room has proven gain >= min_proven_gain_f. Duty <= 30 min per
    clock hour; never with `off` (fan on with hvac off is still allowed by HA, but keep
    `off` windows pure: conservative)."""
    fc = cx.s.fan
    per_h = int(fc.max_min_per_hour // cx.s.planner.step_min)
    used: dict[tuple[int, int, int, int], int] = {}
    out: list[bool] = []
    for k, t in enumerate(temps):
        on = False
        if cx.tl.labels[k] != "away" and modes[k] != "off":
            rooms = cx.rooms(k, t)
            for r in remote_rooms(cx.s):
                gain = cx.inp.fan_gain.get(r, 0.0)
                if (
                    r in rooms
                    and abs(rooms[r] - t) > fc.gap_trigger_f
                    and (gain >= fc.min_proven_gain_f)
                ):
                    on = True
        lt = cx.tl.times[k].astimezone(TZ)
        hk = (lt.year, lt.month, lt.day, lt.hour)
        if on and used.get(hk, 0) < per_h:
            used[hk] = used.get(hk, 0) + 1
        else:
            on = False
        out.append(on)
    return out


def fallback(inp: PlanInputs, reason: str) -> Plan:
    cur = (inp.current_mode, *(inp.current_sp or (None, None)))
    return fallback_plan(
        inp.settings,
        inp.now,
        reason,
        current=cur,
        house=inp.house,
        away_until=inp.away_until,
        season=forecast_season(inp.forecast, inp.now, inp.settings.season, inp.settings.weather),
        indoor_f=inp.t0,
    )


def plan(inp: PlanInputs) -> Plan:
    """Optimised plan, or the plain-schedule fallback when confidence is low, the forecast
    is missing/stale, or the optimiser raises (D\x154.3)."""
    s = inp.settings
    fc = inp.forecast
    if fc is None or not fc.points or is_stale(fc, inp.now, s.weather):
        return fallback(inp, "fallback: forecast missing or stale")
    if inp.confidence != "ok":
        return fallback(inp, "fallback: thermal model confidence low")
    try:
        return _optimise(inp, fc)
    except Exception as e:  # noqa: BLE001 - any optimiser fault degrades to the fallback
        return fallback(inp, f"fallback: planner error ({type(e).__name__})")


def _optimise(inp: PlanInputs, fc: Forecast) -> Plan:
    s = inp.settings
    horizon = min(s.planner.horizon_h, max(24.0, coverage_h(fc, inp.now)))
    tl = build_timeline(inp, fc, horizon)
    cx = Ctx(inp, tl, season_from_forecast(s.season, fc, inp.now))
    temps: list[float] = []
    duties: list[float] = []
    choices: list[Choice] = []
    off_left = max(0.0, OFF_ABS_CAP_H - inp.off_used_h)
    prev_mode, t = inp.current_mode, inp.t0
    for bn, (i, j, label) in enumerate(tl.blocks):
        c, sim = choose_block(cx, t, i, j, label, prev_mode, off_left)
        temps += sim.temps
        duties += sim.us
        choices += [c] * (j - i)
        if not s.seasonal_control.enabled and bn + 1 < len(tl.blocks):
            nxt = tl.blocks[bn + 1][2]
            if not cx.in_band(j, temps[-1]):
                pre = precondition(cx, temps, t, i, j, nxt)
                if pre is not None:
                    p, n, pt, pu = pre
                    lo, hi = hold_setpoints(cx.bands[nxt], s.writes.deadband_f)
                    why = f"precondition {n} h for {nxt} {lo:g}-{hi:g}°F"
                    pc = Choice("heat_cool", lo, hi, why)
                    temps[p:j], duties[p:j], choices[p:j] = pt, pu, [pc] * (j - p)
                    if c.mode == "off" and p > i:
                        reason = off_reason(cx, i, p, Sim(temps=temps[i:p]))
                        choices[i:p] = [Choice("off", None, None, reason)] * (p - i)
                    elif c.mode == "off":
                        c = pc
        off_left -= sum(1 for x in choices[i:j] if x.mode == "off") * DT_H
        prev_mode, t = choices[j - 1].mode, temps[-1]
    fans = fan_steps(cx, temps, [c.mode for c in choices])
    steps: list[PlanStep] = []
    last: tuple[object, ...] | None = None
    for k, (c, tk, u, f) in enumerate(zip(choices, temps, duties, fans, strict=True)):
        key = (c.mode, c.heat_sp, c.cool_sp, f)
        why = c.reason + (" +fan circulate (upstairs gap)" if f else "") if key != last else ""
        last = key
        steps.append(
            PlanStep(
                t=tl.times[k],
                hvac_mode=c.mode,
                heat_sp=c.heat_sp,
                cool_sp=c.cool_sp,
                fan_mode="on" if f else "auto",
                pred_t=round(tk, 2),
                pred_rooms={r: round(v, 2) for r, v in cx.rooms(k, tk, f).items()},
                pred_u=round(u, 3),
                score=round(cx.score(k, tk, f)[0], 1),
                reason=why,
            )
        )
    return Plan(kind="optimised", created_at=inp.now, steps=steps)


def runtime_min(p: Plan, step_min: int = 15) -> float:
    """Predicted conditioning runtime of a plan (|duty| x step length)."""
    return sum(abs(st.pred_u or 0.0) * step_min for st in p.steps)


def as_mapping(p: Plan) -> Mapping[datetime, PlanStep]:
    return {st.t: st for st in p.steps}
