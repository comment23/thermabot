"""Server-rendered UI (D§11.1): Jinja2 + htmx + Pico.css, vendored static assets.

Signed-in pages use the same `require(cap)` dependency as the API (401 -> /login
redirect for browsers). Pages only read; every action posts to the existing JSON API with
session CSRF, or signed first-run CSRF before an account exists. The UI never calls HA directly.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from thermabot import __version__
from thermabot.adapters import db
from thermabot.app import forms
from thermabot.app import routes_api as api
from thermabot.app.auth import COOKIE, FIRST_RUN_COOKIE, FIRST_RUN_TTL, MIN_PW, Principal, require
from thermabot.app.routes_api import Ctx, ctx_of, load_settings
from thermabot.core import statemachine as sm
from thermabot.core.overrides import nudge_active, nudges_today
from thermabot.core.schedule import TZ, block_at

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

# (href, label, cap) in header order; links are shown only when the user holds the cap.
NAV = [
    ("/", "Dashboard", "view"),
    ("/plan", "Plan", "view"),
    ("/room", "My Room", "view"),
    ("/history", "History", "view"),
    ("/forecast", "Forecast", "view"),
    ("/efficiency", "Efficiency", "view"),
    ("/shadow", "Shadow report", "view"),
    ("/settings", "Settings", "edit_comfort"),
    ("/recovery", "Backup & recovery", "edit_safety"),
    ("/setup", "Setup wizard", "edit_safety"),
    ("/model", "Model", "view_model"),
    ("/health", "Health", "view_health"),
    ("/users", "Users", "manage_users"),
    ("/account", "Account", "view"),
]


def localtime(t: datetime | str | None, fmt: str = "%a %H:%M") -> str:
    if t is None:
        return "—"
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    return t.astimezone(TZ).strftime(fmt)


templates.env.filters["localtime"] = localtime
templates.env.filters["num"] = lambda x: "—" if x is None else f"{x:g}"
# v0.3.0 ingress: every page lives at the top level, so links are written relative
# ('/plan' -> 'plan', '/' -> './') and resolve under any root path (HA ingress, a proxy).
templates.env.filters["rel"] = lambda h: h.lstrip("/") or "./"


@cache
def asset(f: str) -> str:
    """Fingerprint packaged assets once per process, including same-version releases."""
    digest = sha256((HERE / "static" / f).read_bytes()).hexdigest()[:16]
    return f"static/{f}?v={__version__}-{digest}"


templates.env.globals["asset"] = asset


class NeedLogin(Exception):
    pass


def page(cap: str) -> Callable[[Request], Awaitable[Principal]]:
    """`require(cap)` for HTML pages: no session -> redirect to /login; 403 stays 403."""
    inner = require(cap)

    async def dep(request: Request) -> Principal:
        try:
            return await inner(request)
        except HTTPException as e:
            if e.status_code == 401:
                raise NeedLogin from e
            raise

    return dep


CtxD = Annotated[Ctx, Depends(ctx_of)]
router = APIRouter(include_in_schema=False)


def render(
    request: Request, c: Ctx, p: Principal | None, name: str, charts: bool = False, **kw: Any
) -> HTMLResponse:
    st = db.read_bot_state(c.conn)
    cur = load_settings(c)
    saved = cur.thermostat.entity_id
    ctx: dict[str, Any] = {
        "p": p,
        "version": __version__,
        "csrf": p.csrf if p else None,
        "eff": c.eff(),
        "override": c.open_override(),
        "kill": st.kill_switch if st else None,
        "run_mode": st.run_mode if st else None,
        "nav": [(h, lbl) for h, lbl, cap in NAV if p and p.can(cap)],
        "path": request.url.path.removeprefix(request.scope.get("root_path", "")) or "/",
        "charts": charts,
        "now": c.clock(),
        "timezone": TZ.key,
        "restart_needed": p is not None
        and p.can("edit_comfort")
        and saved != (c.running_entity or ""),
        "saved_entity": saved,
        "running_entity": c.running_entity,
        "weather_stale": p is not None and c.weather_health != "ok",
        "ha_health": c.ha_health,
        "sensors_missing": p is not None and cur.sensors_missing,  # v0.6.1
        **kw,
    }
    return templates.TemplateResponse(request, name, ctx)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, c: CtxD) -> Response:
    if c.auth.needs_first_account():
        return RedirectResponse("first-run", 303)
    p = c.auth.session(request.cookies.get(COOKIE))
    if p is not None:
        return RedirectResponse(_after_login(c, p), 303)
    return render(request, c, None, "login.html")


def _after_login(c: Ctx, p: Principal) -> str:
    # Environment credentials are not a saved household configuration.
    configured = api._has_row(c) or load_settings(c).thermostat.configured
    return "setup" if p.can("edit_safety") and not configured else "./"


@router.get("/first-run", response_class=HTMLResponse)
async def first_run_page(request: Request, c: CtxD) -> Response:
    if not c.auth.needs_first_account():
        p = c.auth.session(request.cookies.get(COOKIE))
        return RedirectResponse(_after_login(c, p) if p else "login", 303)
    token = c.auth.first_run_token()
    response = render(request, c, None, "first_run.html", csrf=token, min_password=MIN_PW)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    response.set_cookie(
        FIRST_RUN_COOKIE,
        token,
        max_age=int(FIRST_RUN_TTL.total_seconds()),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path=request.scope.get("root_path") or "/",
    )
    return response


@router.get("/recovery", response_class=HTMLResponse)
async def recovery_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("edit_safety"))]
) -> Response:
    return render(request, c, p, "recovery.html")


def _kv(c: Ctx, key: str) -> Any:
    r = c.conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
    return None if r is None else json.loads(r["value_json"])


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("view"))]
) -> Response:
    now, st = c.clock(), db.read_bot_state(c.conn)
    s = load_settings(c)
    nxt = None
    if c.plan is not None:
        nxt = next(
            (f"{localtime(s.t)}: {s.reason}" for s in c.plan.steps if s.t > now and s.reason), None
        )
    g = _kv(c, "guest") or {}
    from thermabot.app.efficiency import report as eff_report

    try:
        eff_tile = eff_report(c.conn, now)["tile"]
    except Exception:  # noqa: BLE001 - a read-only tile never breaks the dashboard
        eff_tile = None
    return render(
        request,
        c,
        p,
        "dashboard.html",
        eff_tile=eff_tile,
        snap=c.snapshot,
        score=c.score,
        rooms=list(c.snapshot.rooms.values()) if c.snapshot else [],
        next_change=nxt,
        shadow_days=sm.shadow_days(st, now) if st else 0.0,
        shadow_min_days=sm.SHADOW_MIN_DAYS,
        settings=s,
        opening_assessment=c.opening_assessment if p.can("edit_safety") else [],
        guest_on=bool(g.get("on")),
        charts=True,
        units=s.display.units,
    )


@router.get("/plan", response_class=HTMLResponse)
async def plan_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("view"))]
) -> Response:
    """P2: chart + HVAC bar track + presence/sleep track + change table (D§11.1)."""
    s = load_settings(c)
    steps = c.plan.steps if c.plan else []
    bars: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    changes = []
    prev: tuple[object, ...] | None = None
    for st in steps:
        kind = "fan" if st.hvac_mode == "off" and st.fan_mode == "on" else st.hvac_mode
        key = (st.hvac_mode, st.heat_sp, st.cool_sp, st.fan_mode)
        if key != prev:
            bars.append({"kind": kind, "t": st.t, "n": 1})
            changes.append(st)
        else:
            bars[-1]["n"] += 1
        prev = key
        blk = block_at(s.schedule, st.t)
        if blocks and blocks[-1]["block"] == blk:
            blocks[-1]["n"] += 1
        else:
            blocks.append({"block": blk, "t": st.t, "n": 1})
    est = sorted(k for k, r in (c.snapshot.rooms.items() if c.snapshot else []) if r.estimated)
    data = {
        "t": [x.t.timestamp() for x in steps],
        "pred_t": [x.pred_t for x in steps],
        "heat_sp": [x.heat_sp for x in steps],
        "cool_sp": [x.cool_sp for x in steps],
        "rooms": {
            k: [x.pred_rooms.get(k) for x in steps]
            for k in sorted({k for x in steps for k in x.pred_rooms})
        },
        "estimated": est,
    }
    return render(
        request,
        c,
        p,
        "plan.html",
        charts=True,
        plan=c.plan,
        bars=bars,
        blocks=blocks,
        changes=changes,
        total=max(len(steps), 1),
        data=json.dumps(data),
    )


def forecast_chart(hours: list[dict[str, Any]], units: str) -> dict[str, Any]:
    """v0.6.0: columnar data for the uPlot Forecast chart (same /api/forecast projection).
    Temperatures are converted to the display units here; runtime stays in minutes."""

    def conv(f: float | None) -> float | None:
        if f is None:
            return None
        return round((f - 32) * 5 / 9, 1) if units == "C" else f

    def ts(t: Any) -> float:
        return (datetime.fromisoformat(t) if isinstance(t, str) else t).timestamp()

    return {
        "units": units,
        "t": [ts(h["t"]) for h in hours],
        "outdoor": [conv(h["temp_f"]) for h in hours],
        "indoor": [conv(h["indoor_f"]) for h in hours],
        "heat_sp": [conv(h["heat_sp"]) for h in hours],
        "cool_sp": [conv(h["cool_sp"]) for h in hours],
        "heat_min": [h["heat_min"] or 0 for h in hours],
        "cool_min": [h["cool_min"] or 0 for h in hours],
    }


@router.get("/forecast", response_class=HTMLResponse)
async def forecast_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("view"))]
) -> Response:
    """v0.4.0: 48 h forecast + plan projection (GET /api/forecast); uPlot chart (v0.6.0)
    and table."""
    d = api.forecast_view(c)
    hours = d["projection"]["hours"]
    units = d["units"]

    def disp(f: float | None) -> str:
        if f is None:
            return "-"
        return f"{(f - 32) * 5 / 9:.1f}" if units == "C" else f"{f:g}"

    return render(
        request,
        c,
        p,
        "forecast.html",
        d=d,
        proj=d["projection"],
        hours=hours,
        units=units,
        disp=disp,
        charts=bool(hours),
        chart=forecast_chart(hours, units),
    )


def scatter_chart(curve: dict[str, Any], season: str) -> dict[str, Any] | None:
    """v0.6.0: uPlot data for one season's scatter (runtime per degree-day vs mean outdoor
    °F) with the fitted line and its 95% band (same /api/efficiency report)."""
    pts = curve["points"]
    if not pts:
        return None
    fit = curve.get("fit")
    return {
        "season": season,
        "points": [{"x": p["x"], "y": p["y"], "day": p["day"]} for p in pts],
        "band": fit["band"] if fit else [],
    }


def savings_chart(s: dict[str, Any]) -> dict[str, Any] | None:
    """v0.6.0: uPlot data for per-day saved minutes (+/-) and the cumulative line."""
    days = [d for d in s["days"] if d["saved_min"] is not None]
    if not days:
        return None
    return {
        "t": [datetime.fromisoformat(d["day"]).replace(tzinfo=TZ).timestamp() for d in days],
        "day": [d["day"] for d in days],
        "saved": [d["saved_min"] for d in days],
        "cum": [d["cum_saved_min"] for d in days],
    }


@router.get("/efficiency", response_class=HTMLResponse)
async def efficiency_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("view"))]
) -> Response:
    """v0.5.0: efficiency curve per season + shadow savings over time (GET /api/efficiency)."""
    from thermabot.app.efficiency import report

    r = report(c.conn, c.clock())
    return render(
        request,
        c,
        p,
        "efficiency.html",
        r=r,
        charts=True,
        charts_eff={k: scatter_chart(r[k], k) for k in ("heat", "cool")},
        sav_chart=savings_chart(r["savings"]),
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("edit_comfort"))]
) -> Response:
    """P3 (v0.3.0): forms per section, saved via PUT /api/settings (errors shown per field).
    The Owner also gets a read-only JSON view under 'Advanced'."""
    s = load_settings(c)
    return render(
        request,
        c,
        p,
        "settings.html",
        settings=s,
        settings_json=s.model_dump_json(indent=2),
        sections=forms.sections(s, owner=p.can("edit_safety")),
        units=s.display.units,
    )


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("edit_safety"))]
) -> Response:
    """v0.3.0 first-run wizard (Owner). Reads HA through /api/setup/*; never writes to HA."""
    s = load_settings(c)
    return render(
        request,
        c,
        p,
        "setup.html",
        settings=s,
        settings_json=s.model_dump_json(),
        ha_url=c.ha_url,
    )


SUMMARY_KEY = "summary_latest"  # bot_kv: rendered text of the last weekly summary sent


@router.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("view"))], days: int = 30
) -> Response:
    """P4: daily metrics table + runtime chart + savings + latest weekly summary (D§12.3)."""
    days = max(1, min(days, 400))
    since = (c.clock().astimezone(TZ).date() - timedelta(days=days)).isoformat()
    rows = [
        dict(r)
        for r in c.conn.execute("SELECT * FROM daily_metrics WHERE day >= ? ORDER BY day", (since,))
    ]
    data = {
        "t": [datetime.fromisoformat(r["day"]).replace(tzinfo=TZ).timestamp() for r in rows],
        "heat": [(r["heat_min"] or 0) / 60 for r in rows],
        "cool": [(r["cool_min"] or 0) / 60 for r in rows],
    }
    return render(
        request,
        c,
        p,
        "history.html",
        charts=bool(rows),
        rows=rows,
        days=days,
        savings=c.savings(),
        summary=db.read_kv(c.conn, SUMMARY_KEY),
        data=json.dumps(data),
    )


@router.get("/shadow", response_class=HTMLResponse)
async def shadow_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("view"))]
) -> Response:
    """v0.1.9: is SHADOW worth it? Measured actual vs simulated bot, per day + 7-day verdict.
    Its own page under More (not a Home card): a per-day table does not fit the hero card."""
    from thermabot.app.jobs import shadow_report

    return render(request, c, p, "shadow.html", r=shadow_report(c.conn, c.clock()))


@router.get("/model", response_class=HTMLResponse)
async def model_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("view_model"))]
) -> Response:
    """P5: active model params, MAE, room offsets, fan experiments; refit via the API."""
    r = c.conn.execute(
        "SELECT * FROM models WHERE active=1 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    model = (
        None
        if r is None
        else {
            "version": r["version"],
            "fitted_at": r["fitted_at"],
            "n_samples": r["n_samples"],
            "params": json.loads(r["params_json"]),
            "mae": json.loads(r["mae_json"]),
        }
    )
    params = dict(model["params"]) if model else {}
    offsets = params.pop("offsets", {}) if isinstance(params.get("offsets"), dict) else {}
    fans = [dict(x) for x in c.conn.execute("SELECT * FROM fan_experiments ORDER BY id")]
    s = load_settings(c)
    return render(
        request,
        c,
        p,
        "model.html",
        model=model,
        params=params,
        offsets=offsets,
        fans=fans,
        mae_ok={"tstat": s.planner.mae_ok_tstat_f, "room": s.planner.mae_ok_room_f},
        min_gain=s.fan.min_proven_gain_f,
    )


@router.get("/health", response_class=HTMLResponse)
async def health_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("view_health"))]
) -> Response:
    """P6: same data as GET /api/health + /api/audit, plus Life360/presence sources."""
    h = await api.health(c, p)
    rows = await api.audit_rows(c, p, limit=50)
    pres = c.snapshot.presence if c.snapshot else None
    return render(
        request,
        c,
        p,
        "health.html",
        h=h,
        audit=rows,
        people=pres.people if pres else {},
        tick=c.last_tick,
    )


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("manage_users"))]
) -> Response:
    """P7: list + create/patch/delete through /api/users (last Owner protected there)."""
    users = await api.users_list(c, p)
    return render(request, c, p, "users.html", users=users, rooms=sorted(c.settings.rooms))


@router.get("/account", response_class=HTMLResponse)
async def account_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("view"))]
) -> Response:
    """Every role: change your own password (POST /api/me/password)."""
    return render(request, c, p, "account.html")


@router.get("/room", response_class=HTMLResponse)
async def room_page(
    request: Request,
    c: CtxD,
    p: Annotated[Principal, Depends(page("view"))],
    room: str | None = None,
) -> Response:
    """P8: own room (members) or any room (nudge_any). Nudges go through POST /api/nudge."""
    if room is not None and room != p.room and not p.can("nudge_any"):
        raise HTTPException(403, "members can only view their own room")
    room = room or p.room
    if room is not None and room not in c.settings.rooms:
        raise HTTPException(404, "unknown room")
    now = c.clock()
    nudges = api._nudges(c)
    active = [(i, n) for i, n in nudges if n.room == room and nudge_active(n, now)]
    used = nudges_today([n for _, n in nudges], p.user_id or 0, now)
    reading = c.snapshot.rooms.get(room) if c.snapshot and room else None
    return render(
        request,
        c,
        p,
        "room.html",
        room=room,
        reading=reading,
        active=active,
        used=used,
        cfg=c.settings.nudge,
        can_nudge=p.can("nudge_own"),
        rooms=sorted(c.settings.rooms) if p.can("nudge_any") else [],
    )


class CachedStatic(StaticFiles):
    """Content-versioned assets can be cached for a year; unversioned ones are revalidated."""

    async def get_response(self, path: str, scope: Any) -> Response:
        resp = await super().get_response(path, scope)
        if resp.status_code == 200 and b"v=" in scope.get("query_string", b""):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


def mount(app: FastAPI) -> None:
    app.mount("/static", CachedStatic(directory=HERE / "static"), name="static")
    app.include_router(router)

    @app.exception_handler(NeedLogin)
    async def _to_login(request: Request, _: NeedLogin) -> Response:
        return RedirectResponse("login", 303)  # relative: works under an ingress root path
