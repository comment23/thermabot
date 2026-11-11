"""48 h forecast plan projection (v0.4.0, FR-WX / Forecast page). Pure: `now` passed in.

For each hour of the next `hours` hours: the planned mode and heat/cool setpoints, the
expected indoor (thermostat) temperature and the expected heat/cool runtime in minutes.
Reuses the plan the planner produced and the planner's own thermostat-control step
(planner.ctrl_step) over the thermal model. Without a fitted model the projection uses the
fallback plan (never `off`) and the default model parameters, and says so in `banner`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from thermabot.config import Settings
from thermabot.core.fallback import Current, fallback_plan
from thermabot.core.planner import Choice, ctrl_step
from thermabot.core.schedule import TZ
from thermabot.core.thermal import ThermalParams
from thermabot.core.types import Forecast, ForecastPoint, HvacMode, Plan, PlanStep
from thermabot.core.weather import forecast_season

HOURS = 48
STEP_MIN = 15
NOT_FITTED = "Fallback plan, model not fitted yet"


class ProjectedHour(BaseModel):
    t: datetime  # start of the hour (UTC)
    temp_f: float | None = None  # outdoor forecast
    humidity: float | None = None
    conditions: str | None = None
    wind_mph: float | None = None
    cloud_pct: float | None = None
    mode: HvacMode
    heat_sp: float | None = None
    cool_sp: float | None = None
    indoor_f: float | None = None  # expected thermostat temperature at the end of the hour
    heat_min: float | None = None  # expected runtime in this hour
    cool_min: float | None = None


class Projection(BaseModel):
    now: datetime
    plan_kind: Literal["optimised", "fallback"]
    fallback: bool
    model_fitted: bool
    banner: str = ""
    hours: list[ProjectedHour] = Field(default_factory=list)


def _hour_of(t: datetime) -> float:
    lt = t.astimezone(TZ)
    return lt.hour + lt.minute / 60


def step_at(plan: Plan, t: datetime) -> PlanStep:
    """The plan step in force at t: the latest step at or before t, else the first."""
    best = plan.steps[0]
    for st in plan.steps:
        if st.t <= t:
            best = st
        else:
            break
    return best


def point_at(fc: Forecast | None, t: datetime) -> ForecastPoint | None:
    """Hourly step function, like weather.forecast_at; before the first point -> the first."""
    if fc is None or not fc.points:
        return None
    pts = sorted(fc.points, key=lambda p: p.t)
    best = pts[0]
    for p in pts:
        if p.t <= t:
            best = p
        else:
            break
    return best


def project(
    s: Settings,
    now: datetime,
    plan: Plan | None,
    fc: Forecast | None,
    t0: float | None,
    params: ThermalParams | None,
    current: Current | None = None,
    hours: int = HOURS,
) -> Projection:
    """Deterministic: same inputs, same output. `params` None = no fitted model: the fallback
    plan is projected with default model parameters and flagged."""
    fitted = params is not None
    if plan is None or not plan.steps or (not fitted and plan.kind != "fallback"):
        why = NOT_FITTED if not fitted else "Fallback plan: no plan yet"
        plan = fallback_plan(
            s,
            now,
            why,
            horizon_h=hours + 1,
            current=current,
            season=forecast_season(fc, now, s.season, s.weather),
            indoor_f=t0,
        )
    p = params or ThermalParams()
    h0 = now.replace(minute=0, second=0, microsecond=0)
    per_h = 60 // STEP_MIN
    temp = t0
    out: list[ProjectedHour] = []
    for h in range(hours):
        th = h0 + timedelta(hours=h)
        first = step_at(plan, th)
        heat = cool = 0.0
        ok = True
        for k in range(per_h):
            t = th + timedelta(minutes=k * STEP_MIN)
            st = step_at(plan, t)
            pt = point_at(fc, t)
            if temp is None or pt is None:
                ok = False
                continue
            c = Choice(st.hvac_mode, st.heat_sp, st.cool_sp)
            temp, u = ctrl_step(temp, pt.temp_f, _hour_of(t), c, p)
            heat += max(u, 0.0) * STEP_MIN
            cool += max(-u, 0.0) * STEP_MIN
        wp = point_at(fc, th)
        out.append(
            ProjectedHour(
                t=th,
                temp_f=None if wp is None else round(wp.temp_f, 1),
                humidity=None if wp is None else wp.humidity,
                conditions=None if wp is None else wp.conditions,
                wind_mph=None if wp is None else wp.wind_mph,
                cloud_pct=None if wp is None else wp.cloud_pct,
                mode=first.hvac_mode,
                heat_sp=first.heat_sp,
                cool_sp=first.cool_sp,
                indoor_f=round(temp, 1) if ok and temp is not None else None,
                heat_min=round(heat, 1) if ok else None,
                cool_min=round(cool, 1) if ok else None,
            )
        )
    if not fitted:
        banner = NOT_FITTED
    elif plan.kind == "fallback":
        banner = plan.reason_banner or "Fallback plan"
    else:
        banner = ""
    return Projection(
        now=now,
        plan_kind=plan.kind,
        fallback=plan.kind == "fallback",
        model_fitted=fitted,
        banner=banner,
        hours=out,
    )
