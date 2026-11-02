"""Bot state machine (D\x153). Pure: step(persisted, live, now) -> (EffectiveState, transitions)."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

State = Literal["DISABLED", "SHADOW", "ACTIVE", "OVERRIDE", "DEGRADED"]
SHADOW_MIN_DAYS = 7  # owner decision 2026-10-05: 7-day soak (was 14)
SHADOW_RESET_AFTER_H = 24
HA_DOWN_S = 60
TSTAT_MAX_AGE_MIN = 10
RECENT_WRITE_MIN = 10
WATCHDOG_WINDOW_MIN = 2
WATCHDOG_HOLD_MIN = 60
WATCHED_ATTRS = (
    "temperature",
    "target_temp_low",
    "target_temp_high",
    "hvac_mode",
    "fan_mode",
    "preset_mode",
)


class Persisted(BaseModel):
    """The `bot_state` row. None fields / a None row mean corrupt or missing -> DISABLED."""

    kill_switch: Literal["on", "off"]
    run_mode: Literal["shadow", "active"]
    shadow_started_at: datetime | None = None
    disabled_since: datetime | None = None
    preset_cleared: bool = False


class Live(BaseModel):
    ha_down_s: float = 0  # seconds both WS and REST have been down
    tstat_unavailable: bool = False
    tstat_age_min: float | None = 0  # None = never read
    weather_stale: bool = False  # both forecasts stale
    override_open: bool = False
    ha_mirror: Literal["on", "off"] | None = "on"
    setup_needed: bool = False  # v0.2.0: no thermostat configured -> DISABLED, no writes
    sensors_missing: bool = False  # v0.6.1: no HVAC sensors configured -> DEGRADED(sensors)


class EffectiveState(BaseModel):
    state: State
    base: Literal["DISABLED", "SHADOW", "ACTIVE"]
    degraded_reason: Literal["ha", "weather", "sensors"] | None = None
    label: str
    plan_writes: bool  # plan writes may go out (subject to guards)
    restore_writes: bool  # safety restore may go out (subject to guards)


class Transition(BaseModel):
    t: datetime
    frm: State
    to: State
    note: str = ""


def _ha_degraded(live: Live) -> bool:
    return (
        live.ha_down_s > HA_DOWN_S
        or live.tstat_unavailable
        or live.tstat_age_min is None
        or live.tstat_age_min > TSTAT_MAX_AGE_MIN
    )


def effective(persisted: Persisted | None, live: Live) -> EffectiveState:
    """Precedence SETUP NEEDED (DISABLED) > DISABLED > DEGRADED > OVERRIDE > SHADOW/ACTIVE."""
    if live.setup_needed:
        return EffectiveState(
            state="DISABLED",
            base="DISABLED",
            label="SETUP NEEDED",
            plan_writes=False,
            restore_writes=False,
        )
    if persisted is None or persisted.kill_switch != "on" or live.ha_mirror != "on":
        return EffectiveState(
            state="DISABLED",
            base="DISABLED",
            label="DISABLED",
            plan_writes=False,
            restore_writes=False,
        )
    base: Literal["SHADOW", "ACTIVE"] = "ACTIVE" if persisted.run_mode == "active" else "SHADOW"
    active = base == "ACTIVE"
    if _ha_degraded(live):
        return EffectiveState(
            state="DEGRADED",
            base=base,
            degraded_reason="ha",
            label=f"{base} · DEGRADED",
            plan_writes=False,
            restore_writes=active
            and not live.tstat_unavailable
            and live.tstat_age_min is not None
            and live.tstat_age_min <= TSTAT_MAX_AGE_MIN,
        )
    if live.sensors_missing:
        # v0.6.1: no blower/compressor/outdoor sensor configured: no plan writes; a safety
        # restore may still go out in ACTIVE (the thermostat itself is fresh).
        return EffectiveState(
            state="DEGRADED",
            base=base,
            degraded_reason="sensors",
            label=f"{base} \u00b7 DEGRADED",
            plan_writes=False,
            restore_writes=active,
        )
    if live.override_open:
        return EffectiveState(
            state="OVERRIDE",
            base=base,
            label=f"{base} · OVERRIDE",
            plan_writes=False,
            restore_writes=active,
        )
    if live.weather_stale:
        # Forecast-only outage: fallback plan writes allowed (FR-WX-2).
        return EffectiveState(
            state="DEGRADED",
            base=base,
            degraded_reason="weather",
            label=f"{base} · DEGRADED",
            plan_writes=active,
            restore_writes=active,
        )
    return EffectiveState(
        state=base, base=base, label=base, plan_writes=active, restore_writes=active
    )


def step(
    persisted: Persisted | None, live: Live, now: datetime, prev: State | None = None
) -> tuple[EffectiveState, list[Transition]]:
    eff = effective(persisted, live)
    tr: list[Transition] = []
    if prev is not None and prev != eff.state:
        tr.append(Transition(t=now, frm=prev, to=eff.state, note=eff.degraded_reason or ""))
    return eff, tr


# --- Owner commands (FR-KILL-4, FR-MODE-3) ------------------------------------------------


def shadow_days(p: Persisted, now: datetime) -> float:
    if p.shadow_started_at is None:
        return 0.0
    return (now - p.shadow_started_at).total_seconds() / 86400


def turn_off(p: Persisted | None, now: datetime) -> Persisted:
    """Owner/Admin OFF from any state. A missing row becomes a fresh disabled row."""
    if p is None:
        return Persisted(kill_switch="off", run_mode="shadow", disabled_since=now)
    if p.kill_switch == "off":
        return p
    return p.model_copy(update={"kill_switch": "off", "disabled_since": now})


def turn_on(p: Persisted | None, mode: Literal["shadow", "active"], now: datetime) -> Persisted:
    """ON always names SHADOW or ACTIVE. ACTIVE requires >= SHADOW_MIN_DAYS d of SHADOW (raises).
    The SHADOW clock restarts only after > 24 h DISABLED (or no clock yet)."""
    started = p.shadow_started_at if p else None
    long_off = (
        p is None
        or p.disabled_since is None
        or now - p.disabled_since > timedelta(hours=SHADOW_RESET_AFTER_H)
    )
    if p is not None and p.kill_switch == "on":
        long_off = False
    if started is None or (p is not None and p.kill_switch == "off" and long_off):
        started = now
    if mode == "active":
        days = 0.0 if started is None else (now - started).total_seconds() / 86400
        if days < SHADOW_MIN_DAYS:
            raise ValueError(f"ACTIVE needs {SHADOW_MIN_DAYS} d of SHADOW ({days:.1f} d so far)")
    return Persisted(
        kill_switch="on",
        run_mode=mode,
        shadow_started_at=started,
        disabled_since=None,
        preset_cleared=p.preset_cleared if p else False,
    )


def to_shadow(p: Persisted) -> Persisted:
    """Owner 'back to SHADOW' from ACTIVE: the SHADOW clock keeps running."""
    return p.model_copy(update={"run_mode": "shadow"})


# --- External change (FR-OVR-3) -------------------------------------------------------------


def classify_change(
    old: Mapping[str, object],
    new: Mapping[str, object],
    context_id: str,
    context_user: str | None,
    recent_write_ids: Collection[str],
    watchdog_user: str | None = None,
    *,
    watchdog_last: datetime | None = None,
    now: datetime | None = None,
) -> Literal["none", "bot", "watchdog", "external"]:
    """`state_changed` on the thermostat: ignore unwatched-attr changes; bot writes are those
    whose context id is in the last-10-min write set. A change within 2 min after
    `input_datetime.thermabot_watchdog_last` changed is 'watchdog' (D§13); the HA context
    user id is an optional extra. Unknown watchdog_last -> 'external' (safe: bot pauses)."""
    if all(old.get(a) == new.get(a) for a in WATCHED_ATTRS):
        return "none"
    if context_id in recent_write_ids:
        return "bot"
    if (
        watchdog_last is not None
        and now is not None
        and timedelta(0) <= now - watchdog_last < timedelta(minutes=WATCHDOG_WINDOW_MIN)
    ):
        return "watchdog"
    if watchdog_user is not None and context_user == watchdog_user:
        return "watchdog"
    return "external"


# --- Startup reconcile (NFR-SAFE-9) ---------------------------------------------------------


Reconcile = Literal["continue", "safety_restore", "external_override", "clear_preset"]


def reconcile(
    p: Persisted | None,
    hvac_mode: str,
    preset_mode: str | None,
    off_window_open: bool,
    floor_or_ceiling: bool,
) -> list[Reconcile]:
    """Boot-time actions. Floor/ceiling wins over everything; `off` without a covering
    off window becomes an external override; the first ACTIVE write clears a preset."""
    out: list[Reconcile] = []
    if floor_or_ceiling:
        out.append("safety_restore")
    elif hvac_mode == "off" and not off_window_open:
        out.append("external_override")
    if (
        p is not None
        and p.kill_switch == "on"
        and preset_mode not in (None, "none", "None")
        and not p.preset_cleared
    ):
        out.append("clear_preset")
    return out or ["continue"]
