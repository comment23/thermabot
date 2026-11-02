"""T1.36: base layout. Header on every page, no external requests, no console errors,
vendored assets match VENDOR.md, SSE with polling fallback, login redirect."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from thermabot.app.routes_ui import NAV

STATIC = Path(__file__).parents[2] / "thermabot" / "app" / "static"


def _pages(srv: Srv) -> list[str]:
    """NAV entries that are implemented (grows as P1-P8 land)."""
    from thermabot.app import routes_ui

    paths = {getattr(r, "path", None) for r in routes_ui.router.routes}
    return [h for h, _, _ in NAV if h in paths]


@pytest.mark.brd("FR-UI-1")
def test_vendor_hashes_match_vendor_md() -> None:
    md = (STATIC / "vendor" / "README.md").read_text(encoding="utf-8")
    rows = re.findall(r"\| `([^`]+)` \|.*\| `([0-9a-f]{64})` \|", md)
    assert len(rows) == 5  # v0.6.0: uPlot (JS, CSS, LICENSE) under vendor/uplot/
    assert (
        (STATIC / "vendor" / "uplot" / "LICENSE")
        .read_text(encoding="utf-8")
        .startswith("The MIT License")
    )
    for name, sha in rows:
        assert hashlib.sha256((STATIC / "vendor" / name).read_bytes()).hexdigest() == sha


@pytest.mark.brd("FR-UI-1")
def test_no_cdn_references_in_templates() -> None:
    for f in (STATIC.parent / "templates").glob("*.html"):
        assert not re.search(r"(src|href)=\"(https?:)?//", f.read_text(encoding="utf-8")), f


@pytest.mark.brd("NFR-SEC-1")
def test_anonymous_redirects_to_login(page: Page, srv: Srv) -> None:
    page.goto(f"{srv.url}/")
    expect(page).to_have_url(f"{srv.url}/login")
    assert page.locator("[data-testid=header]").count() == 0
    assert srv.errors == []


@pytest.mark.brd("FR-UI-1")
@pytest.mark.parametrize(("width", "theme"), [(375, "light"), (1440, "dark")])
def test_branding_is_visible_and_local(
    page: Page, srv: Srv, width: int, theme: Literal["light", "dark"]
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(color_scheme=theme)
    page.goto(f"{srv.url}/login")
    mark = page.locator(".auth-brand img")
    expect(mark).to_be_visible()
    assert mark.evaluate("img => img.complete && img.naturalWidth > 0")
    assert mark.get_attribute("alt") == ""
    favicon = page.locator('link[rel="icon"]').get_attribute("href")
    assert favicon is not None and favicon.startswith("static/brand-mark.svg?v=")
    assert page.request.get(f"{srv.url}/{favicon}").status == 200

    login(page, srv, "alex")
    brand = page.get_by_role("link", name="thermabot", exact=True)
    expect(brand).to_be_visible()
    assert brand.locator("img").evaluate("img => img.complete && img.naturalWidth > 0")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert_page_healthy(page, srv)


@pytest.mark.brd("NFR-A11Y-1")
@pytest.mark.parametrize("width", [390, 1440])
def test_keyboard_skip_link_and_control_dialog_names(page: Page, srv: Srv, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{srv.url}/login")
    skip = page.get_by_role("link", name="Skip to content")
    page.keyboard.press("Tab")
    expect(skip).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#content")).to_be_focused()

    login(page, srv, "alex")
    opener = page.locator("[data-testid=kill-open]")
    opener.focus()
    page.keyboard.press("Enter")
    stop_dialog = page.get_by_role("dialog", name="Stop the bot")
    expect(stop_dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(stop_dialog).to_be_hidden()
    expect(opener).to_be_focused()

    mode = page.locator("[data-testid=control-mode]")
    mode.click()
    start_dialog = page.get_by_role("dialog", name="Start the bot")
    expect(start_dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(start_dialog).to_be_hidden()
    expect(mode).to_be_focused()
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-MODE-1")
@pytest.mark.parametrize("user", ["alex", "ann", "jordan"])
def test_header_on_every_page(page: Page, srv: Srv, user: str) -> None:
    login(page, srv, user)
    for href in _pages(srv):
        r = page.goto(f"{srv.url}{href}")
        assert r is not None
        if r.status == 403:  # expected for pages above the role; the browser logs it
            srv.errors.clear()
            continue
        assert r.status == 200, href
        assert_page_healthy(page, srv)
        expect(page.locator("[data-testid=state-badge]")).to_have_attribute("data-state", "SHADOW")
        expect(page.locator("[data-testid=kill-state]")).to_have_text("Bot: Watching (Shadow)")
        expect(page.locator("[data-testid=controls-link]")).to_have_attribute("href", "./#controls")
        if user != "jordan" and href == "/":
            expect(page.locator("[data-testid=kill-open]")).to_have_text("Stop bot")
        assert page.locator("meta[name=csrf-token]").count() == 1
        assert (page.locator("[data-testid=kill-open]").count() == 1) == (
            user != "jordan" and href == "/"
        )


@pytest.mark.brd("FR-UI-2")
def test_sse_updates_badge_live(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    expect(page.locator("html")).to_have_attribute("data-live", "sse")
    srv.ctx.broker.publish("state", {"state": "DISABLED", "label": "DISABLED"})
    expect(page.locator("[data-testid=state-badge]")).to_have_text("DISABLED")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-2")
def test_polling_fallback_when_sse_fails(page: Page, srv: Srv) -> None:
    page.route("**/events", lambda r: r.abort())
    page.clock.install()
    login(page, srv, "alex")
    expect(page.locator("html")).to_have_attribute("data-live", "poll")
    srv.errors.clear()  # the aborted /events request logs a console error by design
    db_state = srv.ctx.conn
    srv.ctx.ha_health = "down"
    db_state.execute("UPDATE bot_state SET checksum='x'")  # tampered -> DISABLED
    page.clock.run_for(31_000)
    expect(page.locator("[data-testid=state-badge]")).to_have_attribute("data-state", "DISABLED")
    expect(page.locator("#ha-connection-warning")).to_be_visible()
    srv.ctx.ha_health = "ok"
    page.clock.run_for(31_000)
    expect(page.locator("#ha-connection-warning")).to_be_hidden()


@pytest.mark.brd("FR-KILL-1")
def test_kill_switch_dialog_goes_through_api(page: Page, srv: Srv) -> None:
    login(page, srv, "ann")
    page.click("[data-testid=kill-open]")
    expect(page.locator("[data-testid=kill-dialog]")).to_be_visible()
    expect(page.locator("[data-testid=kill-on-shadow]")).to_be_hidden()
    expect(page.locator("[data-testid=kill-dialog] h3:visible")).to_have_text("Stop the bot")
    page.click("[data-testid=kill-off]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Bot stopped")
    expect(page.locator("[data-testid=state-badge]")).to_have_attribute("data-state", "DISABLED")
    expect(page.locator("[data-testid=kill-state]")).to_have_text("Bot: Stopped")
    expect(page.locator("[data-testid=kill-open]")).to_be_hidden()  # admin cannot start
    acts = [r[0] for r in srv.ctx.conn.execute("SELECT action FROM audit WHERE actor='ann'")]
    assert "kill_off" in acts
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-KILL-3")
@pytest.mark.parametrize("restore_result,kind", [("failed", "error"), ("sent", "ok")])
def test_kill_restore_result_is_visible(
    page: Page, srv: Srv, restore_result: str, kind: str
) -> None:
    from thermabot.core.types import Snapshot, ThermostatState

    async def restore(now: datetime) -> str:
        return restore_result

    srv.ctx.kill_restore = restore
    srv.ctx.snapshot = Snapshot(t=datetime.now(UTC), thermostat=ThermostatState(hvac_mode="off"))
    login(page, srv, "ann")
    page.click("[data-testid=kill-open]")
    page.click("[data-testid=kill-off-band]")
    toast = page.locator("[data-testid=toast]")
    expect(toast).to_contain_text(f"Bot stopped, restore: {restore_result}")
    expect(toast).to_have_attribute("data-kind", kind)
    expect(page.locator("[data-testid=state-badge]")).to_have_attribute("data-state", "DISABLED")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-KILL-1")
def test_owner_stop_then_start_buttons(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    btn = page.locator("[data-testid=kill-open]")
    expect(btn).to_have_text("Stop bot")
    btn.click()
    page.click("[data-testid=kill-off]")
    expect(page.locator("[data-testid=kill-state]")).to_have_text("Bot: Stopped")
    expect(btn).to_have_text("Start bot")
    expect(btn).to_have_class("start")
    btn.click()
    expect(page.locator("[data-testid=kill-dialog] h3:visible")).to_have_text("Start the bot")
    expect(page.locator("[data-testid=kill-off]")).to_be_hidden()
    page.click("[data-testid=kill-on-shadow]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Bot started")
    expect(page.locator("[data-testid=kill-state]")).to_have_text("Bot: Watching (Shadow)")
    expect(btn).to_have_text("Stop bot")
    page.reload()  # server-rendered header agrees with the live update
    expect(page.locator("[data-testid=kill-open]")).to_have_text("Stop bot")
    body = page.locator("body").inner_text().lower()
    assert "kill" not in body
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-KILL-3")
def test_kill_dialog_warns_when_hvac_off(page: Page, srv: Srv) -> None:
    from datetime import UTC, datetime

    from thermabot.core.types import Snapshot, ThermostatState

    srv.ctx.snapshot = Snapshot(t=datetime.now(UTC), thermostat=ThermostatState(hvac_mode="off"))
    login(page, srv, "alex")
    page.click("[data-testid=kill-open]")
    expect(page.locator("#kill-off-warning")).to_be_visible()
    expect(page.locator("[data-testid=kill-off-band]")).to_be_visible()
    page.click("[data-testid=kill-off-band]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("restore: skipped")
    assert_page_healthy(page, srv)
