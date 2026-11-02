"""Mobile/desktop UAT of account creation and configuration-aware first-run routing."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import PW, Srv, assert_page_healthy
from tests.e2e.test_setup_wizard import _shot, run_wizard
from thermabot.adapters import db
from thermabot.app.routes_api import _kv_put, load_settings
from thermabot.config import Settings
from thermabot.core.statemachine import Persisted


def empty_install(srv: Srv) -> None:
    srv.ctx.conn.execute("DELETE FROM users")
    srv.ctx.settings = Settings()
    srv.ctx.running_entity = ""
    srv.ctx.mock_ha.states["binary_sensor.front_door"] = {  # type: ignore[attr-defined]
        "entity_id": "binary_sensor.front_door",
        "state": "off",
        "attributes": {"device_class": "door", "friendly_name": "Front door"},
    }
    db.write_bot_state(srv.ctx.conn, Persisted(kill_switch="off", run_mode="shadow"), by="test")


def fill_account(page: Page) -> None:
    page.get_by_label("Username", exact=True).fill("alex")
    page.get_by_label("Password", exact=True).fill(PW)
    page.get_by_label("Confirm password", exact=True).fill(PW)


@pytest.mark.brd("FR-USR-1")
@pytest.mark.parametrize("width", [375, 1440])
def test_account_creation_then_complete_wizard(page: Page, srv: Srv, width: int) -> None:
    empty_install(srv)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(srv.url + "/")
    expect(page).to_have_url(srv.url + "/first-run")
    expect(page.get_by_role("heading", name="Welcome to thermabot")).to_be_visible()
    expect(page.locator(".auth-mark")).to_be_visible()
    assert page.locator(".auth-mark").evaluate("img => img.complete && img.naturalWidth > 0")
    _shot(page, f"first-run-{width}.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    button = page.get_by_role("button", name="Create administrator")
    box = button.bounding_box()
    assert box is not None and box["height"] >= 44
    fill_account(page)
    button.click()
    page.wait_for_url(srv.url + "/setup")
    assert_page_healthy(page, srv)
    assert srv.ctx.eff().state == "DISABLED"
    run_wizard(page, srv)
    page.click("[data-testid=setup-save]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    page.goto(srv.url + "/login")
    expect(page).to_have_url(srv.url + "/")
    assert load_settings(srv.ctx).thermostat.entity_id == "climate.living_room"
    assert srv.ctx.eff().state == "DISABLED"
    assert srv.ctx.mock_ha.calls == []  # type: ignore[attr-defined]
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-USR-1")
def test_existing_config_goes_to_dashboard_without_wizard(page: Page, srv: Srv) -> None:
    saved = srv.ctx.settings
    empty_install(srv)
    _kv_put(srv.ctx, "settings", saved.model_dump(mode="json"), "existing")
    page.goto(srv.url + "/login")
    fill_account(page)
    page.get_by_role("button", name="Create administrator").click()
    page.wait_for_url(srv.url + "/")
    assert load_settings(srv.ctx) == saved
    assert srv.ctx.eff().state == "DISABLED"
    page.goto(srv.url + "/first-run")
    expect(page).to_have_url(srv.url + "/")
    assert_page_healthy(page, srv)


@pytest.mark.brd("NFR-SEC-3")
def test_password_confirmation_error_is_visible(page: Page, srv: Srv) -> None:
    empty_install(srv)
    page.goto(srv.url + "/first-run")
    fill_account(page)
    page.get_by_label("Confirm password", exact=True).fill("another password")
    page.get_by_role("button", name="Create administrator").click()
    expect(page.locator("[data-testid=errors]")).to_contain_text("Passwords do not match")
    assert srv.ctx.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    srv.errors.clear()  # expected rejected request is reported by Chromium
    page.get_by_label("Confirm password", exact=True).fill(PW)
    page.get_by_role("button", name="Create administrator").click()
    page.wait_for_url(srv.url + "/setup")
    assert_page_healthy(page, srv)
