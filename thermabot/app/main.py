"""App factory (D§1.3, D§15): one process, one worker, loops started in the FastAPI lifespan.

`uvicorn thermabot.app.main:app --workers 1` resolves `app` lazily from the environment, so
importing this module never needs secrets. Missing/invalid env fails fast before any I/O.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from thermabot import __version__
from thermabot.adapters import db
from thermabot.adapters.ha_rest import MIRROR_ENTITY, HARest
from thermabot.adapters.heartbeat import HeartbeatWebhook
from thermabot.adapters.influx import Influx, line
from thermabot.adapters.notify import Notifier, envelope
from thermabot.adapters.weather import CachedHourly, make_provider
from thermabot.app import efficiency as E
from thermabot.app import jobs as J
from thermabot.app import routes_api
from thermabot.app.auth import Auth
from thermabot.app.controller import Controller, parse_last_reported
from thermabot.app.jobs import Background
from thermabot.app.loops import HEARTBEAT_ENTITY, TICK_S, Jobs, Loops, slot_store
from thermabot.app.routes_api import Ctx, _kv_put, load_settings
from thermabot.app.routes_ui import SUMMARY_KEY
from thermabot.config import Env, Settings
from thermabot.core import openings as opening_policy
from thermabot.core import planner, shadow, thermal
from thermabot.core import statemachine as sm
from thermabot.core.fallback import fallback_plan
from thermabot.core.metrics import degree_days
from thermabot.core.metrics import runtime as day_runtime
from thermabot.core.presence import TrackerState
from thermabot.core.presence import resolve as resolve_presence
from thermabot.core.safety import cumulative_off_h, off_allowed_now
from thermabot.core.schedule import TZ, season_for, season_from_forecast
from thermabot.core.summary import LastWeek, render
from thermabot.core.types import Forecast, HouseState, Plan, Presence, Snapshot, ThermostatState
from thermabot.core.weather import is_stale

log = logging.getLogger(__name__)
FORECAST_TIMEOUT_S = 15.0  # replan must always produce a plan (fallback) quickly
BG_JOB_TIMEOUT_S = 600.0  # refit / rollup run off-loop; bounded so they can't pile up
SAMPLE_READ_TIMEOUT_S = 5.0  # extra sampler reads (blower, compressor, outdoor, rooms)
CATCHUP_EVERY = timedelta(hours=6)  # no model yet: try a refit at most this often
MIGRATION_BY = "migration-v0.1.11"  # updated_by of the first-start settings row (FR-CFG-1)
SENSORS_MIGRATION_BY = "migration-v0.6.1"  # updated_by of the one-time env sensors seed
SENSOR_ENV = {
    "blower": "THERMABOT_SENSOR_BLOWER",
    "compressor": "THERMABOT_SENSOR_COMPRESSOR",
    "outdoor": "THERMABOT_SENSOR_OUTDOOR",
}


class ConfigError(SystemExit):
    """Missing/invalid env. Raised before the DB is opened or anything is written."""


def load_env(environ: Mapping[str, str] | None = None) -> Env:
    try:
        env = Env.model_validate(dict(environ)) if environ is not None else Env()  # type: ignore[call-arg,unused-ignore]
    except ValidationError as e:
        missing = ", ".join(str(err["loc"][0]) for err in e.errors())
        raise ConfigError(f"thermabot: missing or invalid environment: {missing}") from None
    if not env.HA_URL.strip() or not env.HA_TOKEN.get_secret_value().strip():
        raise ConfigError("thermabot: HA_URL and HA_TOKEN must be non-empty")
    return env


def _secret(s: Any) -> str | None:
    return None if s is None else str(s.get_secret_value())


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def thermostat(st: dict[str, Any], read_at: datetime | None = None) -> ThermostatState:
    """`read_at` = when we fetched `st` from HA. The Trane/Nexia entity only bumps
    last_reported on change (homeassistant.update_entity does not bump it either,
    probed 2026-10-05), so a steady house looks "stale" by HA's stamp. A successful
    read of an available entity IS fresh: on integration failure HA marks it
    unavailable, which DEGRADES via tstat_unavailable. So freshness = read time.
    Without read_at (or when unavailable) fall back to HA's own stamp."""
    a = st.get("attributes", {})
    mode = str(st.get("state"))
    if read_at is not None and mode not in ("unavailable", "unknown", "", "None"):
        seen: datetime | None = read_at
    else:
        # parse_last_reported() returns None when the dict has no 'state', so pass it.
        seen = parse_last_reported(st)
    single_sp = _num(a.get("temperature"))
    return ThermostatState(
        hvac_mode=mode,
        hvac_action=a.get("hvac_action"),
        current_temp=_num(a.get("current_temperature")),
        heat_sp=single_sp
        if mode.lower() == "heat" and single_sp is not None
        else _num(a.get("target_temp_low")),
        cool_sp=single_sp
        if mode.lower() == "cool" and single_sp is not None
        else _num(a.get("target_temp_high")),
        min_temp=_num(a.get("min_temp")),
        max_temp=_num(a.get("max_temp")),
        target_temp_step=_num(a.get("target_temp_step")),
        temperature_unit=str(a.get("temperature_unit") or a.get("unit_of_measurement") or "")
        or None,
        fan_mode=a.get("fan_mode"),
        preset_mode=a.get("preset_mode"),
        humidity=_num(a.get("current_humidity")),
        last_reported=seen,
    )


