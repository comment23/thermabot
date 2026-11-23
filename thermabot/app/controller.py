"""Controller: the SINGLE write path (D§1.3, D§6, NFR-SAFE-5). Only this module may import
`_call_service` (T1.29). Every evaluate() result lands in `writes` (sent/shadow/denied/failed).

Safe by default: any adapter error -> outcome 'failed', no further write this plan step.
Planned read-back mismatches retry once; manual overrides never retry automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from math import isfinite
from typing import Any

import httpx
from pydantic import ValidationError

from thermabot.adapters import db
from thermabot.adapters.ha_rest import MIRROR_ENTITY, HAError, HARest, WriteRefused, _call_service
from thermabot.app.units import c_to_f_exact, delta_c_to_f, temperature_scale
from thermabot.config import Settings
from thermabot.core import safety, statemachine
from thermabot.core.fallback import choose_hold_mode
from thermabot.core.guards import GuardCtx, WriteRecord, evaluate, readable
from thermabot.core.overrides import OVERRIDE_COMMAND_STATES, override_command_key
from thermabot.core.schedule import block_at, next_boundary
from thermabot.core.types import (
    Action,
    AlertEvent,
    Decision,
    OverrideIn,
    PlanStep,
    Snapshot,
    ThermostatState,
)

log = logging.getLogger(__name__)
RETRY_AFTER_S = 120.0
READBACK_POLL_S = 5.0
MODE_WAIT_S = 30.0  # set_hvac_mode confirmation before sending setpoints (T1.44)
MODE_POLL_S = 1.0
READBACK_TOL_F = 0.5
OVERRIDE_READBACK_TOL_F = 0.000001
OVERRIDE_PENDING_TIMEOUT = timedelta(minutes=3)
CONTEXT_MATCH_S = 90
RESTORE_FAIL_BLOCK = timedelta(minutes=10)
WATCHDOG_LAST_ENTITY = "input_datetime.thermabot_watchdog_last"


def parse_last_changed(st: dict[str, Any] | None) -> datetime | None:
    """Genuine last state-transition time (HA's `last_changed`). Do NOT use this for
    freshness/staleness checks: `last_changed` only bumps when the *value* changes, so an
    entity that has sat unchanged for hours still looks ancient even though HA is actively
    polling it every cycle. Use `parse_last_reported` for "is this reading still being
    confirmed by HA" questions (v0.6.4, presence staleness bug)."""
    if not st or st.get("state") in (None, "unavailable", "unknown", ""):
        return None
    try:
        t = datetime.fromisoformat(str(st.get("last_changed")))
    except ValueError:
        return None
    return t if t.tzinfo else None


def parse_last_reported(st: dict[str, Any] | None) -> datetime | None:
    """Last time HA actually confirmed/polled this entity's state, regardless of whether the
    *value* changed: HA's `last_reported` (fallback `last_updated` for older HA/integrations
    that don't expose it; identical in value to `last_reported` for climate/device_tracker
    entities on HA 2026.9.4, confirmed live). This is the correct field for staleness checks
    (e.g. presence tracker freshness, v0.6.4) -- `last_changed` answers a different question
    (when did the value last transition) and must not be conflated with "is this still fresh".
    """
    if not st:
        return None
    return parse_last_changed(
        {
            "state": st.get("state"),
            "last_changed": st.get("last_reported") or st.get("last_updated"),
        }
    )


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def thermostat(st: dict[str, Any], read_at: datetime | None = None) -> ThermostatState:
    """HA climate state → internal °F. °C readings and ranges are converted exactly.

    An unknown temperature unit clears every temperature (current, setpoints, range
    and step) and is logged. Safety then has nothing to compare, and the device
    check refuses the write instead of treating Celsius as Fahrenheit.
    """
    a = st.get("attributes", {})
    mode = str(st.get("state"))
    if read_at is not None and mode not in ("unavailable", "unknown", "", "None"):
        seen: datetime | None = read_at
    else:
        seen = parse_last_reported(st)
    single_sp = _num(a.get("temperature"))
    modes = a.get("hvac_modes")
    unit = str(a.get("temperature_unit") or a.get("unit_of_measurement") or "") or None
    scale = temperature_scale(unit)
    current = _num(a.get("current_temperature"))
    heat = (
        single_sp
        if mode.lower() == "heat" and single_sp is not None
        else _num(a.get("target_temp_low"))
    )
    cool = (
        single_sp
        if mode.lower() == "cool" and single_sp is not None
        else _num(a.get("target_temp_high"))
    )
    min_temp = _num(a.get("min_temp"))
    max_temp = _num(a.get("max_temp"))
    step = _num(a.get("target_temp_step"))
    if scale is None:
        log.warning(
            "thermostat temperature unit %r is unknown; temperatures cleared and writes blocked",
            unit,
        )
        current = heat = cool = min_temp = max_temp = step = None
    elif scale == "C":
        current = None if current is None else c_to_f_exact(current)
        heat = None if heat is None else c_to_f_exact(heat)
        cool = None if cool is None else c_to_f_exact(cool)
        min_temp = None if min_temp is None else c_to_f_exact(min_temp)
        max_temp = None if max_temp is None else c_to_f_exact(max_temp)
        step = None if step is None else delta_c_to_f(step)
    return ThermostatState(
        hvac_mode=mode,
        hvac_modes=[v.lower() for v in modes if isinstance(v, str)]
        if isinstance(modes, list)
        else [],
        hvac_action=a.get("hvac_action"),
        current_temp=current,
        heat_sp=heat,
        cool_sp=cool,
        min_temp=min_temp,
        max_temp=max_temp,
        target_temp_step=step,
        temperature_unit=unit,
        fan_mode=a.get("fan_mode"),
        preset_mode=a.get("preset_mode"),
        humidity=_num(a.get("current_humidity")),
        last_reported=seen,
    )


def diff(
    step: PlanStep, snap: Snapshot, entity_id: str, *, include_fan: bool = True
) -> list[Action]:
    """Desired vs actual -> 0-2 actions. Mode + setpoints is ONE logical write (D§6.3);
    the controller sends set_hvac_mode first, then the setpoints (T1.44 Trane quirk)."""
    th = snap.thermostat
    out: list[Action] = []
    data: dict[str, float | str | None] = {}
    if step.hvac_mode == "off":
        if th.hvac_mode.lower() != "off":
            out.append(
                Action(service="set_hvac_mode", entity_id=entity_id, data={"hvac_mode": "off"})
            )
    else:
        if step.hvac_mode in ("heat_cool", "auto"):
            want = {"target_temp_low": step.heat_sp, "target_temp_high": step.cool_sp}
            have = {"target_temp_low": th.heat_sp, "target_temp_high": th.cool_sp}
        else:
            sp = step.heat_sp if step.hvac_mode == "heat" else step.cool_sp
            have_sp = th.heat_sp if step.hvac_mode == "heat" else th.cool_sp
            want, have = {"temperature": sp}, {"temperature": have_sp}
        changed = th.hvac_mode.lower() != step.hvac_mode or any(
            v is not None and (have[k] is None or abs(float(v) - float(have[k] or 0)) > 0.01)
            for k, v in want.items()
        )
        if changed:
            data = {"hvac_mode": step.hvac_mode, **{k: v for k, v in want.items() if v is not None}}
            out.append(Action(service="set_temperature", entity_id=entity_id, data=data))
    if (
        include_fan
        and step.hvac_mode != "off"
        and th.fan_mode is not None
        and step.fan_mode.lower() != th.fan_mode.lower()
    ):
        out.append(
            Action(service="set_fan_mode", entity_id=entity_id, data={"fan_mode": step.fan_mode})
        )
    return out


def device_value(value: str, options: object) -> str:
    """Send the device's own spelling: the Trane lists fan_modes ['Auto', 'On', 'Circulate']
    and preset_modes ['None', ...], and HA validates the value case-sensitively."""
    if isinstance(options, list):
        for o in options:
            if isinstance(o, str) and o.lower() == value.lower():
                return o
    return value


def _capability_error(
    minimum: float | None, maximum: float | None, step: float | None, unit: str | None
) -> str:
    if temperature_scale(unit) is None:
        return "thermostat temperature unit is unknown"
    if step is None or not isfinite(step) or step <= 0:
        return "thermostat setpoint step unavailable"
    if (
        minimum is None
        or maximum is None
        or not isfinite(minimum)
        or not isfinite(maximum)
        or minimum >= maximum
    ):
        return "thermostat setpoint range unavailable"
    return ""


def _supported_setpoints(
    action: Action,
    minimum: float | None,
    maximum: float | None,
    step: float | None,
    unit: str | None,
) -> bool:
    if _capability_error(minimum, maximum, step, unit):
        return False
    assert minimum is not None and maximum is not None and step is not None
    for key in ("temperature", "target_temp_low", "target_temp_high"):
        if key not in action.data:
            continue
        value = action.data[key]
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not isfinite(value)
            or not minimum <= value <= maximum
        ):
            return False
        units = (Decimal(str(value)) - Decimal(str(minimum))) / Decimal(str(step))
        if abs(units - units.to_integral_value(rounding=ROUND_HALF_UP)) > Decimal("0.000001"):
            return False
    return True


