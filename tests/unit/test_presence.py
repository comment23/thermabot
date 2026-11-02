from collections.abc import Mapping
from datetime import datetime, timedelta

import pytest

from tests.conftest import local
from tests.house import HOUSE
from thermabot.config import PresenceCfg
from thermabot.core.presence import (
    TrackerState,
    life360_stale_names,
    map_state,
    resolve,
)
from thermabot.core.types import HouseState, PersonState, Presence

CFG = HOUSE.presence
T0 = local(2026, 10, 5, 12, 0)
L360 = {n: f"device_tracker.life360_{n}" for n in ("alex", "sam", "jordan", "casey", "riley")}


def trackers(states: Mapping[str, str], t: datetime) -> dict[str, TrackerState]:
    return {L360[n]: TrackerState(state=s, last_reported=t) for n, s in states.items()}


@pytest.mark.brd("FR-PRS-3a")
@pytest.mark.parametrize(
    "raw, st",
    [
        ("home", PersonState.HOME),
        ("not_home", PersonState.AWAY),
        ("Work", PersonState.AWAY),
        ("unknown", PersonState.HOME),
        ("unavailable", PersonState.HOME),
        (None, PersonState.HOME),
    ],
)
def test_map_state(raw: str | None, st: PersonState) -> None:
    assert map_state(raw) == st


@pytest.mark.brd("FR-PRS-3")
def test_casey_home_others_away_30min_empty() -> None:
    away = {"alex": "not_home", "sam": "Work", "jordan": "School", "casey": "home"}
    p = resolve(trackers(away, T0), T0, CFG)
    assert p.house == HouseState.OCCUPIED  # clock just started
    assert "casey" not in p.people and "riley" not in p.people
    t = T0 + timedelta(minutes=29)
    p = resolve(trackers(away, t), t, CFG, prev=p)
    assert p.house == HouseState.OCCUPIED
    t = T0 + timedelta(minutes=30)
    p = resolve(trackers(away, t), t, CFG, prev=p)
    assert p.house == HouseState.EMPTY
    assert p.since == t


@pytest.mark.brd("FR-PRS-2")
def test_one_returns_home_occupied_and_clock_resets() -> None:
    away = {"alex": "not_home", "sam": "not_home", "jordan": "not_home"}
    p = resolve(trackers(away, T0), T0, CFG)
    t1 = T0 + timedelta(minutes=40)
    p = resolve(trackers({**away, "sam": "home"}, t1), t1, CFG, prev=p)
    assert p.house == HouseState.OCCUPIED
    t2 = t1 + timedelta(minutes=10)
    p = resolve(trackers(away, t2), t2, CFG, prev=p)
    assert p.house == HouseState.OCCUPIED  # sam away only 10 min


@pytest.mark.brd("FR-PRS-3a")
def test_unknown_counts_as_home() -> None:
    p = resolve({}, T0, CFG)  # no trackers at all
    assert all(pp.state == PersonState.HOME and pp.source == "unknown" for pp in p.people.values())
    later = T0 + timedelta(hours=2)
    away = trackers({"alex": "not_home", "sam": "not_home", "jordan": "unavailable"}, later)
    prev = Presence(house=HouseState.OCCUPIED, since=T0)
    assert resolve(away, later, CFG, prev=prev).house == HouseState.OCCUPIED


@pytest.mark.brd("FR-PRS-4")
def test_life360_stale_falls_back_to_companion() -> None:
    old = T0 - timedelta(minutes=31)
    tr = trackers({"alex": "home"}, old)
    tr["device_tracker.alex_phone"] = TrackerState(state="not_home", last_reported=T0)
    p = resolve(tr, T0, CFG)
    assert p.people["alex"].source == "companion"
    assert p.people["alex"].state == PersonState.AWAY
    assert "alex" in life360_stale_names(p)


@pytest.mark.brd("FR-PRS-4")
def test_companion_stale_and_jordan_no_fallback_unknown() -> None:
    old = T0 - timedelta(minutes=121)
    tr = trackers({"alex": "not_home", "jordan": "not_home"}, T0 - timedelta(minutes=31))
    tr["device_tracker.alex_phone"] = TrackerState(state="not_home", last_reported=old)
    p = resolve(tr, T0, CFG)
    assert p.people["alex"].source == "unknown" and p.people["alex"].state == PersonState.HOME
    assert p.people["jordan"].source == "unknown" and p.people["jordan"].state == PersonState.HOME


@pytest.mark.brd("FR-PRS-1")
def test_guest_forces_occupied() -> None:
    away = {"alex": "not_home", "sam": "not_home", "jordan": "not_home"}
    p = resolve(trackers(away, T0), T0, CFG)
    t = T0 + timedelta(hours=1)
    guest = t + timedelta(hours=3)
    g = resolve(trackers(away, t), t, CFG, prev=p, guest_until=guest)
    assert g.house == HouseState.OCCUPIED and g.guest_until == guest
    t2 = guest + timedelta(minutes=1)
    e = resolve(trackers(away, t2), t2, CFG, prev=g, guest_until=guest)
    assert e.house == HouseState.EMPTY and e.guest_until is None


@pytest.mark.brd("FR-PRS-3")
def test_excluded_cannot_be_configured() -> None:
    with pytest.raises(ValueError):
        PresenceCfg(
            people={"casey": {"life360": L360["casey"]}},  # type: ignore[dict-item]
            excluded=["casey"],
        )
