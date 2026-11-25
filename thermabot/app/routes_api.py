"""JSON API (D§11.2). Protected routes use `require(cap)`; initial account setup uses
signed CSRF and an empty-user guard. Mutations are audited. Runtime lives in `Ctx`."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.middleware.gzip import GZipMiddleware

from thermabot import __version__
from thermabot.adapters import db
from thermabot.app import auth as A
from thermabot.app.auth import COOKIE, Auth, LoginError, Principal, require
from thermabot.app.sse import Broker, frame, stream
from thermabot.config import Settings
from thermabot.core import statemachine as sm
from thermabot.core.openings import OpeningFinding
from thermabot.core.overrides import (
    OVERRIDE_COMMAND_STATES,
    Nudge,
    create_nudge,
    override_command_key,
    override_expiry,
)
from thermabot.core.schedule import active_zone
from thermabot.core.types import (
    Forecast,
    GuestIn,
    HealthzOut,
    HistoryOut,
    KillIn,
    KillOut,
    NudgeIn,
    NudgeOut,
    OkOut,
    OverrideIn,
    OverrideOut,
    Plan,
    RestoreStatus,
    SavingsOut,
    Snapshot,
    StatusOut,
    StopBandStatus,
    UserIn,
    UserOut,
    UserPatch,
)

TICK_STALE = timedelta(minutes=3)
# Before the first tick, /healthz is "starting" rather than stale. Covers the
# startup replan (bounded at 45s) plus one tick.
START_GRACE = timedelta(seconds=90)
KILL_BUDGET_S = 5.0
MIRROR_TIMEOUT_S = 0.9
STOP_WARNING_KEY = "stop_handoff_warning"
RESTART_NOTICE_KEY = "restart_shadow_notice"
STOP_SAVE_WARNING = (
    "Stop could not be saved. Thermostat writes are paused in this process only. "
    "Check the thermostat and Home Assistant, then retry Stop."
)
log = logging.getLogger(__name__)


async def _noop_async(*_: object) -> None:
    return None


class HARead(Protocol):
    """The setup wizard's view of HA: reads only, no service call is reachable."""

    async def get_config(self) -> dict[str, Any]: ...

    async def get_all_states(self) -> list[dict[str, Any]]: ...


