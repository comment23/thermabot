import pytest

from thermabot.core.metrics import (
    Baseline,
    MinuteSample,
    best_base,
    comfort_guard,
    degree_days,
    fit_baseline,
    heating_baseline_confident,
    normalise_cs,
    runtime,
    savings,
    t90,
    usable_days,
)


def m(action: str | None = None, blower: bool = False, cs: float | None = None) -> MinuteSample:
    return MinuteSample(hvac_action=action, blower_active=blower, cs=cs)


@pytest.mark.brd("FR-FAN-1")
def test_fan_only_min() -> None:
    day = [m("fan", True, 0)] * 5 + [m("idle", True, 0.0)] * 3 + [m("idle", False, 0)] * 2
    r = runtime(day)
    assert (r.fan_only_min, r.blower_min, r.cool_min, r.heat_min) == (8, 8, 0, 0)


@pytest.mark.brd("BRD-7.2")
def test_variable_speed_day_hand_calculated() -> None:
    # 60 min at 30% + 30 min at 100% cooling, 20 heating min, 10 idle-off
    day = (
        [m("cooling", True, 0.3)] * 60
        + [m("cooling", True, 1.0)] * 30
        + [m("heating", True, None)] * 20
        + [m("idle", False, 0)] * 10
    )
    r = runtime(day)
    assert r.cool_min == 90 and r.cool_effort == pytest.approx(60 * 0.3 + 30)  # 48
    assert r.heat_min == 20 and r.heat_effort == 20
    assert r.blower_min == 110 and r.fan_only_min == 0 and r.minutes == 120


@pytest.mark.brd("BRD-7.2")
def test_missing_cs_falls_back_to_action() -> None:
    r = runtime([m("cooling", True, None)] * 10)
    assert r.cool_min == 10 and r.cool_effort == 10 and r.fan_only_min == 0
    assert normalise_cs(45) == 0.45 and normalise_cs(2, 4) == 0.5 and normalise_cs(None) is None


@pytest.mark.brd("BRD-7.2")
def test_degree_days() -> None:
    assert degree_days(50) == (15, 0)
    assert degree_days(72) == (0, 7)
    assert degree_days(60, base_f=58) == (0, 2)


@pytest.mark.brd("BRD-7.2")
def test_baseline_ols_hand_calculated() -> None:
    b = fit_baseline([0, 1, 2, 3], [1, 3, 2, 4])
    assert b.beta == pytest.approx(0.8) and b.alpha == pytest.approx(1.3)
    assert b.resid_sd == pytest.approx(0.9**0.5) and b.r2 == pytest.approx(0.64)
    with pytest.raises(ValueError):
        fit_baseline([1, 2], [1, 2])


@pytest.mark.brd("BRD-7.2")
def test_savings_and_interval() -> None:
    b = fit_baseline([0, 10, 20, 30], [10, 30, 50, 70])  # exact: 2x + 10, zero residual
    s = savings(b, [10, 20], [25, 40])
    assert (s.baseline, s.actual, s.saved) == (pytest.approx(80), 65, pytest.approx(15))
    assert s.pct == pytest.approx(18.75) and s.pi90 == pytest.approx(0, abs=1e-6)
    noisy = Baseline(beta=1, alpha=0, n=12, resid_sd=2, x_mean=5, sxx=100, r2=0.9)
    s = savings(noisy, [5], [3])
    assert s.pi90 == pytest.approx(t90(10) * 2 * (1 + 1 / 12) ** 0.5)


@pytest.mark.brd("BRD-7.2")
def test_t90_accuracy() -> None:
    assert t90(10) == pytest.approx(1.812, rel=5e-3)
    assert t90(30) == pytest.approx(1.697, rel=5e-3)


@pytest.mark.brd("BRD-7.2")
def test_best_base_grid() -> None:
    means = [40, 45, 50, 55, 58, 62]
    y = [max(0.0, 60 - t) * 10 for t in means]  # true base 60
    base, r2 = best_base(means, y)
    assert base == 60 and r2 == pytest.approx(1.0)


@pytest.mark.brd("BRD-7.2")
def test_comfort_guard_and_data_rules() -> None:
    s = savings(fit_baseline([0, 10, 20], [10, 30, 50]), [10], [20])
    assert comfort_guard(s, 95, 96, 0, 0).counted
    assert not comfort_guard(s, 93.9, 96, 0, 0).counted
    assert not comfort_guard(s, 96, 96, 5, 2).counted
    assert usable_days([24, 19.9, 20]) == [True, False, True]
    assert not heating_baseline_confident([5] * 14 + [4.9])
    assert heating_baseline_confident([5] * 15)