def last_week(conn: sqlite3.Connection, now: datetime) -> LastWeek:
    since = (now.astimezone(TZ).date() - timedelta(days=7)).isoformat()
    r = conn.execute(
        "SELECT COALESCE(SUM(heat_min),0) h, COALESCE(SUM(cool_min),0) c,"
        " COALESCE(SUM(cool_effort),0) ce, COALESCE(SUM(fan_only_min),0) f,"
        " COALESCE(AVG(comfort_pct),100) cp, COALESCE(SUM(violation_min),0) v"
        " FROM daily_metrics WHERE day >= ?",
        (since,),
    ).fetchone()
    return LastWeek(
        heat_h=r["h"] / 60,
        cool_h=r["c"] / 60,
        cool_effort_h=r["ce"] / 60,
        fan_only_h=r["f"] / 60,
        comfort_pct=r["cp"],
        violation_min=r["v"],
    )


@dataclass
class Runtime:
    """Everything the lifespan owns. Jobs are methods so tests can drive them directly."""

    env: Env
    conn: sqlite3.Connection
    ha: HARest
    influx: Influx
    notifier: Notifier
    controller: Controller
    ctx: Ctx
    loops: Loops
    clock: Callable[[], datetime]
    pending: set[asyncio.Task[Any]]
    bg: Background
    nws_transport: httpx.AsyncBaseTransport | None = None
    open_meteo_transport: httpx.AsyncBaseTransport | None = None
    weather: CachedHourly | None = None  # v0.4.0 provider (settings.weather.provider)
    weather_key: tuple[str, float, float] | None = None
    heartbeat: HeartbeatWebhook | None = None
    forecast: Forecast | None = None
    weather_stale: bool = False
    refit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_catchup: datetime | None = None
    prev_presence: Presence | None = None  # v0.6.2: carries away_since/since across ticks

    async def tick(self, now: datetime) -> None:
        """D§1.3: read the thermostat + mirror, run the controller (guards decide writes)."""
        saved = load_settings(self.ctx)
        # Keep the adapter/controller bound to the same entity until restart, while
        # applying imported comfort, presence and safety settings together.
        self.controller.settings = saved.model_copy(
            update={"thermostat": self.controller.settings.thermostat}
        )
        entity = self.controller.entity  # "" = not configured (v0.2.0): read nothing climate
        try:
            st = await self.ha.get_state(entity) if entity else None
            mirror = await self.ha.get_state(MIRROR_ENTITY)
            self.ctx.ha_health = "ok"
        except Exception:
            self.ctx.ha_health = "down"
            self.ctx.live = self.ctx.live.model_copy(
                update={"tstat_unavailable": True, "tstat_age_min": None}
            )
            self.ctx.snapshot = None
            self.ctx.broker.publish(
                "state",
                {"state": self.ctx.eff().state, "label": self.ctx.eff().label, "ha_health": "down"},
            )
            await self.sample(now, None, self.ctx.plan, None)
            raise
        finally:
            self.ctx.last_tick = now  # the loop is alive; /healthz reports ha separately
        th = None if st is None else thermostat(st, now)
        age = (
            None
            if th is None or th.last_reported is None
            else (now - th.last_reported).total_seconds() / 60
        )
        m = None if mirror is None else mirror.get("state")
        self.ctx.live = sm.Live(
            tstat_unavailable=th is None or th.hvac_mode in ("unavailable", "unknown"),
            tstat_age_min=age,
            weather_stale=self.weather_stale,
            ha_mirror=m if m in ("on", "off") else None,
            setup_needed=not entity,
            sensors_missing=bool(entity) and self.ctx.settings.sensors_missing,
        )
        pres = await self.read_presence(now)
        prev_house = None if self.prev_presence is None else self.prev_presence.house
        self.prev_presence = pres
        snap = (
            None
            if th is None
            else Snapshot(t=now, thermostat=th, forecast=self.forecast, presence=pres)
        )
        self.ctx.snapshot = snap
        self.ctx.broker.publish(
            "state",
            {"state": self.ctx.eff().state, "label": self.ctx.eff().label, "ha_health": "ok"},
        )
        if pres is not None and prev_house is not None and pres.house != prev_house:
            self.loops.trigger_replan()  # FR-PRS: react within one tick, not up to 1 h
        plan = self.ctx.plan
        step = J.current_step(plan, now)
        s = self.controller.settings
        extra = J.off_guard_extra(
            plan,
            step,
            self.forecast,
            s.comfort.target_score,
            cumulative_off_h(J.load_off_windows(self.conn), now),
        )
        try:
            await self.controller.tick(snap, step, self.ctx.live, now, guard_extra=extra)
            J.track_off_window(
                self.conn, None if th is None else th.hvac_mode, now, s.off.reevaluate_h
            )
        finally:
            await self.sample(now, th, plan, step)

    # ---- presence (v0.6.2, FR-PRS-1..4) ---------------------------------------------
    async def read_presence(self, now: datetime) -> Presence | None:
        """Read every configured person's life360/companion tracker from HA and resolve
        (core.presence.resolve stays pure). A read failure on a tracker is simply absent
        from `trackers`, which resolve_person -> map_state(None) maps to HOME (conservative),
        never to a false AWAY. No people configured -> resolve() still runs and returns
        OCCUPIED (v0.2.0 PresenceCfg convention), so this never breaks an unconfigured house.
        v0.6.4: staleness is computed from `last_reported`/`last_updated` (HA's poll-confirm
        stamp), NOT `last_changed` (HA's last-transition stamp). A tracker can sit `not_home`
        unchanged for hours while HA keeps confirming it every cycle; using `last_changed`
        made that reading look stale, discarded it, and fell through to unknown -> HOME --
        the longer someone was confirmed away, the more likely the bot was to think they'd
        come home. See TrackerState.last_reported / parse_last_reported.
        """
        s = self.controller.settings
        people = s.presence.people
        eids = sorted({eid for p in people.values() for eid in (p.life360, p.companion) if eid})
        try:
            states = await self.ha.get_states(eids) if eids else {}
        except Exception:  # noqa: BLE001 - HA down: treat every tracker as missing (-> HOME)
            states = {}
        trackers: dict[str, TrackerState] = {}
        for eid, st in states.items():
            raw = st.get("state")
            if raw is None or str(raw) in ("unknown", "unavailable"):
                continue  # map_state(None) = HOME; leaving it out of trackers is equivalent
            # v0.6.4 fix: `last_changed` is the last state-TRANSITION time, which stays old
            # for hours while someone is confirmed away and the value hasn't flipped back.
            # `last_reported` (fallback last_updated) is HA's actual poll-confirmation stamp,
            # which is what life360_stale_min/companion_stale_min are meant to bound.
            seen = parse_last_reported(st) or now
            trackers[eid] = TrackerState(state=str(raw), last_reported=seen)
        guest_row = self.conn.execute(
            "SELECT value_json FROM settings WHERE key='guest'"
        ).fetchone()
        guest_on = bool(guest_row and json.loads(guest_row["value_json"]).get("on"))
        return resolve_presence(
            trackers, now, s.presence, prev=self.prev_presence, guest_on=guest_on
        )

    # ---- 1-minute samples (v0.1.9, FR-MET / D8) -----------------------------------------
    async def _read(self, eid: str) -> dict[str, Any] | None:
        try:
            return await self.ha.get_state(eid)
        except Exception:  # noqa: BLE001 - best effort; the row is written with nulls
            return None

    async def sample(
        self, now: datetime, th: ThermostatState | None, plan: Plan | None, step: Any
    ) -> None:
        """One `samples` row per minute in every bot state (incl. SHADOW). Extra entities are
        read concurrently, best effort and time-bounded; nothing here can break the tick."""
        self.ctx.opening_assessment = []
        try:
            s = load_settings(self.ctx)
            ids = [
                *J.extra_entities(s),
                *(r.temp for k, r in s.rooms.items() if k != "thermostat"),
                *(s.openings.sensors if s.openings.enabled else []),
            ]
            try:
                got = await asyncio.wait_for(
                    asyncio.gather(*(self._read(e) for e in ids)), SAMPLE_READ_TIMEOUT_S
                )
            except TimeoutError:
                got = [None] * len(ids)
            states = dict(zip(ids, got, strict=True))
            opening_states = {eid: states.get(eid) for eid in s.openings.sensors}
            self.ctx.opening_assessment = opening_policy.assess(s.openings, opening_states, now)
            snap = self.ctx.snapshot
            pres = None if snap is None or snap.presence is None else snap.presence.house.value
            doc = J.sample_doc(now, s, th, states, plan, step, self.forecast, pres)
            row = J.minute_rows([(now, doc)], s)[0]
            temps = dict(row.rooms)
            if row.t_in is not None:
                temps["thermostat"] = row.t_in
            score = None
            if temps:
                season = (
                    season_from_forecast(s.season, self.forecast, now)
                    if self.forecast is not None
                    and any(now <= p.t < now + timedelta(hours=24) for p in self.forecast.points)
                    else "shoulder"
                )
                score, _, _ = shadow.house(s, season, row.block, temps, row.ages, row.occupied)
            self.ctx.score = score
            stale = [
                k
                for k, a in row.ages.items()
                if k in s.rooms and (a is None or a > s.rooms[k].stale_min)
            ]
            eff = self.ctx.eff()
            J.save_sample(
                self.conn,
                now,
                doc,
                eff.base,
                score,
                stale,
                effective_state=eff.state,
                degraded_reason=eff.degraded_reason,
            )
        except Exception:  # noqa: BLE001 - sampling must never break the tick
            log.exception("sample write failed")

    # ---- replan (D§4.2) -------------------------------------------------------------
    async def weather_location(self, s: Settings) -> tuple[float, float]:
        """settings.weather lat/lon, else HA's home location (zone.home), as before v0.4.0."""
        if s.weather.lat is not None and s.weather.lon is not None:
            return float(s.weather.lat), float(s.weather.lon)
        z = await self.ha.get_state("zone.home")
        a = (z or {}).get("attributes", {})
        if a.get("latitude") is None or a.get("longitude") is None:
            raise RuntimeError("zone.home has no latitude/longitude")
        return float(a["latitude"]), float(a["longitude"])

    async def fetch_forecast(self, now: datetime) -> Forecast:
        """Hourly forecast from the configured provider (v0.4.0: nws | open_meteo). The
        provider client is rebuilt only when the provider or the explicit location changes;
        with no lat/lon set, zone.home is read once (as the NWS client always did)."""
        s = load_settings(self.ctx)
        w = s.weather
        if self.weather is not None and self.weather_key is not None:
            stale = self.weather_key[0] != w.provider or (
                w.lat is not None
                and w.lon is not None
                and self.weather_key[1:] != (round(w.lat, 4), round(w.lon, 4))
            )
            if stale:
                await self.weather.aclose()
                self.weather = None
        if self.weather is None:
            lat, lon = await self.weather_location(s)
            self.weather = make_provider(
                w.provider,
                lat,
                lon,
                {"nws": self.nws_transport, "open_meteo": self.open_meteo_transport},
            )
            self.weather_key = (w.provider, round(lat, 4), round(lon, 4))
        return await self.weather.hourly(now=now)

    async def replan(self, now: datetime) -> None:
        """Hourly at :05 and on trigger. Any error or missing/stale forecast -> the plain
        schedule fallback (never `off`). The plan is stored on ctx.plan (tick + /plan read
        it), in `plans`, and as thermabot_plan points."""
        s = load_settings(self.ctx)
        fc: Forecast | None
        try:
            fc = await asyncio.wait_for(self.fetch_forecast(now), FORECAST_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - weather outage -> fallback plan
            log.warning("forecast fetch failed: %s", type(e).__name__)
            fc = self.forecast  # last good one; stale check below decides
        self.forecast = fc
        self.ctx.forecast = fc  # v0.4.0: GET /api/forecast + Forecast page
        self.weather_stale = is_stale(fc, now, s.weather)
        self.ctx.weather_health = "stale" if self.weather_stale else "ok"
        version: int | None = None
        try:
            params, conf, version = J.active_model(self.conn, s.planner)
            th = None if self.ctx.snapshot is None else self.ctx.snapshot.thermostat
            if th is None and not self.controller.entity:
                raise RuntimeError("no thermostat configured")
            if th is None:  # start-up replan runs before the first tick
                st = await self.ha.get_state(self.controller.entity)
                th = None if st is None else thermostat(st, now)
            if th is None or th.current_temp is None:
                raise RuntimeError("no thermostat temperature")
            windows = J.load_off_windows(self.conn)
            snap = self.ctx.snapshot
            house = (
                HouseState.OCCUPIED
                if snap is None or snap.presence is None
                else snap.presence.house
            )
            p: Plan = planner.plan(
                planner.PlanInputs(
                    now=now,
                    t0=th.current_temp,
                    current_mode=th.hvac_mode,
                    forecast=None if self.weather_stale else fc,
                    params=params or thermal.ThermalParams(),
                    confidence=conf,
                    settings=s,
                    house=house,
                    # No bounded 'away until <date>' / vacation-end-date concept exists yet
                    # (none in config.py or BRD); next_boundary's per-day re-derivation in
                    # build_timeline already re-evaluates every replan, so a multi-day trip
                    # 'just works' for as long as presence stays EMPTY. Flagged in the report.
                    away_until=None,
                    off_allowed=off_allowed_now(windows, self.controller.hold_until, now),
                    off_used_h=cumulative_off_h(windows, now),
                    current_sp=(th.heat_sp, th.cool_sp),
                )
            )
        except Exception as e:  # noqa: BLE001 - never leave the controller without a plan
            log.warning("replan failed (%s); using fallback plan", type(e).__name__)
            snap = self.ctx.snapshot
            cur = (
                None
                if snap is None
                else (snap.thermostat.hvac_mode, snap.thermostat.heat_sp, snap.thermostat.cool_sp)
            )
            house = (
                HouseState.OCCUPIED
                if snap is None or snap.presence is None
                else snap.presence.house
            )
            p = fallback_plan(
                s, now, f"fallback: replan error ({type(e).__name__})", current=cur, house=house
            )
        self.ctx.plan = p
        self.catch_up(now, version)
        try:
            J.save_plan(self.conn, p, "replan", version, None if fc is None else fc.source)
            self.bg.enqueue(J.plan_lines(p), now)
        except Exception:  # noqa: BLE001 - persistence/telemetry never undoes the plan
            log.exception("plan persist failed")

    def catch_up(self, now: datetime, version: int | None) -> None:
        """No model yet: try a refit after the hourly replan instead of waiting for 03:15.
        At most once per CATCHUP_EVERY; refit itself enforces MIN_TRAIN_STEPS."""
        if version is not None or self.refit_lock.locked():
            return
        if self.last_catchup is not None and now - self.last_catchup < CATCHUP_EVERY:
            return
        since = (now - timedelta(days=J.REFIT_DAYS)).astimezone(UTC).isoformat()
        n = self.conn.execute("SELECT COUNT(*) FROM samples WHERE ts >= ?", (since,)).fetchone()[0]
        if n < J.MIN_TRAIN_STEPS * 15:  # cheap pre-check: ~1 row/min, 15 rows per step
            return
        self.last_catchup = now
        self.spawn("refit-catchup", self.refit_now(now))

    async def influx_steps(self, start: datetime, now: datetime) -> list[Any]:
        if not self.influx.readable or not self.controller.entity:
            return []
        try:
            tst = await J.fetch(
                self.influx,
                self.controller.entity,
                ["current_temperature", "hvac_action_str"],
                start,
                now,
            )
            sn = load_settings(self.ctx).sensors
            out = await J.fetch(self.influx, sn.outdoor, ["value"], start, now)
            cs = await J.fetch(self.influx, sn.compressor, ["value"], start, now)
        except Exception as e:  # noqa: BLE001 - Influx down: the local samples still count
            log.warning("refit: influx read failed (%s); local samples only", type(e).__name__)
            return []
        return J.build_samples(
            tst["current_temperature"],
            tst["hvac_action_str"],
            out["value"],
            cs["value"],
            start,
            now,
        )

    # ---- refit (D§4.1) --------------------------------------------------------------
    async def refit(self, now: datetime) -> bool:
        """Nightly 03:15, P5 button, and the no-model catch-up: OLS on 15-min steps from the
        local `samples` table merged with Influx history (local wins on overlap). Not gated
        on bot state. The new model becomes active only if the fit is accepted (in physical
        range). Returns True when adopted."""
        async with self.refit_lock:
            start = now - timedelta(days=J.REFIT_DAYS)
            local = J.local_steps(self.conn, start, now)
            steps = J.merge_steps(await self.influx_steps(start, now), local)
            spans = J.override_spans(self.conn, start)
            steps = [(t, x) for t, x in steps if J.outside(t, spans)]
            train, test = J.split(steps, now)
            if len(train) < J.MIN_TRAIN_STEPS:
                log.warning("refit skipped: %d training steps", len(train))
                return False
            prev, _, _ = J.active_model(self.conn)
            res = thermal.fit(train, prev)
            if not res.accepted or not thermal.in_range(res.params):
                log.warning("refit rejected, previous model kept: %s", res.note)
                return False
            mae = J.holdout(test, res.params)
            v = J.save_model(self.conn, res.params, mae, len(train), now)
            log.info(
                "refit adopted model v%d (holdout MAE %.2f, %d local steps) %s",
                v,
                mae,
                len(local),
                res.note,
            )
            return True

    def spawn(self, name: str, coro: Any) -> None:
        """Run a network-heavy job in the background so the 60 s tick/heartbeat never wait
        on Influx; errors are logged in the task, never raised into the loop."""
        t = asyncio.create_task(coro, name=f"thermabot-{name}")
        self.pending.add(t)
        t.add_done_callback(self.pending.discard)

    async def refit_job(self, now: datetime) -> None:
        """Loops 03:15 hook: start the refit in the background."""
        self.spawn("refit", self.refit_now(now))

    async def rollup_job(self, now: datetime) -> None:
        """Loops 00:05 hook: start the rollup in the background."""
        self.spawn("rollup", self._safe("rollup", self.rollup(now)))

    async def _safe(self, name: str, coro: Any) -> None:
        try:
            await asyncio.wait_for(coro, BG_JOB_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - background job; logged, never surfaces
            log.exception("%s failed", name)

    async def refit_now(self, now: datetime | None = None) -> object:
        """routes_api refit hook (fire-and-forget): same job, then replan on adoption."""
        try:
            ok = await asyncio.wait_for(self.refit(now or self.clock()), BG_JOB_TIMEOUT_S)
            if ok:
                self.loops.trigger_replan()
        except Exception:  # noqa: BLE001 - background task; never surfaces
            log.exception("refit failed; previous model kept")
        return None

    # ---- metrics rollup (D§8.3) -------------------------------------------------------
    async def rollup(self, now: datetime) -> None:
        """00:05 local: yesterday's daily_metrics + shadow_daily rows. Runtime and comfort
        come from the local `samples` table; runtime falls back to Influx minute data when
        there are no local samples. Missing data stays NULL (never fake zeros)."""
        s = load_settings(self.ctx)
        day = (now.astimezone(TZ) - timedelta(days=1)).date()
        start, end = J.local_day_bounds(day)
        rt: dict[str, float | None] = dict.fromkeys(
            ("heat_min", "cool_min", "fan_only_min", "heat_effort", "cool_effort", "hdd", "cdd")
        )
        rows = J.minute_rows(J.load_samples(self.conn, start, end), s)
        outs = [r.t_out for r in rows if r.t_out is not None]
        mean_out = J.mean(outs)
        season = "shoulder" if mean_out is None else season_for(s.season, [mean_out])
        params, _, version = J.active_model(self.conn, s.planner)
        minutes = J.elapsed_minutes(start, end)
        sd = shadow.compare(s, season, rows, params, minutes)
        if rows:
            rt.update(
                heat_min=sd.actual_heat_min,
                cool_min=sd.actual_cool_min,
                fan_only_min=sd.actual_fan_only_min,
                heat_effort=sd.actual_heat_min,
                cool_effort=sd.actual_cool_effort,
            )
            if mean_out is not None:
                rt["hdd"], rt["cdd"] = degree_days(mean_out, s.metrics.hdd_base_f)
        elif self.influx.readable:
            q0 = start - timedelta(minutes=15)  # step-hold the value from before midnight
            act = await J.fetch(self.influx, self.controller.entity, ["hvac_action_str"], q0, end)
            blower = await J.fetch(self.influx, s.sensors.blower, ["value"], q0, end)
            cs = await J.fetch(self.influx, s.sensors.compressor, ["value"], q0, end)
            out = await J.fetch(self.influx, s.sensors.outdoor, ["value"], start, end)
            mins = J.minute_samples(
                act["hvac_action_str"], blower["value"], cs["value"], start, end
            )
            if mins:
                r = day_runtime(mins)
                rt.update(
                    heat_min=r.heat_min,
                    cool_min=r.cool_min,
                    fan_only_min=r.fan_only_min,
                    heat_effort=r.heat_effort,
                    cool_effort=r.cool_effort,
                )
            m = J.mean([v for _, x in out["value"] if (v := J.num(x)) is not None])
            if m is not None:
                rt["hdd"], rt["cdd"] = degree_days(m, s.metrics.hdd_base_f)
        n_ovr = self.conn.execute(
            "SELECT COUNT(*) FROM overrides WHERE started_at >= ? AND started_at < ?",
            (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
        ).fetchone()[0]
        row: dict[str, Any] = {
            "day": day.isoformat(),
            **rt,
            "comfort_pct": sd.actual_comfort_pct,
            "violation_min": sd.actual_violation_min if rows else None,
            "overrides": int(n_ovr),
            "bot_state": self.ctx.eff().base,
        }
        cols = ", ".join(row)
        self.conn.execute(
            f"INSERT OR REPLACE INTO daily_metrics ({cols}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )
        fields = {k: v for k, v in row.items() if k != "day" and v is not None}
        self.bg.enqueue([line("thermabot_daily", {"day": row["day"]}, fields, start)], now)
        J.save_shadow_day(self.conn, day.isoformat(), sd, version if params else None, now)
        try:  # v0.5.0: efficiency curve input (own table; never blocks the rollup)
            E.compute_day(self.conn, day, s.metrics.hdd_base_f, now, "rollup")
        except Exception:  # noqa: BLE001
            log.exception("efficiency rollup failed")
        J.prune_samples(self.conn, now)

    async def efficiency_backfill(self) -> dict[str, Any]:
        """v0.5.0 one-time start-up backfill of efficiency_daily from `samples`. Idempotent
        (only days without a row), one local day per chunk with a yield in between."""
        s = load_settings(self.ctx)
        return await E.backfill(self.conn, self.clock(), s.metrics.hdd_base_f)

    async def backup(self, now: datetime) -> None:
        d = Path(self.env.DATA_DIR) / "backup"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"thermabot-{now.astimezone(UTC):%Y-%m-%d}.db"
        if not dest.exists():
            db.backup(self.conn, dest)
            dest.chmod(0o600)
        daily = d.glob("thermabot-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].db")
        for old in sorted(daily)[:-7]:
            old.unlink()

    async def summary(self, now: datetime) -> None:
        """Weekly summary. Delivery (with retries up to ~21 min) runs in the background so the
        60 s heartbeat never stalls; `summary_latest` is written only after a successful send."""
        if not load_settings(self.ctx).summary.enabled:
            return
        text = render(last_week(self.conn, now), None, None, None)
        t = asyncio.create_task(self.send_summary(text, now))
        self.pending.add(t)
        t.add_done_callback(self.pending.discard)

    async def send_summary(self, text: str, now: datetime) -> str:
        body = envelope(
            "weekly_summary",
            "weekly_summary",
            "info",
            "closed",
            "thermabot weekly summary",
            text,
            now,
        )
        res = await self.notifier.deliver(body)
        if res == "ok":
            db.write_kv(self.conn, SUMMARY_KEY, text, now=now)
        else:
            log.warning("weekly summary not delivered: %s", res)
        return res

    async def send_alert(self, text: str) -> str:
        return await self.notifier.deliver(
            envelope("alert", "test", "info", "open", "thermabot alert", text, self.clock())
        )

    async def aclose(self) -> None:
        for t in list(self.pending):
            t.cancel()
        try:
            assert self.heartbeat is not None
            await self.heartbeat.publish(
                HEARTBEAT_ENTITY,
                self.clock().astimezone(UTC).isoformat(),
                {"state": "stopped", "version": __version__},
            )
        except Exception:  # noqa: BLE001 - HA down at shutdown
            log.warning("final heartbeat failed")
        with contextlib.suppress(Exception):  # best-effort final Influx flush (D§15)
            await asyncio.wait_for(self.influx.flush(now=self.clock()), 5.0)
        for c in (self.ha, self.influx, self.notifier, self.weather, self.heartbeat):
            if c is not None:
                await c.aclose()
        self.conn.close()


def persist_effective_settings(ctx: Ctx) -> bool:
    """v0.1.11 (FR-CFG-1): if no `settings` row exists, store the effective Settings so the
    live config lives in the DB before code defaults go generic. Never overwrites a row.
    v0.2.0: never persists an unconfigured (generic) row: a fresh install stays row-less until
    the Owner saves Settings, and a wrong-order deploy onto a pre-v0.1.11 DB leaves the DB
    untouched, so rolling back to v0.1.11 still restores the old defaults."""
    if not ctx.settings.thermostat.configured:
        return False
    if ctx.conn.execute("SELECT 1 FROM settings WHERE key='settings'").fetchone() is not None:
        return False
    _kv_put(ctx, "settings", ctx.settings.model_dump(mode="json"), MIGRATION_BY)
    log.info("settings: persisted effective settings (no row existed; by %s)", MIGRATION_BY)
    return True


def env_sensors(env: Env) -> dict[str, str]:
    """THERMABOT_SENSOR_* values that are set (stripped; empty ones left out)."""
    out = {k: str(getattr(env, v)).strip() for k, v in SENSOR_ENV.items()}
    return {k: v for k, v in out.items() if v}


def migrate_env_sensors(ctx: Ctx, env: Env) -> bool:
    """v0.6.1: one-time, idempotent. A stored, configured settings row written before v0.3.0
    has no `sensors` key; earlier versions filled in built-in ids at read time, which are
    gone. If THERMABOT_SENSOR_* are set, store them into that row once (only the `sensors`
    key is added; every other stored byte of value_json is kept). A row that already has
    `sensors`, no row, or no env values: nothing is written. Never touches bot_state."""
    r = ctx.conn.execute("SELECT value_json FROM settings WHERE key='settings'").fetchone()
    if r is None:
        return False
    raw = json.loads(r["value_json"])
    if not isinstance(raw, dict) or "sensors" in raw:
        return False
    vals = env_sensors(env)
    if not vals:
        return False
    new = {**raw, "sensors": {"blower": "", "compressor": "", "outdoor": "", "humidity": ""}}
    new["sensors"].update(vals)
    try:
        s = Settings.model_validate(new)
    except ValidationError as e:
        log.error("settings: THERMABOT_SENSOR_* rejected (%s); nothing stored", e.error_count())
        return False
    _kv_put(ctx, "settings", new, SENSORS_MIGRATION_BY)
    ctx.settings = s
    log.warning(
        "settings: stored HVAC sensors from THERMABOT_SENSOR_* (%s; by %s)",
        ", ".join(sorted(vals)),
        SENSORS_MIGRATION_BY,
    )
    return True


def log_sensor_status(s: Settings) -> None:
    if s.sensors_missing:
        log.warning(
            "sensors not configured: no blower/compressor/outdoor sensor is set"
            " (Settings > HVAC sensors, or THERMABOT_SENSOR_* for a pre-v0.3.0 row);"
            " the bot stays DEGRADED and makes no plan writes"
        )


def build(
    env: Env,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ha_transport: httpx.AsyncBaseTransport | None = None,
    notify_transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
    nws_transport: httpx.AsyncBaseTransport | None = None,
    open_meteo_transport: httpx.AsyncBaseTransport | None = None,
    influx_transport: httpx.AsyncBaseTransport | None = None,
    heartbeat_transport: httpx.AsyncBaseTransport | None = None,
) -> Runtime:
    """Wire DB (migrations), adapters, controller, auth, Ctx and Loops. No network I/O here."""
    data = Path(env.DATA_DIR)
    data.mkdir(parents=True, exist_ok=True)
    conn = db.open_db(data / "thermabot.db", now=clock())
    influx = Influx(
        env.INFLUX_URL,
        env.INFLUX_ORG,
        env.INFLUX_READ_BUCKET,
        env.INFLUX_WRITE_BUCKET,
        _secret(env.INFLUX_READ_TOKEN),
        _secret(env.INFLUX_WRITE_TOKEN),
        transport=influx_transport,
    )
    notifier = Notifier(
        env.ALERT_WEBHOOK_URL, _secret(env.ALERT_WEBHOOK_TOKEN), transport=notify_transport
    )
    auth = Auth(conn, bob_token=_secret(env.BOB_TOKEN), clock=clock)
    ctx = Ctx(
        conn=conn,
        auth=auth,
        clock=clock,
        heartbeat_configured=bool(env.HEARTBEAT_WEBHOOK_URL),
    )
    ctx.settings = load_settings(ctx)
    migrate_env_sensors(ctx, env)  # v0.6.1, before anything binds the settings
    log_sensor_status(ctx.settings)
    if not ctx.settings.thermostat.configured:
        log.warning(
            "setup needed: no thermostat configured (settings.thermostat.entity_id);"
            " the bot reads nothing climate and never writes"
        )
    # Both write layers (core guard via controller.settings, adapter check_write) use the
    # same configured entity; a change to settings.thermostat takes effect on restart.
    ha = HARest(
        env.HA_URL,
        env.HA_TOKEN.get_secret_value(),
        transport=ha_transport,
        writable_entity=ctx.settings.thermostat.entity_id,
    )
    controller = Controller(conn, ha, settings=ctx.settings, clock=clock)
    ctx.ha_read, ctx.ha_url = ha, env.HA_URL  # setup wizard: reads only (never the token)
    ctx.running_entity = ctx.settings.thermostat.entity_id
    load, save = slot_store(conn)
    heartbeat = HeartbeatWebhook(
        env.HEARTBEAT_WEBHOOK_URL, transport=heartbeat_transport or ha_transport
    )
    loops = Loops(
        publish=heartbeat.publish,
        state_fn=lambda: ctx.eff().state,
        clock=clock,
        sleep=sleep,
        version=__version__,
        load_slot=load,
        save_slot=save,
    )
    rt = Runtime(
        env=env,
        conn=conn,
        ha=ha,
        influx=influx,
        notifier=notifier,
        controller=controller,
        ctx=ctx,
        loops=loops,
        clock=clock,
        pending=set(),
        bg=Background(
            conn=conn, influx=influx, state_fn=lambda: ctx.eff().state, score_fn=lambda: ctx.score
        ),
        nws_transport=nws_transport,
        open_meteo_transport=open_meteo_transport,
        heartbeat=heartbeat,
    )
    loops.jobs = Jobs(
        tick=rt.tick,
        replan=rt.replan,
        refit=rt.refit_job,
        rollup=rt.rollup_job,
        backup=rt.backup,
        summary=rt.summary,
        flush=rt.bg.flush,
    )
    ctx.refit = rt.refit_now
    ctx.kill_restore = controller.kill_restore
    ctx.set_mirror = ha.set_mirror
    ctx.replan = loops.trigger_replan
    ctx.send_alert = rt.send_alert
    return rt


def create_app(env: Env | None = None, **kw: Any) -> FastAPI:
    """Fail fast on env, then wire everything; loops run only inside the lifespan."""
    rt = build(env if env is not None else load_env(), **kw)
    app = routes_api.create_app(rt.ctx)
    app.state.runtime = rt

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        persist_effective_settings(rt.ctx)  # v0.1.11 first-start row (FR-CFG-1)
        rt.spawn("efficiency-backfill", rt._safe("efficiency backfill", rt.efficiency_backfill()))
        rt.loops._stop = False
        task = asyncio.create_task(rt.loops.run(), name="thermabot-loops")
        try:
            yield
        finally:
            rt.loops.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await rt.aclose()

    app.router.lifespan_context = lifespan
    return app


def __getattr__(name: str) -> FastAPI:
    """`thermabot.app.main:app` for uvicorn: built on first access, from the environment."""
    if name == "app":
        app = create_app()
        globals()["app"] = app
        return app
    raise AttributeError(name)


__all__ = ["TICK_S", "ConfigError", "Runtime", "build", "create_app", "load_env"]
