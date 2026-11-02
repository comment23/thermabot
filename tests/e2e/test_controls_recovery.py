"""Desktop/mobile UAT for the command center and sensitive recovery workflows."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from thermabot import __version__
from thermabot.app.routes_api import load_settings


@pytest.mark.parametrize("width", [375, 768, 1440, 1920])
def test_controls_and_recovery_fit_all_screens(page: Page, srv: Srv, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    login(page, srv, "alex")
    expect(page.locator("[data-testid=dashboard-controls]")).to_be_visible()
    expect(page.locator("[data-testid=control-mode]")).to_be_visible()
    expect(page.locator("[data-testid=control-pause]")).to_be_visible()
    expect(page.locator("header [data-testid=kill-open]")).to_have_count(0)
    for path in ("/", "/recovery"):
        page.goto(srv.url + path)
        expect(page.locator("[data-testid=app-version]")).to_have_text(f"v{__version__}")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        for button in page.locator("main button:visible, main [role=button]:visible").all():
            box = button.bounding_box()
            assert box is not None and box["height"] >= 43.5
        assert_page_healthy(page, srv)
    expect(page.locator("[data-testid=recovery-health]")).to_contain_text("HA connection: ok")


def test_mode_switch_keeps_active_soak_gate(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.click("[data-testid=control-mode]")
    page.click("[data-testid=kill-on-active]")
    expect(page.locator("[data-testid=toast]")).to_have_attribute("data-kind", "error")
    assert srv.ctx.eff().state == "SHADOW"
    srv.errors.clear()  # expected rejected 409 request is logged by Chromium
    page.click("[data-testid=control-mode]")
    page.click("[data-testid=kill-on-shadow]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("watching (Shadow)")
    assert srv.ctx.eff().state == "SHADOW"
    assert_page_healthy(page, srv)


def test_control_network_failure_is_visible(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.route("**/api/override", lambda route: route.abort())
    page.click("[data-testid=control-pause]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Cannot reach Thermabot")
    expect(page.locator("[data-testid=toast]")).to_have_attribute("data-kind", "error")
    assert srv.ctx.open_override() is None
    srv.errors.clear()  # expected aborted HTTP request, not an unhandled JS error
    assert_page_healthy(page, srv)


def test_pause_and_guest_controls_persist(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.click("[data-testid=control-pause]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Plan paused")
    assert srv.ctx.open_override() is not None
    page.reload()
    expect(page.locator("[data-testid=override-cancel]")).to_be_visible()
    page.click("[data-testid=guest-toggle]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    page.reload()
    expect(page.locator("[data-testid=guest-toggle]")).to_contain_text("ON")
    assert_page_healthy(page, srv)


def test_import_preview_apply_and_backup_workflow(page: Page, srv: Srv, tmp_path: Path) -> None:
    login(page, srv, "alex")
    page.click("[data-testid=kill-open]")
    page.click("[data-testid=kill-off]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Bot stopped")
    page.click("[data-testid=recovery-link]")
    document = page.request.get(srv.url + "/api/config/export").json()
    old_units = document["settings"]["display"]["units"]
    document["settings"]["display"]["units"] = "C" if old_units == "F" else "F"
    page.set_input_files(
        "[data-testid=config-file]",
        {
            "name": "config.json",
            "mimeType": "application/json",
            "buffer": json.dumps(document).encode(),
        },
    )
    page.click("[data-testid=config-preview]")
    expect(page.locator("[data-testid=import-changes]")).to_contain_text("display")
    assert load_settings(srv.ctx).display.units == old_units
    page.click("[data-testid=config-apply]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Configuration imported")
    assert load_settings(srv.ctx).display.units != old_units
    assert srv.ctx.eff().state == "DISABLED"
    with page.expect_download() as downloaded:
        page.click("[data-testid=backup-create]")
    dest = tmp_path / "download.db"
    downloaded.value.save_as(dest)
    with sqlite3.connect(dest) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    expect(page.locator("[data-testid=backup-list]")).to_contain_text("manual")
    assert_page_healthy(page, srv)


@pytest.mark.parametrize("user", ["ann", "jordan"])
def test_controls_respect_role(page: Page, srv: Srv, user: str) -> None:
    login(page, srv, user)
    expect(page.locator("[data-testid=dashboard-controls]")).to_be_visible()
    expect(page.locator("[data-testid=control-mode]")).to_have_count(0)
    expect(page.locator("[data-testid=recovery-link]")).to_have_count(0)
    expect(page.locator("[data-testid=control-pause]")).to_have_count(1 if user == "ann" else 0)
    assert_page_healthy(page, srv)