@dataclass
class Ctx:
    conn: sqlite3.Connection
    auth: Auth
    settings: Settings = field(default_factory=Settings)
    broker: Broker = field(default_factory=Broker)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    live: sm.Live = field(default_factory=sm.Live)
    snapshot: Snapshot | None = None
    opening_assessment: list[OpeningFinding] = field(default_factory=list)
    plan: Plan | None = None
    score: float | None = None
    last_tick: datetime | None = None
    started_at: datetime | None = None
    ha_health: str = "connecting"
    weather_health: str = "ok"
    heartbeat_configured: bool = False
    watchdog_helper: str | None = None  # input_boolean.thermabot_watchdog state
    kill_restore: Callable[[datetime, Settings], Awaitable[tuple[RestoreStatus, str]]] | None = None
    set_mirror: Callable[[bool], Awaitable[None]] = _noop_async
    replan: Callable[[], None] = lambda: None
    refit: Callable[[], Awaitable[object]] = _noop_async
    send_alert: Callable[[str], Awaitable[object]] = _noop_async
    savings: Callable[[], SavingsOut] = SavingsOut
    kill_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stop_unpersisted: bool = False
    # v0.3.0 setup wizard: read-only HA client (GET /api/config, /api/states only), the
    # configured HA_URL for display (never the token), a short discovery cache, and the
    # thermostat entity the running controller was built with (a change needs a restart).
    ha_read: HARead | None = None
    ha_url: str = ""
    running_entity: str | None = None  # None = take it from `settings` at construction
    setup_cache: dict[str, tuple[float, Any]] = field(default_factory=dict)
    # v0.4.0 weather: the last forecast the replan used (Runtime sets it) and optional
    # transports per provider for the Settings/wizard test fetch (tests mock HTTP here).
    forecast: Forecast | None = None
    weather_transports: dict[str, httpx.AsyncBaseTransport | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.running_entity is None:
            self.running_entity = self.settings.thermostat.entity_id

    def eff(self) -> sm.EffectiveState:
        live = self.live.model_copy(
            update={
                "override_open": self.open_override() is not None,
                "ha_mirror": "off" if self.stop_unpersisted else self.live.ha_mirror,
                "setup_needed": self.live.setup_needed
                or not self.settings.thermostat.configured
                or self.settings.thermostat.entity_id != self.running_entity,
            }
        )
        return sm.effective(db.read_bot_state(self.conn), live)

    def open_override(self) -> OverrideOut | None:
        r = self.conn.execute(
            "SELECT id, kind, started_at, expires_at FROM overrides WHERE ended_at IS NULL"
            " AND expires_at > ? ORDER BY id DESC LIMIT 1",
            (_iso(self.clock()),),
        ).fetchone()
        if r is None:
            return None
        data = dict(r)
        if r["kind"] in ("ui_target", "ui_mode"):
            raw = db.read_kv(self.conn, override_command_key(r["id"]))
            status, _, detail = (raw or "").partition(":")
            if status not in OVERRIDE_COMMAND_STATES:
                if raw is not None:
                    log.error("invalid override command state for id %s", r["id"])
                status, detail = "unconfirmed", ""
            data.update(command_status=status, command_detail=detail or None)
        return OverrideOut(**data)

    def audit(self, p: Principal, action: str, request: Request, **detail: object) -> None:
        ip = request.client.host if request.client else None
        db.audit(self.conn, p.username, action, dict(detail), now=self.clock(), ip=ip)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def _stored_stop_warning(c: Ctx) -> str | None:
    warning = db.read_kv(c.conn, STOP_WARNING_KEY)
    if (
        warning is None
        and c.conn.execute("SELECT 1 FROM bot_kv WHERE key=?", (STOP_WARNING_KEY,)).fetchone()
    ):
        warning = "Stop warning is unreadable. Check the thermostat before acknowledging."
    return warning or None


def stop_warning_of(c: Ctx) -> str | None:
    warning = _stored_stop_warning(c)
    if c.stop_unpersisted:
        return _append_warning(warning, STOP_SAVE_WARNING)
    return warning


def _append_warning(previous: str | None, warning: str) -> str:
    if not previous:
        return warning
    return previous if warning in previous else f"{previous} {warning}"


def ctx_of(request: Request) -> Ctx:
    c: Ctx = request.app.state.ctx
    return c


CtxD = Annotated[Ctx, Depends(ctx_of)]
View = Annotated[Principal, Depends(require("view"))]
ViewHealth = Annotated[Principal, Depends(require("view_health"))]
ViewModel = Annotated[Principal, Depends(require("view_model"))]
KillOff = Annotated[Principal, Depends(require("kill_off"))]
Override = Annotated[Principal, Depends(require("override"))]
NudgeOwn = Annotated[Principal, Depends(require("nudge_own"))]
Guest = Annotated[Principal, Depends(require("guest_toggle"))]
Comfort = Annotated[Principal, Depends(require("edit_comfort"))]
Users = Annotated[Principal, Depends(require("manage_users"))]

router = APIRouter()


# ---- read routes ---------------------------------------------------------------------
def _starting(c: Ctx, now: datetime) -> bool:
    return (
        c.last_tick is None
        and c.started_at is not None
        and timedelta(0) <= now - c.started_at <= START_GRACE
    )


@router.get("/healthz", response_model=HealthzOut)
async def healthz(c: CtxD) -> JSONResponse:
    """Public (no secrets in body). 503 when the tick is > 3 min old.

    During the startup grace, before any tick, the state is `starting` and the
    status is 200 so a probe does not treat a booting process as stale.
    """
    now = c.clock()
    fresh = c.last_tick is not None and now - c.last_tick <= TICK_STALE
    starting = _starting(c, now)
    out = HealthzOut(
        ok=fresh or starting,
        state="starting" if starting else c.eff().state,
        ha=c.ha_health,
        weather=c.weather_health,
        last_tick=c.last_tick,
        version=__version__,
    )
    return JSONResponse(out.model_dump(mode="json"), status_code=200 if fresh or starting else 503)


@router.get("/api/status", response_model=StatusOut)
async def status(c: CtxD, _: View) -> StatusOut:
    p, e, now = db.read_bot_state(c.conn), c.eff(), c.clock()
    nxt = None
    if c.plan is not None:
        nxt = next((s.reason for s in c.plan.steps if s.t > now and s.reason), None)
    snap = c.snapshot
    return StatusOut(
        state=e.state,
        label=e.label,
        ha_health=c.ha_health,
        kill_switch=p.kill_switch if p else None,
        run_mode=p.run_mode if p else None,
        shadow_days=round(sm.shadow_days(p, now), 2) if p else 0.0,
        override=c.open_override(),
        snapshot=snap.thermostat if snap else None,
        score=c.score,
        rooms=list(snap.rooms.values()) if snap else [],
        next_change=nxt,
        stop_warning=stop_warning_of(c),
        stop_retry_required=c.stop_unpersisted,
        restart_notice=db.read_kv(c.conn, RESTART_NOTICE_KEY) or None,
        stop_handoff=StopBandStatus(**c.settings.stop_handoff.model_dump()),
        display_units=c.settings.display.units,
    )


@router.get("/api/plan", response_model=Plan)
async def plan(c: CtxD, _: View) -> Plan:
    if c.plan is None:
        raise HTTPException(404, "no plan yet")
    return c.plan


class ReadyCheck(BaseModel):
    id: str
    ok: bool
    label: str
    detail: str


class ReadinessOut(BaseModel):
    ready: bool
    ready_without_watch: bool
    checks: list[ReadyCheck]
    first_write: str | None = None


def shadow_evidence(c: Ctx, st: sm.Persisted | None, now: datetime) -> tuple[bool, str]:
    """Require a bounded, tracked seven-day observation, not just an old start time."""
    from thermabot.app.jobs import LOCAL_MAX_AGE, minute_key

    start = now - timedelta(days=sm.SHADOW_MIN_DAYS)
    if (
        st is None
        or st.shadow_started_at is None
        or st.shadow_started_at.tzinfo is None
        or st.shadow_started_at > start
    ):
        days = (
            max(0.0, sm.shadow_days(st, now))
            if st and st.shadow_started_at and st.shadow_started_at.tzinfo
            else 0.0
        )
        return False, f"Needs 7 d of Watch; {days:.1f} d elapsed."
    start_key, end_key = minute_key(start), _iso(now)
    rows = c.conn.execute(
        "SELECT ts, bot_state, snapshot_json, stale_rooms FROM samples"
        " WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (start_key, end_key),
    ).fetchall()
    required = sm.SHADOW_MIN_DAYS * 24 * 60 * 95 // 100
    if len(rows) < required:
        return False, f"7 d needs at least 95% sampled minutes ({len(rows)}/{required} minimum)."
    previous: datetime | None = None
    degraded = stale = 0
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row["ts"])
            snapshot = json.loads(row["snapshot_json"])
        except (TypeError, ValueError):
            return False, "Watch evidence contains an unreadable sample."
        if stamp.tzinfo is None or stamp.second or stamp.microsecond:
            return False, "Watch evidence contains an invalid sample time."
        if (previous is None and stamp - start > LOCAL_MAX_AGE) or (
            previous is not None and not timedelta(0) < stamp - previous <= LOCAL_MAX_AGE
        ):
            return False, "Watch evidence has a sampling gap longer than 3 minutes."
        metadata = snapshot.get("_thermabot") if isinstance(snapshot, dict) else None
        if (
            row["bot_state"] != "SHADOW"
            or not isinstance(metadata, dict)
            or metadata.get("state") not in {"SHADOW", "DEGRADED", "OVERRIDE"}
        ):
            return False, "Watch evidence contains an untracked or non-Shadow sample."
        if metadata["state"] == "DEGRADED" and metadata.get("degraded_reason") not in {
            "ha",
            "weather",
            "sensors",
        }:
            return False, "Watch evidence contains a degraded sample without a known reason."
        degraded += metadata["state"] == "DEGRADED"
        stale += bool(row["stale_rooms"])
        previous = stamp
    if previous is None or not timedelta(0) <= now - previous <= LOCAL_MAX_AGE:
        return False, "Watch evidence is not current."
    write = c.conn.execute(
        "SELECT 1 FROM writes WHERE ts >= ? AND ts <= ?"
        " AND (outcome='sent' OR (outcome='failed' AND COALESCE(guard, '') != 'not_sent'))"
        " LIMIT 1",
        (start_key, end_key),
    ).fetchone()
    if write:
        return False, "A sent or unconfirmed thermostat write occurred during the Watch window."
    events = c.conn.execute(
        "SELECT action, detail_json FROM audit WHERE ts >= ? AND ts <= ?"
        " AND action IN ('kill_off', 'kill_on', 'restart_to_shadow')",
        (start_key, end_key),
    ).fetchall()
    for event in events:
        if event["action"] != "kill_on":
            return False, "Watch was interrupted during the seven-day window."
        try:
            detail = json.loads(event["detail_json"])
        except (TypeError, ValueError):
            return False, "Watch transition history is unreadable."
        if not isinstance(detail, dict) or detail.get("mode") != "shadow":
            return False, "Control was enabled during the seven-day Watch window."
    return (
        True,
        f"{len(rows)} samples; {degraded} degraded, {stale} with stale rooms. Review required.",
    )


