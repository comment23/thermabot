"""Safety restore (D\x156.1, NFR-SAFE-3) + off-window bookkeeping (D\x154.2, NFR-SAFE-1/2). Pure."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from thermabot.config import BlockName, Settings
from thermabot.core.fallback import fallback_setpoints
from thermabot.core.limits import OFF_ABS_CAP_H, OFF_FORCED_ON_H
from thermabot.core.types import Action, HouseState, Snapshot


class OffWindow(BaseModel):
    start: datetime
    reevaluate_at: datetime
    end: datetime | None = None  # None = open


class RestoreResult(BaseModel):
    action: Action
    kind: Literal["heat", "cool"]
    trigger: str
    hold_until: datetime  # planner may not pick `off` before this


def _occupied(snap: Snapshot) -> bool:
    # Unknown presence counts as occupied (conservative).
    return snap.presence is None or snap.presence.house == HouseState.OCCUPIED


def crossing(snap: Snapshot, s: Settings) -> tuple[Literal["heat", "cool"], str] | None:
    """Which limit is crossed, if any. Thermostat <= 62 / >= 80; occupied fresh room <= 60 / >= 82.
    Floor wins if both somehow hold (cold is checked first)."""
    sc = s.safety
    t = snap.thermostat.current_temp
    fresh = [
        (k, r.temp)
        for k, r in snap.rooms.items()
        if r.temp is not None and not r.stale and not r.estimated and _occupied(snap)
    ]
    if t is not None and t <= sc.floor_tstat_f:
        return "heat", f"thermostat {t:g}°F <= {sc.floor_tstat_f:g}"
    for k, v in fresh:
        if v <= sc.floor_room_f:
            return "heat", f"{k} {v:g}°F <= {sc.floor_room_f:g}"
    if t is not None and t >= sc.ceil_tstat_f:
        return "cool", f"thermostat {t:g}°F >= {sc.ceil_tstat_f:g}"
    for k, v in fresh:
        if v >= sc.ceil_room_f:
            return "cool", f"{k} {v:g}°F >= {sc.ceil_room_f:g}"
    return None


def allows_drift(snap: Snapshot, kind: Literal["heat", "cool"], lo: float, hi: float) -> bool:
    """True unless the thermostat is already conditioning toward the restore target."""
    th = snap.thermostat
    if kind == "heat":
        return th.hvac_mode not in ("heat", "heat_cool") or th.heat_sp is None or th.heat_sp < lo
    return th.hvac_mode not in ("cool", "heat_cool") or th.cool_sp is None or th.cool_sp > hi


def check(snap: Snapshot, s: Settings, block: BlockName, now: datetime) -> RestoreResult | None:
    """Every tick, independent of the plan. One heat_cool write at the active block's band
    edges (heat-band low / cool-band high), flagged safety_restore."""
    hit = crossing(snap, s)
    if hit is None or not s.thermostat.configured:  # v0.2.0: no thermostat, no restore
        return None
    kind, trigger = hit
    lo, hi = fallback_setpoints(s, block)
    if not allows_drift(snap, kind, lo, hi):
        return None
    act = Action(
        service="set_temperature",
        entity_id=s.thermostat.entity_id,
        data={"hvac_mode": "heat_cool", "target_temp_low": lo, "target_temp_high": hi},
        safety_restore=True,
    )
    hold = now + timedelta(minutes=s.safety.hold_after_restore_min)
    return RestoreResult(action=act, kind=kind, trigger=trigger, hold_until=hold)


def open_off_window(now: datetime, s: Settings) -> OffWindow:
    """Created when the first `off` write actually happens; re-evaluate after 8 h."""
    return OffWindow(start=now, reevaluate_at=now + timedelta(hours=s.off.reevaluate_h))


def close_off_window(w: OffWindow, now: datetime) -> OffWindow:
    return w if w.end is not None else w.model_copy(update={"end": now})


def needs_reevaluation(w: OffWindow | None, now: datetime) -> bool:
    """At reevaluate_at the planner must pick `off` again with fresh inputs."""
    return w is not None and w.end is None and now >= w.reevaluate_at


def cumulative_off_h(windows: list[OffWindow], now: datetime) -> float:
    """Off hours in the current contiguous run of back-to-back windows (open one up to now).
    A run ends at the first non-adjacent gap; the forced-on hour resets it (NFR-SAFE-2)."""
    total = 0.0
    run_start: datetime | None = now  # >= 1 h on since the last window also ends the run
    for w in sorted(windows, key=lambda x: x.start, reverse=True):
        end = w.end or now
        if run_start is not None and run_start - end >= timedelta(hours=OFF_FORCED_ON_H):
            break
        total += max(0.0, (end - w.start).total_seconds() / 3600)
        run_start = w.start
    return total


def off_cap_reached(windows: list[OffWindow], now: datetime) -> bool:
    return cumulative_off_h(windows, now) >= OFF_ABS_CAP_H


def forced_on_until(windows: list[OffWindow], now: datetime) -> datetime | None:
    """After the 24 h cap, the next 1 h is forced to `hold`. Returns the end of that hour, or
    None when `off` is not blocked by the cap."""
    if not windows:
        return None
    last = max(windows, key=lambda x: x.start)
    if last.end is None:
        return now + timedelta(hours=OFF_FORCED_ON_H) if off_cap_reached(windows, now) else None
    if cumulative_off_h(windows, last.end) >= OFF_ABS_CAP_H:
        until = last.end + timedelta(hours=OFF_FORCED_ON_H)
        return until if now < until else None
    return None


def off_allowed_now(windows: list[OffWindow], hold_until: datetime | None, now: datetime) -> bool:
    """Planner-side gate: no off during post-restore hold or the forced-on hour/cap."""
    if hold_until is not None and now < hold_until:
        return False
    return forced_on_until(windows, now) is None and not off_cap_reached(windows, now)
