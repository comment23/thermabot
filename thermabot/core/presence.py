"""Presence resolver (D§9, FR-PRS-1..4). Pure: tracker states + previous result + now in.

Per person: fresh Life360 -> fresh companion -> unknown. unknown/unavailable/missing = HOME.
House EMPTY only when every configured person has been AWAY continuously >= empty_after_min.
Excluded people are never read. `away_since` is carried in the previous Presence, so a
restart (prev=None) restarts the clock: the conservative direction (OCCUPIED).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel

from thermabot.config import PersonCfg, PresenceCfg
from thermabot.core.types import HouseState, PersonPresence, PersonState, Presence

_UNKNOWN = {"unknown", "unavailable", ""}


class TrackerState(BaseModel):
    state: str
    last_reported: datetime


def map_state(raw: str | None) -> PersonState:
    """home -> HOME; not_home or any other zone -> AWAY; unknown/unavailable/missing -> HOME."""
    if raw is None or raw.lower() in _UNKNOWN or raw.lower() == "home":
        return PersonState.HOME
    return PersonState.AWAY


def _age_min(now: datetime, ts: TrackerState) -> float:
    return (now - ts.last_reported).total_seconds() / 60.0


def resolve_person(
    p: PersonCfg, trackers: Mapping[str, TrackerState], now: datetime, cfg: PresenceCfg
) -> PersonPresence:
    if p.life360 and p.life360 in trackers:
        ts = trackers[p.life360]
        age = _age_min(now, ts)
        if age <= cfg.life360_stale_min:
            return PersonPresence(state=map_state(ts.state), source="life360", age_min=age)
    if p.companion and p.companion in trackers:
        ts = trackers[p.companion]
        age = _age_min(now, ts)
        if age <= cfg.companion_stale_min:
            return PersonPresence(state=map_state(ts.state), source="companion", age_min=age)
    return PersonPresence(state=PersonState.HOME, source="unknown")


def resolve(
    trackers: Mapping[str, TrackerState],
    now: datetime,
    cfg: PresenceCfg,
    prev: Presence | None = None,
    guest_until: datetime | None = None,
    guest_on: bool = False,
) -> Presence:
    people: dict[str, PersonPresence] = {}
    for name, pcfg in cfg.people.items():
        if name in cfg.excluded:
            continue
        pp = resolve_person(pcfg, trackers, now, cfg)
        if pp.state == PersonState.AWAY:
            before = prev.people.get(name) if prev else None
            since = before.away_since if before and before.away_since else now
            pp = pp.model_copy(update={"away_since": since})
        people[name] = pp

    guest = guest_on or (guest_until is not None and now < guest_until)
    all_away_long = bool(people) and all(
        pp.away_since is not None
        and (now - pp.away_since).total_seconds() / 60.0 >= cfg.empty_after_min
        for pp in people.values()
    )
    house = HouseState.EMPTY if all_away_long and not guest else HouseState.OCCUPIED
    since = prev.since if prev is not None and prev.house == house else now
    return Presence(
        house=house, since=since, people=people, guest_until=guest_until if guest else None
    )


def life360_stale_names(p: Presence) -> list[str]:
    """People not resolved from Life360 (P6 'Life360 stale -> companion', FR-PRS-4)."""
    return [n for n, pp in p.people.items() if pp.source != "life360"]