def readiness_of(c: Ctx) -> ReadinessOut:
    """Checks enforced by the server before the Owner may enable Active."""
    from thermabot.app.controller import stop_handoff_action
    from thermabot.app.jobs import active_model, current_step
    from thermabot.app.present import action_sentence
    from thermabot.app.units import temperature_scale
    from thermabot.core.guards import GuardCtx, hard_limits
    from thermabot.core.thermal import in_range

    now = c.clock()
    st = db.read_bot_state(c.conn)
    soak_ok, soak_detail = shadow_evidence(c, st, now)
    stop_warning = stop_warning_of(c)
    s = load_settings(c)
    try:
        params, _, _ = active_model(c.conn, s.planner)
        model_ok = params is not None and in_range(params) and math.isfinite(params.sigma)
    except (ValidationError, ValueError, TypeError, AttributeError) as exc:
        log.error("active thermal model is invalid: %s", type(exc).__name__)
        model_ok = False
    snap = c.snapshot
    th = snap.thermostat if snap else None
    caps = (
        th is not None
        and temperature_scale(th.temperature_unit) is not None
        and th.target_temp_step is not None
        and math.isfinite(th.target_temp_step)
        and th.target_temp_step > 0
        and th.min_temp is not None
        and th.max_temp is not None
        and math.isfinite(th.min_temp)
        and math.isfinite(th.max_temp)
        and th.min_temp < th.max_temp
        and bool({"heat_cool", "auto"}.intersection(th.hvac_modes))
    )
    watch = (
        st is not None
        and st.kill_switch == "on"
        and st.run_mode == "shadow"
        and c.eff().state == "SHADOW"
    )
    fresh = (
        c.ha_health == "ok"
        and c.last_tick is not None
        and timedelta(0) <= now - c.last_tick <= TICK_STALE
        and th is not None
        and th.last_reported is not None
        and timedelta(0) <= now - th.last_reported <= timedelta(minutes=sm.TSTAT_MAX_AGE_MIN)
        and s.thermostat.entity_id == c.running_entity
    )
    handoff = False
    if caps and th is not None and th.min_temp is not None and th.max_temp is not None:
        action, _ = stop_handoff_action(s, th, s.thermostat.entity_id)
        if action is not None:
            guard = GuardCtx(
                kill_switch="on",
                ha_mirror="on",
                run_mode="active",
                snapshot=snap,
                settings=s,
                now=now,
                device_min_f=th.min_temp,
                device_max_f=th.max_temp,
            )
            limited, _ = hard_limits(action, guard)
            handoff = limited is not None and limited.data == action.data
    net = c.heartbeat_configured or c.watchdog_helper == "on"
    plan_ok = (
        c.plan is not None
        and bool(c.plan.steps)
        and timedelta(0) <= now - c.plan.created_at <= timedelta(hours=2)
    )
    checks = [
        ReadyCheck(
            id="watch",
            ok=watch,
            label="Watching without an active override",
            detail="Watch is running"
            if watch
            else "Start Watch and resolve any degraded state or override.",
        ),
        ReadyCheck(
            id="stop_warning",
            ok=stop_warning is None,
            label="Previous stop checked",
            detail="No unresolved stop warning"
            if stop_warning is None
            else "Check the thermostat and acknowledge the stop warning.",
        ),
        ReadyCheck(id="soak", ok=soak_ok, label="Seven-day Watch evidence", detail=soak_detail),
        ReadyCheck(
            id="model",
            ok=model_ok,
            label="Thermal model fitted",
            detail="A valid model is active" if model_ok else "No valid fitted model is active.",
        ),
        ReadyCheck(
            id="ha",
            ok=fresh,
            label="Home Assistant and thermostat current",
            detail="Recent tick and thermostat reading"
            if fresh
            else "A current HA tick and thermostat reading are required.",
        ),
        ReadyCheck(
            id="sensors",
            ok=not s.sensors_missing,
            label="Sensors configured",
            detail="Blower, compressor, or outdoor is set"
            if not s.sensors_missing
            else "No HVAC sensor is configured",
        ),
        ReadyCheck(
            id="device",
            ok=bool(caps),
            label="Device capabilities verified",
            detail="Unit, step, range, and dual-setpoint mode are known"
            if caps
            else "A dual-setpoint mode, unit, step, and range are required.",
        ),
        ReadyCheck(
            id="handoff",
            ok=handoff,
            label="Stop comfort band configured",
            detail="Owner-approved band is compatible with current device capabilities"
            if handoff
            else "Configure a stop band compatible with the device and safety limits.",
        ),
        ReadyCheck(
            id="net",
            ok=net,
            label="Safety-net configuration present",
            detail="Configuration is not proof the HA safety net works; verify it independently."
            if net
            else "Configure a heartbeat or armed watchdog; independently verify the safety net.",
        ),
        ReadyCheck(
            id="plan",
            ok=plan_ok,
            label="Recent plan available",
            detail="A recent plan is available" if plan_ok else "Wait for a current plan.",
        ),
    ]
    first: str | None = None
    if c.plan is not None:
        nxt = current_step(c.plan, now) or next(
            (step for step in c.plan.steps if step.t > now), None
        )
        if nxt is not None:
            first = action_sentence(nxt, s.display.units)
    return ReadinessOut(
        ready=all(ch.ok for ch in checks),
        ready_without_watch=all(ch.ok for ch in checks if ch.id != "soak"),
        checks=checks,
        first_write=first,
    )


@router.get("/api/readiness", response_model=ReadinessOut)
async def readiness(
    c: CtxD, _: Annotated[Principal, Depends(require("edit_safety"))]
) -> ReadinessOut:
    return readiness_of(c)


@router.post("/api/schedule/preview")
async def schedule_preview(
    body: Settings, c: CtxD, _: Annotated[Principal, Depends(require("edit_comfort"))]
) -> dict[str, str]:
    """Ask the planner how runtime would change. Nothing is saved."""
    from thermabot.core import planner

    current = load_settings(c)
    try:
        before = planner.runtime_min(c.plan) if c.plan is not None else None
        draft = c.plan
        if c.forecast is not None and c.snapshot is not None and c.snapshot.thermostat.current_temp:
            th = c.snapshot.thermostat
            draft = planner.plan(
                planner.PlanInputs(
                    now=c.clock(),
                    t0=th.current_temp,
                    current_mode=th.hvac_mode,
                    forecast=c.forecast,
                    settings=body,
                    hvac_modes=list(th.hvac_modes),
                    current_sp=(th.heat_sp, th.cool_sp),
                )
            )
        after = planner.runtime_min(draft) if draft is not None else None
    except Exception:  # noqa: BLE001 - preview never blocks the editor
        before = after = None
    if before is None or after is None:
        sentence = "Runtime preview waits for a forecast. Saving still replans."
    else:
        delta = after - before
        sentence = (
            f"Predicted runtime {delta:+.0f} min over the horizon versus the saved schedule "
            f"({current.display.units} display)."
        )
    return {"sentence": sentence}


def forecast_view(c: Ctx) -> dict[str, Any]:
    """v0.4.0: hourly forecast + 48 h plan projection (display only; never feeds the plan)."""
    from thermabot.app.jobs import active_model
    from thermabot.core.projection import project
    from thermabot.core.weather import is_stale

    s, now, fc = load_settings(c), c.clock(), c.forecast
    params, _, version = active_model(c.conn, s.planner)
    snap = c.snapshot
    th = snap.thermostat if snap else None
    cur = None if th is None else (th.hvac_mode, th.heat_sp, th.cool_sp)
    proj = project(s, now, c.plan, fc, th.current_temp if th else None, params, cur)
    units = s.display.units if s.weather.units == "auto" else s.weather.units
    return {
        "provider": s.weather.provider,
        "source": fc.source if fc else None,
        "fetched_at": fc.fetched_at if fc else None,
        "stale": fc is None or fc.stale or is_stale(fc, now, s.weather),
        "units": units,
        "model_version": version,
        "projection": proj.model_dump(mode="json"),
    }


@router.get("/api/forecast")
async def forecast_api(c: CtxD, _: View) -> dict[str, Any]:
    """Any signed-in role. Temperatures are °F; `units` is the display preference."""
    return forecast_view(c)


