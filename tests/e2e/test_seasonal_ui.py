from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from tests.house import house_settings
from thermabot.config import Settings


def _configured(srv: Srv) -> None:
    data = house_settings().model_dump(mode="json")
    data["presence"] = {"people": {}, "excluded": []}
    data["seasonal_control"] = {"enabled": True, "home": [70, 73], "sleep": [68, 70]}
    data["display"]["units"] = "C"
    srv.ctx.settings = Settings.model_validate(data)


@pytest.mark.brd("FR-CFG-1")
def test_weather_aware_settings_round_trip_in_celsius(page: Page, srv: Srv) -> None:
    _configured(srv)
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings")
    page.locator("[data-testid=section-seasonal_control] summary").click()
    expect(page.locator("[data-path='seasonal_control.home.0']")).to_have_value("21.1")
    expect(page.locator("[data-path='seasonal_control.sleep.1']")).to_have_value("21.1")
    page.fill("[data-path='seasonal_control.home.0']", "22")
    page.click("[data-testid=settings-save]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    assert srv.ctx.settings.seasonal_control.home == (71.5, 73)
    assert srv.ctx.settings.seasonal_control.sleep == (68, 70)
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CFG-1")
def test_wizard_edits_and_reviews_active_ranges_without_resetting_them(
    page: Page, srv: Srv
) -> None:
    _configured(srv)
    login(page, srv, "alex")
    page.goto(f"{srv.url}/setup")
    page.click("[data-testid=ha-test]")
    expect(page.locator("[data-testid=ha-result]")).to_contain_text("OK")
    for _ in range(4):
        page.click("[data-testid=setup-next]")
    expect(page.locator("[data-testid=seasonal-notice]")).to_be_visible()
    expect(page.locator("[data-testid=seasonal-band-home-0]")).to_have_value("21.1")
    expect(page.locator("[data-testid=seasonal-band-sleep-1]")).to_have_value("21.1")

    page.fill("[data-testid=seasonal-band-sleep-1]", "18")
    page.click("[data-testid=setup-next]")
    expect(page.locator("[data-testid=setup-errors]")).to_contain_text("weather-aware sleep")
    page.fill("[data-testid=seasonal-band-sleep-1]", "21.1")
    page.fill("[data-testid=seasonal-band-home-0]", "22")
    page.click("[data-testid=bands-defaults]")
    expect(page.locator("[data-testid=seasonal-band-home-0]")).to_have_value("22")
    page.click("[data-testid=setup-next]")
    page.click("[data-testid=setup-next]")
    expect(page.locator("[data-testid=review]")).to_contain_text("weather-aware 21.9–22.8 °C")
    expect(page.locator("[data-testid=review]")).to_contain_text("weather-aware 20–21.1 °C")
    page.click("[data-testid=setup-save]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    assert srv.ctx.settings.seasonal_control.home == (71.5, 73)
    assert srv.ctx.settings.seasonal_control.sleep == (68, 70)
    assert_page_healthy(page, srv)
