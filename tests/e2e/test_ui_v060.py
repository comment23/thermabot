"""v0.6.0 UI overhaul e2e: dashboard history chart (24 h / 7 d / 30 d from GET
/api/history?range=), theme toggle (localStorage), WCAG AA contrast (computed in the page, no
axe-core), page weight budget, ingress-safe relative URLs on every page, and the docs
screenshots (phone 390x844, desktop 1440x900; light + dark dashboard). Generic test house and
synthetic samples only; screenshots are written only when THERMABOT_SCREENSHOTS=1."""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, capture_chart_labels, login
from tests.e2e.test_layout import _pages
from tests.e2e.test_setup_wizard import ABS, DESKTOP, PHONE, _ctx_page, _shot
from thermabot.app import efficiency as EF
from thermabot.app import history as H
from thermabot.app import jobs as J
from thermabot.core.schedule import TZ
from thermabot.core.shadow import ShadowDay
from thermabot.core.types import (
    Forecast,
    ForecastPoint,
    Plan,
    PlanStep,
    RoomReading,
    Snapshot,
    ThermostatState,
)

UPLOT = "uPlot.iife.min.js"
BUDGET = 300 * 1024


def seed(srv: Srv, days: int = 2, now: datetime | None = None) -> datetime:
    """A generic house: 1-min samples for `days`, a plan, a forecast, rooms, metrics."""
    now = (now or datetime.now(UTC)).replace(second=0, microsecond=0)
    conn = srv.ctx.conn
    for m in range(days * 24 * 60):
        t = now - timedelta(minutes=m)
        h = t.hour + t.minute / 60
        out = 45 + 10 * math.sin((h - 9) / 24 * 2 * math.pi)
        heating = (t.minute + 7 * t.hour) % 60 < (20 if out < 45 else 8)
        doc = {
            "tstat": {
                "temp": round(68.2 + (0.6 if heating else -0.2) + 0.3 * math.sin(m / 50), 2),
                "heat_sp": 66 if 23 <= t.hour or t.hour < 6 else 68,
                "cool_sp": 76,
                "hvac_mode": "heat_cool",
                "hvac_action": "heating" if heating else "idle",
            },
            "t_out": round(out, 1),
            "blower": 1.0 if heating else 0.0,
        }
        J.save_sample(conn, t, doc, "SHADOW", 88.0, [])
    srv.ctx.snapshot = Snapshot(
        t=now,
        thermostat=ThermostatState(
            hvac_mode="heat_cool",
            hvac_action="heating",
            current_temp=68.5,
            heat_sp=68,
            cool_sp=76,
            outdoor_temp=47,
        ),
        rooms={
            "bedroom_1": RoomReading(key="bedroom_1", temp=67.4, age_min=3, score=84),
            "study": RoomReading(key="study", temp=69.1, age_min=6, score=91),
            "living_room": RoomReading(key="living_room", temp=68.5, age_min=1, score=88),
        },
    )
    srv.ctx.score = 88.0
    steps = []
    for i in range(48 * 4):
        t = now + timedelta(minutes=15 * i)
        night = 23 <= t.hour or t.hour < 6
        steps.append(
            PlanStep(
                t=t,
                hvac_mode="heat",
                heat_sp=66 if night else 68,
                pred_t=67.5 + (0 if night else 0.8),
                pred_rooms={"bedroom_1": 66.8, "study": 68.4},
                reason=("sleep setback" if night else "home block") if t.minute == 0 else "",
            )
        )
    srv.ctx.plan = Plan(kind="optimised", created_at=now, steps=steps)
    srv.ctx.forecast = Forecast(
        source="nws",
        fetched_at=now,
        points=[
            ForecastPoint(
                t=now.replace(minute=0) + timedelta(hours=h),
                temp_f=round(45 + 10 * math.sin((h - 9) / 24 * 2 * math.pi), 1),
                humidity=60,
                conditions="Partly cloudy",
            )
            for h in range(60)
        ],
    )
    today = now.astimezone(TZ).date()
    for i in range(14):
        conn.execute(
            "INSERT INTO daily_metrics (day, heat_min, cool_min, fan_only_min, hdd, cdd,"
            " comfort_pct, bot_state) VALUES (?,?,?,?,?,?,?,?)",
            ((today - timedelta(days=i + 1)).isoformat(), 180 + 9 * (i % 5), 0, 25, 18, 0,
             93 + i % 4, "SHADOW"),
        )  # fmt: skip
    for i in range(12):
        d = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        t_out = 22 + 3 * i
        hdd = 65 - t_out
        EF.save_day(conn, d, {"mean_out_f": t_out, "hdd": hdd, "cdd": 0,
                              "heat_min": (12 - 0.12 * t_out + (-1) ** i * 0.6) * hdd,
                              "cool_min": 0, "n_samples": 1440, "out_hours": 24,
                              "coverage_pct": 99, "base_f": 65}, now, "backfill")  # fmt: skip
    for i in range(7):
        d = (today - timedelta(days=i + 1)).isoformat()
        J.save_shadow_day(conn, d, ShadowDay(actual_heat_min=200 + 5 * i, actual_cool_min=0,
                                             actual_comfort_pct=95, n_samples=1440,
                                             coverage_pct=100), None, now)  # fmt: skip
    return now


