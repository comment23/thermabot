from datetime import timedelta

import pytest

from tests.conftest import local
from thermabot.core.comfort import (
    AuxHeatState,
    aux_adjust,
    aux_heat_step,
    expected_room_temp,
    room_band,
    room_score,
)

T0 = local(2026, 12, 5, 20, 0)
EXP = 68.0


def step(prev: AuxHeatState, mins: int, t_room: float, **kw: object) -> AuxHeatState:
    args: dict[str, object] = dict(
        season="heat",
        fresh=True,
        t_room=t_room,
        t_room_30m_ago=None,
        expected=EXP,
        furnace_idle_30m=False,
    )
    args.update(kw)
    return aux_heat_step(prev, T0 + timedelta(minutes=mins), **args)  # type: ignore[arg-type]


@pytest.mark.brd("FR-CMF-4")
def test_expected_temp() -> None:
    assert expected_room_temp(68.0, 28.0, -1.0, 0.05) == pytest.approx(65.0)


@pytest.mark.brd("FR-CMF-4")
def test_excess_flags() -> None:
    assert step(AuxHeatState(), 0, 71.5).flag
    assert not step(AuxHeatState(), 0, 70.9).flag


@pytest.mark.brd("FR-CMF-4")
def test_synthetic_spike_flags() -> None:
    s = step(AuxHeatState(), 0, 70.0, t_room_30m_ago=67.5, furnace_idle_30m=True)
    assert s.flag
    # same rise while the furnace ran is not a space heater
    s = step(AuxHeatState(), 0, 70.0, t_room_30m_ago=67.5, furnace_idle_30m=False)
    assert not s.flag


@pytest.mark.brd("FR-CMF-4")
def test_only_heating_season_and_fresh() -> None:
    assert not step(AuxHeatState(), 0, 75.0, season="cool").flag
    assert not step(AuxHeatState(), 0, 75.0, season="shoulder").flag
    assert not step(AuxHeatState(), 0, 75.0, fresh=False).flag


@pytest.mark.brd("FR-CMF-4")
def test_clears_after_60_min_below_threshold() -> None:
    s = AuxHeatState(flag=True)
    s = step(s, 0, 69.0)  # excess 1.0 < 1.5: hysteresis run starts
    assert s.flag and s.below_since == T0
    s = step(s, 59, 69.0)
    assert s.flag
    s = step(s, 60, 69.0)
    assert not s.flag


@pytest.mark.brd("FR-CMF-4")
def test_hysteresis_resets_on_rebound() -> None:
    s = step(AuxHeatState(flag=True), 0, 69.0)
    s = step(s, 40, 70.0)  # excess 2.0 >= 1.5 resets the run
    assert s.flag and s.below_since is None
    s = step(s, 90, 69.0)
    assert s.flag and s.below_since == T0 + timedelta(minutes=90)
    assert step(s, 149, 69.0).flag
    assert not step(s, 150, 69.0).flag


@pytest.mark.brd("FR-CMF-4")
def test_flagged_ignores_too_warm_but_penalises_too_cold() -> None:
    w, band = aux_adjust(0.4, room_band((68, 71), tol=2), flag=True)
    assert w == pytest.approx(0.1)
    assert room_score(80.0, *band) == 100.0  # too warm ignored
    assert room_score(63.0, *band) == 0.0  # too cold: lo 66, 3°F below
    assert aux_adjust(0.4, (66, 73), flag=False) == (0.4, (66, 73))