@router.get("/api/efficiency")
async def efficiency_api(c: CtxD, _: View, days: int = 365) -> dict[str, Any]:
    """v0.5.0, any signed-in role, read-only: per-day runtime per degree-day points, robust
    fits with 95% bands, the empty state (k/5), the efficiency index and the shadow savings
    series (simulated). Temperatures are °F."""
    from thermabot.app.efficiency import report

    return report(c.conn, c.clock(), max(1, min(days, 400)))


@router.get("/api/history", response_model=None)
async def history(
    c: CtxD,
    _: View,
    days: int = 30,
    rng: Annotated[str | None, Query(alias="range")] = None,
) -> HistoryOut | dict[str, Any]:
    """Any signed-in role, read-only. `?days=N` (1..400): daily metrics + savings (v0.1).
    `?range=24h|7d|30d` (v0.6.0): the dashboard chart series from `samples`, downsampled
    on the server to <= 500 points."""
    if rng is not None:
        from thermabot.app import history as H

        if rng not in H.RANGES:
            raise HTTPException(422, "range must be one of 24h, 7d, 30d")
        return H.series(c.conn, c.clock(), rng, load_settings(c).display.units)
    if not 1 <= days <= 400:
        raise HTTPException(422, "days must be 1..400")
    since = (c.clock().astimezone(active_zone()).date() - timedelta(days=days)).isoformat()
    rows = c.conn.execute(
        "SELECT * FROM daily_metrics WHERE day >= ? ORDER BY day", (since,)
    ).fetchall()
    return HistoryOut(days=[dict(r) for r in rows], savings=c.savings())


@router.get("/api/shadow")
async def shadow_api(c: CtxD, _: View, days: int = 30) -> dict[str, Any]:
    """v0.1.9 Shadow report: samples, model status, actual vs bot per day, 7-day verdict."""
    from thermabot.app.jobs import shadow_report

    return shadow_report(c.conn, c.clock(), max(1, min(days, 400)))


@router.get("/events")
async def events(request: Request, c: CtxD, _: View) -> StreamingResponse:
    e = c.eff()
    first = frame("state", {"state": e.state, "label": e.label})
    return StreamingResponse(
        stream(c.broker, first, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


# ---- login / logout (D§10) -------------------------------------------------------------
class LoginIn(BaseModel):
    username: str
    password: str
    trusted: bool = False


def cookie_secure(request: Request) -> bool:
    """Secure when the request URL is https.

    This matches the first-run cookie. Uvicorn rewrites the scheme from
    X-Forwarded-Proto only when started with proxy headers, which this app is
    not. The header is not read here.
    """
    return request.url.scheme == "https"


def _login_response(
    request: Request, tok: str, csrf: str, trusted: bool = False, status_code: int = 200
) -> JSONResponse:
    r = JSONResponse({"ok": True, "csrf": csrf}, status_code=status_code)
    age = int((A.SESSION_LONG if trusted else A.SESSION_SHORT).total_seconds())
    r.set_cookie(
        COOKIE,
        tok,
        httponly=True,
        secure=cookie_secure(request),
        samesite="strict",
        max_age=age,
    )
    r.headers["Cache-Control"] = "no-store"
    return r


@router.post("/login")
async def login(body: LoginIn, request: Request, c: CtxD) -> JSONResponse:
    ip = request.client.host if request.client else "?"
    try:
        tok, csrf = c.auth.login(body.username, body.password, ip=ip, trusted=body.trusted)
    except LoginError as e:
        raise HTTPException(e.status, str(e)) from e
    return _login_response(request, tok, csrf, body.trusted)


class FirstAccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    password: str = Field(min_length=A.MIN_PW, max_length=128, repr=False)
    confirm: str = Field(min_length=A.MIN_PW, max_length=128, repr=False)


@router.post("/api/first-run", status_code=201)
async def first_account(request: Request, c: CtxD) -> JSONResponse:
    if not c.auth.needs_first_account():
        raise HTTPException(409, "Initial account setup is already complete. Sign in instead.")
    if request.headers.get("sec-fetch-site") == "cross-site" or not c.auth.valid_first_run_token(
        request.cookies.get(A.FIRST_RUN_COOKIE, ""), request.headers.get(A.CSRF_HEADER, "")
    ):
        raise HTTPException(
            403, "Setup session expired or missing. Refresh the page and try again."
        )
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "Account setup requires JSON.")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 4096:
            raise HTTPException(413, "Account setup request is too large.")
        raw.extend(chunk)
    try:
        body = FirstAccountIn.model_validate_json(raw)
    except ValidationError as exc:
        errors = [
            {"loc": list(e["loc"]), "msg": e["msg"]}
            for e in exc.errors(include_input=False, include_context=False)
        ]
        raise HTTPException(422, errors) from None
    if body.password != body.confirm:
        raise HTTPException(422, "Passwords do not match.")
    ip = request.client.host if request.client else "?"
    try:
        tok, csrf = c.auth.create_first_owner(body.username, body.password, ip)
    except LoginError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    response = _login_response(request, tok, csrf, status_code=201)
    response.delete_cookie(A.FIRST_RUN_COOKIE, path=request.scope.get("root_path") or "/")
    return response


@router.post("/logout")
async def logout(request: Request, c: CtxD) -> JSONResponse:
    tok = request.cookies.get(COOKIE)
    if tok:
        c.auth.logout(tok)
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE)
    return r


# ---- kill switch (FR-KILL-1..5, D§11.2) ------------------------------------------------
@router.post("/api/kill", response_model=KillOut)
async def kill(body: KillIn, request: Request, c: CtxD, p: KillOff) -> KillOut:
    """Stop always disables the bot, even when the optional Active handoff fails."""
    async with c.kill_lock:
        return await asyncio.wait_for(_kill(body, request, c, p), KILL_BUDGET_S)