def _chart_ready(pg: Page, rng: str) -> None:
    ch = pg.locator("[data-testid=history-chart]")
    expect(ch).to_have_attribute("data-range", rng)
    expect(ch).to_have_attribute("data-ready", "1")
    expect(ch.locator("canvas")).to_have_count(1)


# ---- dashboard history chart -----------------------------------------------------------
@pytest.mark.brd("FR-UI-1")
def test_dashboard_history_chart_ranges(browser: Browser, srv: Srv) -> None:
    seeded_at = seed(srv)
    srv.ctx.clock = lambda: seeded_at
    pg = _ctx_page(browser, srv, DESKTOP)
    login(pg, srv, "jordan")  # any role
    _chart_ready(pg, "24h")
    ch = pg.locator("[data-testid=history-chart]")
    assert int(ch.get_attribute("data-points") or 0) <= 500
    assert int(ch.get_attribute("data-count") or 0) >= 24 * 60
    assert (ch.get_attribute("data-series") or "").split(",") == [
        "Indoor",
        "Outdoor",
        "Heat to",
        "Cool to",
    ]
    for rng in ("7d", "30d", "24h"):
        pg.click(f"[data-testid=range-{rng}]")
        _chart_ready(pg, rng)
        expect(pg.locator(f"[data-testid=range-{rng}]")).to_have_attribute("aria-pressed", "true")
        assert int(ch.get_attribute("data-points") or 0) <= 500
    # tooltip on hover; accessible fallback table
    ch.scroll_into_view_if_needed()
    box = ch.locator(".u-over").bounding_box()
    assert box is not None
    pg.mouse.move(box["x"] + box["width"] * 0.7, box["y"] + box["height"] / 2)
    expect(ch.locator(".chart-tip")).to_be_visible()
    expect(ch.locator(".chart-tip")).to_contain_text("Indoor")
    pg.locator("[data-testid=history-fallback] summary").click()
    expect(pg.locator("[data-testid=history-table] tbody tr").nth(100)).to_be_attached()
    assert srv.ctx.mock_ha.calls == []  # type: ignore[attr-defined]
    assert_page_healthy(pg, srv)


