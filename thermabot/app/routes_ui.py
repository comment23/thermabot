"""Server-rendered UI (D§11.1): Jinja2 + htmx + Pico.css, vendored static assets.

Signed-in pages use the same `require(cap)` dependency as the API (401 -> /login
redirect for browsers). Pages only read; every action posts to the existing JSON API with
session CSRF, or signed first-run CSRF before an account exists. The UI never calls HA directly.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from thermabot import __version__
from thermabot.adapters import db
from thermabot.app import forms, history, room_insights, timeline
from thermabot.app import routes_api as api
from thermabot.app.auth import COOKIE, FIRST_RUN_COOKIE, FIRST_RUN_TTL, MIN_PW, Principal, require
from thermabot.app.present import action_sentence
from thermabot.app.routes_api import Ctx, ctx_of, load_settings
from thermabot.app.units import Units, f_to_c, format_temp
from thermabot.core import explain as explain_plan
from thermabot.core import statemachine as sm
from thermabot.core.explain import GUARD_LABELS, coldest_room, infer_factors
from thermabot.core.guards import evaluate
from thermabot.core.overrides import nudge_active, nudges_today
from thermabot.core.schedule import active_zone, block_at
from thermabot.core.types import PlanStep, RoomReading
from thermabot.core.weather import forecast_season

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

# (href, label, cap) — every signed-in page the layout tests visit. The tab bar is TABS.
NAV = [
    ("/", "Home", "view"),
    ("/plan", "Plan", "view"),
    ("/room", "My Room", "view"),
    ("/insights", "Insights", "view"),
    ("/history", "History", "view"),
    ("/forecast", "Forecast", "view"),
    ("/efficiency", "Efficiency", "view"),
    ("/shadow", "Shadow report", "view"),
    ("/settings", "Settings", "edit_comfort"),
    ("/settings/schedule", "Schedule", "edit_comfort"),
    ("/settings/safety", "Safety & control", "edit_safety"),
    ("/settings/ha", "Home Assistant", "edit_safety"),
    ("/settings/people", "People & rooms", "edit_comfort"),
    ("/settings/advanced", "Advanced", "edit_comfort"),
    ("/recovery", "Backup & recovery", "edit_safety"),
    ("/setup", "Setup wizard", "edit_safety"),
    ("/model", "Model", "view_model"),
    ("/health", "Health", "view_health"),
    ("/users", "Users", "manage_users"),
    ("/account", "Account", "view"),
]

# (href, label, cap, section). A member only sees Home and Insights.
TABS = [
    ("/", "Home", "view", "home"),
    ("/plan", "Plan", "view", "plan"),
    ("/insights", "Insights", "view", "insights"),
    ("/settings", "Settings", "edit_comfort", "settings"),
]
INSIGHT_PATHS = {"/insights", "/history", "/forecast", "/efficiency", "/shadow", "/model"}
TAB_ICONS = {"/": "⌂", "/plan": "◷", "/insights": "▤", "/settings": "⚙"}


def localtime(t: datetime | str | None, fmt: str = "%a %H:%M") -> str:
    if t is None:
        return "—"
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    return t.astimezone(active_zone()).strftime(fmt)


def _temp(value: object, units: str = "F", delta: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "—"
    unit: Units = "C" if units == "C" else "F"
    return format_temp(float(value), unit, delta=delta)


def _temp_num(value: object, units: str = "F") -> str:
    """The display number only, for inputs that must not include the unit suffix."""
    text = _temp(value, units)
    return text[:-2] if text.endswith(("°F", "°C")) else text


templates.env.filters["localtime"] = localtime
templates.env.filters["num"] = lambda x: "—" if x is None else f"{x:g}"
templates.env.filters["temp"] = _temp
templates.env.filters["tempnum"] = _temp_num
# v0.3.0 ingress: links are relative ('/plan' -> 'plan', '/' -> './'). Nested pages prefix
# them with `up` (`../`) so they still resolve under any root path.
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


def _path_of(request: Request) -> str:
    return request.url.path.removeprefix(request.scope.get("root_path", "")) or "/"


def _tabs(p: Principal | None) -> list[tuple[str, str, str]]:
    if p is None:
        return []
    out: list[tuple[str, str, str]] = []
    for href, label, cap, section in TABS:
        if not p.can(cap):
            continue
        if p.role == "member" and section not in ("home", "insights"):
            continue
        out.append((href, label, section))
    return out


def _section(path: str) -> str:
    if path in INSIGHT_PATHS:
        return "insights"
    if path == "/plan" or path.startswith("/plan/"):
        return "plan"
    settings_paths = {"/recovery", "/setup", "/health", "/users", "/account"}
    if path.startswith("/settings") or path in settings_paths:
        return "settings"
    return "home"


def render(
    request: Request, c: Ctx, p: Principal | None, name: str, charts: bool = False, **kw: Any
) -> HTMLResponse:
    st = db.read_bot_state(c.conn)
    cur = load_settings(c)
    saved = cur.thermostat.entity_id
    eff = c.eff()
    path = _path_of(request)
    depth = 0 if path == "/" else path.strip("/").count("/")
    embed = request.query_params.get("embed") == "1" or request.cookies.get("tb_embed") == "1"
    mode = _mode_view(eff, st.kill_switch if st else None, st.run_mode if st else None)
    ctx: dict[str, Any] = {
        "p": p,
        "version": __version__,
        "csrf": p.csrf if p else None,
        "eff": eff,
        "override": c.open_override(),
        "kill": st.kill_switch if st else None,
        "run_mode": st.run_mode if st else None,
        "nav": _tabs(p),
        "icons": TAB_ICONS,
        "section": _section(path),
        "task": path.rsplit("/", 1)[-1],
        "tasks": [
            (key, forms.TASK_TITLES[key])
            for key in ("comfort", "schedule", "safety", "ha", "people", "advanced")
            if p is not None and p.can("edit_comfort") and (key != "safety" or p.can("edit_safety"))
        ],
        "path": path,
        "up": "../" * depth,
        "charts": charts,
        "now": c.clock(),
        "timezone": active_zone().key,
        "restart_needed": p is not None
        and p.can("edit_comfort")
        and saved != (c.running_entity or ""),
        "saved_entity": saved,
        "running_entity": c.running_entity,
        "weather_stale": p is not None and c.weather_health != "ok",
        "weather_health": c.weather_health,
        "ha_health": c.ha_health,
        "stop_warning": api.stop_warning_of(c) if p else None,
        "stop_retry_required": c.stop_unpersisted,
        "restart_notice": db.read_kv(c.conn, api.RESTART_NOTICE_KEY) if p else None,
        "sensors_missing": p is not None and cur.sensors_missing,  # v0.6.1
        "units": cur.display.units,
        "shadow_days": sm.shadow_days(st, c.clock()) if st else 0.0,
        "shadow_min_days": sm.SHADOW_MIN_DAYS,
        "mode": mode,
        "embed": embed,
        "guest_on": bool((_kv(c, "guest") or {}).get("on")),
        **kw,
    }
    response = templates.TemplateResponse(request, name, ctx)
    if request.query_params.get("embed") == "1":
        response.set_cookie(
            "tb_embed",
            "1",
            httponly=False,
            samesite="lax",
            path=request.scope.get("root_path") or "/",
        )
    return response


def _mode_view(eff: sm.EffectiveState, kill: str | None, run_mode: str | None) -> dict[str, str]:
    if eff.label == "SETUP NEEDED":
        return {"key": "setup", "text": "Setup needed", "degraded": ""}
    if kill != "on" or eff.base == "DISABLED":
        return {"key": "stopped", "text": "Stopped", "degraded": ""}
    text = "Active" if (run_mode == "active" or eff.base == "ACTIVE") else "Shadow"
    return {
        "key": "active" if text == "Active" else "shadow",
        "text": text,
        "degraded": eff.degraded_reason or "",
    }


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
        secure=api.cookie_secure(request),
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


def _model_label(c: Ctx) -> str:
    row = c.conn.execute(
        "SELECT version, mae_json FROM models WHERE active=1 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "not fitted"
    mae = json.loads(row["mae_json"]).get("thermostat")
    err = f", error {mae:.2f}" if isinstance(mae, int | float) else ""
    return f"v{row['version']}{err}"


def _house_label(c: Ctx) -> str:
    pres = c.snapshot.presence if c.snapshot else None
    if pres is None:
        return "occupied"
    return pres.house.value.lower()


def _explained(c: Ctx, step: PlanStep) -> PlanStep:
    if step.checks and step.factors:
        return step
    s = load_settings(c)
    now = c.clock()
    st = db.read_bot_state(c.conn)
    snap = c.snapshot
    dev_min = snap.thermostat.min_temp if snap and snap.thermostat.min_temp is not None else 50.0
    dev_max = snap.thermostat.max_temp if snap and snap.thermostat.max_temp is not None else 90.0
    from thermabot.core.guards import GuardCtx

    ctx = GuardCtx(
        kill_switch=None if st is None else st.kill_switch,
        ha_mirror=c.live.ha_mirror,
        run_mode="shadow" if st is None else st.run_mode,
        override_open=c.open_override() is not None,
        snapshot=snap,
        settings=s,
        now=now,
        device_min_f=dev_min,
        device_max_f=dev_max,
    )
    block = block_at(s.schedule, step.t)
    factors = infer_factors(
        step,
        block=block,
        forecast=c.forecast,
        house=_house_label(c),
        model_label=_model_label(c),
        now=now,
    )
    action = explain_plan.action_for(step, s.thermostat.entity_id)
    return step.model_copy(
        update={
            "factors": factors,
            "decision": evaluate(action, ctx),
            "checks": explain_plan.guard_chain(action, ctx),
        }
    )


def _step_view(c: Ctx, step: PlanStep, index: int) -> dict[str, Any]:
    shown = _explained(c, step)
    units: Units = load_settings(c).display.units
    cold = coldest_room(shown)
    return {
        "i": index,
        "step": shown,
        "sentence": action_sentence(shown, units),
        "reasons": [f.text for f in shown.factors[:3]],
        "coldest": None if cold is None else {"room": cold[0], "temp": format_temp(cold[1], units)},
        "checks": [(GUARD_LABELS.get(ch.guard, ch.guard), ch.ok, ch.note) for ch in shown.checks],
    }


def home_view(c: Ctx, p: Principal) -> dict[str, Any]:
    now = c.clock()
    s = load_settings(c)
    steps = c.plan.steps if c.plan else []
    nxt_i = next((i for i, step in enumerate(steps) if step.t > now), None)
    nxt = None if nxt_i is None else _step_view(c, steps[nxt_i], nxt_i)
    win = timeline.window(steps, now)
    member = p.role == "member"
    rooms = list(c.snapshot.rooms.values()) if c.snapshot else []
    seen = {room.key for room in rooms}
    rooms.extend(RoomReading(key=key, temp=None) for key in s.rooms if key not in seen)
    if member and p.room:
        rooms = [r for r in rooms if r.key == p.room]
    room_groups: dict[str, list[RoomReading]] = {}
    for room in rooms:
        config = s.rooms.get(room.key)
        floor = config.floor if config and config.floor else "Unassigned floor"
        room_groups.setdefault(floor, []).append(room)
    snap = c.snapshot
    stale_tstat = False
    if snap is not None and snap.thermostat.last_reported is not None:
        stale_tstat = (now - snap.thermostat.last_reported).total_seconds() > 10 * 60
    elif snap is not None and snap.thermostat.current_temp is None:
        stale_tstat = False
    return {
        "settings": s,
        "snap": snap,
        "score": c.score,
        "rooms": rooms,
        "room_groups": room_groups,
        "next_step": nxt,
        "next_change": None if nxt is None else nxt["step"].reason or nxt["sentence"],
        "timeline_svg": timeline.svg(win, s, s.display.units),
        "timeline_rows": timeline.table_rows(win, s.display.units),
        "timeline_summary": timeline.summary(win, s.display.units),
        "opening_assessment": c.opening_assessment if p.can("edit_safety") else [],
        "member": member,
        "stale_tstat": stale_tstat,
        "plan_eta": "The first replan runs at startup." if c.plan is None else "",
        "eff_tile": _eff_tile(c),
    }


def _eff_tile(c: Ctx) -> str:
    try:
        from thermabot.app.efficiency import report

        return str(report(c.conn, c.clock())["tile"]["text"])
    except Exception:  # noqa: BLE001 - a read-only tile never breaks Home
        return "collecting data 0/5"


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("view"))]
) -> Response:
    return render(request, c, p, "dashboard.html", **home_view(c, p))


@router.get("/partials/{name}", response_class=HTMLResponse)
async def partial(
    name: str, request: Request, c: CtxD, p: Annotated[Principal, Depends(page("view"))]
) -> Response:
    if name not in {"now", "next", "timeline", "rooms", "quick"}:
        raise HTTPException(404, "unknown partial")
    return render(request, c, p, f"partials/{name}.html", up="", **home_view(c, p))


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
        "units": s.display.units,
        "step_s": s.planner.step_min * 60,
    }
    numbered: list[tuple[int, int, PlanStep]] = []
    seen: tuple[object, ...] | None = None
    n = 0
    for i, step in enumerate(steps):
        key = (step.hvac_mode, step.heat_sp, step.cool_sp, step.fan_mode)
        if key != seen:
            n += 1
            numbered.append((n, i, step))
        seen = key
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
        numbered=[(n, i, action_sentence(step, s.display.units), step) for n, i, step in numbered],
        total=max(len(steps), 1),
        data=json.dumps(data),
    )


@router.get("/plan/{index}/why", response_class=HTMLResponse)
async def plan_why(
    index: int, request: Request, c: CtxD, p: Annotated[Principal, Depends(page("view"))]
) -> Response:
    steps = c.plan.steps if c.plan else []
    if index < 0 or index >= len(steps):
        raise HTTPException(404, "no such plan step")
    view = _step_view(c, steps[index], index)
    name = "partials/why.html" if request.headers.get("HX-Request") else "why.html"
    return render(request, c, p, name, why=view, plan=c.plan)


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
        "t": [
            datetime.fromisoformat(d["day"]).replace(tzinfo=active_zone()).timestamp() for d in days
        ],
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


def _settings_ctx(c: Ctx, p: Principal, task: str) -> dict[str, Any]:
    s = load_settings(c)
    only = forms.TASKS.get(task)
    ready = api.readiness_of(c) if task == "safety" else None
    since = (c.clock() - timedelta(hours=24)).astimezone(UTC).isoformat()
    recent: list[dict[str, Any]] = []
    if task == "safety":
        recent = [
            dict(r)
            for r in c.conn.execute(
                "SELECT ts, outcome, guard, reason FROM writes"
                " WHERE ts >= ? AND outcome IN ('shadow', 'denied') ORDER BY id DESC LIMIT 40",
                (since,),
            )
        ]
    return {
        "settings": s,
        "settings_json": s.model_dump_json(indent=2),
        "sections": forms.sections(s, owner=p.can("edit_safety"), only=only),
        "task": task,
        "task_title": forms.TASK_TITLES.get(task, "Settings"),
        "readiness": ready,
        "recent_decisions": recent,
        "assigned_rooms": [
            row["room"]
            for row in c.conn.execute("SELECT DISTINCT room FROM users WHERE room IS NOT NULL")
        ],
    }


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("edit_comfort"))]
) -> Response:
    """Simple preferences are drafts until previewed and explicitly applied."""
    from thermabot.core.preferences import FLEXIBILITY, saved_priority, selected_rooms

    s = load_settings(c)
    return render(
        request,
        c,
        p,
        "simple_settings.html",
        **_settings_ctx(c, p, "comfort"),
        preference_fingerprint=room_insights.fingerprint(s),
        priority=saved_priority(s),
        flexibility=FLEXIBILITY,
        ideal_display=f_to_c(s.insights.ideal_temp_f)
        if s.display.units == "C"
        else s.insights.ideal_temp_f,
        home_rooms=selected_rooms(s, "home"),
        sleep_rooms=selected_rooms(s, "sleep"),
        model_label=_model_label(c),
    )


@router.get("/settings/{task}", response_class=HTMLResponse)
async def settings_task(
    task: str, request: Request, c: CtxD, p: Annotated[Principal, Depends(page("edit_comfort"))]
) -> Response:
    if task == "schedule":
        s = load_settings(c)
        kw = _settings_ctx(c, p, "comfort")
        kw["task"] = "schedule"
        kw["task_title"] = "Schedule"
        return render(request, c, p, "schedule.html", **kw, schedule=s.schedule, bands=s.bands)
    if task == "comfort":
        return await settings_page(request, c, p)
    if task not in forms.TASKS:
        raise HTTPException(404, "unknown settings page")
    if task == "safety" and not p.can("edit_safety"):
        raise HTTPException(403, "needs edit_safety")
    name = "safety.html" if task == "safety" else "settings.html"
    return render(request, c, p, name, **_settings_ctx(c, p, task))


@router.get("/insights", response_class=HTMLResponse)
async def insights_page(
    request: Request, c: CtxD, p: Annotated[Principal, Depends(page("view"))]
) -> Response:
    return await history_page(request, c, p)


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
    since = (c.clock().astimezone(active_zone()).date() - timedelta(days=days)).isoformat()
    rows = [
        dict(r)
        for r in c.conn.execute("SELECT * FROM daily_metrics WHERE day >= ? ORDER BY day", (since,))
    ]
    data = {
        "t": [
            datetime.fromisoformat(r["day"]).replace(tzinfo=active_zone()).timestamp() for r in rows
        ],
        "heat": [(r["heat_min"] or 0) / 60 for r in rows],
        "cool": [(r["cool_min"] or 0) / 60 for r in rows],
    }
    return render(
        request,
        c,
        p,
        "history.html",
        charts=True,
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
    range: Literal["24h", "7d", "30d"] = "24h",
    view: Literal["overview", "temperatures", "efficiency", "tools"] = "overview",
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
    settings = load_settings(c)
    season = forecast_season(c.forecast, now, settings.season, settings.weather) or "shoulder"
    report = history.room_report(c.conn, now, range, room, settings, season) if room else None
    insights = room_insights.report(c.conn, now, room, settings) if room else None
    readiness = (
        room_insights.setup_readiness(c.conn, now, room, settings, c.snapshot, c.forecast, insights)
        if room and insights
        else None
    )
    room_chart = None
    if report is not None and view == "temperatures":
        rows = report["rows"]
        room_chart = {
            "t": [(r["start"].timestamp() + r["end"].timestamp()) / 2 for r in rows],
            "temp": [r["temp"] for r in rows],
            "indoor": [r.get("indoor") for r in rows],
            "start": rows[0]["start"].timestamp(),
            "end": rows[-1]["end"].timestamp(),
            "ideal": settings.insights.ideal_temp_f,
            "tolerance": settings.insights.tolerance_f,
            "units": settings.display.units,
        }
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
        report=report,
        season=season,
        floor=settings.rooms[room].floor if room else "",
        insights=insights,
        setback=settings.insights.setback_f,
        room_view=view,
        range_label={"24h": "24 h", "7d": "7 days", "30d": "30 days"}[range],
        readiness=readiness,
        room_chart=room_chart,
        charts=room_chart is not None,
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