async def _kill(body: KillIn, request: Request, c: Ctx, p: Principal) -> KillOut:
    now = c.clock()
    cur = db.read_bot_state(c.conn)
    restore: RestoreStatus | None = None
    restore_detail: str | None = None
    watch_evidence: str | None = None
    if body.watch_override_reason is not None and not (body.enabled and body.mode == "active"):
        raise HTTPException(422, "A Watch exception applies only to one Control start.")
    if body.enabled:
        if not p.can("kill_on"):
            raise HTTPException(403, "needs kill_on")
        if c.stop_unpersisted:
            raise HTTPException(409, "Retry Stop before starting the bot.")
        if body.mode is None:
            raise HTTPException(422, "turning ON needs mode 'shadow' or 'active'")
        if body.mode == "active" and not p.can("set_active"):
            raise HTTPException(403, "needs set_active")
        if body.mode == "active":
            readiness = readiness_of(c)
            if not readiness.ready and not (
                body.watch_override_reason is not None and readiness.ready_without_watch
            ):
                why = "; ".join(check.detail for check in readiness.checks if not check.ok)
                c.audit(p, "kill_on_denied", request, mode=body.mode, why=why)
                raise HTTPException(409, f"Control unavailable: {why}")
            if (
                body.confirmation != "ACTIVE"
                or not body.reviewed_shadow
                or not body.reviewed_safety_net
            ):
                why = (
                    "Owner review of Watch evidence and the HA safety net, plus ACTIVE "
                    "confirmation, is required."
                )
                c.audit(p, "kill_on_denied", request, mode=body.mode, why=why)
                raise HTTPException(409, why)
        try:
            new = sm.turn_on(
                cur, body.mode, now, watch_override=body.watch_override_reason is not None
            )
        except ValueError as e:
            c.audit(p, "kill_on_denied", request, mode=body.mode, why=str(e))
            raise HTTPException(409, str(e)) from e
        if body.mode == "active":
            # Keep Watch persisted while awaiting HA, then recheck before enabling writes.
            try:
                await asyncio.wait_for(c.set_mirror(True), MIRROR_TIMEOUT_S)
            except asyncio.CancelledError:
                c.audit(p, "kill_on_denied", request, mode=body.mode, why="mirror interrupted")
                raise
            except Exception as e:  # noqa: BLE001 - an unconfirmed mirror cannot enable control
                log.warning("control enable mirror failed: %s", type(e).__name__)
                c.audit(p, "kill_on_denied", request, mode=body.mode, why="mirror unavailable")
                raise HTTPException(
                    503, "Control was not enabled; check the Home Assistant enable mirror."
                ) from e
            readiness = readiness_of(c)
            if not readiness.ready and not (
                body.watch_override_reason is not None and readiness.ready_without_watch
            ):
                why = "; ".join(check.detail for check in readiness.checks if not check.ok)
                c.audit(p, "kill_on_denied", request, mode=body.mode, why=why)
                raise HTTPException(409, f"Control unavailable: {why}")
            watch_evidence = next(check.detail for check in readiness.checks if check.id == "soak")
            new = new.model_copy(update={"shadow_started_at": None})
    else:
        previous_warning: str | None = None
        handoff_pending = False
        try:
            if c.stop_unpersisted:
                previous_warning = _stored_stop_warning(c)
                restore, restore_detail = (
                    "failed",
                    "A previous Stop could not be saved; the comfort handoff was not repeated. "
                    "Check the thermostat.",
                )
            elif cur is None or cur.kill_switch == "off" or cur.run_mode == "shadow":
                restore, restore_detail = (
                    "skipped",
                    "No thermostat write is made while stopped or watching.",
                )
            elif c.kill_restore is None:
                previous_warning = stop_warning_of(c)
                restore, restore_detail = (
                    "failed",
                    "Comfort handoff is unavailable; check the thermostat.",
                )
            else:
                previous_warning = stop_warning_of(c)
                pending = (
                    "Comfort handoff is in progress and may change the thermostat. "
                    "Check it if interrupted."
                )
                db.write_kv(
                    c.conn,
                    STOP_WARNING_KEY,
                    _append_warning(previous_warning, pending),
                    now=now,
                )
                handoff_pending = True
                restore, restore_detail = await c.kill_restore(now, load_settings(c))
        except asyncio.CancelledError:
            restore, restore_detail = (
                "failed",
                "Comfort handoff was interrupted and may have changed the thermostat. Check it.",
            )
            c.audit(
                p,
                "kill_off",
                request,
                mode=body.mode,
                restore=restore,
                restore_detail=restore_detail,
                mirror_ok=False,
                interrupted=True,
            )
            raise
        except Exception:  # noqa: BLE001 - a failed handoff must never prevent stopping
            log.exception("stop comfort handoff failed")
            restore, restore_detail = "failed", "Comfort handoff failed; check the thermostat."
        finally:
            new = sm.turn_off(cur, now).model_copy(update={"shadow_started_at": None})
            try:
                if restore == "failed":
                    warning = restore_detail or "Check the thermostat."
                    db.write_kv(
                        c.conn,
                        STOP_WARNING_KEY,
                        _append_warning(previous_warning, warning),
                        now=now,
                    )
                elif handoff_pending:
                    db.write_kv(c.conn, STOP_WARNING_KEY, previous_warning or "", now=now)
            finally:
                try:
                    db.write_bot_state(c.conn, new, by=p.username, now=now)
                except Exception as exc:  # noqa: BLE001 - fail closed if Stop cannot persist
                    c.stop_unpersisted = True
                    log.exception("stop state could not be saved; thermostat writes are paused")
                    c.broker.publish(
                        "state",
                        {
                            "state": "DISABLED",
                            "label": "DISABLED",
                            "kill_switch": cur.kill_switch if cur else "off",
                            "run_mode": cur.run_mode if cur else "shadow",
                            "stop_warning": STOP_SAVE_WARNING,
                            "stop_retry_required": True,
                        },
                    )
                    try:
                        await asyncio.wait_for(c.set_mirror(False), MIRROR_TIMEOUT_S)
                    except asyncio.CancelledError:
                        raise
                    except Exception as mirror_exc:  # noqa: BLE001 - local latch remains engaged
                        log.warning("HA enable mirror off failed: %s", type(mirror_exc).__name__)
                    raise HTTPException(503, STOP_SAVE_WARNING) from exc
                else:
                    c.stop_unpersisted = False
    if body.enabled:
        with c.conn:
            c.conn.execute("BEGIN")
            if body.watch_override_reason is not None:
                c.audit(
                    p,
                    "watch_override",
                    request,
                    reason=body.watch_override_reason,
                    watch_evidence=watch_evidence,
                )
            db.write_bot_state(c.conn, new, by=p.username, now=now)
    mirror_ok = True
    try:
        if not (body.enabled and body.mode == "active"):
            await asyncio.wait_for(c.set_mirror(new.kill_switch == "on"), MIRROR_TIMEOUT_S)
    except asyncio.CancelledError:
        if not body.enabled:
            previous = stop_warning_of(c)
            warning = (
                "Stop was interrupted before the HA enable mirror was confirmed. "
                "Check the thermostat and HA."
            )
            db.write_kv(
                c.conn,
                STOP_WARNING_KEY,
                _append_warning(previous, warning),
                now=now,
            )
            c.audit(
                p,
                "kill_off",
                request,
                mode=body.mode,
                restore=restore,
                restore_detail=restore_detail,
                mirror_ok=False,
                interrupted=True,
            )
        raise
    except Exception as e:  # noqa: BLE001 - mirror disagreement => DISABLED anyway (safe)
        log.warning("HA enable mirror update failed: %s", type(e).__name__)
        mirror_ok = False
    if mirror_ok:
        c.live = c.live.model_copy(update={"ha_mirror": new.kill_switch})
    elif not body.enabled:
        previous = stop_warning_of(c)
        warning = "HA enable mirror was not confirmed off. Check the thermostat and HA."
        db.write_kv(c.conn, STOP_WARNING_KEY, _append_warning(previous, warning), now=now)
    c.audit(
        p,
        "kill_on" if body.enabled else "kill_off",
        request,
        mode=body.mode,
        restore=restore,
        restore_detail=restore_detail,
        mirror_ok=mirror_ok,
        reviewed_shadow=body.reviewed_shadow if body.mode == "active" else None,
        reviewed_safety_net=body.reviewed_safety_net if body.mode == "active" else None,
        watch_override_reason=body.watch_override_reason,
        watch_evidence=watch_evidence,
    )
    eff = c.eff()
    c.broker.publish(
        "state",
        {
            "state": eff.state,
            "label": eff.label,
            "kill_switch": new.kill_switch,
            "run_mode": new.run_mode,
            "stop_warning": stop_warning_of(c),
            "stop_retry_required": c.stop_unpersisted,
            "restart_notice": db.read_kv(c.conn, RESTART_NOTICE_KEY) or None,
        },
    )
    return KillOut(
        state=eff.state,
        kill_switch=new.kill_switch,
        run_mode=new.run_mode,
        restore=restore,
        restore_detail=restore_detail,
        stop_warning=stop_warning_of(c),
        mirror_ok=mirror_ok,
    )


