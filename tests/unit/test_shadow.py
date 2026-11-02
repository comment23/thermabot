"""v0.1.9: shadow vs actual comparison math (core/shadow.py)."""

from datetime import timedelta

import pytest

from tests.conftest import local
from tests.house import HOUSE
from thermabot.core.shadow import (
    MinuteRow,
    Setpoints,
    ShadowDay,
    buckets,
    comfort,
    compare,
    simulate,
    totals,
    verdict,
)
from thermabot.core.thermal import ThermalParams

T0 = local(2026, 10, 5, 12, 0)  # Monday, home block
S = HOUSE
P = ThermalParams(a=0.1, s=0.0, q_h=3.0, q_c0=4.0, q_c1=0.0)
HOLD = Setpoints(mode="heat_cool", heat_sp=73, cool_sp=76)


def rows(n: int, t_in: float = 74.0, t_out: float = 90.0, **kw: object) -> list[MinuteRow]:
    base = {
        "block": "home",
        "t_in": t_in,
        "t_out": t_out,
        "rooms": {"study": t_in, "bedroom_1": t_in, "bedroom_2": t_in},
        "ages": {"study": 1.0, "bedroom_1": 1.0, "bedroom_2": 1.0},
        "actual": HOLD,
        "bot": HOLD,
    }
    base.update(kw)
    return [MinuteRow(t=T0 + timedelta(minutes=i), **base) for i in range(n)]  # type: ignore[arg-type]


@pytest.mark.brd("FR-MET-1")
def test_buckets_align_to_quarter_hours() -> None:
    rs = [MinuteRow(t=T0 + timedelta(minutes=10 + i)) for i in range(10)]  # 12:10-12:19
    assert [len(b) for b in buckets(rs)] == [5, 5]


@pytest.mark.brd("FR-MET-1")
def test_comfort_in_band_is_100_and_no_violation() -> None:
    pct, viol = comfort(S, "cool", rows(60))
    assert pct == 100 and viol == 0


@pytest.mark.brd("FR-CMF-3")
def test_violation_minutes_after_30_consecutive_zero() -> None:
    cold = rows(60, t_in=60.0)  # 13 °F below the 73 cool-band low: every room scores 0
    pct, viol = comfort(S, "cool", cold)
    assert pct == 0 and viol == 30  # minutes 31..60 exceed violation_min=30


@pytest.mark.brd("FR-MET-1")
def test_unoccupied_minutes_not_counted() -> None:
    rs = rows(30, t_in=60.0, block="away", occupied=False)
    assert comfort(S, "cool", rs) == (None, 0)


@pytest.mark.brd("FR-MET-1")
def test_no_model_leaves_bot_null() -> None:
    d = compare(S, "cool", rows(60, hvac_action="cooling", cs=0.5), None)
    assert d.actual_cool_min == 60 and d.actual_cool_effort == 30
    assert d.bot_heat_min is None and d.saved_pct is None and "no thermal model" in d.note
    assert d.coverage_pct == round(100 * 60 / 1440, 1)


@pytest.mark.brd("FR-MET-1")
def test_same_setpoints_save_nothing() -> None:
    d = compare(S, "cool", rows(120, t_in=76.0, hvac_action="cooling"), P)
    assert d.bot_steps == 8 and d.saved_pct == pytest.approx(0)
    assert d.bot_comfort_pct == d.actual_comfort_pct


@pytest.mark.brd("FR-MET-1")
def test_wider_bot_setpoints_save_runtime() -> None:
    bot = Setpoints(mode="heat_cool", heat_sp=70, cool_sp=78)
    d = compare(S, "cool", rows(120, t_in=76.0, hvac_action="cooling", bot=bot), P)
    assert d.saved_pct is not None and d.saved_pct > 0
    assert d.saved_min is not None and 0 < d.saved_min <= 120
    assert d.energy_saved_pct is not None and d.energy_saved_pct > 0
    assert d.bot_cool_min is not None and d.sim_actual_runtime_min is not None
    assert d.bot_cool_min < d.sim_actual_runtime_min


@pytest.mark.brd("FR-MET-1")
def test_resync_each_step_does_not_compound() -> None:
    """Every 15-min step restarts from the measured temperature: a large model error in one
    step never leaks into the next."""
    rs = rows(30, t_in=76.0)
    sim = simulate(rs, ThermalParams(a=0.5, s=0.0, q_c0=0.5), "actual")
    assert sim.shifts[15] == pytest.approx(sim.shifts[0])  # step 2 starts fresh like step 1


@pytest.mark.brd("FR-MET-1")
def test_totals_and_verdict() -> None:
    days = [
        ShadowDay(
            actual_heat_min=0,
            actual_cool_min=100,
            saved_min=12,
            actual_comfort_pct=95,
            bot_comfort_pct=95,
        ),
        ShadowDay(actual_heat_min=0, actual_cool_min=100),  # no model that day
    ]
    t = totals(days)
    assert t.actual_runtime_min == 200 and t.saved_pct == pytest.approx(12)
    assert verdict(t, True, 200, 96) == "Bot would have run 12% less with the same comfort"
    assert verdict(t, False, 41, 96) == "Not enough data yet: 41 of 96 steps"
    worse = totals([days[0].model_copy(update={"bot_comfort_pct": 80, "saved_min": -5})])
    assert (
        verdict(worse, True, 200, 96) == "Bot would have run 5% more with lower comfort (-15 pts)"
    )
