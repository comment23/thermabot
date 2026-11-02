"""Override + nudge lifecycle (D§3, D§5.5, FR-OVR-1..3, FR-CMF-6). Pure: `now` passed in."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from thermabot.config import NudgeCfg, OverrideDuration, ScheduleCfg
from thermabot.core.schedule import next_boundary

TZ = ZoneInfo("America/New_York")
Role = Literal["owner", "admin", "member", "viewer"]

_HOURS = {"1h": 1, "2h": 2, "4h": 4}


class Override(BaseModel):
    source: Literal["ui", "external"]
    started_at: datetime
    expires_at: datetime
    cancelled_at: datetime | None = None
    target_f: float | None = None
    mode: str | None = None
    pause: bool = False


def override_expiry(
    duration: OverrideDuration, now: datetime, sched: ScheduleCfg, until: datetime | None = None
) -> datetime:
    if duration in _HOURS:
        return now + timedelta(hours=_HOURS[duration])
    if duration == "next_block":
        return next_boundary(sched, now)
    if until is None or until <= now:
        raise ValueError("'until' needs a future datetime")
    return until


def start_override(
    now: datetime,
    sched: ScheduleCfg,
    duration: OverrideDuration,
    *,
    source: Literal["ui", "external"] = "ui",
    until: datetime | None = None,
    target_f: float | None = None,
    mode: str | None = None,
    pause: bool = False,
) -> Override:
    """External changes always last to the next block boundary (FR-OVR-3)."""
    if source == "external":
        duration = "next_block"
    return Override(
        source=source,
        started_at=now,
        expires_at=override_expiry(duration, now, sched, until),
        target_f=target_f,
        mode=mode,
        pause=pause,
    )


def override_active(o: Override | None, now: datetime) -> bool:
    return o is not None and o.cancelled_at is None and o.started_at <= now < o.expires_at


def cancel_override(o: Override, now: datetime) -> Override:
    return o.model_copy(update={"cancelled_at": now})


# --- Member nudge (D§5.5) ----------------------------------------------------------


class Nudge(BaseModel):
    user_id: int
    room: str
    delta_f: float
    created_at: datetime
    expires_at: datetime
    cancelled_at: datetime | None = None


def nudge_active(n: Nudge, now: datetime) -> bool:
    return n.cancelled_at is None and n.created_at <= now < n.expires_at


def nudges_today(nudges: Sequence[Nudge], user_id: int, now: datetime) -> int:
    """Nudges created by user on the current local (America/New_York) day."""
    day = now.astimezone(TZ).date()
    return sum(
        1 for n in nudges if n.user_id == user_id and n.created_at.astimezone(TZ).date() == day
    )


def create_nudge(
    existing: Sequence[Nudge],
    now: datetime,
    *,
    user_id: int,
    role: Role,
    user_room: str | None,
    delta_f: float,
    hours: float,
    room: str | None = None,
    cfg: NudgeCfg | None = None,
) -> tuple[Nudge, list[Nudge]]:
    """Validate and create a nudge. Returns (new nudge, existing list with any open nudge on
    the same room cancelled). Raises ValueError/PermissionError on rule violations."""
    cfg = cfg or NudgeCfg()
    if role == "viewer":
        raise PermissionError("viewers cannot nudge")
    if role == "member":
        if user_room is None:
            raise PermissionError("member has no room")
        if room is not None and room != user_room:
            raise PermissionError("members can only nudge their own room")
        room = user_room
    if room is None:
        raise ValueError("room required")
    if delta_f == 0 or abs(delta_f) > cfg.max_delta_f or delta_f != int(delta_f):
        raise ValueError(f"delta_f must be ±1..±{cfg.max_delta_f:g}")
    if not 1 <= hours <= cfg.max_hours:
        raise ValueError(f"hours must be 1..{cfg.max_hours:g}")
    if nudges_today(existing, user_id, now) >= cfg.per_day:
        raise ValueError(f"limit of {cfg.per_day} nudges per day reached")
    updated = [
        n.model_copy(update={"cancelled_at": now}) if n.room == room and nudge_active(n, now) else n
        for n in existing
    ]
    new = Nudge(
        user_id=user_id,
        room=room,
        delta_f=delta_f,
        created_at=now,
        expires_at=now + timedelta(hours=hours),
    )
    return new, updated


def active_nudge_deltas(nudges: Sequence[Nudge], now: datetime) -> dict[str, float]:
    """room -> δ of the open nudge (latest wins)."""
    out: dict[str, float] = {}
    for n in sorted(nudges, key=lambda n: n.created_at):
        if nudge_active(n, now):
            out[n.room] = n.delta_f
    return out