class NoticeIn(BaseModel):
    kind: Literal["stop_handoff", "restart_shadow"]


@router.post("/api/notices/acknowledge", response_model=OkOut)
async def acknowledge_notice(body: NoticeIn, request: Request, c: CtxD, p: KillOff) -> OkOut:
    key = STOP_WARNING_KEY if body.kind == "stop_handoff" else RESTART_NOTICE_KEY
    async with c.kill_lock:
        if body.kind == "stop_handoff" and c.stop_unpersisted:
            raise HTTPException(409, "Retry Stop before acknowledging the warning.")
        db.write_kv(c.conn, key, "", now=c.clock())
        c.audit(p, "notice_acknowledged", request, kind=body.kind)
        eff = c.eff()
        c.broker.publish(
            "state",
            {
                "state": eff.state,
                "label": eff.label,
                "stop_warning": stop_warning_of(c),
                "restart_notice": db.read_kv(c.conn, RESTART_NOTICE_KEY) or None,
            },
        )
    return OkOut(detail="Notice acknowledged")


# ---- override / nudge / guest ---------------------------------------------------------
@router.post("/api/override", response_model=OverrideOut)
async def override_start(body: OverrideIn, request: Request, c: CtxD, p: Override) -> OverrideOut:
    now = c.clock()
    if body.target_f is None and body.mode is None and not body.pause:
        raise HTTPException(422, "need target_f, mode or pause")
    if body.pause and (body.target_f is not None or body.mode is not None):
        raise HTTPException(422, "pause cannot be combined with a target or mode")
    if body.target_f is not None and (
        body.mode == "off"
        or (
            body.mode is None
            and c.snapshot is not None
            and c.snapshot.thermostat.hvac_mode == "off"
        )
    ):
        raise HTTPException(422, "select a heating or cooling mode before setting a target")
    try:
        exp = override_expiry(body.duration, now, c.settings.schedule, body.until)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    kind = "ui_pause" if body.pause else "ui_mode" if body.mode else "ui_target"
    command_status = None if body.pause else "pending"
    c.conn.execute("BEGIN")
    try:
        c.conn.execute(
            "UPDATE overrides SET ended_at=?, end_reason='replaced' WHERE ended_at IS NULL",
            (_iso(now),),
        )
        cur = c.conn.execute(
            "INSERT INTO overrides (kind, source, payload_json, started_at, expires_at)"
            " VALUES (?,?,?,?,?)",
            (kind, f"ui:{p.username}", body.model_dump_json(), _iso(now), _iso(exp)),
        )
        if cur.lastrowid is None:
            raise sqlite3.DatabaseError("override insert returned no id")
        override_id = int(cur.lastrowid)
        if command_status:
            db.write_kv(c.conn, override_command_key(override_id), command_status, now=now)
        c.audit(p, "override_start", request, **body.model_dump(mode="json"))
        c.conn.execute("COMMIT")
    except Exception:
        c.conn.execute("ROLLBACK")
        raise
    c.replan()
    return OverrideOut(
        id=override_id, kind=kind, started_at=now, expires_at=exp, command_status=command_status
    )


@router.delete("/api/override", response_model=OkOut)
async def override_cancel(request: Request, c: CtxD, p: Override) -> OkOut:
    n = c.conn.execute(
        "UPDATE overrides SET ended_at=?, end_reason='cancelled' WHERE ended_at IS NULL",
        (_iso(c.clock()),),
    ).rowcount
    c.audit(p, "override_cancel", request, n=n)
    c.replan()
    return OkOut(detail=f"cancelled {n}")


def _nudges(c: Ctx) -> list[tuple[int, Nudge]]:
    rows = c.conn.execute("SELECT * FROM nudges").fetchall()
    return [
        (
            r["id"],
            Nudge(
                user_id=r["user_id"],
                room=r["room"],
                delta_f=r["delta_f"],
                created_at=r["started_at"],
                expires_at=r["expires_at"],
                cancelled_at=r["ended_at"],
            ),
        )
        for r in rows
    ]


@router.post("/api/nudge", response_model=NudgeOut)
async def nudge(body: NudgeIn, request: Request, c: CtxD, p: NudgeOwn) -> NudgeOut:
    now = c.clock()
    if body.room is not None and body.room != p.room and not p.can("nudge_any"):
        raise HTTPException(403, "needs nudge_any")
    if body.room is not None and body.room not in c.settings.rooms:
        raise HTTPException(422, f"unknown room {body.room}")
    existing = _nudges(c)
    try:
        new, _ = create_nudge(
            [n for _, n in existing],
            now,
            user_id=p.user_id or 0,
            role=p.role if p.role != "service" else "viewer",
            user_room=p.room,
            delta_f=body.delta_f,
            hours=body.hours,
            room=body.room,
            cfg=c.settings.nudge,
        )
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    c.conn.execute(
        "UPDATE nudges SET ended_at=? WHERE room=? AND ended_at IS NULL AND expires_at > ?",
        (_iso(now), new.room, _iso(now)),
    )
    cur = c.conn.execute(
        "INSERT INTO nudges (user_id, room, delta_f, started_at, expires_at) VALUES (?,?,?,?,?)",
        (new.user_id, new.room, new.delta_f, _iso(now), _iso(new.expires_at)),
    )
    c.audit(p, "nudge", request, room=new.room, delta_f=new.delta_f, hours=body.hours)
    c.replan()
    return NudgeOut(
        id=int(cur.lastrowid or 0), room=new.room, delta_f=new.delta_f, expires_at=new.expires_at
    )


@router.delete("/api/nudge/{nid}", response_model=OkOut)
async def nudge_cancel(nid: int, request: Request, c: CtxD, p: NudgeOwn) -> OkOut:
    r = c.conn.execute("SELECT user_id FROM nudges WHERE id=?", (nid,)).fetchone()
    if r is None:
        raise HTTPException(404, "no such nudge")
    if r["user_id"] != p.user_id and not p.can("nudge_any"):
        raise HTTPException(403, "needs nudge_any")
    c.conn.execute(
        "UPDATE nudges SET ended_at=? WHERE id=? AND ended_at IS NULL", (_iso(c.clock()), nid)
    )
    c.audit(p, "nudge_cancel", request, id=nid)
    c.replan()
    return OkOut()


def _kv_put(c: Ctx, key: str, value: object, by: str) -> None:
    c.conn.execute(
        "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by)"
        " VALUES (?,?,?,?)",
        (key, json.dumps(value, default=str), _iso(c.clock()), by),
    )


@router.post("/api/guest", response_model=OkOut)
async def guest(body: GuestIn, request: Request, c: CtxD, p: Guest) -> OkOut:
    _kv_put(c, "guest", body.model_dump(mode="json"), p.username)
    c.audit(p, "guest", request, **body.model_dump(mode="json"))
    c.replan()
    return OkOut()