_SETPOINT_KEYS = ("temperature", "target_temp_low", "target_temp_high")


def resolve_device_mode(mode: str, modes: list[str]) -> str | None:
    """The device's spelling of `mode`, or auto↔heat_cool when only one is listed."""
    listed = {m.casefold(): m for m in modes}
    found = listed.get(mode.casefold())
    if found is not None:
        return found
    if mode.casefold() == "heat_cool":
        return listed.get("auto")
    if mode.casefold() == "auto":
        return listed.get("heat_cool")
    return None


def align_action(action: Action, th: ThermostatState) -> Action:
    """Snap °C-device setpoints onto the device step. Values stay in °F.

    °F devices are unchanged, including off-grid values the capability check rejects.
    """
    if temperature_scale(th.temperature_unit) != "C":
        return action
    if (
        th.min_temp is None
        or th.max_temp is None
        or th.target_temp_step is None
        or not isfinite(th.target_temp_step)
        or th.target_temp_step <= 0
    ):
        return action
    min_c = (Decimal(str(th.min_temp)) - Decimal(32)) * Decimal(5) / Decimal(9)
    max_c = (Decimal(str(th.max_temp)) - Decimal(32)) * Decimal(5) / Decimal(9)
    step_c = Decimal(str(th.target_temp_step)) * Decimal(5) / Decimal(9)
    if step_c <= 0:
        return action
    data = dict(action.data)
    changed = False
    for key in _SETPOINT_KEYS:
        raw = data.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            continue
        native = (Decimal(str(float(raw))) - Decimal(32)) * Decimal(5) / Decimal(9)
        units = ((native - min_c) / step_c).to_integral_value(rounding=ROUND_HALF_UP)
        quantized = min(max(min_c + units * step_c, min_c), max_c)
        data[key] = float(quantized * Decimal(9) / Decimal(5) + Decimal(32))
        changed = True
    return action.model_copy(update={"data": data}) if changed else action


