"""T1.40: P4 History & Savings with 0, 1 and 90-day fixtures + latest weekly summary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, capture_chart_labels, login
from thermabot.adapters import db
from thermabot.app.routes_ui import SUMMARY_KEY
from thermabot.core.schedule import TZ
from thermabot.core.types import SavingsOut


def _seed(srv: Srv, n: int) -> None:
    now = srv.ctx.clock()
    srv.ctx.clock = lambda: now
    today = now.astimezone(TZ).date()
    for i in range(n):
        srv.ctx.conn.execute(
            "INSERT INTO daily_metrics (day, heat_min, cool_min, fan_only_min, hdd, cdd,"
            " comfort_pct, bot_state) VALUES (?,?,?,?,?,?,?,?)",
            ((today - timedelta(days=i + 1)).isoformat(), 120 + i, 0, 30, 10, 0, 95, "SHADOW"),
        )


@pytest.mark.brd("FR-UI-1")
@pytest.mark.parametrize(("n", "days", "shown"), [(0, 30, 0), (1, 30, 1), (90, 90, 90), (90, 7, 7)])
def test_history_fixtures(page: Page, srv: Srv, n: int, days: int, shown: int) -> None:
    _seed(srv, n)
    login(page, srv, "jordan")
    page.goto(f"{srv.url}/history?days={days}")
    expect(page.locator("[data-testid=day-row]")).to_have_count(shown)
    if shown:
        expect(page.locator("[data-testid=chart] canvas")).to_have_count(1)
    else:
        expect(page.locator("[data-testid=no-history]")).to_be_visible()
    expect(page.locator("[data-testid=savings-pending]")).to_be_visible()
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-1")
@pytest.mark.parametrize("width", [375, 1440])
def test_history_year_chart_uses_home_dates(browser: Browser, srv: Srv, width: int) -> None:
    srv.ctx.clock = lambda: datetime(2027, 3, 28, 8, 30, tzinfo=UTC)
    _seed(srv, 400)
    labels = []
    for viewer in ("America/New_York", "Europe/Paris", "Australia/Sydney"):
        context = browser.new_context(
            viewport={"width": width, "height": 900}, timezone_id=viewer, locale="en-US"
        )
        pg = context.new_page()
        capture_chart_labels(pg)
        login(pg, srv, "jordan")
        pg.goto(f"{srv.url}/history?days=400")
        chart = pg.locator("[data-testid=chart]")
        expect(chart).to_have_attribute("data-ready", "1")
        pg.wait_for_function("window.chartLabels.length > 0")
        drawn = pg.evaluate("[...new Set(window.chartLabels)].sort()")
        if width == 1440:
            x = max(int(label.split("|")[1]) for label in drawn if label.startswith("Mar|"))
            chart.scroll_into_view_if_needed()
            canvas = chart.locator("canvas").bounding_box()
            overlay = chart.locator(".u-over").bounding_box()
            assert canvas is not None and overlay is not None
            pg.mouse.move(canvas["x"] + x, overlay["y"] + overlay["height"] / 2)
            expect(chart.locator(".chart-tip b")).to_have_text("3/1/2027")
        labels.append(drawn)
        context.close()
    assert labels[0] == labels[1] == labels[2]


@pytest.mark.brd("FR-ALR-2")
def test_savings_and_latest_summary(page: Page, srv: Srv) -> None:
    srv.ctx.savings = lambda: SavingsOut(saved_h=12.34, ci_h=3.1, pct=18, counted=True)
    db.write_kv(srv.ctx.conn, SUMMARY_KEY, "Last week\n  heat 41.0 h <b>x</b>")
    login(page, srv, "alex")
    page.goto(f"{srv.url}/history")
    expect(page.locator("[data-testid=savings]")).to_contain_text("12.3 h")
    expect(page.locator("[data-testid=savings]")).to_contain_text("±3.1")
    expect(page.locator("[data-testid=summary]")).to_contain_text("heat 41.0 h <b>x</b>")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-ALR-2")
def test_no_summary_yet(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.goto(f"{srv.url}/history")
    expect(page.locator("[data-testid=no-summary]")).to_be_visible()
    assert_page_healthy(page, srv)