@pytest.mark.brd("FR-UI-1")
@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 30, 4, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 7, 30, tzinfo=UTC),
        datetime(2027, 3, 14, 8, 30, tzinfo=UTC),
        datetime(2027, 3, 28, 8, 30, tzinfo=UTC),
    ],
)
def test_history_chart_uses_house_timezone(browser: Browser, srv: Srv, now: datetime) -> None:
    seeded_at = seed(srv, now=now)
    srv.ctx.clock = lambda: seeded_at
    series = H.series(srv.ctx.conn, seeded_at, "24h")
    latest = next(t for t, n in zip(reversed(series["t"]), reversed(series["n"]), strict=True) if n)
    labels = []
    for viewer in (
        "America/New_York",
        "America/Los_Angeles",
        "Europe/Paris",
        "Australia/Sydney",
        "Asia/Kathmandu",
    ):
        context = browser.new_context(
            viewport={"width": 2200, "height": 900}, timezone_id=viewer, locale="en-US"
        )
        pg = context.new_page()
        capture_chart_labels(pg)
        login(pg, srv, "jordan")
        expect(pg.locator("[data-testid=home-timezone]")).to_have_text(
            "Home time: America/New_York"
        )
        _chart_ready(pg, "24h")
        pg.wait_for_function("window.chartLabels.length > 0")
        pg.locator("[data-testid=history-fallback] summary").click()
        expected = pg.evaluate(
            """t => new Date(t * 1000).toLocaleString('en-US', {
                timeZone: 'America/New_York', weekday: 'short', month: 'short',
                day: 'numeric', hour: '2-digit', minute: '2-digit'
            })""",
            latest,
        )
        expect(
            pg.locator("[data-testid=history-table] tbody tr").first.locator("td").first
        ).to_have_text(expected)
        drawn = pg.evaluate("[...new Set(window.chartLabels)].sort()")
        if now == datetime(2027, 3, 28, 8, 30, tzinfo=UTC):
            tick = next(label for label in drawn if label.startswith("2am|"))
            x = int(tick.split("|")[1])
            chart = pg.locator("[data-testid=history-chart]")
            chart.scroll_into_view_if_needed()
            canvas = chart.locator("canvas").bounding_box()
            overlay = chart.locator(".u-over").bounding_box()
            assert canvas is not None and overlay is not None
            pg.mouse.move(canvas["x"] + x, overlay["y"] + overlay["height"] / 2)
            target = datetime(2027, 3, 28, 6, 0, tzinfo=UTC).timestamp()
            nearby = [t for t in series["t"] if abs(t - target) <= series["step_s"]]
            expected_labels = pg.evaluate(
                """times => times.map(t => new Date(t * 1000).toLocaleString('en-US', {
                    timeZone: 'America/New_York', weekday: 'short', hour: '2-digit',
                    minute: '2-digit'
                }))""",
                nearby,
            )
            tip = chart.locator(".chart-tip b")
            expect(tip).to_be_visible()
            assert tip.inner_text() in expected_labels
        ranges = [drawn]
        if now == datetime(2027, 3, 28, 8, 30, tzinfo=UTC):
            for rng in ("7d", "30d"):
                pg.evaluate("window.chartLabels = []")
                pg.click(f"[data-testid=range-{rng}]")
                _chart_ready(pg, rng)
                pg.wait_for_function("window.chartLabels.length > 0")
                ranges.append(pg.evaluate("[...new Set(window.chartLabels)].sort()"))
        labels.append(ranges)
        context.close()
    assert all(viewer_labels == labels[0] for viewer_labels in labels[1:])


@pytest.mark.brd("FR-UI-1")
def test_daily_history_tooltip_uses_house_date(browser: Browser, srv: Srv) -> None:
    now = seed(srv, now=datetime(2026, 10, 30, 4, 30, tzinfo=UTC))
    srv.ctx.clock = lambda: now
    context = browser.new_context(
        viewport=DESKTOP, timezone_id="America/Los_Angeles", locale="en-US"
    )
    pg = context.new_page()
    login(pg, srv, "jordan")
    pg.goto(f"{srv.url}/history")
    chart = pg.locator("[data-testid=chart]")
    expect(chart).to_have_attribute("data-ready", "1")
    first = pg.evaluate("JSON.parse(document.getElementById('hist-data').textContent).t[0]")
    expected = pg.evaluate(
        "t => new Date(t * 1000).toLocaleDateString('en-US', {timeZone: 'America/New_York'})",
        first,
    )
    chart.scroll_into_view_if_needed()
    box = chart.locator(".u-over").bounding_box()
    assert box is not None
    pg.mouse.move(box["x"] + 1, box["y"] + box["height"] / 2)
    expect(chart.locator(".chart-tip b")).to_have_text(expected)
    context.close()


@pytest.mark.brd("FR-UI-1")
def test_history_chart_touch_tooltip_and_resize(browser: Browser, srv: Srv) -> None:
    seed(srv, days=1)
    pg = _ctx_page(browser, srv, PHONE)
    login(pg, srv, "alex")
    _chart_ready(pg, "24h")
    ch = pg.locator("[data-testid=history-chart]")
    over = ch.locator(".u-over")
    box = over.bounding_box()
    assert box is not None and box["x"] + box["width"] <= PHONE["width"]
    over.tap(position={"x": box["width"] * 0.5, "y": box["height"] * 0.5})
    expect(ch.locator(".chart-tip")).to_be_visible()
    w0 = ch.locator("canvas").bounding_box()
    pg.set_viewport_size({"width": 1200, "height": 900})
    pg.wait_for_function(
        "w => document.querySelector('[data-testid=history-chart] canvas')"
        ".getBoundingClientRect().width > w + 100",
        arg=w0["width"] if w0 else 0,
    )
    assert_page_healthy(pg, srv)


