"""Evidence thresholds and counterfactuals for the read-only room research features."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.house import HOUSE, house_settings
from thermabot.config import InsightsCfg
from thermabot.core import insights as I
from thermabot.core.efficiency import EffDay
from thermabot.core.thermal import ThermalParams

NOW = datetime(2026, 10, 5, 18, tzinfo=UTC)


@pytest.mark.brd("FR-UI-1")
def test_sensor_and_humidity_evidence() -> None:
    rows = [I.Observation(NOW + timedelta(minutes=i), 73, 70, "fresh", 50) for i in range(120)]
    quality = I.sensor_quality(rows, 1440)
    assert quality["persistent_disagreement"] and quality["offset_f"] == 3
    assert quality["coverage_pct"] == pytest.approx(100 * 120 / 1440)
    assert not I.sensor_quality(rows[:119], 1440)["persistent_disagreement"]
    rows.append(I.Observation(NOW, 73, humidity=70))
    rh = I.humidity_comfort(rows, InsightsCfg())
    assert rh["outside_rh_minutes"] == 1
    assert rh["comfortable_pct"] == pytest.approx(100 * 120 / 121)
    assert I.humidity_comfort([], InsightsCfg())["comfortable_pct"] is None


@pytest.mark.brd("FR-MET-1")
def test_meter_reset_source_change_gap_and_power_integration() -> None:
    cfg = InsightsCfg(off_peak_rate=0.1, peak_rate=0.3)
    rows = [
        I.Observation(NOW + timedelta(minutes=i), energy=I.Meter(value, source))
        for i, value, source in (
            (0, 10, "a"),
            (1, 10.1, "a"),
            (2, 0, "a"),
            (3, 20, "b"),
            (10, 21, "b"),
            (11, 21.2, "b"),
        )
    ]
    usage = I.energy_usage(rows, cfg)
    assert usage["kwh"] == pytest.approx(0.3) and usage["covered_minutes"] == 2
    assert usage["resets"] == 1 and usage["gaps"] == 2
    assert usage["cost"] == pytest.approx(0.03)
    power = [I.Observation(NOW + timedelta(minutes=i), power=I.Meter(3, "p")) for i in (0, 1)]
    assert I.energy_usage(power, InsightsCfg())["kwh"] == pytest.approx(0.05)
    assert I.energy_usage([], cfg)["kwh"] is None


@pytest.mark.brd("FR-UI-1")
def test_equipment_short_cycles_and_gaps() -> None:
    rows = [
        I.Observation(
            NOW + timedelta(minutes=i), action="idle" if i % 3 == 0 else "heating", aux=i == 2
        )
        for i in range(10)
    ]
    diagnosis = I.heat_pump_diagnostics(rows)
    assert diagnosis["starts"] == 3 and diagnosis["short_runs"] == 3
    assert diagnosis["cycling_alert"] and diagnosis["aux_minutes"] == 1
    for row in rows[3:]:
        row.t += timedelta(minutes=20)
    assert not I.heat_pump_diagnostics(rows)["cycling_alert"]
    assert I.heat_pump_diagnostics([])["aux_minutes"] is None


@pytest.mark.brd("FR-UI-1")
def test_schedule_requires_five_days_and_eighty_percent_away() -> None:
    start = datetime(2026, 10, 5, 14, tzinfo=UTC)  # 10:00 local, weekday
    rows = [
        I.Observation(start + timedelta(days=d, minutes=m), house="EMPTY", block="home")
        for d in range(5)
        for m in range(60)
    ]
    suggestions = I.schedule_suggestions(rows)
    assert suggestions == [
        {"id": "weekday-10", "kind": "weekday", "hour": 10, "days": 5, "away_pct": 100}
    ]
    assert I.schedule_suggestions(rows[:240]) == []
    for r in rows[:20]:
        r.house = "OCCUPIED"
    assert I.schedule_suggestions(rows) == []


def days(start: datetime, runtime: float, count: int = 5, outdoor: float = 45) -> list[EffDay]:
    return [
        EffDay(
            day=(start + timedelta(days=i)).date().isoformat(),
            mean_out_f=outdoor,
            hdd=20,
            heat_min=runtime,
            coverage_pct=80,
        )
        for i in range(count)
    ]


@pytest.mark.brd("FR-MET-1")
def test_weather_comparison_requires_qualifying_matching_evidence() -> None:
    baseline = days(NOW - timedelta(days=20), 100)
    recent = days(NOW - timedelta(days=5), 125)
    result = I.weather_comparison(baseline, recent)
    assert result["available"] and result["worse"] and result["change_pct"] == 25
    boundary = I.weather_comparison(baseline, days(NOW, 120))
    assert boundary["worse"] and boundary["change_pct"] == 20
    assert not I.weather_comparison(baseline[:4], recent)["available"]
    assert not I.weather_comparison(baseline, days(NOW, 125, outdoor=60))["available"]
    recent[0].coverage_pct = 79.9
    assert not I.weather_comparison(baseline, recent)["available"]


@pytest.mark.brd("FR-PLN-3")
def test_ideal_baseline_setback_and_tariff_are_real_simulations() -> None:
    settings = house_settings(
        insights={"off_peak_rate": 0.1, "peak_rate": 0.4, "rated_power_kw": 3}
    )
    times = [NOW + timedelta(minutes=15 * i) for i in range(32)]
    params = ThermalParams(a=0.03)
    baseline = I.simulate(times, [45] * 32, 73, 0, params, settings, "heat", "baseline")
    setback = I.simulate(times, [45] * 32, 73, 0, params, settings, "heat", "setback")
    tariff = I.simulate(times, [45] * 32, 73, 0, params, settings, "heat", "tariff")
    assert baseline["comfort_pct"] == 100 and baseline["low_f"] == 73
    assert setback["runtime_min"] < baseline["runtime_min"]
    assert setback["comfort_pct"] < baseline["comfort_pct"] and setback["high_f"] == 73
    assert tariff["cost"] is not None and tariff["runtime_min"] != baseline["runtime_min"]
    assert I.simulate(times, [45] * 32, 73, 0, params, HOUSE, "heat", "baseline")["cost"] is None
