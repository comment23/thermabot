from datetime import timedelta

import pytest

from tests.conftest import local
from tests.house import HOUSE
from thermabot.core.comfort import (
    apply_staleness,
    house_score,
    remote_rooms,
    room_age_min,
    staleness_factor,
)

S = HOUSE
NOW = local(2026, 10, 5, 23, 0)


@pytest.mark.brd("D§5.3")
def test_age_uses_newer_stamp() -> None:
    a = room_age_min(NOW, NOW - timedelta(minutes=50), NOW - timedelta(minutes=10))
    assert a == pytest.approx(10.0)
    assert room_age_min(NOW, None, None) is None


@pytest.mark.brd("D§5.3")
@pytest.mark.parametrize(
    "age, f", [(0, 1.0), (90, 1.0), (225, 0.5), (360, 0.0), (1000, 0.0), (None, 0.0)]
)
def test_staleness_factor_jordan(age: float | None, f: float) -> None:
    r = S.rooms["bedroom_1"]
    assert staleness_factor(age, r.stale_min, r.dead_min) == pytest.approx(f)


@pytest.mark.brd("D§5.3")
def test_jordan_dead_weight_zero_others_renormalize() -> None:
    w = S.weights["sleep"]
    ages = {"bedroom_1": 400.0, "bedroom_2": 5.0, "thermostat": 1.0}
    eff, est = apply_staleness(w, ages, S.rooms)
    assert eff["bedroom_1"] == 0.0
    assert eff["bedroom_2"] == 0.4 and eff["thermostat"] == 0.2
    assert est == set()
    scores = {"bedroom_1": 0.0, "bedroom_2": 100.0, "thermostat": 40.0}
    # (0.4*100 + 0.2*40) / 0.6 = 80; jordan (weight 0) does not drag the floor down
    assert house_score(scores, eff) == pytest.approx(80.0)


@pytest.mark.brd("D§5.3")
def test_all_upstairs_dead_estimated_half_weight() -> None:
    w = S.weights["sleep"]
    ages = {"bedroom_1": None, "bedroom_2": 500.0, "thermostat": 1.0}
    pred = {"bedroom_1": 67.0, "bedroom_2": 66.5}
    eff, est = apply_staleness(w, ages, S.rooms, predicted=pred, remote=remote_rooms(S))
    assert remote_rooms(S) == ("bedroom_1", "bedroom_2")  # sleep-weighted rooms
    assert est == {"bedroom_1", "bedroom_2"}
    assert eff["bedroom_1"] == pytest.approx(0.2)
    assert eff["bedroom_2"] == pytest.approx(0.2)
    assert eff["thermostat"] == 0.2


@pytest.mark.brd("D§5.3")
def test_one_upstairs_alive_no_estimate() -> None:
    ages = {"bedroom_1": None, "bedroom_2": 5.0, "thermostat": 1.0}
    _, est = apply_staleness(
        S.weights["sleep"], ages, S.rooms, predicted={"bedroom_1": 67}, remote=remote_rooms(S)
    )
    assert est == set()


@pytest.mark.brd("D§5.3")
def test_thermostat_never_stale_dropped() -> None:
    eff, _ = apply_staleness(S.weights["away"], {"thermostat": 999.0}, S.rooms)
    assert eff["thermostat"] == 1.0
