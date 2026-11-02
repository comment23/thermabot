"""v0.4.0: 48 h plan projection (pure, deterministic)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.house import HOUSE
from thermabot.core import planner
from thermabot.core.fallback import fallback_plan
from thermabot.core.projection import NOT_FITTED, Projection, point_at, project, step_at
from thermabot.core.thermal import ThermalParams
from thermabot.core.types import Forecast, ForecastPoint, Plan, PlanStep

NOW = datetime(2026, 1, 15, 17, 20, tzinfo=UTC)
H0 = NOW.replace(minute=0)


def fc(temp: float = 30.0, hours: int = 60) -> Forecast:
    return Forecast(
        source="open_meteo",
        fetched_at=NOW,
        points=[
            ForecastPoint(t=H0 + timedelta(hours=h), temp_f=temp, humidity=55, conditions="Clear")
            for h in range(hours)
        ],
    )


P = ThermalParams(a=0.1, s=0.5, q_h=3.0, q_c0=3.0, q_c1=0.05)


@pytest.mark.brd("FR-PLN-7")
def test_unfitted_model_uses_fallback_and_flags() -> None:
    pr = project(HOUSE, NOW, None, fc(), 68.0, None)
    assert isinstance(pr, Projection)
    assert pr.fallback and not pr.model_fitted and pr.banner == NOT_FITTED
    assert len(pr.hours) == 48
    assert all(h.mode == "heat_cool" for h in pr.hours)  # fallback never chooses off
    assert pr.hours[0].t == H0 and pr.hours[-1].t == H0 + timedelta(hours=47)
    # cold outside: heating runs, no cooling
    assert sum(h.heat_min or 0 for h in pr.hours) > 0
    assert all(h.cool_min == 0 for h in pr.hours)
    assert all(0 <= (h.heat_min or 0) <= 60 for h in pr.hours)


@pytest.mark.brd("FR-PLN-7")
def test_unfitted_model_ignores_optimised_plan() -> None:
    """An optimised plan without a fitted model can't happen in practice; still falls back."""
    opt = Plan(kind="optimised", created_at=NOW, steps=[PlanStep(t=NOW, hvac_mode="off")])
    pr = project(HOUSE, NOW, opt, fc(), 68.0, None)
    assert pr.plan_kind == "fallback" and all(h.mode != "off" for h in pr.hours)


@pytest.mark.brd("FR-PLN-7")
def test_fallback_holds_current_setpoints() -> None:
    pr = project(HOUSE, NOW, None, fc(), 70.0, None, current=("heat_cool", 69.0, 75.0))
    assert {(h.heat_sp, h.cool_sp) for h in pr.hours} == {(69.0, 75.0)}


@pytest.mark.brd("FR-PLN-1")
def test_projection_uses_the_plan_and_model() -> None:
    f = fc(30.0)
    p = planner.plan(
        planner.PlanInputs(now=NOW, t0=68.0, forecast=f, params=P, confidence="ok", settings=HOUSE)
    )
    assert p.kind == "optimised"
    pr = project(HOUSE, NOW, p, f, 68.0, P)
    assert pr.model_fitted and not pr.fallback and pr.banner == ""
    # planned mode/setpoints come from the plan step in force at each hour
    for h in pr.hours:
        st = step_at(p, h.t)
        assert (h.mode, h.heat_sp, h.cool_sp) == (st.hvac_mode, st.heat_sp, st.cool_sp)
    # indoor stays near/above the heat setpoint in steady state
    lows = [h.indoor_f for h in pr.hours[4:] if h.heat_sp is not None and h.indoor_f is not None]
    assert lows and min(lows) > 60


@pytest.mark.brd("FR-PLN-1")
def test_deterministic() -> None:
    a = project(HOUSE, NOW, None, fc(), 68.0, P)
    b = project(HOUSE, NOW, None, fc(), 68.0, P)
    assert a == b


@pytest.mark.brd("FR-PLN-1")
def test_hot_day_cools() -> None:
    pr = project(HOUSE, NOW, None, fc(95.0), 76.0, P)
    assert sum(h.cool_min or 0 for h in pr.hours) > 0
    assert all(h.heat_min == 0 for h in pr.hours)


@pytest.mark.brd("FR-WX-2")
def test_no_forecast_or_no_indoor_gives_none_estimates() -> None:
    pr = project(HOUSE, NOW, None, None, 68.0, P)
    assert len(pr.hours) == 48
    assert all(h.temp_f is None and h.indoor_f is None and h.heat_min is None for h in pr.hours)
    pr2 = project(HOUSE, NOW, None, fc(), None, P)
    assert all(h.indoor_f is None and h.temp_f == 30.0 for h in pr2.hours)


@pytest.mark.brd("FR-WX-2")
def test_weather_extras_carried_and_hours_param() -> None:
    pr = project(HOUSE, NOW, None, fc(), 68.0, P, hours=6)
    assert len(pr.hours) == 6
    assert pr.hours[0].humidity == 55 and pr.hours[0].conditions == "Clear"


def test_step_and_point_helpers() -> None:
    p = fallback_plan(HOUSE, NOW, "x", horizon_h=2)
    assert step_at(p, NOW - timedelta(hours=1)) is p.steps[0]
    assert step_at(p, NOW + timedelta(minutes=20)) is p.steps[1]
    f = fc(hours=3)
    assert point_at(None, NOW) is None
    assert point_at(f, H0 - timedelta(hours=2)) is f.points[0]
    assert point_at(f, H0 + timedelta(hours=9)) is f.points[-1]


def test_fallback_plan_banner_when_fitted() -> None:
    p = fallback_plan(HOUSE, NOW, "fallback: forecast missing or stale")
    pr = project(HOUSE, NOW, p, fc(), 68.0, P)
    assert pr.fallback and pr.model_fitted and "stale" in pr.banner
