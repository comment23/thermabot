"""UAT: use customer actions, not hand-seeded snapshots or replacement controllers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, expect

from scripts.uat import Lab, Scenario, create_lab
from tests.e2e.conftest import PW, Srv, assert_page_healthy, login
from tests.e2e.test_first_run_ui import fill_account
from tests.e2e.test_setup_wizard import run_wizard
from tests.uat.conftest import customer_page, running_lab


def apply_scenario(page: Page, srv: Srv, name: Scenario) -> dict[str, Any]:
    response = page.request.post(
        srv.url + "/uat/api/scenario",
        headers={"X-Thermabot-UAT": "1"},
        data={"name": name},
    )
    assert response.ok, response.text()
    data: dict[str, Any] = response.json()
    assert data["scenario"] == name
    return data


@pytest.mark.brd("FR-USR-1")
@pytest.mark.brd("FR-CFG-1")
@pytest.mark.parametrize("width", [390, 1440])
def test_customer_first_run_setup_and_real_restart(
    browser: Browser, tmp_path: Path, width: int
) -> None:
    data_dir = tmp_path / "fresh-customer"
    lab = create_lab(data_dir, fresh=True)
    with running_lab(lab) as srv, customer_page(browser, srv, width) as page:
        page.goto(srv.url + "/")
        expect(page).to_have_url(srv.url + "/first-run")
        assert lab.evidence()["climate_reads"] == []
        fill_account(page)
        page.get_by_role("button", name="Create administrator").click()
        page.wait_for_url(srv.url + "/setup")
        run_wizard(page, srv)
        page.click("[data-testid=setup-save]")
        expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
        page.goto(srv.url + "/")
        expect(page.locator("[data-testid=restart-banner]")).to_contain_text("Restart")
        assert lab.evidence()["kill_switch"] == "off"
        assert lab.evidence()["climate_reads"] == []
        assert lab.evidence()["climate_service_calls"] == []
        assert_page_healthy(page, srv)

    restarted = create_lab(data_dir, fresh=True)
    with running_lab(restarted) as srv, customer_page(browser, srv, width) as page:
        login(page, srv, "alex")
        # The wizard selected Celsius display; 70.5 F is 21.4 C.
        expect(page.locator("[data-testid=room-study] .room-temp")).to_have_text("21.4°C")
        person = restarted.runtime.ctx.settings.presence.people["alex"]
        assert "person.alex" in {person.life360, person.companion}
        assert restarted.evidence()["run_mode"] == "shadow"
        assert restarted.evidence()["kill_switch"] == "off"
        assert restarted.evidence()["climate_service_calls"] == []
        assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-1")
@pytest.mark.parametrize(("user", "width"), [("alex", 390), ("ann", 1440)])
def test_customer_settings_tabs_survive_utility_navigation(
    page: Page, srv: Srv, user: str, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    login(page, srv, user)
    page.goto(srv.url + "/settings")
    labels = ["Account", "Health"] if user == "ann" else ["Backup", "Health", "Account", "Users"]
    for label in labels:
        nav = page.get_by_role("navigation", name="Settings", exact=True)
        nav.get_by_role("link", name=label, exact=True).click()
        expect(nav).to_be_visible()
        expect(nav.get_by_role("link", name=label, exact=True)).to_have_attribute(
            "aria-current", "page"
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        nav.get_by_role("link", name="Comfort", exact=True).click()
        page.wait_for_url(srv.url + "/settings")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-PRS-1")
@pytest.mark.brd("FR-CFG-1")
def test_customer_adds_and_removes_presence_people(page: Page, srv: Srv, lab: Lab) -> None:
    login(page, srv, "alex")
    page.goto(srv.url + "/settings/people")
    page.get_by_role("button", name="Add person", exact=True).click()
    row = page.locator("[data-testid=person-row]").last
    row.locator("[data-p=name]").fill("Sam")
    row.locator("[data-p=companion]").fill("person.alex")
    assert "sam" not in lab.runtime.ctx.settings.presence.people
    page.click("[data-testid=settings-save]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    page.reload()
    assert lab.runtime.ctx.settings.presence.people["sam"].companion == "person.alex"
    row = page.locator("[data-testid=person-row]").last
    row.get_by_role("button", name="Remove person", exact=True).click()
    page.click("[data-testid=confirm-yes]")
    assert "sam" in lab.runtime.ctx.settings.presence.people
    page.click("[data-testid=settings-save]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    page.reload()
    assert "sam" not in lab.runtime.ctx.settings.presence.people
    assert "alex" in lab.runtime.ctx.settings.presence.people
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-2")
@pytest.mark.brd("FR-HA-1")
@pytest.mark.parametrize("scenario", ["celsius", "stale-room", "missing-room", "ha-outage"])
def test_customer_sees_runtime_sensor_fault_and_recovery(
    page: Page, srv: Srv, scenario: Scenario
) -> None:
    login(page, srv, "alex")
    room = page.locator("[data-testid=room-study]")
    expect(room.locator(".room-temp")).to_have_text("70.5°F")
    evidence = apply_scenario(page, srv, scenario)
    if scenario == "celsius":
        expect(room.locator(".room-temp")).to_have_text("68°F")
        assert evidence["latest_sample"]["rooms"]["study"]["temp"] == 68
        reading = evidence["snapshot"]["rooms"]["study"]
        assert reading["temp"] == 68 and not reading["stale"]
    elif scenario == "stale-room":
        expect(room).to_contain_text("Stale")
        expect(room.locator(".room-temp")).to_have_count(0)
        assert evidence["latest_sample"]["rooms"]["study"]["age_min"] == 120
    else:
        expect(room).to_contain_text("Waiting")
        if scenario == "ha-outage":
            expect(page.locator("#ha-connection-warning")).to_be_visible()
            assert evidence["ha_health"] == "down" and evidence["snapshot"] is None
    recovered = apply_scenario(page, srv, "healthy")
    expect(room.locator(".room-temp")).to_have_text("70.5°F")
    expect(page.locator("#ha-connection-warning")).to_be_hidden()
    assert recovered["ha_health"] == "ok"
    assert recovered["latest_sample"]["rooms"]["study"]["temp"] == 70.5
    if evidence["snapshot"] is not None:
        assert recovered["snapshot"]["t"] > evidence["snapshot"]["t"]
    status = page.request.get(srv.url + "/api/status").json()
    assert next(r for r in status["rooms"] if r["key"] == "study")["temp"] == 70.5
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-KILL-1")
@pytest.mark.brd("FR-KILL-2")
def test_customer_starts_watch_and_stops_without_writes(page: Page, srv: Srv, lab: Lab) -> None:
    login(page, srv, "alex")
    expect(page.locator("[data-testid=state-badge]")).to_have_attribute("data-state", "DISABLED")
    page.locator("[data-watch-action]").click()
    expect(page.locator("[data-testid=state-badge]")).to_have_attribute("data-state", "SHADOW")
    apply_scenario(page, srv, "healthy")
    assert lab.evidence()["kill_switch"] == "on"
    assert lab.transport.ha.states["input_boolean.thermabot_enabled"]["state"] == "on"
    page.locator("[data-watch-action]").click()
    page.click("[data-testid=kill-off]")
    expect(page.locator("[data-testid=state-badge]")).to_have_attribute("data-state", "DISABLED")
    assert lab.evidence()["kill_switch"] == "off"
    assert lab.evidence()["climate_service_calls"] == []
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-USR-2")
def test_customer_member_cannot_open_privileged_settings(page: Page, srv: Srv) -> None:
    login(page, srv, "jordan")
    expect(
        page.get_by_role("navigation", name="Main").get_by_role("link", name="Settings")
    ).to_have_count(0)
    for path in ("/settings", "/users", "/recovery"):
        assert page.request.get(srv.url + path).status == 403
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-1")
def test_customer_reviews_plan_and_read_only_diagnostics(page: Page, srv: Srv, lab: Lab) -> None:
    login(page, srv, "alex")
    nav = page.get_by_role("navigation", name="Main")
    for label, path in (("Plan", "/plan"), ("Insights", "/insights")):
        nav.get_by_role("link", name=label, exact=True).click()
        page.wait_for_url(srv.url + path)
        assert_page_healthy(page, srv)
    for path in ("/history", "/forecast", "/efficiency", "/shadow", "/model", "/health"):
        response = page.goto(srv.url + path)
        assert response is not None and response.status == 200
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert_page_healthy(page, srv)
    plan = lab.runtime.ctx.plan
    assert plan is not None and plan.steps
    assert all(step.hvac_mode != "off" for step in plan.steps)


@pytest.mark.brd("FR-UI-2")
def test_customer_lab_panel_changes_real_runtime(page: Page, srv: Srv) -> None:
    page.goto(srv.url + "/uat")
    expect(page.locator("#result")).to_contain_text("Zero climate writes")
    page.select_option("#scenario", "celsius")
    page.get_by_role("button", name="Apply scenario and sample now").click()
    expect(page.locator("#result")).to_contain_text("celsius")
    evidence = page.request.get(srv.url + "/uat/api/status").json()
    assert evidence["snapshot"]["rooms"]["study"]["temp"] == 68
    assert PW in page.locator("main").inner_text()
