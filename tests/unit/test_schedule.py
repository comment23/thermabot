from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import local
from thermabot.config import ScheduleCfg, SeasonCfg
from thermabot.core.schedule import block_at, next_boundary, season_for, season_from_forecast
from thermabot.core.types import Forecast, ForecastPoint

S = ScheduleCfg()

# 2026-10-05 is a Monday; 2026-10-10 Sat, 2026-10-11 Sun.


@pytest.mark.brd("FR-CMF-2")
@pytest.mark.parametrize(
    "t, block",
    [
        (local(2026, 10, 5, 6, 29), "sleep"),  # Mon before 06:30
        (local(2026, 10, 5, 6, 30), "home"),
        (local(2026, 10, 5, 21, 59), "home"),
        (local(2026, 10, 5, 22, 0), "sleep"),
        (local(2026, 10, 10, 7, 0), "sleep"),  # Sat: sleep until 08:00
        (local(2026, 10, 10, 8, 0), "home"),
        (local(2026, 10, 11, 7, 59), "sleep"),  # Sun
        (local(2026, 10, 12, 6, 45), "home"),  # Mon after weekend
    ],
)
def test_block_weekday_weekend(t: datetime, block: str) -> None:
    assert block_at(S, t) == block


@pytest.mark.brd("FR-CMF-2")
def test_block_accepts_utc() -> None:
    # 10:45 UTC = 06:45 EDT on a Monday
    assert block_at(S, datetime(2026, 10, 5, 10, 45, tzinfo=UTC)) == "home"


@pytest.mark.brd("FR-CMF-2")
def test_naive_rejected() -> None:
    with pytest.raises(ValueError):
        block_at(S, datetime(2026, 10, 5, 12, 0))


@pytest.mark.brd("FR-CMF-2")
def test_next_boundary() -> None:
    assert next_boundary(S, local(2026, 10, 5, 12, 0)) == local(2026, 10, 5, 22, 0)
    assert next_boundary(S, local(2026, 10, 5, 22, 0)) == local(2026, 10, 6, 6, 30)
    assert next_boundary(S, local(2026, 10, 9, 23, 0)) == local(2026, 10, 10, 8, 0)


@pytest.mark.brd("D§8.5")
def test_dst_fall_back_2026_11_01() -> None:
    # Sun 2026-11-01: clocks go 02:00 EDT -> 01:00 EST; the day has 25 h.
    sat_night = local(2026, 10, 31, 23, 0)
    nb = next_boundary(S, sat_night)
    assert nb == local(2026, 11, 1, 8, 0)
    assert nb.utcoffset() == timedelta(hours=-5)
    # Same-tzinfo subtraction is wall-clock in Python; compare real elapsed time in UTC.
    assert nb.astimezone(UTC) - sat_night.astimezone(UTC) == timedelta(hours=10)
    assert block_at(S, datetime(2026, 11, 1, 5, 30, tzinfo=UTC)) == "sleep"  # 01:30 EDT
    assert block_at(S, datetime(2026, 11, 1, 6, 30, tzinfo=UTC)) == "sleep"  # 01:30 EST
    assert block_at(S, datetime(2026, 11, 1, 13, 0, tzinfo=UTC)) == "home"  # 08:00 EST


@pytest.mark.brd("D§8.5")
def test_dst_spring_forward_2027_03_14() -> None:
    # Sun 2027-03-14: 02:00 EST -> 03:00 EDT; the day has 23 h.
    sat_night = local(2027, 3, 13, 23, 0)
    nb = next_boundary(S, sat_night)
    assert nb == local(2027, 3, 14, 8, 0)
    assert nb.utcoffset() == timedelta(hours=-4)
    assert nb.astimezone(UTC) - sat_night.astimezone(UTC) == timedelta(hours=8)
    assert block_at(S, datetime(2027, 3, 14, 11, 59, tzinfo=UTC)) == "sleep"  # 07:59 EDT
    assert block_at(S, datetime(2027, 3, 14, 12, 0, tzinfo=UTC)) == "home"  # 08:00 EDT


@pytest.mark.brd("D§5.2")
@pytest.mark.parametrize(
    "temps, season",
    [([50.0] * 24, "heat"), ([75.0] * 24, "cool"), ([64.0] * 24, "shoulder"), ([60.0], "shoulder")],
)
def test_season(temps: list[float], season: str) -> None:
    assert season_for(SeasonCfg(), temps) == season


@pytest.mark.brd("D§5.2")
def test_season_from_forecast_uses_next_24h() -> None:
    now = local(2026, 10, 5, 12, 0)
    pts = [ForecastPoint(t=now + timedelta(hours=h), temp_f=50.0) for h in range(24)]
    pts += [ForecastPoint(t=now + timedelta(hours=h), temp_f=90.0) for h in range(24, 48)]
    fc = Forecast(source="ha", fetched_at=now, points=pts)
    assert season_from_forecast(SeasonCfg(), fc, now) == "heat"
