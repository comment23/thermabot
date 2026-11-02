"""Alert catalogue + dedup lifecycle (D\x1512.2, FR-ALR-1, FR-OVR-4). Pure.

Level alerts (conditions that hold or not) open once, remind after 24 h, close once.
Edge alerts (one-off events) are emitted as-is by `event()`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from thermabot.core.types import AlertEvent

Severity = Literal["info", "warn", "critical"]

SEVERITY: dict[str, Severity] = {
    "degraded": "warn",
    "write_failed": "warn",
    "safety_restore": "critical",
    "watchdog_fired": "critical",
    "comfort_violation": "warn",
    "kill_switch": "info",
    "container_restart": "info",
    "room_stale": "info",
    "life360_stale": "info",
    "override": "info",
    "ha_auth": "critical",
}


def severity(key: str) -> Severity:
    return SEVERITY[key.split(":", 1)[0]]


class Conditions(BaseModel):
    """Live inputs for level alerts."""

    degraded_min: float = 0  # minutes continuously DEGRADED
    comfort_zero_min: dict[str, float] = Field(default_factory=dict)  # occupied rooms only
    room_dead_h: dict[str, float] = Field(default_factory=dict)
    life360_all_stale_h: float = 0
    ha_auth_rejected: bool = False


class OpenAlert(BaseModel):
    key: str
    opened_at: datetime
    last_sent: datetime
    text: str


def wanted(
    c: Conditions,
    degraded_after_min: float = 15,
    violation_min: float = 30,
    room_dead_h: float = 12,
    life360_h: float = 2,
) -> dict[str, str]:
    """Level keys that should be open now -> text."""
    out: dict[str, str] = {}
    if c.degraded_min > degraded_after_min:
        out["degraded"] = f"DEGRADED for {c.degraded_min:.0f} min"
    bad = sorted(r for r, m in c.comfort_zero_min.items() if m > violation_min)
    if bad:
        out["comfort_violation"] = f"comfort violation: {', '.join(bad)} at score 0 > 30 min"
    for r, h in sorted(c.room_dead_h.items()):
        if h > room_dead_h:
            out[f"room_stale:{r}"] = f"{r} sensor silent for {h:.0f} h"
    if c.life360_all_stale_h > life360_h:
        out["life360_stale"] = f"all Life360 trackers stale for {c.life360_all_stale_h:.1f} h"
    if c.ha_auth_rejected:
        out["ha_auth"] = "HA token rejected"
    return out


def evaluate(
    c: Conditions,
    open_alerts: Mapping[str, OpenAlert],
    now: datetime,
    remind_after_h: float = 24,
    degraded_after_min: float = 15,
) -> tuple[list[AlertEvent], dict[str, OpenAlert]]:
    """Returns (events to send, new open set)."""
    want = wanted(c, degraded_after_min=degraded_after_min)
    events: list[AlertEvent] = []
    new: dict[str, OpenAlert] = {}
    for key, text in want.items():
        cur = open_alerts.get(key)
        if cur is None:
            events.append(AlertEvent(key=key, severity=severity(key), text=text, t=now))
            new[key] = OpenAlert(key=key, opened_at=now, last_sent=now, text=text)
        elif now - cur.last_sent >= timedelta(hours=remind_after_h):
            events.append(AlertEvent(key=key, severity=severity(key), text=f"still: {text}", t=now))
            new[key] = cur.model_copy(update={"last_sent": now, "text": text})
        else:
            new[key] = cur
    for key, cur in open_alerts.items():
        if key not in want:
            events.append(
                AlertEvent(
                    key=key, severity=severity(key), text=f"resolved: {cur.text}", t=now, clear=True
                )
            )
    return events, new


def event(key: str, text: str, now: datetime, override_notify: bool = False) -> AlertEvent | None:
    """One-off alerts: write_failed, safety_restore, watchdog_fired, kill_switch,
    container_restart, override (only when the Owner enabled it, default off)."""
    if key == "override" and not override_notify:
        return None
    return AlertEvent(key=key, severity=severity(key), text=text, t=now)