# ---- settings (D§14.3) -----------------------------------------------------------------
SAFETY_SECTIONS = ("safety", "off", "thermostat", "stop_handoff")


def load_settings(c: Ctx) -> Settings:
    r = c.conn.execute("SELECT value_json FROM settings WHERE key='settings'").fetchone()
    return c.settings if r is None else Settings.model_validate_json(r["value_json"])


def _has_row(c: Ctx) -> bool:
    return c.conn.execute("SELECT 1 FROM settings WHERE key='settings'").fetchone() is not None


def _check_thermostat_change(c: Ctx, old: Settings, new: Settings) -> None:
    if old.thermostat.entity_id == new.thermostat.entity_id:
        return
    state = db.read_bot_state(c.conn)
    if state is not None and state.kill_switch == "on" and state.run_mode == "active":
        raise HTTPException(409, "Stop Control before changing the thermostat.")


@router.get("/api/settings", response_model=Settings)
async def settings_get(c: CtxD, _: Comfort) -> Settings:
    return load_settings(c)


def _commit_settings(
    c: Ctx,
    old: Settings,
    new: Settings,
    request: Request,
    p: Principal,
    action: str,
    **detail: object,
) -> None:
    removed = set(old.rooms) - set(new.rooms)
    assigned = {
        row["room"]
        for row in c.conn.execute("SELECT DISTINCT room FROM users WHERE room IS NOT NULL")
    }
    if removed & assigned:
        raise HTTPException(
            409, "Reassign users before removing rooms: " + ", ".join(sorted(removed & assigned))
        )
    with c.conn:
        c.conn.execute("BEGIN")
        _kv_put(c, "settings", new.model_dump(mode="json"), p.username)
        if old.thermostat.entity_id != new.thermostat.entity_id:
            state = db.read_bot_state(c.conn)
            if state is not None:
                now = c.clock()
                started = now if state.kill_switch == "on" and state.run_mode == "shadow" else None
                if state.shadow_started_at != started:
                    db.write_bot_state(
                        c.conn,
                        state.model_copy(update={"shadow_started_at": started}),
                        by=p.username,
                        now=now,
                    )
        c.audit(p, action, request, **detail)
    c.settings = new
    c.replan()


def _save_settings(new: Settings, request: Request, c: Ctx, p: Principal) -> Settings:
    """Persist validated settings while the caller holds the lifecycle lock."""
    old = load_settings(c)
    touched = [s for s in SAFETY_SECTIONS if getattr(old, s) != getattr(new, s)]
    if touched and not p.can("edit_safety"):
        raise HTTPException(403, f"needs edit_safety for {', '.join(touched)}")
    _check_thermostat_change(c, old, new)
    if new == old and _has_row(c):
        return old  # v0.6.1: nothing differs -> no row write, no audit, no replan
    _commit_settings(c, old, new, request, p, "settings_save", safety=touched)
    return new


@router.put("/api/settings", response_model=Settings)
async def settings_put(body: dict[str, Any], request: Request, c: CtxD, p: Comfort) -> Settings:
    from pydantic import ValidationError

    try:
        new = Settings.model_validate(body)
    except ValidationError as e:
        errs = [{"loc": list(x["loc"]), "msg": x["msg"]} for x in e.errors()]
        raise HTTPException(422, errs) from e
    async with c.kill_lock:
        return _save_settings(new, request, c, p)


