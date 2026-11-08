"""JSON API (D§11.2). Protected routes use `require(cap)`; initial account setup uses
signed CSRF and an empty-user guard. Mutations are audited. Runtime lives in `Ctx`."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Protocol

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
from thermabot.core.schedule import TZ
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
    SavingsOut,
    Snapshot,
    StatusOut,
    UserIn,
    UserOut,
    UserPatch,
)

TICK_STALE = timedelta(minutes=3)
KILL_BUDGET_S = 5.0
MIRROR_TIMEOUT_S = 0.9
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
    ha_health: str = "connecting"
    weather_health: str = "ok"
    heartbeat_configured: bool = False
    watchdog_helper: str | None = None  # input_boolean.thermabot_watchdog state
    kill_restore: Callable[[datetime], Awaitable[str]] | None = None  # Controller.kill_restore
    set_mirror: Callable[[bool], Awaitable[None]] = _noop_async
    replan: Callable[[], None] = lambda: None
    refit: Callable[[], Awaitable[object]] = _noop_async
    send_alert: Callable[[str], Awaitable[object]] = _noop_async
    savings: Callable[[], SavingsOut] = SavingsOut
    kill_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
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
                "setup_needed": self.live.setup_needed or not self.settings.thermostat.configured,
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
@router.get("/healthz", response_model=HealthzOut)
async def healthz(c: CtxD) -> JSONResponse:
    """Public (no secrets in body). 503 when the tick is > 3 min old."""
    now = c.clock()
    fresh = c.last_tick is not None and now - c.last_tick <= TICK_STALE
    out = HealthzOut(
        ok=fresh,
        state=c.eff().state,
        ha=c.ha_health,
        weather=c.weather_health,
        last_tick=c.last_tick,
        version=__version__,
    )
    return JSONResponse(out.model_dump(mode="json"), status_code=200 if fresh else 503)


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
    )


@router.get("/api/plan", response_model=Plan)
async def plan(c: CtxD, _: View) -> Plan:
    if c.plan is None:
        raise HTTPException(404, "no plan yet")
    return c.plan


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
    since = (c.clock().astimezone(TZ).date() - timedelta(days=days)).isoformat()
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


def _login_response(
    tok: str, csrf: str, trusted: bool = False, status_code: int = 200
) -> JSONResponse:
    r = JSONResponse({"ok": True, "csrf": csrf}, status_code=status_code)
    age = int((A.SESSION_LONG if trusted else A.SESSION_SHORT).total_seconds())
    r.set_cookie(COOKIE, tok, httponly=True, samesite="strict", max_age=age)
    r.headers["Cache-Control"] = "no-store"
    return r


@router.post("/login")
async def login(body: LoginIn, request: Request, c: CtxD) -> JSONResponse:
    ip = request.client.host if request.client else "?"
    try:
        tok, csrf = c.auth.login(body.username, body.password, ip=ip, trusted=body.trusted)
    except LoginError as e:
        raise HTTPException(e.status, str(e)) from e
    return _login_response(tok, csrf, body.trusted)


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
    response = _login_response(tok, csrf, status_code=201)
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
    """OFF: restore=band + system `off` -> ONE restore write, THEN the row goes OFF (the only
    write permitted as part of disabling); restore failure never blocks OFF. ON needs a mode
    (kill_on; ACTIVE also set_active + SHADOW_MIN_DAYS of SHADOW). DB row first, then HA mirror."""
    async with c.kill_lock:
        return await asyncio.wait_for(_kill(body, request, c, p), KILL_BUDGET_S)


async def _kill(body: KillIn, request: Request, c: Ctx, p: Principal) -> KillOut:
    now = c.clock()
    cur = db.read_bot_state(c.conn)
    restore: str | None = None
    if body.enabled:
        if not p.can("kill_on"):
            raise HTTPException(403, "needs kill_on")
        if body.mode is None:
            raise HTTPException(422, "turning ON needs mode 'shadow' or 'active'")
        if body.mode == "active" and not p.can("set_active"):
            raise HTTPException(403, "needs set_active")
        try:
            new = sm.turn_on(cur, body.mode, now)
        except ValueError as e:
            c.audit(p, "kill_on_denied", request, mode=body.mode, why=str(e))
            raise HTTPException(409, str(e)) from e
    else:
        already_off = cur is None or cur.kill_switch == "off"
        if body.restore == "leave":
            restore = "skipped"
        elif already_off or c.kill_restore is None:
            restore = "skipped"
        else:
            restore = await c.kill_restore(now)
        new = sm.turn_off(cur, now)
    db.write_bot_state(c.conn, new, by=p.username, now=now)
    mirror_ok = True
    try:
        await asyncio.wait_for(c.set_mirror(new.kill_switch == "on"), MIRROR_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - mirror disagreement => DISABLED anyway (safe)
        mirror_ok = False
    if mirror_ok:
        c.live = c.live.model_copy(update={"ha_mirror": new.kill_switch})
    c.audit(
        p,
        "kill_on" if body.enabled else "kill_off",
        request,
        mode=body.mode,
        restore=restore,
        mirror_ok=mirror_ok,
    )
    e = c.eff()
    c.broker.publish("state", {"state": e.state, "label": e.label})
    return KillOut(
        state=e.state,
        kill_switch=new.kill_switch,
        run_mode=new.run_mode,
        restore=restore,
        mirror_ok=mirror_ok,
    )  # type: ignore[arg-type]


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
SAFETY_SECTIONS = ("safety", "off", "thermostat")  # changing these needs edit_safety (Owner)


def load_settings(c: Ctx) -> Settings:
    r = c.conn.execute("SELECT value_json FROM settings WHERE key='settings'").fetchone()
    return c.settings if r is None else Settings.model_validate_json(r["value_json"])


def _has_row(c: Ctx) -> bool:
    return c.conn.execute("SELECT 1 FROM settings WHERE key='settings'").fetchone() is not None


@router.get("/api/settings", response_model=Settings)
async def settings_get(c: CtxD, _: Comfort) -> Settings:
    return load_settings(c)


@router.put("/api/settings", response_model=Settings)
async def settings_put(body: dict[str, Any], request: Request, c: CtxD, p: Comfort) -> Settings:
    from pydantic import ValidationError

    try:
        new = Settings.model_validate(body)
    except ValidationError as e:
        errs = [{"loc": list(x["loc"]), "msg": x["msg"]} for x in e.errors()]
        raise HTTPException(422, errs) from e
    old = load_settings(c)
    touched = [s for s in SAFETY_SECTIONS if getattr(old, s) != getattr(new, s)]
    if touched and not p.can("edit_safety"):
        raise HTTPException(403, f"needs edit_safety for {', '.join(touched)}")
    if new == old and _has_row(c):
        return old  # v0.6.1: nothing differs -> no row write, no audit, no replan
    _kv_put(c, "settings", new.model_dump(mode="json"), p.username)
    c.settings = new
    c.audit(p, "settings_save", request, safety=touched)
    c.replan()
    return new


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
