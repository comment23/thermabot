"""v0.5.0 Efficiency page e2e (phone 390x844, desktop 1440x900, dark, empty state). Synthetic
data only. Screenshots for docs/screenshots/ are written only when THERMABOT_SCREENSHOTS=1."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from tests.e2e.test_mobile import TAP, TAP_SEL
from tests.e2e.test_setup_wizard import ABS, DESKTOP, PHONE, _ctx_page, _shot
from thermabot.app import efficiency as EF
from thermabot.app import jobs as J
from thermabot.core.shadow import ShadowDay


def _seed(srv: Srv) -> None:
    """12 synthetic heating days and 6 cooling days, plus 10 simulated shadow days."""
    now = datetime.now(UTC)
    conn = srv.ctx.conn
    for i in range(12):
        day = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        t = 22 + 3 * i
        hdd = 65 - t
        noise = (-1) ** i * 0.6
        EF.save_day(conn, day, {"mean_out_f": t, "hdd": hdd, "cdd": 0,
                                "heat_min": (12 - 0.12 * t + noise) * hdd, "cool_min": 0,
                                "n_samples": 1440, "out_hours": 24, "coverage_pct": 99,
                                "base_f": 65}, now, "backfill")  # fmt: skip
    for i in range(6):
        day = (date(2026, 7, 1) + timedelta(days=i)).isoformat()
        t = 72 + 2.5 * i
        cdd = t - 65
        EF.save_day(conn, day, {"mean_out_f": t, "hdd": 0, "cdd": cdd, "heat_min": 0,
                                "cool_min": (4 + 0.2 * (t - 72)) * cdd, "n_samples": 1440,
                                "out_hours": 24, "coverage_pct": 97, "base_f": 65},
                    now, "backfill")  # fmt: skip
    for i in range(10):
        day = (date(2026, 7, 10) + timedelta(days=i)).isoformat()
        J.save_shadow_day(conn, day, ShadowDay(actual_heat_min=0, actual_cool_min=300 + 10 * i,
                                               saved_min=25 - 4 * (i % 4)), 1, now)  # fmt: skip


def _check(page: Page, srv: Srv, width: int) -> None:
    # v0.6.0: uPlot canvases (was inline SVG); points also listed in the fallback tables
    for k in ("heat", "cool"):
        ch = page.locator(f"[data-testid=eff-chart-{k}]")
        expect(ch.locator("canvas")).to_have_count(1)
        expect(ch).to_have_attribute("data-fit", "1")  # fitted curve + 95% band
    pts = page.locator("[data-testid=eff-chart-heat]").get_attribute("data-points")
    assert pts == "12"
    assert page.locator("[data-testid=eff-point]").count() == 18
    expect(page.locator("[data-testid=eff-fit-stats]").first).to_contain_text("R")
    expect(page.locator("[data-testid=simulated-label]")).to_be_visible()
    sav = page.locator("[data-testid=savings-chart]")
    expect(sav.locator("canvas")).to_have_count(1)
    assert sav.get_attribute("data-bars") == "10"
    assert page.locator("[data-testid=savings-row]").count() == 10
    expect(page.locator("[data-testid=eff-index-heat]")).to_contain_text("Efficiency index")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert not ABS.findall(page.content())
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-EFF-2")
def test_efficiency_phone_from_more_menu(browser: Browser, srv: Srv) -> None:
    _seed(srv)
    pg = _ctx_page(browser, srv, PHONE)
    login(pg, srv, "jordan")  # any role
    pg.locator("[data-testid=nav-more] summary").click()
    pg.click("[data-testid=nav-more] a[href='efficiency']")
    pg.wait_for_url("**/efficiency")
    _check(pg, srv, PHONE["width"])
    for el in pg.locator(TAP_SEL).all():
        if el.evaluate("e => !!e.closest('dialog, #kill-open')"):
            continue
        box = el.bounding_box()
        if box is not None and box["width"] > 0:
            assert box["height"] >= TAP - 0.5, el.evaluate("e => e.outerHTML.slice(0, 100)")
    _shot(pg, "efficiency-phone.png")


@pytest.mark.brd("FR-EFF-2")
def test_efficiency_desktop_from_dashboard_tile(browser: Browser, srv: Srv) -> None:
    _seed(srv)
    pg = _ctx_page(browser, srv, DESKTOP)
    login(pg, srv, "alex")
    tile = pg.locator("[data-testid=efficiency-tile]")
    expect(tile).to_contain_text(re.compile(r"Efficiency: index \d+"))
    box = tile.bounding_box()
    assert box is not None and box["height"] >= TAP - 0.5
    tile.click()
    pg.wait_for_url("**/efficiency")
    _check(pg, srv, DESKTOP["width"])
    _shot(pg, "efficiency-desktop.png")
    assert srv.ctx.mock_ha.calls == []  # type: ignore[attr-defined]


@pytest.mark.brd("FR-EFF-2")
def test_efficiency_dark_mode(browser: Browser, srv: Srv) -> None:
    _seed(srv)
    pg = _ctx_page(browser, srv, PHONE, dark=True)
    login(pg, srv, "alex")
    pg.goto(f"{srv.url}/efficiency")
    bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
    rgb = [int(x) for x in re.findall(r"\d+", bg)[:3]]
    assert sum(rgb) < 3 * 80, bg
    _check(pg, srv, PHONE["width"])
    _shot(pg, "efficiency-phone-dark.png")


@pytest.mark.brd("FR-EFF-2")
@pytest.mark.parametrize("vp", ["phone", "desktop"])
def test_efficiency_empty_state(browser: Browser, srv: Srv, vp: str) -> None:
    size = PHONE if vp == "phone" else DESKTOP
    pg = _ctx_page(browser, srv, size)
    login(pg, srv, "jordan")
    expect(pg.locator("[data-testid=efficiency-tile]")).to_contain_text("collecting data 0/5")
    pg.goto(f"{srv.url}/efficiency")
    expect(pg.locator("[data-testid=eff-empty-heat]")).to_contain_text("(0/5)")
    expect(pg.locator("[data-testid=eff-empty-cool]")).to_contain_text("needs >= 5")
    expect(pg.locator("[data-testid=savings-empty]")).to_be_visible()
    assert pg.locator("[data-testid=eff-point]").count() == 0
    assert pg.evaluate("document.documentElement.scrollWidth") <= size["width"]
    assert_page_healthy(pg, srv)
    if vp == "phone":
        _shot(pg, "efficiency-empty-phone.png")
