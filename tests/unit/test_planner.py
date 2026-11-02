import time
from datetime import timedelta

import pytest

from tests.conftest import local
from tests.house import HOUSE
from thermabot.core.planner import PlanInputs, plan
from thermabot.core.thermal import ThermalParams
from thermabot.core.types import Forecast, ForecastPoint, HouseState

MON_NOON = local(2026, 10, 5, 12, 0)  # Monday


def fc(now, temps):  # type: ignore[no-untyped-def]
    """Hourly forecast for 49 h; temps is a constant or f(hour_index)."""
    f = temps if callable(temps) else (lambda _h: temps)
    pts = [ForecastPoint(t=now + timedelta(hours=h), temp_f=f(h)) for h in range(49)]
    return Forecast(source="ha", fetched_at=now, points=pts)


def at(p, t):  # type: ignore[no-untyped-def]
    return next(s for s in p.steps if s.t == t)


@pytest.mark.brd("FR-PLN-5")
def test_hot_afternoon_precools_before_arrival() -> None:
    now = MON_NOON
    arrive = local(2026, 10, 5, 17, 0)
    inp = PlanInputs(
        now=now,
        t0=79,
        forecast=fc(now, 95),
        house=HouseState.EMPTY,
        away_until=arrive,
        params=ThermalParams(a=0.1, s=0.5, q_c0=6),
    )
    p = plan(inp)
    assert p.kind == "optimised"
    st = at(p, arrive)
    assert st.pred_t is not None and st.pred_t <= 76.01  # in home band at arrival
    pre = [s for s in p.steps if s.t < arrive and s.reason.startswith("precondition")]
    assert pre and arrive - pre[0].t <= timedelta(hours=4)
    assert at(p, now).cool_sp == 80  # away band before preconditioning


@pytest.mark.brd("FR-PLN-5")
def test_cold_night_recovery_on_time() -> None:
    now = local(2026, 10, 5, 23, 0)
    wake = local(2026, 10, 6, 6, 30)
    inp = PlanInputs(now=now, t0=67, forecast=fc(now, 20), params=ThermalParams(a=0.08, q_h=8))
    p = plan(inp)
    assert p.kind == "optimised"
    assert at(p, wake).pred_t is not None and at(p, wake).pred_t >= 67.99  # type: ignore[operator]
    pre = [s for s in p.steps if s.t < wake and s.reason.startswith("precondition")]
    assert pre and wake - pre[0].t <= timedelta(hours=4)
    assert at(p, local(2026, 10, 6, 1, 0)).heat_sp == 66  # sleep band overnight


@pytest.mark.brd("FR-PLN-1")
def test_mild_day_off() -> None:
    inp = PlanInputs(
        now=MON_NOON, t0=71, forecast=fc(MON_NOON, 66), params=ThermalParams(a=0.1, s=1.0)
    )
    p = plan(inp)
    first = p.steps[0]
    assert first.hvac_mode == "off" and first.reason.startswith("off 12:00")
    assert "forecast 66–66°F" in first.reason
    off = [s for s in p.steps if s.hvac_mode == "off"]
    assert all((s.score or 0) >= 80 for s in off)


@pytest.mark.brd("NFR-SAFE-1")
def test_no_off_outside_outdoor_window_or_when_blocked() -> None:
    cold = plan(
        PlanInputs(now=MON_NOON, t0=69, forecast=fc(MON_NOON, 45), params=ThermalParams(q_h=6))
    )
    assert all(s.hvac_mode != "off" for s in cold.steps)
    blocked = plan(
        PlanInputs(
            now=MON_NOON,
            t0=71,
            forecast=fc(MON_NOON, 66),
            params=ThermalParams(a=0.1, s=1.0),
            off_allowed=False,
        )
    )
    assert all(s.hvac_mode != "off" for s in blocked.steps)


@pytest.mark.brd("FR-CMF-2")
def test_bedtime_switches_to_sleep_band() -> None:
    now = local(2026, 10, 5, 18, 0)
    p = plan(PlanInputs(now=now, t0=69, forecast=fc(now, 35), params=ThermalParams(q_h=6)))
    st = at(p, local(2026, 10, 5, 22, 0))
    assert st.heat_sp == 66 and "sleep" in st.reason
    assert at(p, local(2026, 10, 5, 21, 0)).heat_sp == 68


@pytest.mark.brd("FR-FAN-1")
def test_fan_duty_cap() -> None:
    now = local(2026, 10, 5, 17, 0)
    inp = PlanInputs(
        now=now,
        t0=75,
        forecast=fc(now, 90),
        params=ThermalParams(q_c0=6),
        room_offsets={"bedroom_1": (4.0, 0.0)},
        fan_gain={"bedroom_1": 1.5},
        settings=HOUSE,
    )
    p = plan(inp)
    on = [s for s in p.steps if s.fan_mode == "on"]
    assert on, "stratified evening should circulate"
    per_hour: dict[tuple[int, int], int] = {}
    for s in on:
        k = (s.t.day, s.t.hour)
        per_hour[k] = per_hour.get(k, 0) + 1
    assert max(per_hour.values()) <= 2  # <= 30 min per hour
    no_gain = plan(inp.model_copy(update={"fan_gain": {"bedroom_1": 0.2}}))
    assert all(s.fan_mode == "auto" for s in no_gain.steps)


@pytest.mark.brd("FR-PLN-7")
def test_missing_forecast_fallback() -> None:
    p = plan(PlanInputs(now=MON_NOON, t0=70, forecast=None))
    assert p.kind == "fallback" and "forecast" in p.reason_banner
    stale = fc(MON_NOON - timedelta(hours=4), 50)
    assert plan(PlanInputs(now=MON_NOON, t0=70, forecast=stale)).kind == "fallback"


@pytest.mark.brd("FR-PLN-7")
def test_low_confidence_fallback() -> None:
    p = plan(PlanInputs(now=MON_NOON, t0=70, forecast=fc(MON_NOON, 50), confidence="low"))
    assert p.kind == "fallback" and "confidence" in p.reason_banner
    assert all(s.hvac_mode == "heat_cool" for s in p.steps)


@pytest.mark.brd("FR-PLN-1")
def test_plan_under_2s_and_full_horizon() -> None:
    inp = PlanInputs(
        now=MON_NOON,
        t0=70,
        forecast=fc(MON_NOON, lambda h: 50 + 15 * (h % 24 > 10)),
        room_offsets={"bedroom_1": (2, 0.05), "bedroom_2": (2, 0.05), "study": (0, 0.0)},
        fan_gain={"bedroom_1": 1.0},
    )
    t0 = time.perf_counter()
    p = plan(inp)
    assert time.perf_counter() - t0 < 2.0
    assert p.kind == "optimised" and len(p.steps) == 192
    for s in p.steps:
        if s.hvac_mode == "heat_cool":
            assert s.cool_sp is not None and s.heat_sp is not None
            assert s.cool_sp - s.heat_sp >= 3 and s.cool_sp <= 80 and s.heat_sp >= 60
