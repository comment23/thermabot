"""v0.5.0 core.efficiency: qualifying days, robust fit + 95% band, empty state, index,
savings series. Pure and deterministic; includes degenerate cases."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from thermabot.core import efficiency as E
from thermabot.core.efficiency import EffDay, Point, ShadowIn


def d(i: int) -> str:
    return (date(2026, 1, 1) + timedelta(days=i)).isoformat()


def heat_day(i: int, t: float, k: float = 10.0, cov: float = 100.0, **kw: float) -> EffDay:
    hdd = max(0.0, 65 - t)
    return EffDay(day=d(i), mean_out_f=t, hdd=hdd, cdd=0.0, heat_min=k * hdd, cool_min=0.0,
                  coverage_pct=cov, **kw)  # fmt: skip


def pts(xy: list[tuple[float, float]]) -> list[Point]:
    return [Point(day=d(i), x=x, dd=10, runtime_min=10 * y, y=y) for i, (x, y) in enumerate(xy)]


@pytest.mark.brd("FR-EFF-1")
def test_hourly_degree_days() -> None:
    assert E.hourly_degree_days([]) is None
    assert E.hourly_degree_days([None, float("nan")]) is None
    m, h, c = E.hourly_degree_days([60.0] * 12 + [70.0] * 12)  # type: ignore[misc]
    assert (m, h, c) == (65.0, 2.5, 2.5)  # a swing day counts both
    assert E.hourly_degree_days([50.0, None]) == (50.0, 15.0, 0.0)


@pytest.mark.brd("FR-EFF-1")
def test_points_qualifying_rules() -> None:
    days = [
        heat_day(0, 40),  # ok
        heat_day(1, 63.5),  # HDD 1.5 <= 2
        heat_day(2, 40, cov=79.9),  # coverage too low
        heat_day(3, 40, cov=80.0),  # boundary ok
        EffDay(day=d(4), mean_out_f=float("nan"), hdd=20, heat_min=10, coverage_pct=100),
        EffDay(day=d(5), mean_out_f=40, hdd=25, heat_min=None, coverage_pct=100),
        EffDay(day=d(6), mean_out_f=40, hdd=25, heat_min=-1, coverage_pct=100),
    ]
    p = E.points(days, "heat")
    assert [x.day for x in p] == [d(0), d(3)]
    assert p[0].y == pytest.approx(10.0) and p[0].dd == 25
    assert E.points(days, "cool") == []


@pytest.mark.brd("FR-EFF-1")
def test_points_duplicate_day_keeps_last_sorted() -> None:
    p = E.points([heat_day(2, 40), heat_day(0, 30), heat_day(2, 50, k=4)], "heat")
    assert [x.day for x in p] == [d(0), d(2)] and p[1].y == pytest.approx(4)


@pytest.mark.brd("FR-EFF-1")
@pytest.mark.parametrize("n", [0, 1, 2])
def test_fit_needs_three_points(n: int) -> None:
    assert E.fit_curve(pts([(30 + i, 5.0) for i in range(n)])) is None


@pytest.mark.brd("FR-EFF-1")
def test_fit_identical_x_is_none() -> None:
    assert E.fit_curve(pts([(40, 5), (40, 6), (40, 7), (40, 8)])) is None


@pytest.mark.brd("FR-EFF-1")
def test_fit_nan_points_ignored() -> None:
    p = pts([(30, 8), (35, 7), (40, 6)])
    p.append(Point(day=d(9), x=float("nan"), dd=10, runtime_min=1, y=1))
    f = E.fit_curve(p)
    assert f is not None and f.n == 3


@pytest.mark.brd("FR-EFF-1")
def test_exact_line_r2_one_and_tight_band() -> None:
    f = E.fit_curve(pts([(x, 20 - 0.2 * x) for x in range(20, 45, 3)]))
    assert f is not None and f.kind == "linear"
    assert f.coef[0] == pytest.approx(20, abs=1e-6) and f.coef[1] == pytest.approx(-0.2, abs=1e-6)
    assert f.r2 == pytest.approx(1.0) and f.resid_sd == pytest.approx(0, abs=1e-6)
    assert all(b.hi - b.lo < 1e-6 for b in f.band)
    assert E.predict(f, 30) == pytest.approx(14)


@pytest.mark.brd("FR-EFF-1")
def test_constant_y_r2_one() -> None:
    f = E.fit_curve(pts([(x, 5.0) for x in (30, 35, 40, 45)]))
    assert f is not None and f.r2 == 1.0 and f.coef[1] == pytest.approx(0, abs=1e-9)


@pytest.mark.brd("FR-EFF-1")
def test_robust_fit_resists_outlier_and_band_covers() -> None:
    xy = [(x, 20 - 0.2 * x + (0.3 if x % 2 else -0.3)) for x in range(20, 50, 2)]
    xy.append((35, 60.0))  # wild outlier
    f = E.fit_curve(pts(xy))
    assert f is not None and f.downweighted >= 1
    assert f.coef[1] == pytest.approx(-0.2, abs=0.05)
    assert E.predict(f, 35) == pytest.approx(13, abs=0.6)
    assert all(b.lo <= b.y <= b.hi for b in f.band)
    assert len(f.band) == E.BAND_STEPS and f.band[0].x == f.x_min
    mid = f.band[len(f.band) // 2]
    assert (mid.hi - mid.lo) < (f.band[0].hi - f.band[0].lo)  # narrowest near the mean


@pytest.mark.brd("FR-EFF-1")
def test_quadratic_chosen_only_when_clearly_better() -> None:
    curved = pts([(x, 0.02 * (x - 40) ** 2 + 5) for x in range(10, 70, 4)])
    f = E.fit_curve(curved)
    assert f is not None and f.kind == "quadratic" and f.r2 > 0.99
    assert E.predict(f, 40) == pytest.approx(5, abs=1e-6)
    small = E.fit_curve(curved[:8])  # < QUAD_MIN_N points: always linear
    assert small is not None and small.kind == "linear"


@pytest.mark.brd("FR-EFF-1")
def test_fit_deterministic() -> None:
    xy = [(30 + 1.7 * i, 9 - 0.1 * i + (i % 3) * 0.4) for i in range(12)]
    assert E.fit_curve(pts(xy)) == E.fit_curve(pts(xy))


@pytest.mark.brd("FR-EFF-1")
def test_t95() -> None:
    assert E.t95(0) == math.inf
    assert E.t95(1) == pytest.approx(12.706) and E.t95(30) == pytest.approx(2.042)
    assert E.t95(60) == pytest.approx(2.000, abs=0.002)
    assert E.t95(10_000) == pytest.approx(1.96, abs=0.001)


@pytest.mark.brd("FR-EFF-2")
def test_empty_state_progress() -> None:
    c = E.season_curve([heat_day(i, 40 + i) for i in range(3)], "heat")
    assert not c.ready and c.fit is None and c.progress == "3/5"
    assert c.empty == "needs >= 5 qualifying days (3/5)"
    c0 = E.season_curve([], "cool")
    assert c0.progress == "0/5" and c0.n == 0 and c0.index is None
    c5 = E.season_curve([heat_day(i, 30 + 2 * i) for i in range(5)], "heat")
    assert c5.ready and c5.fit is not None and c5.empty is None and c5.progress == "5/5"


@pytest.mark.brd("FR-EFF-2")
def test_ready_but_identical_x() -> None:
    c = E.season_curve([heat_day(i, 40) for i in range(6)], "heat")
    assert c.ready and c.fit is None and c.empty is not None and "same outdoor" in c.empty


@pytest.mark.brd("FR-EFF-3")
def test_index_baseline_and_trend() -> None:
    days = [heat_day(i, 40, k=10) for i in range(7)] + [heat_day(7 + i, 40, k=8) for i in range(7)]
    idx = E.efficiency_index(E.points(days, "heat"))
    assert idx is not None and idx.baseline == pytest.approx(10)
    assert idx.series[0].value == 100.0 and idx.series[0].day == d(6)
    assert idx.latest == 80.0 and len(idx.series) == 8


@pytest.mark.brd("FR-EFF-3")
def test_index_gaps_use_calendar_window_and_degenerate() -> None:
    assert E.efficiency_index(E.points([heat_day(i, 40) for i in range(6)], "heat")) is None
    zero = [heat_day(i, 40, k=0) for i in range(7)]
    assert E.efficiency_index(E.points(zero, "heat")) is None
    days = [heat_day(i, 40, k=10) for i in range(7)] + [heat_day(30, 40, k=5)]
    idx = E.efficiency_index(E.points(days, "heat"))
    assert idx is not None and idx.latest == 50.0  # only day 30 is in its 7-day window


@pytest.mark.brd("FR-EFF-4")
def test_savings_series_cumulative_and_gaps() -> None:
    rows = [
        ShadowIn(day=d(2), actual_min=200, saved_min=-10),
        ShadowIn(day=d(0), actual_min=100, saved_min=10),
        ShadowIn(day=d(1), actual_min=50, saved_min=None),  # no bot estimate
        ShadowIn(day=d(3), actual_min=None, saved_min=5),  # no measurement
        ShadowIn(day=d(4), actual_min=0, saved_min=0),
        ShadowIn(day=d(5), actual_min=float("nan"), saved_min=1),
    ]
    s = E.savings_series(rows)
    assert s.simulated and "Simulated" in s.label
    assert [x.day for x in s.days] == [d(i) for i in range(6)]
    a, b, c, e, z, n = s.days
    assert (a.saved_min, a.saved_pct, a.bot_min, a.cum_saved_min, a.cum_pct) == (
        10,
        10.0,
        90,
        10,
        10.0,
    )
    assert b.saved_min is None and b.cum_saved_min == 10
    assert c.saved_pct == -5.0 and c.cum_saved_min == 0 and c.cum_pct == 0.0
    assert e.saved_min is None and z.saved_pct is None and n.actual_min is None
    assert s.n_estimated == 3 and s.total_saved_min == 0 and s.total_pct == 0.0
    empty = E.savings_series([])
    assert empty.days == [] and empty.total_pct is None


@pytest.mark.brd("FR-EFF-2")
def test_tile_text() -> None:
    none = E.summarize([], [])
    assert none.tile.text == "collecting data 0/5" and none.tile.index is None
    three = E.summarize([heat_day(i, 40 + i) for i in range(3)], [])
    assert three.tile.text == "collecting data 3/5"
    five = E.summarize([heat_day(i, 30 + 2 * i) for i in range(5)], [])
    assert five.tile.text.startswith("curve ready (5 heat days)")
    full = E.summarize([heat_day(i, 30 + i) for i in range(9)], [])
    assert full.tile.index == 100.0 and full.tile.text == "index 100 (heat)"
    assert full.days == 9 and full.heat.n == 9 and full.cool.n == 0


@pytest.mark.brd("FR-EFF-2")
def test_tile_prefers_most_recent_season() -> None:
    heat = [heat_day(i, 30 + i) for i in range(8)]
    cool = [
        EffDay(
            day=d(20 + i),
            mean_out_f=75 + i,
            hdd=0,
            cdd=10 + i,
            heat_min=0,
            cool_min=50 + 5 * i,
            coverage_pct=95,
        )  # fmt: skip
        for i in range(8)
    ]
    t = E.summarize(heat + cool, []).tile
    assert t.season == "cool" and t.index is not None
