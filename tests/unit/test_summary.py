from datetime import date, timedelta

import pytest

from tests.conftest import local
from thermabot.core.metrics import Baseline, Savings
from thermabot.core.summary import (
    DayForecast,
    LastWeek,
    classify,
    next_fire,
    outlook,
    render,
    send_late,
    stretches,
)

LW = LastWeek(
    heat_h=10,
    cool_h=2,
    cool_effort_h=1.2,
    fan_only_h=3,
    comfort_pct=97,
    violation_min=0,
    overrides_by_user={"alex": 2},
    savings=Savings(baseline=900, actual=780, saved=120, pi90=30, pct=13.3),
)
HEAT = Baseline(beta=10, alpha=0, n=20, resid_sd=5, x_mean=10, sxx=100, r2=0.9)


def days(hl: list[tuple[float, float]]) -> list[DayForecast]:
    d0 = date(2026, 10, 12)  # Monday
    return [
        DayForecast(day=d0 + timedelta(days=i), high_f=h, low_f=lo) for i, (h, lo) in enumerate(hl)
    ]


@pytest.mark.brd("FR-ALR-2")
def test_sections_present() -> None:
    txt = render(LW, days([(60, 40)] * 7), HEAT, None)
    assert "Last week" in txt and "Next 7 days" in txt
    assert "saved 2.0 h (±0.5)" in txt and "alex 2" in txt
    assert txt.count("lower confidence") == 4  # days 4-7


@pytest.mark.brd("FR-ALR-2")
def test_missing_extended_forecast() -> None:
    txt = render(LW.model_copy(update={"savings": None}), None, None, None)
    assert "forecast unavailable" in txt and "baseline low confidence" in txt
    assert "Last week" in txt


@pytest.mark.brd("FR-ALR-2")
def test_classify_and_expected_runtime() -> None:
    o = outlook(days([(60, 40), (75, 60), (90, 75)]), HEAT, None)
    assert [x.kind for x in o] == ["heating", "mild", "cooling"]
    assert o[0].expected_h == pytest.approx(15 * 10 / 60, abs=0.05)  # HDD 15 x 10 min
    assert classify(DayForecast(day=date(2026, 1, 1), high_f=70, low_f=66)) == "mild"


@pytest.mark.brd("FR-ALR-2")
def test_stretches() -> None:
    s = stretches(days([(89, 70), (90, 70), (80, 60), (30, 24), (32, 25), (88, 20)]))
    assert s == ["hot stretch Mon–Tue", "cold stretch Thu–Sat"]
    assert stretches(days([(89, 70), (80, 60), (90, 70)])) == []


@pytest.mark.brd("FR-ALR-2")
def test_schedule_sunday_1800_dst_and_late_send() -> None:
    nf = next_fire(local(2026, 10, 30, 9, 0))  # Fri before DST end (Nov 1)
    assert nf == local(2026, 11, 1, 18, 0) and nf.utcoffset() == timedelta(hours=-5)
    assert next_fire(local(2026, 11, 1, 18, 0)) == local(2026, 11, 8, 18, 0)
    sch = local(2026, 10, 4, 18, 0)
    assert send_late(sch, sch + timedelta(hours=5, minutes=59))
    assert not send_late(sch, sch + timedelta(hours=6))
