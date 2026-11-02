"""v0.4.0 Forecast page e2e (phone 390x844, desktop 1440x900, dark), generic test house only.
Screenshots for docs/screenshots/ are written only when THERMABOT_SCREENSHOTS=1."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from tests.e2e.test_mobile import TAP, TAP_SEL
from tests.e2e.test_setup_wizard import ABS, DESKTOP, PHONE, _ctx_page, _shot
from thermabot.core.types import Forecast, ForecastPoint, Snapshot, ThermostatState


def _seed(srv: Srv) -> None:
    """A cold 60 h forecast and a thermostat reading, no fitted model (fallback banner)."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    srv.ctx.forecast = Forecast(
        source="nws",
        fetched_at=datetime.now(UTC),
        points=[
            ForecastPoint(
                t=now + timedelta(hours=h),
                temp_f=38 + 10 * ((h % 24) / 24),
                humidity=60,
                conditions="Cloudy",
            )
            for h in range(60)
        ],
    )
    srv.ctx.snapshot = Snapshot(
        t=datetime.now(UTC),
        thermostat=ThermostatState(
            hvac_mode="heat_cool", current_temp=68.5, heat_sp=68, cool_sp=76
        ),
    )


def _check(page: Page, srv: Srv, width: int) -> None:
    expect(page.locator("[data-testid=fallback-banner]")).to_contain_text("model not fitted yet")
    # v0.6.0: uPlot canvas (was inline SVG); series and bar counts are exposed as data-*
    chart = page.locator("[data-testid=forecast-chart]")
    expect(chart.locator("canvas")).to_have_count(1)
    expect(chart).to_have_attribute("data-ready", "1")
    series = (chart.get_attribute("data-series") or "").split(",")
    assert "Outdoor" in series and "Indoor (expected)" in series
    assert int(chart.get_attribute("data-bars") or 0) > 0
    assert (chart.bounding_box() or {"width": 0})["width"] <= width
    assert page.locator("[data-testid=forecast-row]").count() == 48
    expect(page.locator("[data-testid=forecast-source]")).to_contain_text("NWS")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert not ABS.findall(page.content())
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-1")
def test_forecast_phone_from_more_menu(browser: Browser, srv: Srv) -> None:
    _seed(srv)
    pg = _ctx_page(browser, srv, PHONE)
    login(pg, srv, "jordan")  # any role
    pg.locator("[data-testid=nav-more] summary").click()
    pg.click("[data-testid=nav-more] a[href='forecast']")
    pg.wait_for_url("**/forecast")
    _check(pg, srv, PHONE["width"])
    for el in pg.locator(TAP_SEL).all():
        if el.evaluate("e => !!e.closest('dialog, #kill-open')"):
            continue
        box = el.bounding_box()
        if box is not None and box["width"] > 0:
            assert box["height"] >= TAP - 0.5, el.evaluate("e => e.outerHTML.slice(0, 100)")
    _shot(pg, "forecast-phone.png")


@pytest.mark.brd("FR-UI-1")
def test_forecast_desktop_from_dashboard(browser: Browser, srv: Srv) -> None:
    _seed(srv)
    pg = _ctx_page(browser, srv, DESKTOP)
    login(pg, srv, "alex")
    pg.click("[data-testid=forecast-link]")
    pg.wait_for_url("**/forecast")
    _check(pg, srv, DESKTOP["width"])
    _shot(pg, "forecast-desktop.png")


@pytest.mark.brd("FR-UI-1")
def test_forecast_dark_mode(browser: Browser, srv: Srv) -> None:
    _seed(srv)
    pg = _ctx_page(browser, srv, PHONE, dark=True)
    login(pg, srv, "alex")
    pg.goto(f"{srv.url}/forecast")
    bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
    rgb = [int(x) for x in re.findall(r"\d+", bg)[:3]]
    assert sum(rgb) < 3 * 80, bg
    _check(pg, srv, PHONE["width"])
    _shot(pg, "forecast-phone-dark.png")


