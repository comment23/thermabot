"""Read-only assessment of explicitly mapped exterior-door and garage contacts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from thermabot.config import OpeningPolicyCfg, OpeningSensorCfg

OpeningStatus = Literal["unknown", "brief", "sustained", "stabilizing", "inactive"]


class OpeningFinding(BaseModel):
    status: OpeningStatus
    entity_ids: list[str]
    rooms: list[str]
    since: datetime | None = None
    duration_min: float | None = None
    message: str


def _time(st: Mapping[str, Any], key: str) -> datetime | None:
    value = st.get(key)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _contact(
    entity_id: str,
    cfg: OpeningSensorCfg,
    states: Mapping[str, Mapping[str, Any] | None],
    now: datetime,
    stale_after_min: float,
) -> tuple[bool | None, datetime | None, str | None]:
    st = states.get(entity_id)
    if not st:
        return None, None, "contact is unavailable"
    state = st.get("state")
    if state not in ("on", "off"):
        return None, None, "contact state is unknown"
    if stale_after_min:
        reported = _time(st, "last_reported") or _time(st, "last_updated")
        age_s = None if reported is None else (now - reported).total_seconds()
        if age_s is None or age_s < 0 or age_s > stale_after_min * 60:
            return None, None, "contact reading is stale"
    changed = _time(st, "last_changed")
    if changed is None or changed > now:
        return None, None, "contact transition time is unavailable"
    return state == cfg.active_state, changed, None


def _finding(
    status: OpeningStatus,
    entity_ids: list[str],
    rooms: set[str],
    since: datetime | None,
    duration: float | None,
    message: str,
) -> OpeningFinding:
    return OpeningFinding(
        status=status,
        entity_ids=entity_ids,
        rooms=sorted(rooms),
        since=since,
        duration_min=None if duration is None else round(duration, 1),
        message=message,
    )


def _elapsed(now: datetime, since: datetime) -> float | None:
    mins = (now - since).total_seconds() / 60
    return mins if mins >= 0 else None


def _status_for_open(duration: float | None, policy: OpeningPolicyCfg) -> OpeningStatus:
    if duration is None:
        return "unknown"
    return "sustained" if duration >= policy.min_open_min else "brief"


def _single(
    entity_id: str,
    cfg: OpeningSensorCfg,
    policy: OpeningPolicyCfg,
    states: Mapping[str, Mapping[str, Any] | None],
    now: datetime,
) -> OpeningFinding:
    is_open, changed, problem = _contact(entity_id, cfg, states, now, policy.stale_after_min)
    rooms = set(cfg.affected_rooms)
    if problem:
        return _finding("unknown", [entity_id], rooms, None, None, f"{entity_id}: {problem}.")
    assert changed is not None
    if is_open:
        duration = _elapsed(now, changed)
        status = _status_for_open(duration, policy)
        room_names = ", ".join(sorted(rooms)) or "unassigned rooms"
        msg = (
            f"{entity_id} has a sustained open indication for {room_names}; "
            "SHADOW observation only, no HVAC or device action was sent."
            if status == "sustained"
            else f"{entity_id} is open but below the sustained-open threshold; no action was sent."
        )
        return _finding(status, [entity_id], rooms, changed, duration, msg)
    duration = _elapsed(now, changed)
    if duration is None:
        return _finding(
            "unknown",
            [entity_id],
            rooms,
            None,
            None,
            f"{entity_id}: invalid transition time.",
        )
    if duration < policy.recovery_min:
        return _finding(
            "stabilizing",
            [entity_id],
            rooms,
            changed,
            duration,
            f"{entity_id} is closed; comfort is left to the current plan during "
            "the recovery delay.",
        )
    return _finding(
        "inactive",
        [entity_id],
        rooms,
        changed,
        duration,
        f"{entity_id} is closed; no door-based recommendation is active.",
    )


def _garage_path(
    garage_id: str,
    garage_cfg: OpeningSensorCfg,
    policy: OpeningPolicyCfg,
    states: Mapping[str, Mapping[str, Any] | None],
    now: datetime,
) -> OpeningFinding:
    doors = [
        (garage_id, garage_cfg),
        *((eid, policy.sensors[eid]) for eid in garage_cfg.required_openings),
    ]
    readings = [
        (eid, cfg, *_contact(eid, cfg, states, now, policy.stale_after_min)) for eid, cfg in doors
    ]
    rooms = set(garage_cfg.affected_rooms)
    for eid in garage_cfg.required_openings:
        rooms.update(policy.sensors[eid].affected_rooms)
    problems = [f"{eid}: {problem}" for eid, _, _, _, problem in readings if problem]
    entity_ids = [eid for eid, _ in doors]
    if problems:
        return _finding(
            "unknown",
            entity_ids,
            rooms,
            None,
            None,
            "Garage exposure is unknown; " + "; ".join(problems) + ". No action was sent.",
        )
    opened = [(eid, changed) for eid, _, is_open, changed, _ in readings if is_open]
    if len(opened) == len(readings):
        garage_changed = readings[0][3]
        assert garage_changed is not None
        connector_opened = [changed for _, changed in opened[1:] if changed is not None]
        since = max(garage_changed, max(connector_opened))
        duration = _elapsed(now, since)
        status = _status_for_open(duration, policy)
        msg = (
            "Garage exterior and a mapped garage-to-house door have a sustained open path "
            f"to {', '.join(sorted(rooms)) or 'unassigned rooms'}; SHADOW observation only, "
            "no HVAC or device action was sent."
            if status == "sustained"
            else "Garage and house-door path is open but below the sustained-open "
            "threshold; no action was sent."
        )
        return _finding(status, entity_ids, rooms, since, duration, msg)
    closed_changes = [
        changed for _, _, is_open, changed, _ in readings if not is_open and changed is not None
    ]
    if not closed_changes:
        return _finding(
            "unknown",
            entity_ids,
            rooms,
            None,
            None,
            "Garage-to-house path cannot be confirmed closed; no action was sent.",
        )
    since = max(closed_changes)
    duration = _elapsed(now, since)
    if duration is None:
        return _finding(
            "unknown", entity_ids, rooms, None, None, "Garage transition time is invalid."
        )
    if duration < policy.recovery_min:
        return _finding(
            "stabilizing",
            entity_ids,
            rooms,
            since,
            duration,
            "Garage-to-house path is closed; comfort is left to the current plan "
            "during the recovery delay.",
        )
    return _finding(
        "inactive",
        entity_ids,
        rooms,
        since,
        duration,
        "Garage-to-house path is not open; no door-based recommendation is active.",
    )


def assess(
    policy: OpeningPolicyCfg,
    states: Mapping[str, Mapping[str, Any] | None],
    now: datetime,
) -> list[OpeningFinding]:
    """Assess explicit mappings; never change a thermostat or send a Home Assistant service."""
    if now.tzinfo is None:
        raise ValueError("opening assessment requires a timezone-aware clock")
    if not policy.enabled:
        return []
    now = now.astimezone(UTC)
    out: list[OpeningFinding] = []
    for entity_id, cfg in sorted(policy.sensors.items()):
        if cfg.role == "exterior":
            out.append(_single(entity_id, cfg, policy, states, now))
        elif cfg.role == "garage_exterior":
            out.append(_garage_path(entity_id, cfg, policy, states, now))
    return out