def apply_device_mode(action: Action, modes: list[str]) -> Action:
    mode = action.data.get("hvac_mode")
    if not isinstance(mode, str):
        return action
    resolved = resolve_device_mode(mode, modes)
    if resolved is None or resolved == mode:
        return action
    return action.model_copy(update={"data": {**action.data, "hvac_mode": resolved}})


def device_block_reason(action: Action, th: ThermostatState) -> str:
    """Empty when this write matches a known unit, a supported mode and the device grid."""
    if temperature_scale(th.temperature_unit) is None:
        return "thermostat temperature unit is unknown"
    mode = action.data.get("hvac_mode")
    if isinstance(mode, str) and resolve_device_mode(mode, th.hvac_modes) is None:
        return "thermostat does not report support for the requested HVAC mode"
    if action.service == "set_temperature" or any(k in action.data for k in _SETPOINT_KEYS):
        err = _capability_error(th.min_temp, th.max_temp, th.target_temp_step, th.temperature_unit)
        if err:
            return err
        if not _supported_setpoints(
            action, th.min_temp, th.max_temp, th.target_temp_step, th.temperature_unit
        ):
            return "setpoints incompatible with thermostat capabilities"
    return ""


def action_for_device(action: Action, unit: str | None) -> Action:
    """Setpoints in the device's unit. Internal actions stay in °F."""
    if temperature_scale(unit) != "C":
        return action
    data = dict(action.data)
    for key in _SETPOINT_KEYS:
        raw = data.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            continue
        data[key] = float((Decimal(str(float(raw))) - Decimal(32)) * Decimal(5) / Decimal(9))
    return action.model_copy(update={"data": data})


def _setpoint_detail(data: Mapping[str, float | str | None]) -> str:
    low, high, target = (
        data.get("target_temp_low"),
        data.get("target_temp_high"),
        data.get("temperature"),
    )
    if isinstance(low, int | float) and isinstance(high, int | float):
        return f"heat {low:g}F, cool {high:g}F"
    if isinstance(target, int | float):
        return f"target {target:g}F"
    return ""


def _pending_fresh(started_at: str, now: datetime) -> bool:
    try:
        started = datetime.fromisoformat(started_at)
        return started.utcoffset() is not None and timedelta(0) <= now - started <= (
            OVERRIDE_PENDING_TIMEOUT
        )
    except (TypeError, ValueError):
        return False


def readback_ok(
    action: Action, st: dict[str, Any] | None, tolerance: float = READBACK_TOL_F
) -> bool:
    if st is None:
        return False
    a = st.get("attributes", {})
    raw_unit = a.get("temperature_unit") or a.get("unit_of_measurement")
    scale = temperature_scale(raw_unit if isinstance(raw_unit, str) else None)
    for k, v in action.data.items():
        have = st.get("state") if k == "hvac_mode" else a.get(k)
        if k == "preset_mode":
            v, have = (v or "none"), (have or "none")
        if isinstance(v, int | float) and not isinstance(v, bool):
            if (
                not isinstance(have, int | float)
                or isinstance(have, bool)
                or not isfinite(have)
                or not isfinite(v)
            ):
                return False
            if k in _SETPOINT_KEYS:
                if scale == "C":
                    have = float(Decimal(str(have)) * Decimal(9) / Decimal(5) + Decimal(32))
                elif scale != "F":
                    return False
            if abs(float(have) - float(v)) > tolerance:
                return False
        elif str(have).lower() != str(v).lower():
            return False
    return True