@pytest.mark.brd("FR-UI-1")
def test_forecast_empty_state(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.goto(f"{srv.url}/forecast")
    expect(page.locator("[data-testid=fallback-banner]")).to_be_visible()
    expect(page.locator("[data-testid=weather-stale]")).to_be_visible()
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-WX-2")
def test_settings_weather_tools(browser: Browser, srv: Srv) -> None:
    """Settings > Weather: HA home location fills lat/lon, test fetch previews, save stores."""
    pg = _ctx_page(browser, srv, DESKTOP)
    login(pg, srv, "ann")  # admin: weather is not an Owner section
    pg.goto(f"{srv.url}/settings")
    sec = pg.locator("[data-testid=section-weather]")
    sec.locator("summary").click()
    expect(pg.locator("#f-weather-provider")).to_have_value("nws")  # legacy house row
    sec.locator("[data-testid=weather-ha-location]").click()
    expect(pg.locator("#f-weather-lat")).to_have_value("40")
    sec.locator("[data-testid=weather-test]").click()
    expect(sec.locator("[data-testid=weather-result]")).to_contain_text("OK: nws")
    _shot(pg, "settings-weather-desktop.png")
    pg.click("[data-testid=settings-save]")
    expect(pg.locator("[data-testid=toast]")).to_contain_text("Saved")
    w = srv.ctx.settings.weather
    assert (w.provider, w.lat, w.lon) == ("nws", 40, -75)
    assert srv.ctx.mock_ha.calls == []  # type: ignore[attr-defined]
    st = srv.ctx.conn.execute("SELECT kill_switch, run_mode FROM bot_state").fetchone()
    assert (st["kill_switch"], st["run_mode"]) == ("on", "shadow")
    pg.wait_for_timeout(1000)  # settings.js reloads 600 ms after a save
    pg.goto(f"{srv.url}/settings")
    expect(pg.locator("#f-weather-lat")).to_have_value(re.compile(r"^40(\.0)?$"))
    # clearing lat/lon saves null (= HA home location)
    pg.locator("[data-testid=section-weather] summary").click()
    pg.fill("#f-weather-lat", "")
    pg.fill("#f-weather-lon", "")
    pg.click("[data-testid=settings-save]")
    expect(pg.locator("[data-testid=toast]")).to_contain_text("Saved")
    assert srv.ctx.settings.weather.lat is None and srv.ctx.settings.weather.lon is None
    assert_page_healthy(pg, srv)


@pytest.mark.brd("FR-WX-2")
def test_weather_preview_uses_house_timezone(browser: Browser, srv: Srv) -> None:
    context = browser.new_context(
        viewport=DESKTOP, timezone_id="America/Los_Angeles", locale="en-US"
    )
    pg = context.new_page()
    login(pg, srv, "ann")
    pg.goto(f"{srv.url}/settings")
    sec = pg.locator("[data-testid=section-weather]")
    sec.locator("summary").click()
    pg.fill("#f-weather-lat", "40")
    pg.fill("#f-weather-lon", "-75")
    timestamp = "2026-10-30T04:30:00Z"
    pg.route(
        "**/api/setup/weather-test",
        lambda r: r.fulfill(
            json={
                "ok": True,
                "provider": "nws",
                "points": 1,
                "hours": 1,
                "ms": 0,
                "preview": [{"t": timestamp, "temp_f": 50, "humidity": 50, "conditions": "Clear"}],
            }
        ),
    )
    sec.locator("[data-testid=weather-test]").click()
    expected = pg.evaluate(
        """t => new Date(t).toLocaleTimeString('en-US', {
            timeZone: 'America/New_York', weekday: 'short', hour: '2-digit', minute: '2-digit'
        })""",
        timestamp,
    )
    expect(pg.locator("[data-testid=weather-preview] tbody tr td").first).to_have_text(expected)
    context.close()
