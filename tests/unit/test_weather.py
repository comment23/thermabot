from datetime import timedelta

import pytest

from tests.conftest import local
from thermabot.core.types import Forecast, ForecastPoint
from thermabot.core.weather import (
    apply_bias,
    choose_source,
    coverage_h,
    forecast_at,
    is_stale,
    rolling_bias,
)

NOW = local(2026, 10, 5, 12, 0)


def fc(src: str = "ha", age_h: float = 0, hours: int = 48, temp: float = 50.0) -> Forecast:
    pts = [ForecastPoint(t=NOW + timedelta(hours=h), temp_f=temp) for h in range(hours + 1)]
    return Forecast(source=src, fetched_at=NOW - timedelta(hours=age_h), points=pts)  # type: ignore[arg-type]


@pytest.mark.brd("FR-WX-2")
def test_stale_rules() -> None:
    assert not is_stale(fc(), NOW)
    assert not is_stale(fc(age_h=3), NOW)
    assert is_stale(fc(age_h=3.01), NOW)
    assert is_stale(fc(hours=23), NOW)  # covers < 24 h
    assert not is_stale(fc(hours=24), NOW)
    assert is_stale(None, NOW)
    assert coverage_h(Forecast(source="ha", fetched_at=NOW), NOW) == 0.0


@pytest.mark.brd("FR-WX-2")
def test_source_choice() -> None:
    ha, nws = fc("ha"), fc("nws")
    assert choose_source(ha, nws, NOW) == (ha, "ha")
    assert choose_source(fc("ha", age_h=4), nws, NOW) == (nws, "nws")
    assert choose_source(None, nws, NOW) == (nws, "nws")
    assert choose_source(fc("ha", hours=10), fc("nws", age_h=5), NOW) == (None, "none")


@pytest.mark.brd("FR-WX-3")
def test_forecast_at() -> None:
    f = Forecast(
        source="ha",
        fetched_at=NOW,
        points=[
            ForecastPoint(t=NOW, temp_f=50),
            ForecastPoint(t=NOW + timedelta(hours=1), temp_f=52),
        ],
    )
    assert forecast_at(f, NOW + timedelta(minutes=30)) == 50
    assert forecast_at(f, NOW + timedelta(hours=1)) == 52
    assert forecast_at(f, NOW - timedelta(minutes=1)) is None


@pytest.mark.brd("FR-WX-3")
def test_rolling_bias_7_days() -> None:
    pairs = [
        (NOW - timedelta(days=8), 99.0, 0.0),  # outside window
        (NOW - timedelta(days=2), 52.0, 50.0),
        (NOW - timedelta(hours=1), 51.0, 50.0),
    ]
    assert rolling_bias(pairs, NOW) == pytest.approx(1.5)
    assert rolling_bias([], NOW) is None


@pytest.mark.brd("FR-WX-3")
def test_bias_applied_only_above_threshold() -> None:
    f = fc(temp=50.0)
    assert apply_bias(f, 1.0) is f
    assert apply_bias(f, None) is f
    assert apply_bias(f, 1.5).points[0].temp_f == pytest.approx(48.5)
    assert apply_bias(f, -2.0).points[0].temp_f == pytest.approx(52.0)
