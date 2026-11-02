"""Controller: the SINGLE write path (D§1.3, D§6, NFR-SAFE-5). Only this module may import
`_call_service` (T1.29). Every evaluate() result lands in `writes` (sent/shadow/denied/failed).

Safe by default: any adapter error -> outcome 'failed', no further write this plan step.
Read-back mismatch -> one retry after 2 min -> 'failed' + write_failed alert; never loops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from thermabot.adapters import db
from thermabot.adapters.ha_rest import HAError, HARest, WriteRefused, _call_service
from thermabot.config import Settings
from thermabot.core import safety, statemachine
from thermabot.core.guards import GuardCtx, WriteRecord, evaluate
from thermabot.core.schedule import block_at, next_boundary
from thermabot.core.types import Action, AlertEvent, Decision, PlanStep, Snapshot

log = logging.getLogger(__name__)
RETRY_AFTER_S = 120.0
READBACK_POLL_S = 5.0
MODE_WAIT_S = 30.0  # set_hvac_mode confirmation before sending setpoints (T1.44)
MODE_POLL_S = 1.0
READBACK_TOL_F = 0.5
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


def diff(step: PlanStep, snap: Snapshot, entity_id: str) -> list[Action]:
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
        if step.hvac_mode == "heat_cool":
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
        step.hvac_mode != "off"
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


def readback_ok(action: Action, st: dict[str, Any] | None) -> bool:
    if st is None:
        return False
    a = st.get("attributes", {})
    for k, v in action.data.items():
        have = st.get("state") if k == "hvac_mode" else a.get(k)
        if k == "preset_mode":
            v, have = (v or "none"), (have or "none")
        if isinstance(v, int | float) and not isinstance(v, bool):
            if not isinstance(have, int | float) or abs(float(have) - float(v)) > READBACK_TOL_F:
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
    ) -> None:
        self.conn, self.ha = conn, ha
        self.settings = settings or Settings()
        self.bot_user_id = bot_user_id
        self._sleep = sleep
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
        if snap is not None and self.entity:  # v0.2.0: not configured -> no actions at all
            restore = safety.check(snap, self.settings, block_at(self.settings.schedule, now), now)
            if restore is not None:
                actions = [restore.action]
                self.hold_until = restore.hold_until
            elif step is not None and step.t != self._failed_step:
                actions = diff(step, snap, self.entity)
        out: list[Decision] = []
        for act in actions:
            ctx = GuardCtx(
                kill_switch=None if persisted is None else persisted.kill_switch,
                ha_mirror=live.ha_mirror,
                run_mode="shadow" if persisted is None else persisted.run_mode,
                override_open=live.override_open,
                snapshot=snap,
                recent_writes=self.recent_writes(now),
                preset_cleared=bool(persisted and persisted.preset_cleared),
                settings=self.settings,
                now=now,
                safety_hold_until=self.hold_until,
                **(guard_extra or {}),
            )
            d = evaluate(act, ctx)
            allowed_by_state = eff.restore_writes if act.safety_restore else eff.plan_writes
            if d.allowed and not allowed_by_state:
                d = d.model_copy(
                    update={
                        "allowed": False,
                        "guard": "state",
                        "reason": f"state {eff.label} blocks writes",
                    }
                )
            out.append(d)
            if d.guard == "shadow":
                self._record(d, "shadow", now)
            elif not d.allowed:
                self._record(d, "denied", now)
            else:
                ok = await self.execute(d, now, step_t=None if step is None else step.t)
                if not ok:
                    break  # never chain more writes after a failure
        return out

    # ---- execution + read-back (D§6.2) ---------------------------------------------
    async def _readback(self, action: Action) -> tuple[bool, int]:
        timeout = self.settings.writes.readback_timeout_s
        waited = 0.0
        while True:
            try:
                st = await self.ha.get_state(action.entity_id)
            except (HAError, httpx.HTTPError):
                st = None
            if readback_ok(action, st):
                return True, int(waited * 1000)
            if waited >= timeout:
                return False, int(waited * 1000)
            await self._sleep(READBACK_POLL_S)
            waited += READBACK_POLL_S

    async def execute(self, d: Decision, now: datetime, step_t: datetime | None = None) -> bool:
        """Send, read back, retry once after 2 min, else 'failed' + write_failed alert."""
        act = d.action
        if not self.entity or act.entity_id != self.entity:  # belt and braces; adapter too
            self._record(d, "denied", now)
            return False
        for attempt in (1, 2):
            if attempt == 2:
                await self._sleep(RETRY_AFTER_S)
            try:
                await self._send(act)
            except (HAError, WriteRefused, httpx.HTTPError) as e:
                log.error("write %s failed: %s", act.service, e)
                return self._fail(d, now, step_t, f"adapter error: {type(e).__name__}")
            self._last_send = now
            ok, ms = await self._readback(act)
            if ok:
                self._record(d, "sent", now, readback=True, ms=ms)
                if act.service == "set_preset_mode":
                    self._mark_preset_cleared(now)
                return True
            log.warning("read-back mismatch for %s (attempt %d)", act.service, attempt)
        return self._fail(d, now, step_t, "read-back mismatch after retry")

    async def _send(self, act: Action, mode_wait_s: float = MODE_WAIT_S) -> None:
        """T1.44 (2026-10-05): the Trane ignores `hvac_mode` inside set_temperature when
        leaving OFF. A mode change is therefore always an explicit set_hvac_mode, confirmed by
        read-back (bounded), followed by set_temperature with the setpoints only."""
        if act.service in ("set_fan_mode", "set_preset_mode"):
            key = "fan_mode" if act.service == "set_fan_mode" else "preset_mode"
            st0 = await self.ha.get_state(act.entity_id)
            opts = None if st0 is None else (st0.get("attributes") or {}).get(f"{key}s")
            val = str(act.data.get(key) or "none")
            act = act.model_copy(update={"data": {**act.data, key: device_value(val, opts)}})
        mode = act.data.get("hvac_mode")
        if act.service != "set_temperature" or not isinstance(mode, str):
            await _call_service(self.ha, act)
            return
        st = await self.ha.get_state(act.entity_id)
        if st is None or str(st.get("state")).lower() != mode.lower():
            m = Action(service="set_hvac_mode", entity_id=act.entity_id, data={"hvac_mode": mode})
            await _call_service(self.ha, m)
            waited = 0.0
            while waited < mode_wait_s:
                st = await self.ha.get_state(act.entity_id)
                if st is not None and str(st.get("state")).lower() == mode.lower():
                    break
                await self._sleep(MODE_POLL_S)
                waited += MODE_POLL_S
        sps = {k: v for k, v in act.data.items() if k != "hvac_mode"}
        if sps:
            await _call_service(self.ha, act.model_copy(update={"data": sps}))

    def _fail(self, d: Decision, now: datetime, step_t: datetime | None, why: str) -> bool:
        self._record(d.model_copy(update={"reason": why}), "failed", now, readback=False)
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
        b = self.settings.bands
        act = Action(
            service="set_temperature",
            entity_id=self.entity,
            safety_restore=True,
            data={
                "hvac_mode": "heat_cool",
                "target_temp_low": b.heat.home[0],
                "target_temp_high": b.cool.home[1],
            },
        )
        d = Decision(t=now, action=act, allowed=True, reason="kill switch: restore to band")
        try:
            await asyncio.wait_for(self._send(act, mode_wait_s=timeout_s / 4), timeout_s / 2)
            back = await asyncio.wait_for(self.ha.get_state(self.entity), timeout_s / 4)
        except (TimeoutError, HAError, WriteRefused, httpx.HTTPError) as e:
            self._record(
                d.model_copy(update={"reason": f"kill restore: {type(e).__name__}"}),
                "failed",
                now,
                readback=False,
            )
            return "failed"
        ok = readback_ok(act, back)
        self._record(d, "sent" if ok else "failed", now, readback=ok)
        if ok:
            self._last_send = now
        return "sent" if ok else "failed"