class Controller:
    """One instance per process. `tick()` runs D§1.3 steps 2-7 for one snapshot."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        ha: HARest,
        settings: Settings | None = None,
        bot_user_id: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.conn, self.ha = conn, ha
        self.settings = settings or Settings()
        self.bot_user_id = bot_user_id
        self._sleep = sleep
        self._clock = clock
        self.alerts: list[AlertEvent] = []
        self.recent_write_ids: dict[str, datetime] = {}  # HA context ids of our writes
        self._last_send: datetime | None = None
        self._failed_step: datetime | None = None  # plan step t that hit write_failed
        self.hold_until: datetime | None = None
        self.watchdog_last: datetime | None = None  # last_changed of WATCHDOG_LAST_ENTITY

    @property
    def entity(self) -> str:
        """The one writable climate entity (settings.thermostat.entity_id, FR-CFG-1)."""
        return self.settings.thermostat.entity_id

    def on_watchdog_last(self, st: dict[str, Any] | None) -> None:
        """Feed input_datetime.thermabot_watchdog_last (read-only). Unknown -> None."""
        self.watchdog_last = parse_last_changed(st)

    # ---- DB helpers -----------------------------------------------------------------
    def _record(
        self,
        d: Decision,
        outcome: str,
        now: datetime,
        context_id: str | None = None,
        readback: bool | None = None,
        ms: int | None = None,
    ) -> None:
        a = d.action
        self.conn.execute(
            "INSERT INTO writes (ts, service, entity_id, data_json, reason, outcome, guard,"
            " is_safety_restore, context_id, readback_ok, readback_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                _iso(now),
                a.service,
                a.entity_id,
                json.dumps(a.data),
                d.reason,
                outcome,
                d.guard,
                int(a.safety_restore),
                context_id,
                None if readback is None else int(readback),
                ms,
            ),
        )

    def recent_writes(self, now: datetime) -> list[WriteRecord]:
        rows = self.conn.execute(
            "SELECT ts, service, data_json FROM writes WHERE outcome='sent' AND ts >= ?",
            (_iso(now - timedelta(hours=48)),),
        ).fetchall()
        return [
            WriteRecord(
                t=datetime.fromisoformat(r["ts"]),
                service=r["service"],
                data=json.loads(r["data_json"]),
            )
            for r in rows
        ]

    def override_open(self, now: datetime) -> bool:
        r = self.conn.execute(
            "SELECT 1 FROM overrides WHERE ended_at IS NULL AND expires_at > ?", (_iso(now),)
        ).fetchone()
        return r is not None

    def _override_actions(
        self, snap: Snapshot, now: datetime
    ) -> tuple[int, list[Action] | None] | None:
        row = self.conn.execute(
            "SELECT id, kind, payload_json, started_at FROM overrides"
            " WHERE ended_at IS NULL AND expires_at > ?"
            " ORDER BY id DESC LIMIT 1",
            (_iso(now),),
        ).fetchone()
        if row is None or row["kind"] not in ("ui_target", "ui_mode", "ui_pause"):
            return None
        override_id = int(row["id"])
        if row["kind"] == "ui_pause":
            return override_id, None
        raw = db.read_kv(self.conn, override_command_key(override_id))
        status = (raw or "").partition(":")[0]
        if status != "pending":
            if raw is not None and status not in OVERRIDE_COMMAND_STATES:
                log.error("invalid override command state for id %s", override_id)
            return override_id, None
        if not _pending_fresh(row["started_at"], self._clock()):
            self._set_override_command(
                override_id, "blocked", now, "request timed out", expected="pending"
            )
            return override_id, None
        try:
            override = OverrideIn.model_validate_json(row["payload_json"])
        except ValidationError:
            log.error("invalid override payload for id %s", override_id)
            self._set_override_command(
                override_id, "blocked", now, "invalid override request", expected="pending"
            )
            return override_id, None
        if override.pause or (override.target_f is None and override.mode is None):
            log.error("invalid override payload for id %s", override_id)
            self._set_override_command(
                override_id, "blocked", now, "invalid override request", expected="pending"
            )
            return override_id, None

        mode = override.mode or snap.thermostat.hvac_mode
        if mode not in ("off", "heat", "cool", "heat_cool", "auto"):
            self._set_override_command(
                override_id, "blocked", now, "thermostat mode unavailable", expected="pending"
            )
            return override_id, None
        if override.target_f is not None and mode == "off":
            self._set_override_command(
                override_id, "blocked", now, "thermostat is off", expected="pending"
            )
            return override_id, None
        heat_sp = cool_sp = None
        target = override.target_f
        if target is not None:
            th = snap.thermostat
            error = _capability_error(
                th.min_temp, th.max_temp, th.target_temp_step, th.temperature_unit
            )
            if error:
                self._set_override_command(override_id, "blocked", now, error, expected="pending")
                return override_id, None
            assert th.min_temp is not None and th.target_temp_step is not None
            base, increment = Decimal(str(th.min_temp)), Decimal(str(th.target_temp_step))
            desired = Decimal(str(target))

            def rounded(value: Decimal, rounding: str) -> float:
                units = ((value - base) / increment).to_integral_value(rounding=rounding)
                return float(base + units * increment)

            if mode in ("heat_cool", "auto"):
                half_deadband = Decimal(str(self.settings.writes.deadband_f)) / 2
                heat_sp = rounded(desired - half_deadband, ROUND_FLOOR)
                cool_sp = rounded(desired + half_deadband, ROUND_CEILING)
            elif mode == "heat":
                heat_sp = rounded(desired, ROUND_HALF_UP)
            elif mode == "cool":
                cool_sp = rounded(desired, ROUND_HALF_UP)
        step = PlanStep(
            t=now,
            hvac_mode=mode,
            heat_sp=heat_sp,
            cool_sp=cool_sp,
        )
        return override_id, diff(step, snap, self.entity, include_fan=False)

    def _set_override_command(
        self, override_id: int, state: str, now: datetime, detail: str = "", *, expected: str
    ) -> bool:
        return db.compare_and_set_kv(
            self.conn,
            override_command_key(override_id),
            expected,
            f"{state}:{detail}" if detail else state,
            now=now,
        )

    # ---- external change (FR-OVR-3) -------------------------------------------------
    def on_thermostat_change(
        self,
        old: dict[str, Any],
        new: dict[str, Any],
        now: datetime,
        watchdog_user: str | None = None,
    ) -> str:
        """Feed every WS state_changed for the thermostat. Opens an 'external' override
        when a person (not the bot, not the watchdog) changed a watched attribute.
        A change within 2 min of watchdog_last is 'watchdog': alert + 60 min no-off hold."""
        ctx = new.get("context") or {}
        cid, user = str(ctx.get("id", "")), ctx.get("user_id")
        if (
            self.bot_user_id
            and user == self.bot_user_id
            and self._last_send is not None
            and now - self._last_send <= timedelta(seconds=CONTEXT_MATCH_S)
        ):
            self.recent_write_ids[cid] = now
        cut = now - timedelta(minutes=statemachine.RECENT_WRITE_MIN)
        self.recent_write_ids = {k: t for k, t in self.recent_write_ids.items() if t >= cut}

        def flat(s: dict[str, Any]) -> dict[str, object]:
            return {"hvac_mode": s.get("state"), **(s.get("attributes") or {})}

        kind = statemachine.classify_change(
            flat(old),
            flat(new),
            cid,
            user,
            self.recent_write_ids,
            watchdog_user,
            watchdog_last=self.watchdog_last,
            now=now,
        )
        if kind == "watchdog":
            self.hold_until = now + timedelta(minutes=statemachine.WATCHDOG_HOLD_MIN)
            self.alerts.append(
                AlertEvent(
                    key="watchdog_fired",
                    severity="critical",
                    t=now,
                    text="HA watchdog restored heat_cool",
                )
            )
        if kind == "external" and not self.override_open(now):
            until = next_boundary(self.settings.schedule, now)
            self.conn.execute(
                "INSERT INTO overrides (kind, source, payload_json, started_at, expires_at)"
                " VALUES ('external','wall/app',?,?,?)",
                (json.dumps(flat(new), default=str), _iso(now), _iso(until)),
            )
            self.alerts.append(
                AlertEvent(
                    key="override",
                    severity="info",
                    t=now,
                    text="External thermostat change; bot paused",
                )
            )
        return kind

    # ---- one tick -------------------------------------------------------------------
    async def tick(
        self,
        snap: Snapshot | None,
        step: PlanStep | None,
        live: statemachine.Live,
        now: datetime,
        guard_extra: dict[str, Any] | None = None,
        prev: statemachine.State | None = None,
    ) -> list[Decision]:
        """D§1.3 steps 2-7. `guard_extra` carries planner off-window inputs
        (off_score_proven, off_forecast_f, off_used_h)."""
        persisted = db.read_bot_state(self.conn)  # re-read every tick (FR-KILL-1)
        if snap is not None and snap.watchdog_last is not None:
            self.watchdog_last = snap.watchdog_last
        live = live.model_copy(update={"override_open": self.override_open(now)})
        eff, _ = statemachine.step(persisted, live, now, prev)
        actions: list[Action] = []
        override_id: int | None = None
        override_pending = False
        if snap is not None and self.entity:  # v0.2.0: not configured -> no actions at all
            restore = safety.check(snap, self.settings, block_at(self.settings.schedule, now), now)
            if restore is not None:
                actions = [restore.action]
                self.hold_until = restore.hold_until
            else:
                override_actions = self._override_actions(snap, now)
                if override_actions is not None:
                    override_id, pending_actions = override_actions
                    if pending_actions is not None:
                        actions, override_pending = pending_actions, True
                elif step is not None and step.t != self._failed_step:
                    actions = diff(step, snap, self.entity)
        manual_override = override_id is not None
        if override_id is not None and override_pending and not actions:
            empty_ctx = GuardCtx(
                kill_switch=None if persisted is None else persisted.kill_switch,
                ha_mirror=live.ha_mirror,
                run_mode="shadow" if persisted is None else persisted.run_mode,
                snapshot=snap,
                settings=self.settings,
                now=now,
            )
            ok, reason = readable(empty_ctx)
            if eff.state != "OVERRIDE" or eff.base != "ACTIVE":
                self._set_override_command(
                    override_id,
                    "blocked",
                    now,
                    f"state {eff.label} blocks writes",
                    expected="pending",
                )
            elif not ok:
                self._set_override_command(override_id, "blocked", now, reason, expected="pending")
            else:
                data: dict[str, float | str | None] = {}
                if snap is not None:
                    th = snap.thermostat
                    if th.hvac_mode in ("heat_cool", "auto"):
                        data = {"target_temp_low": th.heat_sp, "target_temp_high": th.cool_sp}
                    elif th.hvac_mode in ("heat", "cool"):
                        data = {"temperature": th.heat_sp if th.hvac_mode == "heat" else th.cool_sp}
                self._set_override_command(
                    override_id, "matched", now, _setpoint_detail(data), expected="pending"
                )
        out: list[Decision] = []
        for act in actions:
            if snap is not None and act.service in ("set_hvac_mode", "set_temperature"):
                act = apply_device_mode(
                    align_action(act, snap.thermostat), snap.thermostat.hvac_modes
                )
            dev_min, dev_max = 50.0, 90.0
            if snap is not None and snap.thermostat.min_temp is not None:
                dev_min = snap.thermostat.min_temp
            if snap is not None and snap.thermostat.max_temp is not None:
                dev_max = snap.thermostat.max_temp
            ctx = GuardCtx(
                kill_switch=None if persisted is None else persisted.kill_switch,
                ha_mirror=live.ha_mirror,
                run_mode="shadow" if persisted is None else persisted.run_mode,
                override_open=live.override_open
                and not (manual_override and eff.state == "OVERRIDE"),
                snapshot=snap,
                recent_writes=self.recent_writes(now),
                preset_cleared=bool(persisted and persisted.preset_cleared),
                settings=self.settings,
                now=now,
                safety_hold_until=self.hold_until,
                device_min_f=dev_min,
                device_max_f=dev_max,
                **(guard_extra or {}),
            )
            d = evaluate(act, ctx)
            allowed_by_state = (
                eff.restore_writes
                if act.safety_restore
                else eff.plan_writes or (manual_override and eff.state == "OVERRIDE")
            )
            if d.allowed and not allowed_by_state:
                d = d.model_copy(
                    update={
                        "allowed": False,
                        "guard": "state",
                        "reason": f"state {eff.label} blocks writes",
                    }
                )
            if (
                d.allowed
                and snap is not None
                and d.action.service in ("set_hvac_mode", "set_temperature")
            ):
                # Every planned write, manual override and safety restore. Seasonal
                # control being off does not skip the device's mode, range or unit.
                reason = device_block_reason(d.action, snap.thermostat)
                if reason:
                    d = d.model_copy(
                        update={
                            "allowed": False,
                            "would_write": False,
                            "guard": "device",
                            "reason": reason,
                        }
                    )
            out.append(d)
            if d.guard == "shadow":
                self._record(d, "shadow", now)
                if override_id is not None:
                    self._set_override_command(
                        override_id, "blocked", now, "shadow mode", expected="pending"
                    )
            elif not d.allowed:
                self._record(d, "denied", now)
                if override_id is not None:
                    self._set_override_command(
                        override_id, "blocked", now, d.reason, expected="pending"
                    )
            else:
                if override_id is not None:
                    still_open = self.conn.execute(
                        "SELECT 1 FROM overrides WHERE id=? AND ended_at IS NULL AND expires_at>?",
                        (override_id, _iso(now)),
                    ).fetchone()
                    if still_open is None or not self._set_override_command(
                        override_id, "sending", now, expected="pending"
                    ):
                        denied = d.model_copy(
                            update={
                                "allowed": False,
                                "would_write": False,
                                "guard": "override",
                                "reason": "override ended or already claimed",
                            }
                        )
                        out[-1] = denied
                        self._record(denied, "denied", now)
                        break
                step_t = None if manual_override or step is None else step.t
                ok = await self.execute(
                    d, now, step_t=step_t, retry=not manual_override, override_id=override_id
                )
                if override_id is not None:
                    detail = d.reason if ok and d.reason != "ok" else ""
                    if ok:
                        setpoints = _setpoint_detail(d.action.data)
                        detail = "; ".join(part for part in (setpoints, detail) if part)
                    self._set_override_command(
                        override_id,
                        "applied" if ok else "failed",
                        now,
                        detail,
                        expected="sending",
                    )
                if not ok:
                    break  # never chain more writes after a failure
        return out

    # ---- execution + read-back (D§6.2) ---------------------------------------------
    async def _readback(
        self, action: Action, tolerance: float = READBACK_TOL_F
    ) -> tuple[bool, int]:
        timeout = self.settings.writes.readback_timeout_s
        waited = 0.0
        while True:
            try:
                st = await self.ha.get_state(action.entity_id)
            except (HAError, httpx.HTTPError):
                st = None
            if readback_ok(action, st, tolerance):
                return True, int(waited * 1000)
            if waited >= timeout:
                return False, int(waited * 1000)
            await self._sleep(READBACK_POLL_S)
            waited += READBACK_POLL_S

    async def execute(
        self,
        d: Decision,
        now: datetime,
        step_t: datetime | None = None,
        *,
        retry: bool = True,
        override_id: int | None = None,
    ) -> bool:
        """Send and read back; planned writes can retry once after 2 min."""
        act = d.action
        if not self.entity or act.entity_id != self.entity:  # belt and braces; adapter too
            self._record(d, "denied", now)
            return False
        for attempt in (1, 2) if retry else (1,):
            if attempt == 2:
                await self._sleep(RETRY_AFTER_S)
            try:
                sent = await self._send(act, override_id=override_id)
            except (HAError, WriteRefused, httpx.HTTPError) as e:
                log.error("write %s failed: %s", act.service, e)
                return self._fail(d, now, step_t, f"adapter error: {type(e).__name__}")
            self._last_send = now
            strict = override_id is not None or (
                self.settings.seasonal_control.enabled and not act.safety_restore
            )
            tolerance = OVERRIDE_READBACK_TOL_F if strict else READBACK_TOL_F
            ok, ms = await self._readback(sent, tolerance)
            if ok:
                self._record(
                    d.model_copy(update={"action": sent}), "sent", now, readback=True, ms=ms
                )
                if act.service == "set_preset_mode":
                    self._mark_preset_cleared(now)
                return True
            log.warning("read-back mismatch for %s (attempt %d)", act.service, attempt)
        return self._fail(
            d, now, step_t, "read-back mismatch after retry" if retry else "read-back mismatch"
        )

    async def _check_write(
        self,
        action: Action,
        override_id: int | None,
        *,
        check_state: bool = True,
    ) -> None:
        if not check_state:
            return
        mirror = await self.ha.get_state(MIRROR_ENTITY)
        persisted = db.read_bot_state(self.conn)
        now = self._clock()
        if (
            mirror is None
            or mirror.get("state") != "on"
            or persisted is None
            or persisted.kill_switch != "on"
            or persisted.run_mode != "active"
        ):
            raise WriteRefused("write no longer authorized")
        if override_id is None:
            if not action.safety_restore and self.override_open(now):
                raise WriteRefused("override blocks planned write")
            return
        active = self.conn.execute(
            "SELECT started_at FROM overrides WHERE id=? AND ended_at IS NULL AND expires_at>?",
            (override_id, _iso(now)),
        ).fetchone()
        if (
            active is None
            or not _pending_fresh(active["started_at"], now)
            or db.read_kv(self.conn, override_command_key(override_id)) != "sending"
        ):
            raise WriteRefused("override no longer authorized")

    async def _require_capabilities(
        self, action: Action, expected_mode: str | None = None
    ) -> tuple[Action, str | None]:
        """Live mode, unit and setpoint check for every climate write, including restore.

        Returns the action in °F (aligned to a °C step when needed) and the device unit.
        """
        st = await self.ha.get_state(action.entity_id)
        if st is None:
            raise WriteRefused("thermostat unavailable before mode or setpoint write")
        th = thermostat(st)
        if th.hvac_mode in ("", "unknown", "unavailable"):
            raise WriteRefused("thermostat unavailable before mode or setpoint write")
        if expected_mode is not None and th.hvac_mode.lower() != expected_mode.lower():
            raise WriteRefused("thermostat mode or setpoint capabilities changed")
        if action.service in ("set_hvac_mode", "set_temperature"):
            action = apply_device_mode(align_action(action, th), th.hvac_modes)
            reason = device_block_reason(action, th)
            if reason:
                raise WriteRefused(reason)
        elif temperature_scale(th.temperature_unit) is None:
            raise WriteRefused("thermostat temperature unit is unknown")
        return action, th.temperature_unit

    async def _send(
        self,
        act: Action,
        mode_wait_s: float = MODE_WAIT_S,
        *,
        override_id: int | None = None,
        check_state: bool = True,
    ) -> Action:
        """T1.44 (2026-10-05): the Trane ignores `hvac_mode` inside set_temperature when
        leaving OFF. A mode change is therefore always an explicit set_hvac_mode, confirmed by
        read-back (bounded), followed by set_temperature with the setpoints only.

        Returns the action that was sent, still in °F, so read-back can compare after the
        device unit is converted back. Capability checks run even when `check_state` is
        false (kill-switch restore skips the active-mode authorization, not the device).
        """
        if act.service in ("set_fan_mode", "set_preset_mode"):
            key = "fan_mode" if act.service == "set_fan_mode" else "preset_mode"
            st0 = await self.ha.get_state(act.entity_id)
            opts = None if st0 is None else (st0.get("attributes") or {}).get(f"{key}s")
            val = str(act.data.get(key) or "none")
            act = act.model_copy(update={"data": {**act.data, key: device_value(val, opts)}})
        prepared, unit = await self._require_capabilities(act)
        mode = prepared.data.get("hvac_mode")
        if prepared.service != "set_temperature" or not isinstance(mode, str):
            await self._check_write(prepared, override_id, check_state=check_state)
            await _call_service(self.ha, action_for_device(prepared, unit))
            return prepared
        st = await self.ha.get_state(prepared.entity_id)
        if st is None or str(st.get("state")).lower() != mode.lower():
            m = Action(
                service="set_hvac_mode", entity_id=prepared.entity_id, data={"hvac_mode": mode}
            )
            await self._check_write(prepared, override_id, check_state=check_state)
            await _call_service(self.ha, m)
            waited = 0.0
            while True:
                st = await self.ha.get_state(prepared.entity_id)
                if st is not None and str(st.get("state")).lower() == mode.lower():
                    break
                if waited >= mode_wait_s:
                    raise HAError(None, f"HVAC mode {mode} did not confirm")
                delay = min(MODE_POLL_S, mode_wait_s - waited)
                await self._sleep(delay)
                waited += delay
        sps = {k: v for k, v in prepared.data.items() if k != "hvac_mode"}
        if sps:
            setpoint_action = prepared.model_copy(update={"data": sps})
            setpoint_action, unit = await self._require_capabilities(setpoint_action, str(mode))
            await self._check_write(setpoint_action, override_id, check_state=check_state)
            await _call_service(self.ha, action_for_device(setpoint_action, unit))
            prepared = prepared.model_copy(
                update={"data": {**prepared.data, **setpoint_action.data}}
            )
        return prepared

    def _fail(self, d: Decision, now: datetime, step_t: datetime | None, why: str) -> bool:
        self._record(d.model_copy(update={"reason": why}), "failed", now, readback=False)
        if step_t is not None:
            self._failed_step = step_t
        self.alerts.append(
            AlertEvent(
                key="write_failed", severity="warn", t=now, text=f"{d.action.service} failed: {why}"
            )
        )
        return False

    def _mark_preset_cleared(self, now: datetime) -> None:
        p = db.read_bot_state(self.conn)
        if p is not None and not p.preset_cleared:
            db.write_bot_state(
                self.conn, p.model_copy(update={"preset_cleared": True}), by="controller", now=now
            )

    # ---- kill-switch restore (FR-KILL-3, D§11.2) ------------------------------------
    async def kill_restore(self, now: datetime, timeout_s: float = 4.0) -> str:
        """One restore to the Home band when the system is `off`, sent just BEFORE the kill
        switch goes OFF (the only write permitted as part of disabling). No retry, single
        read-back, bounded by timeout_s so the API answers within 5 s. An unreadable initial
        state fails without a climate write; the caller must still turn the bot OFF.
        -> 'not_needed' | 'sent' | 'failed' | 'skipped' (no thermostat configured)."""
        if not self.entity:
            return "skipped"
        try:
            st = await asyncio.wait_for(self.ha.get_state(self.entity), timeout_s / 2)
        except (TimeoutError, HAError, httpx.HTTPError) as e:
            log.warning("kill restore not attempted: state read failed (%s)", type(e).__name__)
            return "failed"
        if st is None or st.get("state") in (None, "", "unknown", "unavailable"):
            log.warning("kill restore not attempted: thermostat state unreadable")
            return "failed"
        if st.get("state") != "off":
            return "not_needed"
        th = thermostat(st)
        if temperature_scale(th.temperature_unit) is None:
            log.warning("kill restore not attempted: thermostat temperature unit is unknown")
            return "failed"
        b = self.settings.bands
        lo, hi = float(b.heat.home[0]), float(b.cool.home[1])
        choice = choose_hold_mode(th.hvac_modes, lo, hi, th.current_temp)
        data = None if choice is None else safety._restore_data(*choice)
        if data is None:
            log.warning("kill restore not attempted: thermostat has no supported hold mode")
            return "failed"
        act = Action(
            service="set_temperature",
            entity_id=self.entity,
            safety_restore=True,
            data=data,
        )
        d = Decision(t=now, action=act, allowed=True, reason="kill switch: restore to band")
        try:
            # check_state stays false: this is the one write allowed while disabling,
            # including from SHADOW. Capability checks still run inside _send.
            sent = await asyncio.wait_for(
                self._send(act, mode_wait_s=timeout_s / 4, check_state=False), timeout_s / 2
            )
            back = await asyncio.wait_for(self.ha.get_state(self.entity), timeout_s / 4)
        except (TimeoutError, HAError, WriteRefused, httpx.HTTPError) as e:
            self._record(
                d.model_copy(update={"reason": f"kill restore: {type(e).__name__}"}),
                "failed",
                now,
                readback=False,
            )
            return "failed"
        ok = readback_ok(sent, back)
        self._record(
            d.model_copy(update={"action": sent}), "sent" if ok else "failed", now, readback=ok
        )
        if ok:
            self._last_send = now
        return "sent" if ok else "failed"