@pytest.mark.brd("FR-UI-1")
def test_history_chart_empty_and_error_states(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    ch = page.locator("[data-testid=history-chart]")
    expect(ch).to_have_attribute("data-state", "empty")
    expect(ch.locator(".chart-state")).to_contain_text("No samples")
    page.route("**/api/history?range=7d", lambda r: r.fulfill(status=500, body="{}"))
    page.click("[data-testid=range-7d]")
    expect(ch).to_have_attribute("data-state", "error")
    expect(ch.locator(".chart-state")).to_contain_text("HTTP 500")
    srv.errors.clear()  # the deliberate 500 is logged by the browser
    page.unroute("**/api/history?range=7d")
    ch.locator(".chart-state button").click()
    expect(ch).to_have_attribute("data-state", "empty")
    assert_page_healthy(page, srv)


# ---- theme -------------------------------------------------------------------------------
def _bg_dark(pg: Page) -> bool:
    rgb = pg.evaluate(
        "getComputedStyle(document.body).backgroundColor.match(/\\d+/g).slice(0,3).map(Number)"
    )
    return sum(rgb) < 3 * 80


@pytest.mark.brd("FR-UI-1")
def test_theme_toggle_persists(browser: Browser, srv: Srv) -> None:
    pg = _ctx_page(browser, srv, PHONE)  # light system scheme
    login(pg, srv, "alex")
    assert not _bg_dark(pg)
    btn = pg.locator("[data-testid=theme-toggle]")
    box = btn.bounding_box()
    assert box is not None and box["height"] >= 44 - 0.5
    btn.click()
    expect(pg.locator("html")).to_have_attribute("data-theme", "dark")
    assert _bg_dark(pg)
    pg.goto(f"{srv.url}/plan")  # stored in localStorage, applied before paint
    expect(pg.locator("html")).to_have_attribute("data-theme", "dark")
    assert _bg_dark(pg)
    pg.locator("[data-testid=theme-toggle]").click()
    expect(pg.locator("html")).to_have_attribute("data-theme", "light")
    assert not _bg_dark(pg)
    assert_page_healthy(pg, srv)


@pytest.mark.brd("FR-UI-1")
def test_status_pills(page: Page, srv: Srv) -> None:
    from thermabot.config import Settings

    srv.ctx.weather_health = "stale"
    login(page, srv, "alex")
    expect(page.locator("[data-testid=weather-pill]")).to_have_text("Weather stale")
    expect(page.locator("[data-testid=state-badge]")).to_have_text("SHADOW")
    srv.ctx.settings = Settings()
    page.reload()
    expect(page.locator("[data-testid=state-badge]")).to_have_attribute(
        "data-label", "SETUP NEEDED"
    )
    page.click("[data-testid=kill-open]")
    page.click("[data-testid=kill-off]")
    expect(page.locator("[data-testid=kill-state]")).to_have_attribute("data-on", "off")
    assert_page_healthy(page, srv)


# ---- contrast (WCAG AA, computed) ---------------------------------------------------------
CONTRAST_JS = (Path(__file__).parent / "contrast.js").read_text(encoding="utf-8")


@pytest.mark.brd("NFR-A11Y-1")
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_contrast_wcag_aa_every_page(browser: Browser, srv: Srv, dark: bool) -> None:
    seed(srv, days=1)
    srv.ctx.weather_health = "stale"
    pg = _ctx_page(browser, srv, PHONE, dark=dark)
    login(pg, srv, "alex")
    for href in [*_pages(srv), "/login"]:
        if href == "/login":
            pg.context.clear_cookies()
        pg.goto(f"{srv.url}{href}")
        pg.wait_for_load_state("load")
        bad = pg.evaluate(CONTRAST_JS)
        assert bad == [], (href, bad)
    assert srv.errors == [], srv.errors


# ---- performance + ingress --------------------------------------------------------------
@pytest.mark.brd("NFR-PERF-1")
def test_page_weight_budget(browser: Browser, srv: Srv) -> None:
    seed(srv, days=1)
    pg = _ctx_page(browser, srv, DESKTOP)
    login(pg, srv, "alex")
    weights: dict[str, int] = {}
    for href in _pages(srv):
        cdp = pg.context.new_cdp_session(pg)
        cdp.send("Network.enable")
        cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
        total: dict[str, int] = {}
        urls: dict[str, str] = {}
        cdp.on(
            "Network.responseReceived",
            lambda e, urls=urls: urls.__setitem__(e["requestId"], e["response"]["url"]),
        )
        cdp.on(
            "Network.loadingFinished",
            lambda e, total=total: total.__setitem__(e["requestId"], int(e["encodedDataLength"])),
        )
        pg.goto(f"{srv.url}{href}")
        pg.wait_for_load_state("load")
        pg.wait_for_timeout(800)  # the dashboard chart fetches api/history after load
        size = sum(n for rid, n in total.items() if UPLOT not in urls.get(rid, ""))
        weights[href] = size
        cdp.detach()
        assert size <= BUDGET, f"{href}: {size} bytes > {BUDGET} (uPlot excluded)"
    print("page weights (bytes, uPlot excluded):", weights)  # noqa: T201


@pytest.mark.brd("FR-UI-1")
def test_every_page_relative_urls(page: Page, srv: Srv) -> None:
    """Ingress: no root-relative URL on any page (v0.3.0 checked setup/settings only)."""
    seed(srv, days=1)
    login(page, srv, "alex")
    for href in _pages(srv):
        page.goto(f"{srv.url}{href}")
        assert not ABS.findall(page.content()), href
        srcs = page.evaluate(
            "() => [...document.querySelectorAll('link[href],script[src],img[src]')]"
            ".map(e => e.getAttribute('href') || e.getAttribute('src'))"
        )
        srcs = [s for s in srcs if not s.startswith("data:")]  # the empty favicon
        assert srcs and all(s.startswith("static/") and "?v=" in s for s in srcs), (href, srcs)
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-1")
def test_dashboard_under_ingress_prefix(page: Page, srv: Srv) -> None:
    seed(srv, days=1)
    prefix = "/api/hassio_ingress/tok123"

    def route(r: object) -> None:
        req = r.request  # type: ignore[attr-defined]
        url = req.url.replace(f"{srv.url}{prefix}", srv.url)
        resp = r.fetch(url=url, headers={**req.headers, "x-ingress-path": prefix}, max_redirects=0)  # type: ignore[attr-defined]  # noqa: E501
        r.fulfill(response=resp)  # type: ignore[attr-defined]

    page.add_init_script(  # poll instead of SSE: an endless stream can't be proxied by route
        "new MutationObserver((_, o) => { if (document.documentElement) {"
        " document.documentElement.dataset.nosse = '1'; o.disconnect(); } })"
        ".observe(document, {childList: true})"
    )
    page.route(re.compile("^" + re.escape(f"{srv.url}{prefix}/")), route)
    bad: list[str] = []
    page.on("response", lambda r: r.status >= 400 and bad.append(f"{r.status} {r.url}"))
    page.goto(f"{srv.url}{prefix}/login")
    assert page.locator(".auth-brand img").evaluate("img => img.complete && img.naturalWidth > 0")
    page.fill("input[name=username]", "alex")
    page.fill("input[name=password]", "correct horse")
    page.click("button[type=submit]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Welcome")
    page.goto(f"{srv.url}{prefix}/")  # Playwright does not route a followed redirect hop
    assert page.locator("header .brand img").evaluate("img => img.complete && img.naturalWidth > 0")
    _chart_ready(page, "24h")
    page.goto(f"{srv.url}{prefix}/forecast")
    expect(page.locator("[data-testid=forecast-chart] canvas")).to_have_count(1)
    assert bad == [], bad
    assert_page_healthy(page, srv)


# ---- screenshots (docs/screenshots, THERMABOT_SCREENSHOTS=1 only) --------------------------
SHOT_PAGES = [
    ("/", "dashboard"),
    ("/plan", "plan"),
    ("/room", "room"),
    ("/history", "history"),
    ("/shadow", "shadow"),
    ("/model", "model"),
    ("/health", "health"),
    ("/users", "users"),
    ("/account", "account"),
]


@pytest.mark.brd("FR-UI-1")
@pytest.mark.parametrize("vp", ["phone", "desktop"])
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_screenshots_every_page(browser: Browser, srv: Srv, vp: str, dark: bool) -> None:
    seed(srv)
    size = PHONE if vp == "phone" else DESKTOP
    pg = _ctx_page(browser, srv, size, dark=dark)
    pg.goto(f"{srv.url}/login")
    _shot(pg, f"login-{vp}{'-dark' if dark else ''}.png")
    login(pg, srv, "alex")
    for href, name in SHOT_PAGES:
        if dark and href != "/":
            continue  # dark: dashboard only (plus the forecast/efficiency/setup dark shots)
        r = pg.goto(f"{srv.url}{href}" + ("?room=bedroom_1" if href == "/room" else ""))
        assert r is not None and r.status == 200, href
        if href == "/":
            _chart_ready(pg, "24h")
        assert pg.evaluate("document.documentElement.scrollWidth") <= size["width"], href
        _shot(pg, f"{name}-{vp}{'-dark' if dark else ''}.png")
    assert_page_healthy(pg, srv)
