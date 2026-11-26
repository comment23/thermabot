from __future__ import annotations

from typing import Literal

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from tests.house import house_settings
from thermabot.adapters import db


@pytest.mark.brd("FR-CFG-1")
@pytest.mark.parametrize(("width", "units"), [(375, "F"), (1440, "F"), (375, "C"), (1440, "C")])
def test_simple_preferences_preview_apply_and_advanced_preserve_existing_configuration(
    page: Page,
    srv: Srv,
    width: int,
    units: Literal["F", "C"],
) -> None:
    srv.ctx.settings = house_settings(display={"units": units})
    before = srv.ctx.settings.model_dump()
    state = db.read_bot_state(srv.ctx.conn)
    replans: list[int] = []
    srv.ctx.replan = lambda: replans.append(1)
    page.set_viewport_size({"width": width, "height": 900})
    login(page, srv, "ann")
    page.goto(f"{srv.url}/settings")
    expect(page.get_by_test_id("simple-ideal")).to_have_value("22.8" if units == "C" else "73")
    expect(page.get_by_test_id("saved-priority")).to_contain_text("Custom (preserved)")
    expect(page.get_by_test_id("section-bands")).to_have_count(0)
    expect(page.get_by_test_id("simple-preview")).to_be_disabled()
    for block, selected in (("home", "study"), ("sleep", "bedroom_1")):
        for checkbox in page.locator(f'input[name="{block}_rooms"]').all():
            checkbox.set_checked(checkbox.get_attribute("value") == selected)
    slider = page.get_by_test_id("simple-priority")
    slider.focus()
    slider.press("ArrowLeft")
    expect(slider).to_have_attribute("aria-valuetext", "Comfort")
    page.get_by_test_id("simple-confirm-mappings").check()
    page.get_by_test_id("simple-preview").click()
    preview = page.get_by_test_id("simple-preview-result")
    expect(preview).to_contain_text("Automatic-mode fallback widens")
    expect(page.get_by_test_id("simple-apply")).to_be_enabled()
    assert srv.ctx.settings.model_dump() == before and replans == []
    slider.focus()
    slider.press("ArrowRight")
    slider.press("ArrowRight")
    expect(slider).to_have_attribute("aria-valuetext", "Efficiency")
    expect(preview).to_be_hidden()
    expect(page.get_by_test_id("simple-apply")).to_be_hidden()
    page.get_by_test_id("simple-preview").click()
    expect(preview).to_contain_text("21.1°C–24.4°C" if units == "C" else "70°F–76°F")
    page.get_by_test_id("simple-apply").click()
    expect(page.get_by_test_id("confirm-sheet")).to_contain_text("operating mode")
    page.get_by_test_id("confirm-sheet").get_by_role("button", name="Cancel", exact=True).click()
    assert srv.ctx.settings.model_dump() == before and replans == []
    page.get_by_test_id("simple-apply").click()
    page.get_by_test_id("confirm-yes").click()
    expect(page.get_by_test_id("toast")).to_contain_text("Saved")
    page.reload()
    expect(page.get_by_test_id("saved-priority")).to_contain_text("Efficiency")
    assert srv.ctx.settings.insights.ideal_temp_f == 73
    assert srv.ctx.settings.seasonal_control.home == (70, 76)
    expected = before
    expected["seasonal_control"] = {"enabled": True, "home": (70.0, 76.0), "sleep": (70.0, 76.0)}
    expected["insights"].update(ideal_temp_f=73, tolerance_f=3)
    expected["weights"].update(home={"study": 1}, sleep={"bedroom_1": 1})
    assert srv.ctx.settings.model_dump() == expected and replans == [1]
    assert db.read_bot_state(srv.ctx.conn) == state and srv.ctx.mock_ha.calls == []
    page.get_by_test_id("simple-advanced").locator("summary").click()
    page.get_by_role("link", name="Open Advanced", exact=True).click()
    expect(page.get_by_test_id("section-bands")).to_be_visible()
    expect(page.get_by_test_id("section-insights")).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CFG-1")
def test_edited_celsius_and_deadband_conflict_are_handled_explicitly(page: Page, srv: Srv) -> None:
    srv.ctx.settings = house_settings(display={"units": "C"})
    before = srv.ctx.settings.model_dump()
    state = db.read_bot_state(srv.ctx.conn)
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings")
    page.get_by_test_id("simple-priority").focus()
    page.get_by_test_id("simple-priority").press("ArrowLeft")
    page.get_by_test_id("simple-confirm-mappings").check()
    page.get_by_test_id("simple-ideal").fill("25.6")
    page.get_by_test_id("simple-preview").click()
    expect(page.get_by_test_id("simple-errors")).to_contain_text("thermostat deadband")
    expect(page.get_by_test_id("simple-apply")).to_be_hidden()
    assert srv.ctx.settings.model_dump() == before
    assert srv.errors == [
        "console: Failed to load resource: the server responded with a status of "
        "422 (Unprocessable Entity)"
    ]
    srv.errors.clear()
    page.get_by_test_id("simple-ideal").fill("22.0")
    page.get_by_test_id("simple-preview").click()
    expect(page.get_by_test_id("simple-apply")).to_be_enabled()
    page.get_by_test_id("simple-apply").click()
    page.get_by_test_id("confirm-yes").click()
    expect(page.get_by_test_id("toast")).to_contain_text("Saved")
    assert srv.ctx.settings.insights.ideal_temp_f == 71.5
    assert srv.ctx.settings.seasonal_control.home == (70.5, 72.5)
    assert db.read_bot_state(srv.ctx.conn) == state and srv.ctx.mock_ha.calls == []
    assert_page_healthy(page, srv)
