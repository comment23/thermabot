import math

import pytest

from tests.house import HOUSE
from thermabot.core.comfort import (
    base_weights,
    block_band,
    house_score,
    is_violation,
    room_band,
    room_score,
    violation_minutes,
)

S = HOUSE


@pytest.mark.brd("FR-CMF-1")
def test_block_band_by_season() -> None:
    assert block_band(S, "heat", "home") == (68, 71)
    assert block_band(S, "cool", "sleep") == (71, 74)
    assert block_band(S, "shoulder", "home") == (68, 76)


@pytest.mark.brd("FR-CMF-3")
def test_room_band_tolerance_and_nudge() -> None:
    assert room_band((68, 71), tol=2) == (66, 73)
    assert room_band((68, 71), tol=2, nudge_f=-2) == (64, 71)


@pytest.mark.brd("FR-HUM-1")
def test_humidity_tightens_hi_in_cooling_only() -> None:
    assert room_band((73, 76), 0, season="cool", rh=65) == (73, 75)
    assert room_band((73, 76), 0, season="cool", rh=55) == (73, 76)
    assert room_band((68, 71), 0, season="heat", rh=65) == (68, 71)


@pytest.mark.brd("FR-CMF-3")
@pytest.mark.parametrize(
    "temp, score",
    [(69.0, 100.0), (68.0, 100.0), (66.5, 50.0), (65.0, 0.0), (60.0, 0.0), (72.5, 50.0)],
)
def test_room_score_linear(temp: float, score: float) -> None:
    assert room_score(temp, 68, 71) == pytest.approx(score)


@pytest.mark.brd("FR-CMF-4")
def test_room_score_inf_hi() -> None:
    assert room_score(90, 68, math.inf) == 100.0
    assert room_score(65, 68, math.inf) == 0.0


@pytest.mark.brd("FR-CMF-3")
def test_house_weighted_mean() -> None:
    w = S.weights["home"]
    scores = {"thermostat": 100.0, "study": 100.0, "bedroom_1": 80.0, "bedroom_2": 80.0}
    assert house_score(scores, w) == pytest.approx(94.0)


@pytest.mark.brd("FR-CMF-3")
def test_house_floor() -> None:
    w = S.weights["home"]
    scores = {"thermostat": 100.0, "study": 100.0, "bedroom_1": 100.0, "bedroom_2": 0.0}
    assert house_score(scores, w) == pytest.approx(40.0)  # mean 85, floor 0+40
    assert house_score(scores, w, occupied=False) == pytest.approx(85.0)


@pytest.mark.brd("FR-CMF-3")
def test_den_zero_weight_ignored() -> None:
    scores = {"thermostat": 100.0, "den": 0.0}
    assert house_score(scores, {"thermostat": 1.0, "den": 0.0}) == 100.0
    assert house_score({}, {"thermostat": 1.0}) is None


@pytest.mark.brd("FR-CMF-6")
def test_nudge_weight_boost() -> None:
    w = base_weights(S, "sleep", nudged={"bedroom_1"})
    assert w["bedroom_1"] == pytest.approx(0.6)
    assert w["bedroom_2"] == pytest.approx(0.4)


@pytest.mark.brd("FR-CMF-3")
def test_violation_counting() -> None:
    z = 0.0
    for _ in range(30):
        z = violation_minutes(z, 0.0, 1.0)
    assert z == 30 and not is_violation(z, 0.4, True)
    z = violation_minutes(z, 0.0, 1.0)
    assert is_violation(z, 0.4, True)
    assert not is_violation(z, 0.0, True)  # weight 0
    assert not is_violation(z, 0.4, False)  # away
    assert violation_minutes(z, 10.0, 1.0) == 0.0