# ---- model / alerts (Owner) ------------------------------------------------------------
@router.get("/api/model")
async def model_get(c: CtxD, _: ViewModel) -> dict[str, Any]:
    r = c.conn.execute(
        "SELECT * FROM models WHERE active=1 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    fans = [dict(x) for x in c.conn.execute("SELECT * FROM fan_experiments ORDER BY id")]
    if r is None:
        return {"model": None, "fan_experiments": fans}
    return {
        "model": {
            "version": r["version"],
            "fitted_at": r["fitted_at"],
            "params": json.loads(r["params_json"]),
            "mae": json.loads(r["mae_json"]),
            "n_samples": r["n_samples"],
        },
        "fan_experiments": fans,
    }


@router.post("/api/model/refit", response_model=OkOut)
async def model_refit(request: Request, c: CtxD, p: ViewModel) -> OkOut:
    c.audit(p, "refit", request)
    asyncio.ensure_future(c.refit())  # noqa: RUF006 - fire and forget
    return OkOut(detail="refit started")


class FanExpIn(BaseModel):
    override_id: int


@router.post("/api/fan-experiment", response_model=OkOut)
async def fan_experiment(body: FanExpIn, request: Request, c: CtxD, p: ViewModel) -> OkOut:
    o = c.conn.execute(
        "SELECT started_at, ended_at FROM overrides WHERE id=?", (body.override_id,)
    ).fetchone()
    if o is None:
        raise HTTPException(404, "no such override")
    c.conn.execute(
        "INSERT INTO fan_experiments (started_at, ended_at) VALUES (?,?)",
        (o["started_at"], o["ended_at"]),
    )
    c.audit(p, "fan_experiment", request, override_id=body.override_id)
    return OkOut()


@router.post("/api/alerts/test", response_model=OkOut)
async def alerts_test(request: Request, c: CtxD, p: ViewModel) -> OkOut:
    await c.send_alert("thermabot test alert")
    c.audit(p, "alert_test", request)
    return OkOut()


# ---- health / audit (Owner + Admin) ---------------------------------------------------
@router.get("/api/health")
async def health(c: CtxD, _: ViewHealth) -> dict[str, Any]:
    start = c.clock().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    start_s, end_s = start.isoformat(), end.isoformat()
    rows = c.conn.execute(
        "SELECT outcome, readback_ms FROM writes WHERE ts >= ? AND ts < ?", (start_s, end_s)
    ).fetchall()
    lags = sorted(r["readback_ms"] for r in rows if r["readback_ms"] is not None)
    q = statistics.quantiles(lags, n=20) if len(lags) >= 2 else lags * 19
    decisions = [
        dict(r)
        for r in c.conn.execute(
            "SELECT outcome, guard, reason, COUNT(*) AS count FROM writes"
            " WHERE ts >= ? AND ts < ? GROUP BY outcome, guard, reason"
            " ORDER BY outcome, guard, reason",
            (start_s, end_s),
        )
    ]
    samples = c.conn.execute(
        "SELECT snapshot_json FROM samples WHERE ts >= ? AND ts < ?", (start_s, end_s)
    ).fetchall()
    degraded_by_reason: dict[str, int] = {}
    degraded = untracked = 0
    for row in samples:
        snapshot = json.loads(row["snapshot_json"])
        metadata = snapshot.get("_thermabot") if isinstance(snapshot, dict) else None
        if not isinstance(metadata, dict) or metadata.get("state") not in {
            "DISABLED",
            "SHADOW",
            "ACTIVE",
            "OVERRIDE",
            "DEGRADED",
        }:
            untracked += 1
            continue
        if metadata["state"] == "DEGRADED":
            degraded += 1
            reason = metadata.get("degraded_reason")
            key = reason if isinstance(reason, str) else "unknown"
            degraded_by_reason[key] = degraded_by_reason.get(key, 0) + 1
    return {
        "day_utc": start.date().isoformat(),
        "sources": {"ha": c.ha_health, "weather": c.weather_health, "last_tick": c.last_tick},
        "writes_today": {
            o: sum(1 for r in rows if r["outcome"] == o)
            for o in ("sent", "shadow", "denied", "failed")
        },
        "write_decisions_today": decisions,
        "degraded_today": {
            "sampled_minutes": len(samples),
            "degraded_minutes": degraded,
            "untracked_minutes": untracked,
            "by_reason": degraded_by_reason,
        },
        "lag_ms": {"p50": statistics.median(lags) if lags else None, "p95": q[18] if q else None},
        "watchdog_helper": c.watchdog_helper,
        "recent_errors": [
            dict(r)
            for r in c.conn.execute(
                "SELECT ts, service, reason FROM writes WHERE outcome='failed'"
                " ORDER BY id DESC LIMIT 10"
            )
        ],
    }


@router.get("/api/audit")
async def audit_rows(c: CtxD, _: ViewHealth, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    return [
        dict(r)
        for r in c.conn.execute(
            "SELECT ts, actor, ip, action, detail_json FROM audit ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    ]


# ---- users (Owner). The last Owner can't be deleted or demoted. ----------------------
def _user(c: Ctx, uid: int) -> sqlite3.Row:
    r = c.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if r is None:
        raise HTTPException(404, "no such user")
    return r  # type: ignore[no-any-return]


def _owners(c: Ctx) -> int:
    return int(c.conn.execute("SELECT COUNT(*) FROM users WHERE role='owner'").fetchone()[0])


def _out(r: sqlite3.Row) -> UserOut:
    return UserOut(id=r["id"], username=r["username"], role=r["role"], room=r["room"])


@router.get("/api/users", response_model=list[UserOut])
async def users_list(c: CtxD, _: Users) -> list[UserOut]:
    return [_out(r) for r in c.conn.execute("SELECT * FROM users ORDER BY id")]


@router.post("/api/users", response_model=UserOut, status_code=201)
async def users_create(body: UserIn, request: Request, c: CtxD, p: Users) -> UserOut:
    try:
        uid = A.create_user(
            c.conn, body.username, body.role, body.password, body.room, now=c.clock()
        )
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, "username taken") from e
    c.audit(p, "user_create", request, username=body.username, role=body.role)
    return _out(_user(c, uid))


@router.patch("/api/users/{uid}", response_model=UserOut)
async def users_patch(uid: int, body: UserPatch, request: Request, c: CtxD, p: Users) -> UserOut:
    r = _user(c, uid)
    if body.role and body.role != "owner" and r["role"] == "owner" and _owners(c) <= 1:
        raise HTTPException(409, "can't demote the last Owner")
    if body.role:
        c.conn.execute("UPDATE users SET role=? WHERE id=?", (body.role, uid))
    if "room" in body.model_fields_set:
        c.conn.execute("UPDATE users SET room=? WHERE id=?", (body.room, uid))
    if body.password:
        c.conn.execute(
            "UPDATE users SET pw_hash=?, failed_logins=0, locked_until=NULL WHERE id=?",
            (A.hash_pw(body.password), uid),
        )
        c.conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        c.audit(p, "password_reset", request, id=uid, username=r["username"])
    c.audit(
        p,
        "user_patch",
        request,
        id=uid,
        role=body.role,
        room=body.room,
        pw_reset=bool(body.password),
    )
    return _out(_user(c, uid))


class PasswordIn(BaseModel):
    current: str
    new: str = Field(min_length=A.MIN_PW)
    confirm: str


@router.post("/api/me/password", response_model=OkOut)
async def change_my_password(body: PasswordIn, request: Request, c: CtxD, p: View) -> OkOut:
    """Any signed-in user changes their own password (current + new + confirm).
    Other sessions of this user are signed out; this one stays."""
    if p.user_id is None:
        raise HTTPException(403, "service token not valid here")
    if body.new != body.confirm:
        raise HTTPException(422, "new passwords don't match")
    if body.new == body.current:
        raise HTTPException(422, "new password must differ from the current one")
    r = c.conn.execute("SELECT pw_hash FROM users WHERE id=?", (p.user_id,)).fetchone()
    if r is None or not A.verify_pw(r["pw_hash"], body.current):
        c.audit(p, "password_change_failed", request)
        raise HTTPException(400, "current password is wrong")
    c.conn.execute(
        "UPDATE users SET pw_hash=?, failed_logins=0, locked_until=NULL WHERE id=?",
        (A.hash_pw(body.new), p.user_id),
    )
    c.auth.end_other_sessions(p.user_id, request.cookies.get(COOKIE))
    c.audit(p, "password_changed", request)
    return OkOut(detail="password changed; other devices signed out")


@router.delete("/api/users/{uid}", response_model=OkOut)
async def users_delete(uid: int, request: Request, c: CtxD, p: Users) -> OkOut:
    r = _user(c, uid)
    if r["role"] == "owner" and _owners(c) <= 1:
        raise HTTPException(409, "can't delete the last Owner")
    c.conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    c.conn.execute("DELETE FROM nudges WHERE user_id=?", (uid,))
    c.conn.execute("DELETE FROM users WHERE id=?", (uid,))
    c.audit(p, "user_delete", request, id=uid, username=r["username"])
    return OkOut()


def create_app(ctx: Ctx) -> FastAPI:
    app = FastAPI(title="thermabot", version=__version__)
    app.state.ctx = ctx
    app.state.auth = ctx.auth
    app.include_router(router)
    from thermabot.app import recovery, routes_ui, setup_api  # local: they import this module

    app.include_router(setup_api.router)
    app.include_router(recovery.router)
    routes_ui.mount(app)
    # v0.6.0: gzip HTML/JSON/CSS/JS over 1 KB (Starlette built-in; SSE is never compressed)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(IngressRootPath)
    return app


class IngressRootPath:
    """v0.3.0 (packaging C): honour Home Assistant ingress. Supervisor proxies
    /api/hassio_ingress/<token>/... to us with the prefix stripped and sends it as
    X-Ingress-Path; we expose it as ASGI root_path so request.url / url_for know the external
    prefix. Templates and JS use relative URLs, so they work with or without it. A root_path
    given to uvicorn (--root-path) is kept when the header is absent.
    TODO(stage 6, add-on): trust X-Remote-User-Id/-Name/-Display-Name from Supervisor ONLY
    when running as an add-on (SUPERVISOR_TOKEN set and the request comes from the ingress
    gateway 172.30.32.2) and map them to bot roles. Until then those headers are ignored:
    auth is the bot's own session cookie."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            for k, v in scope.get("headers") or []:
                if k == b"x-ingress-path":
                    prefix = v.decode("latin-1").rstrip("/")
                    if prefix.startswith("/") and ".." not in prefix:
                        # ASGI: `path` includes `root_path` (as uvicorn --root-path does);
                        # Supervisor strips the prefix, so put it back for routing.
                        path = scope["path"]
                        if not path.startswith(prefix + "/") and path != prefix:
                            path = prefix + path
                        scope = {**scope, "root_path": prefix, "path": path}
                    break
        await self.app(scope, receive, send)
