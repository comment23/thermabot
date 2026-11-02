"""Write guard chain (D\x156, NFR-SAFE-5). Pure: every guard is a function of (action, ctx).

Order: readable (pre) -> kill_switch -> override -> allowlist -> hard_limits (clamp) ->
off_guardrails -> rate_limit. The first deny stops the chain.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from thermabot.config import Settings
from thermabot.core.limits import (  # re-exported: guards is the documented home (D\x1514.2)
    COOL_SP_MAX,
    COOL_SP_MIN,
    HEAT_SP_MIN,
    INDOOR_MAX,
    OFF_ABS_CAP_H,
    OFF_FORCED_ON_H,
    PRECOND_MAX_BEYOND_BAND,
)
from thermabot.core.types import Action, Decision, Snapshot

__all__ = [
    "COOL_SP_MAX",
    "COOL_SP_MIN",
    "HEAT_SP_MIN",
    "INDOOR_MAX",
    "OFF_ABS_CAP_H",
    "OFF_FORCED_ON_H",
    "PRECOND_MAX_BEYOND_BAND",
    "GuardCtx",
    "WriteRecord",
    "evaluate",
]

READABLE_MAX_AGE_MIN = 10
ALLOWED_SERVICES = ("set_hvac_mode", "set_temperature", "set_fan_mode", "set_preset_mode")
TZ = ZoneInfo("America/New_York")


class WriteRecord(BaseModel):
    """A write actually sent to HA (from the `writes` table, status='sent')."""

    t: datetime
    service: str
    data: dict[str, float | str | None] = Field(default_factory=dict)


class GuardCtx(BaseModel):
    kill_switch: Literal["on", "off"] | None  # None = row missing/corrupt -> deny
    ha_mirror: Literal["on", "off"] | None  # input_boolean.thermabot_enabled
    run_mode: Literal["shadow", "active"]
    override_open: bool = False
    snapshot: Snapshot | None
    recent_writes: list[WriteRecord] = Field(default_factory=list)
    preset_cleared: bool = False
    settings: Settings = Field(default_factory=Settings)
    now: datetime
    device_min_f: float = 50
    device_max_f: float = 90
    # off_guardrails inputs, supplied by the planner/off-window state
    off_score_proven: bool = False  # plan proved house score >= 80 over the window
    off_forecast_f: list[float] = Field(default_factory=list)  # outdoor forecast over window
    off_used_h: float = 0  # cumulative off hours in the current run (NFR-SAFE-2)
    safety_hold_until: datetime | None = None


Verdict = tuple[bool, str]  # (ok, note)
OK: Verdict = (True, "")


def readable(ctx: GuardCtx) -> Verdict:
    """No blind writes, not even a restore (NFR-SAFE-6)."""
    s = ctx.snapshot
    if s is None:
        return False, "no thermostat snapshot"
    if s.thermostat.hvac_mode in ("unavailable", "unknown", ""):
        return False, f"thermostat {s.thermostat.hvac_mode or 'empty'}"
    lr = s.thermostat.last_reported
    if lr is None or ctx.now - lr > timedelta(minutes=READABLE_MAX_AGE_MIN):
        return False, "thermostat reading older than 10 min"
    return OK


def kill_switch(ctx: GuardCtx) -> Verdict:
    """FR-KILL-2: denies safety restore too."""
    if ctx.kill_switch is None:
        return False, "bot_state missing or corrupt"
    if ctx.kill_switch != "on":
        return False, "kill switch off"
    if ctx.ha_mirror != "on":
        return False, f"HA mirror {ctx.ha_mirror or 'missing'} disagrees"
    return OK


def override(action: Action, ctx: GuardCtx) -> Verdict:
    if ctx.override_open and not action.safety_restore:
        return False, "override open"
    return OK


def allowlist(action: Action, ctx: GuardCtx) -> Verdict:
    if not ctx.settings.thermostat.configured:  # v0.2.0: no thermostat = no writes at all
        return False, "thermostat not configured (setup needed)"
    if action.entity_id != ctx.settings.thermostat.entity_id:  # exactly one (FR-CFG-1)
        return False, f"entity {action.entity_id} not writable"
    if action.service not in ALLOWED_SERVICES:
        return False, f"service {action.service} not allowed"
    if action.service == "set_preset_mode":
        val = action.data.get("preset_mode")
        if val not in (None, "none", "None"):
            return False, f"preset {val!r} not allowed (only none)"
        if ctx.preset_cleared:
            return False, "preset already cleared once"
    return OK


def _mode(action: Action) -> str | None:
    """Mode this action sets (set_hvac_mode, or set_temperature carrying hvac_mode)."""
    m = action.data.get("hvac_mode")
    return str(m) if isinstance(m, str) else None


def _num(v: float | str | None) -> float | None:
    return float(v) if isinstance(v, int | float) else None


def hard_limits(action: Action, ctx: GuardCtx) -> tuple[Action | None, str]:
    """Clamp setpoints into hard limits + device range + heat_cool deadband (FR-CMF-5, FR-HA-3).
    Returns (None, note) only when no valid clamp exists."""
    if action.service != "set_temperature":
        return action, ""
    data = dict(action.data)
    notes: list[str] = []
    dev_lo, dev_hi = ctx.device_min_f, ctx.device_max_f
    mode = _mode(action) or (ctx.snapshot.thermostat.hvac_mode if ctx.snapshot else None)

    def clamp(key: str, lo: float, hi: float) -> None:
        v = _num(data.get(key))
        if v is None:
            return
        c = min(max(v, lo, dev_lo), hi, dev_hi)
        if c != v:
            notes.append(f"{key} {v:g}->{c:g}")
            data[key] = c

    # Heat setpoint also capped at COOL_SP_MAX: never heat the house above the ceiling.
    clamp("target_temp_low", HEAT_SP_MIN, COOL_SP_MAX)
    clamp("target_temp_high", COOL_SP_MIN, COOL_SP_MAX)
    if mode == "cool":
        clamp("temperature", COOL_SP_MIN, COOL_SP_MAX)
    else:
        clamp("temperature", HEAT_SP_MIN, COOL_SP_MAX)
    lo, hi = _num(data.get("target_temp_low")), _num(data.get("target_temp_high"))
    if (lo is None) != (hi is None):
        return None, "heat_cool write needs both target_temp_low and target_temp_high"
    if lo is not None and hi is not None:
        db = ctx.settings.writes.deadband_f
        if hi - lo < db:
            if lo + db <= min(COOL_SP_MAX, dev_hi):
                hi = lo + db
            elif hi - db >= max(HEAT_SP_MIN, dev_lo):
                lo = hi - db
            else:
                return None, "no valid deadband clamp"
            notes.append(f"deadband -> {lo:g}/{hi:g}")
            data["target_temp_low"], data["target_temp_high"] = lo, hi
    if all(k not in data for k in ("temperature", "target_temp_low")) and "hvac_mode" not in data:
        return None, "set_temperature without a setpoint"
    return action.model_copy(update={"data": data}), "; ".join(notes)


def floor_or_ceiling_crossed(ctx: GuardCtx) -> bool:
    """Thermostat <= floor / >= ceiling, or an occupied fresh room <= 60 / >= 82 (NFR-SAFE-3)."""
    snap = ctx.snapshot
    if snap is None:
        return True  # unknown counts as crossed (conservative)
    sc = ctx.settings.safety
    t = snap.thermostat.current_temp
    if t is None or t <= sc.floor_tstat_f or t >= sc.ceil_tstat_f:
        return True
    occupied = snap.presence is None or snap.presence.house == "OCCUPIED"
    if occupied:
        for r in snap.rooms.values():
            if (
                r.temp is not None
                and not r.stale
                and (r.temp <= sc.floor_room_f or r.temp >= sc.ceil_room_f)
            ):
                return True
    return False


def off_guardrails(action: Action, ctx: GuardCtx) -> Verdict:
    """NFR-SAFE-1/2: `off` only with proven score, outdoor 50-78 over the window,
    cap not used, floor/ceiling not crossed, no post-restore hold."""
    if _mode(action) != "off":
        return OK
    oc = ctx.settings.off
    if not ctx.off_score_proven:
        return False, "off: plan did not prove score >= 80 for the window"
    if not ctx.off_forecast_f:
        return False, "off: no outdoor forecast for the window"
    if min(ctx.off_forecast_f) < oc.outdoor_min_f or max(ctx.off_forecast_f) > oc.outdoor_max_f:
        return False, f"off: forecast outside {oc.outdoor_min_f:g}-{oc.outdoor_max_f:g}°F"
    if ctx.off_used_h >= OFF_ABS_CAP_H:
        return False, "off: 24 h cap used"
    if floor_or_ceiling_crossed(ctx):
        return False, "off: floor/ceiling already crossed"
    if ctx.safety_hold_until is not None and ctx.now < ctx.safety_hold_until:
        return False, "off: post-restore safety hold"
    return OK


def _is_mode_write(w: WriteRecord | Action) -> bool:
    return w.service == "set_hvac_mode" or "hvac_mode" in w.data


def _is_sp_write(w: WriteRecord | Action) -> bool:
    return w.service == "set_temperature"


def heat_cool_flips(modes: Sequence[str]) -> int:
    """Count heat<->cool reversals in a sequence of written modes (off/heat_cool ignored)."""
    seq = [m for m in modes if m in ("heat", "cool")]
    return sum(1 for a, b in zip(seq, seq[1:], strict=False) if a != b)


def _no_change(action: Action, ctx: GuardCtx) -> bool:
    if ctx.snapshot is None:
        return False
    th = ctx.snapshot.thermostat
    cur: dict[str, float | str | None] = {
        "hvac_mode": th.hvac_mode,
        "temperature": th.heat_sp if th.hvac_mode == "heat" else th.cool_sp,
        "target_temp_low": th.heat_sp,
        "target_temp_high": th.cool_sp,
        "fan_mode": th.fan_mode,
        "preset_mode": th.preset_mode or "none",
    }
    for k, v in action.data.items():
        want = "none" if (k == "preset_mode" and v in (None, "None")) else v
        have = cur.get(k)
        if isinstance(want, int | float) and isinstance(have, int | float):
            if abs(float(want) - float(have)) > 0.01:
                return False
        elif str(want).lower() != str(have).lower():
            return False
    return True


def rate_limit(action: Action, ctx: GuardCtx) -> Verdict:
    """NFR-SAFE-10, FR-HA-3/5. Bypassed by safety restore (NFR-SAFE-3)."""
    if action.safety_restore:
        return OK
    wc = ctx.settings.writes
    if _no_change(action, ctx):
        return False, "no change vs read-back state"
    today = ctx.now.astimezone(TZ).date()
    todays = [w for w in ctx.recent_writes if w.t.astimezone(TZ).date() == today]
    if len(todays) >= wc.max_per_day:
        return False, f"{wc.max_per_day} writes today"
    if _is_sp_write(action):
        last = max((w.t for w in ctx.recent_writes if _is_sp_write(w)), default=None)
        if last is not None and ctx.now - last < timedelta(minutes=wc.min_setpoint_gap_min):
            return False, f"< {wc.min_setpoint_gap_min:g} min since last setpoint write"
    new_mode = _mode(action)
    cur_mode = ctx.snapshot.thermostat.hvac_mode if ctx.snapshot else None
    if new_mode is not None and new_mode != cur_mode:
        last = max((w.t for w in ctx.recent_writes if _is_mode_write(w)), default=None)
        if last is not None and ctx.now - last < timedelta(minutes=wc.min_mode_gap_min):
            return False, f"< {wc.min_mode_gap_min:g} min since last mode change"
        modes = [
            str(w.data["hvac_mode"])
            for w in sorted(todays, key=lambda w: w.t)
            if isinstance(w.data.get("hvac_mode"), str)
        ]
        if heat_cool_flips([*modes, new_mode]) > wc.max_heat_cool_flips:
            return False, f"> {wc.max_heat_cool_flips} heat<->cool flips today"
    return OK


def evaluate(action: Action, ctx: GuardCtx) -> Decision:
    """Run the chain. SHADOW: full chain evaluated, result logged as guard='shadow' with
    would_write = whether it would have been sent."""

    def deny(guard: str, note: str, act: Action = action) -> Decision:
        return Decision(t=ctx.now, action=act, allowed=False, reason=note, guard=guard)

    ok, note = readable(ctx)
    if not ok:
        return deny("readable", note)
    ok, note = kill_switch(ctx)
    if not ok:
        return deny("kill_switch", note)
    for name, fn in (("override", override), ("allowlist", allowlist)):
        ok, note = fn(action, ctx)
        if not ok:
            return deny(name, note)
    clamped, clamp_note = hard_limits(action, ctx)
    if clamped is None:
        return deny("hard_limits", clamp_note)
    for name, fn in (("off_guardrails", off_guardrails), ("rate_limit", rate_limit)):
        ok, note = fn(clamped, ctx)
        if not ok:
            return deny(name, note, clamped)
    if ctx.run_mode != "active":
        return Decision(
            t=ctx.now,
            action=clamped,
            allowed=False,
            reason="shadow",
            guard="shadow",
            would_write=True,
        )
    return Decision(
        t=ctx.now, action=clamped, allowed=True, reason=clamp_note or "ok", would_write=True
    )
