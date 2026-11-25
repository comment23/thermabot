"""Desktop/mobile UAT for the command center and sensitive recovery workflows."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from thermabot import __version__
from thermabot.app.routes_api import load_settings


@pytest.mark.parametrize("width", [375, 768, 1440, 1920])
def test_controls_and_recovery_fit_all_screens(page: Page, srv: Srv, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    login(page, srv, "alex")
    expect(page.locator("[data-testid=quick-actions]")).to_be_visible()
    expect(page.locator("[data-testid=control-pause]")).to_be_visible()
    expect(page.locator("header [data-testid=kill-open]")).to_have_count(1)
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
    page.goto(f"{srv.url}/settings/safety")
    expect(page.locator("[data-testid=kill-on-active]")).to_be_disabled()
    status = page.evaluate(
        "async () => (await tb.api('POST', 'api/kill', {enabled: true, mode: 'active'})).status"
    )
    assert status == 409
    assert srv.ctx.eff().state == "SHADOW"
    srv.errors.clear()  # expected rejected 409 request is logged by Chromium
    page.click("[data-testid=kill-open]")
    page.click("[data-testid=kill-off]")
    page.click("[data-testid=kill-open]")  # Resume is one tap back to Shadow
    expect(page.locator("[data-testid=toast]")).to_contain_text("watching (Shadow)")
    assert srv.ctx.eff().state == "SHADOW"
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-MODE-3")
def test_owner_can_review_before_ready_without_bypassing_control_gate(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings/safety")
    expect(page.locator("[data-testid=readiness]")).to_have_attribute("data-ready", "no")
    expect(page.locator("[data-testid=reviewed-shadow]")).to_be_enabled()
    page.get_by_label(
        "I reviewed the seven-day Watch report, including degraded intervals and stale rooms."
    ).check()
    page.get_by_label(
        "I independently verified the Home Assistant safety net and understand its limits."
    ).check()
    page.fill("[data-testid=active-word]", "ACTIVE")
    expect(page.locator("[data-testid=kill-on-active]")).to_be_disabled()
    expect(page.locator("[data-testid=control-blockers]")).to_contain_text(
        "Seven-day Watch evidence"
    )
    assert srv.ctx.eff().state == "SHADOW"
    assert srv.ctx.mock_ha.calls == []  # type: ignore[attr-defined]
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-MODE-3")
def test_control_requires_review_in_the_browser(page: Page, srv: Srv) -> None:
    from tests.integration.test_kill_api import NOW, ready_for_control

    srv.ctx.clock = lambda: NOW
    srv.ctx.last_tick = NOW
    ready_for_control(srv.ctx)
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings/safety")
    expect(page.locator("[data-testid=readiness]")).to_have_attribute("data-ready", "yes")
    button = page.locator("[data-testid=kill-on-active]")
    page.fill("[data-testid=active-word]", "ACTIVE")
    expect(button).to_be_disabled()
    page.check("[data-testid=reviewed-shadow]")
    expect(button).to_be_disabled()
    page.check("[data-testid=reviewed-safety-net]")
    expect(button).to_be_enabled()
    button.click()
    expect(page.locator("[data-testid=toast]")).to_contain_text("controlling")
    assert srv.ctx.eff().state == "ACTIVE"
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-MODE-3")
def test_refresh_readiness_preserves_review_and_rechecks_blockers(page: Page, srv: Srv) -> None:
    from tests.integration.test_kill_api import NOW, ready_for_control

    srv.ctx.clock = lambda: NOW
    srv.ctx.last_tick = NOW
    ready_for_control(srv.ctx)
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings/safety")
    page.check("[data-testid=reviewed-shadow]")
    page.check("[data-testid=reviewed-safety-net]")
    page.fill("[data-testid=active-word]", "ACTIVE")
    expect(page.locator("[data-testid=kill-on-active]")).to_be_enabled()
    srv.ctx.ha_health = "down"
    page.click("[data-readiness-refresh]")
    expect(page.locator("[data-testid=readiness]")).to_have_attribute("data-ready", "no")
    expect(page.locator("[data-testid=kill-on-active]")).to_be_disabled()
    expect(page.locator("[data-testid=reviewed-shadow]")).to_be_checked()
    expect(page.locator("[data-testid=reviewed-safety-net]")).to_be_checked()
    srv.ctx.ha_health = "ok"
    page.click("[data-readiness-refresh]")
    expect(page.locator("[data-testid=kill-on-active]")).to_be_enabled()
    assert srv.ctx.eff().state == "SHADOW"
    assert srv.ctx.mock_ha.calls == []  # type: ignore[attr-defined]
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-MODE-3")
def test_safety_page_explains_how_to_refresh_after_stop_warning(page: Page, srv: Srv) -> None:
    from datetime import UTC, datetime

    from thermabot.adapters import db

    db.write_kv(
        srv.ctx.conn, "stop_handoff_warning", "Check the thermostat.", now=datetime.now(UTC)
    )
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings/safety")
    check = page.locator('[data-check="stop_warning"]')
    expect(check).to_have_attribute("data-ok", "no")
    page.click('[data-notice="stop_handoff"]')
    expect(page.locator("[data-testid=toast]")).to_contain_text("Reload this page")
    page.reload()
    expect(check).to_have_attribute("data-ok", "yes")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-KILL-1")
def test_failed_stop_shows_retry_warning_in_all_sessions(
    page: Page, srv: Srv, browser: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime

    from thermabot.adapters import db
    from thermabot.core.statemachine import Persisted

    page.add_init_script("document.documentElement.dataset.nosse = '1'")
    login(page, srv, "alex")
    admin = browser.new_page()
    try:
        login(admin, srv, "ann")
        admin.wait_for_function("document.documentElement.dataset.live === 'sse'")
        db.write_bot_state(srv.ctx.conn, Persisted(kill_switch="off", run_mode="shadow"), by="test")

        def fail_stop(
            conn: sqlite3.Connection, state: Persisted, by: str, now: datetime | None = None
        ) -> None:
            raise sqlite3.OperationalError("simulated Stop database failure")

        with monkeypatch.context() as broken:
            broken.setattr(db, "write_bot_state", fail_stop)
            page.click("[data-testid=kill-open]")
            page.click("[data-testid=kill-off]")
            expect(page.locator("[data-testid=toast]")).to_contain_text("Stop could not be saved")
            for client in (page, admin):
                warning = client.locator("[data-testid=stop-handoff-warning]")
                expect(warning).to_be_visible()
                expect(warning).to_contain_text("retry Stop")
                expect(client.locator("[data-testid=state-badge]")).to_have_attribute(
                    "data-state", "DISABLED"
                )
                expect(client.locator("[data-testid=retry-stop]")).to_be_visible()
                expect(client.locator('[data-notice="stop_handoff"]')).to_be_disabled()
            page.reload()
            expect(page.locator("[data-testid=retry-stop]")).to_be_visible()
            expect(page.locator("[data-testid=kill-open]")).to_be_disabled()

        page.click("[data-testid=retry-stop]")
        expect(page.locator("[data-testid=toast]")).to_contain_text("Bot stopped")
        expect(page.locator("[data-testid=retry-stop]")).to_be_hidden()
        expect(page.locator('[data-notice="stop_handoff"]')).to_be_enabled()
        expect(page.locator("[data-testid=stop-handoff-warning]")).to_contain_text(
            "previous Stop could not be saved"
        )
        srv.errors.clear()  # expected rejected 503 request
        assert_page_healthy(page, srv)
    finally:
        admin.context.close()


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
    page.goto(f"{srv.url}/settings")
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
    page.fill("[data-testid=confirm-typed]", "RESTORE")
    page.click("[data-testid=confirm-yes]")
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
    expect(page.locator("[data-testid=control-mode]")).to_have_count(0)
    expect(page.locator("[data-testid=recovery-link]")).to_have_count(0)
    expect(page.locator("[data-testid=control-pause]")).to_have_count(1 if user == "ann" else 0)
    expect(page.locator("[data-testid=quick-actions]")).to_have_count(1 if user == "ann" else 0)
    assert_page_healthy(page, srv)
