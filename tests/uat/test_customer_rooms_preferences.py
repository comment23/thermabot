"""0.7 customer acceptance journeys against real runtime and simulated devices."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from scripts.uat import MARKER, Lab, create_lab
from tests.e2e.conftest import Srv, assert_page_healthy, login
from tests.e2e.test_p3_settings import _open
from tests.uat.conftest import customer_page, record_evidence, running_lab
from tests.uat.test_customer_journeys import apply_scenario
from thermabot.core.schedule import active_zone


def accept_expected_http_error(srv: Srv, status: int, reason: str) -> None:
    assert srv.errors == [
        "console: Failed to load resource: the server responded with a status of "
        f"{status} ({reason})"
    ]
    srv.errors.clear()


@pytest.mark.brd("FR-CFG-1")
@pytest.mark.brd("FR-CMF-1")
@pytest.mark.parametrize(
    ("user", "width", "units", "theme", "priority", "band"),
    [
        ("alex", 390, "F", "light", "Comfort", (72, 74)),
        ("ann", 1440, "C", "dark", "Balanced", (71, 75)),
        ("alex", 768, "F", "light", "Efficiency", (70, 76)),
    ],
)
def test_customer_reviews_preferences_before_real_controller_adoption(
    page: Page,
    srv: Srv,
    lab: Lab,
    user: str,
    width: int,
    units: str,
    theme: str,
    priority: str,
    band: tuple[float, float],
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(color_scheme=theme)
    login(page, srv, user)
    if units == "C":
        page.goto(srv.url + "/settings/advanced")
        _open(page, "display")
        page.select_option("[data-path='display.units']", "C")
        page.get_by_test_id("settings-save").click()
        expect(page.get_by_test_id("toast")).to_contain_text("Saved")
    before = lab.runtime.ctx.settings.model_dump()
    page.goto(srv.url + "/settings")
    expect(page.get_by_test_id("saved-priority")).to_contain_text("Custom (preserved)")
    expect(page.get_by_test_id("simple-ideal")).to_have_value("22.8" if units == "C" else "73.0")
    for block, selected in (("home", "study"), ("sleep", "bedroom_1")):
        for checkbox in page.locator(f'input[name="{block}_rooms"]').all():
            checkbox.set_checked(checkbox.get_attribute("value") == selected)
    slider = page.get_by_test_id("simple-priority")
    slider.focus()
    slider.press("ArrowLeft")
    expect(slider).to_have_attribute("aria-valuetext", "Comfort")
    expect(page.get_by_test_id("simple-preview")).to_be_disabled()
    page.get_by_test_id("simple-confirm-mappings").check()
    page.get_by_test_id("simple-preview").click()
    expect(page.get_by_test_id("simple-preview-result")).to_contain_text(
        "Automatic-mode fallback widens"
    )
    assert lab.runtime.ctx.settings.model_dump() == before
    page.get_by_test_id("simple-apply").click()
    page.get_by_test_id("confirm-sheet").get_by_role("button", name="Cancel").click()
    assert lab.runtime.ctx.settings.model_dump() == before
    slider.focus()
    slider.press("ArrowRight")
    expect(page.get_by_test_id("simple-preview-result")).to_be_hidden()
    expect(page.get_by_test_id("simple-apply")).to_be_hidden()
    if priority == "Comfort":
        slider.press("ArrowLeft")
    elif priority == "Efficiency":
        slider.press("ArrowRight")
    page.get_by_test_id("simple-preview").click()
    expect(page.get_by_test_id("simple-apply")).to_be_enabled()
    page.get_by_test_id("simple-apply").click()
    page.get_by_test_id("confirm-yes").click()
    expect(page.get_by_test_id("toast")).to_contain_text("Saved")
    apply_scenario(page, srv, "healthy")
    saved = lab.runtime.controller.settings
    assert saved.seasonal_control.enabled and saved.seasonal_control.home == band
    assert saved.seasonal_control.sleep == band and saved.insights.ideal_temp_f == 73
    assert saved.weights["home"] == {"study": 1} and saved.weights["sleep"] == {"bedroom_1": 1}
    for field in (
        "safety",
        "writes",
        "off",
        "rooms",
        "bands",
        "schedule",
        "presence",
        "thermostat",
    ):
        assert saved.model_dump()[field] == before[field]
    assert saved.weights["away"] == before["weights"]["away"]
    assert lab.evidence()["kill_switch"] == "off"
    page.reload()
    expect(page.get_by_test_id("saved-priority")).to_contain_text(priority)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CFG-1")
def test_customer_old_preview_is_rejected_after_another_tab_saves(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.goto(srv.url + "/settings")
    page.get_by_test_id("simple-confirm-mappings").check()
    page.get_by_test_id("simple-preview").click()
    expect(page.get_by_test_id("simple-apply")).to_be_enabled()
    other = page.context.new_page()
    try:
        other.goto(srv.url + "/settings/advanced")
        _open(other, "insights")
        other.fill("[data-path='insights.currency']", "CAD")
        other.get_by_test_id("settings-save").click()
        expect(other.get_by_test_id("toast")).to_contain_text("Saved")
        page.get_by_test_id("simple-apply").click()
        page.get_by_test_id("confirm-yes").click()
        expect(page.get_by_test_id("simple-errors")).to_contain_text("Settings changed")
        assert not srv.ctx.settings.seasonal_control.enabled
        assert srv.ctx.settings.insights.currency == "CAD"
        accept_expected_http_error(srv, 409, "Conflict")
        page.reload()
        expect(page.get_by_test_id("simple-apply")).to_be_hidden()
    finally:
        other.close()
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CFG-1")
@pytest.mark.brd("FR-UI-2")
def test_customer_room_measurements_and_meter_scope_follow_real_runtime(
    page: Page,
    srv: Srv,
    lab: Lab,
) -> None:
    login(page, srv, "alex")
    page.goto(srv.url + "/settings/people")
    room = page.locator("[data-home-room=study]")
    room.locator("[data-r=humidity]").select_option("sensor.study_humidity")
    room.locator("[data-r=energy]").select_option("sensor.study_energy")
    room.locator("[data-room-devices] summary").click()
    room.locator("[data-room-device-picker]").select_option("climate.living_room")
    room.locator("[data-room-device-add]").click()
    assert not lab.runtime.ctx.settings.rooms["study"].energy
    page.get_by_test_id("settings-save").click()
    expect(page.get_by_test_id("toast")).to_contain_text("Saved")
    before = lab.runtime.ctx.settings.model_dump()
    evidence = apply_scenario(page, srv, "room-evidence")
    assert evidence["historical_replay_minutes"] == 3
    reading = evidence["latest_sample"]["rooms"]["study"]
    assert reading["temp"] == 73 and reading["humidity"]["value"] == 65
    assert reading["energy"]["value"] == pytest.approx(10.2)
    page.goto(srv.url + "/room?room=study")
    expect(page.get_by_test_id("reading")).to_have_text("73°F")
    expect(page.get_by_test_id("room-ideal-delta")).to_have_text("0°F")
    expect(page.get_by_test_id("humidity-comfort")).to_contain_text("65.0% RH")
    expect(page.get_by_test_id("room-low-coverage")).to_be_visible()
    expect(page.get_by_test_id("weekly-room-actions")).to_contain_text("Weekly room action report")
    tabs = page.get_by_role("navigation", name="Room sections")
    tabs.get_by_role("link", name="Temperatures", exact=True).click()
    expect(page.get_by_test_id("room-temperature-chart").locator("canvas").first).to_be_visible()
    expect(page.get_by_test_id("room-temperature-table")).to_contain_text("73°F")
    chart = page.locator("#room-temperature-data").text_content()
    assert chart is not None and "null" in chart
    ranges = page.get_by_role("navigation", name="Room history range")
    for label in ("7 days", "30 days"):
        link = ranges.get_by_role("link", name=label, exact=True)
        link.click()
        expect(link).to_have_attribute("aria-current", "page")
        expect(page.get_by_test_id("room-period-label")).to_contain_text(
            f"Selected history: {label}"
        )
    tabs.get_by_role("link", name="Efficiency", exact=True).click()
    expect(page.get_by_test_id("meter-energy")).to_contain_text("dedicated room meter")
    expect(page.get_by_test_id("meter-energy")).to_contain_text("0.200 kWh")
    expect(page.get_by_test_id("weather-performance")).to_contain_text("Collecting comparable")
    expect(page.get_by_test_id("heat-pump-diagnostics")).to_contain_text("Central HVAC")
    expect(page.get_by_test_id("room-runtime")).to_contain_text("whole-system observations")
    assert lab.runtime.ctx.settings.model_dump() == before
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CFG-1")
def test_customer_experiment_survives_application_restart(
    browser: Browser,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "experiment-restart"
    original = create_lab(data_dir)
    with running_lab(original) as srv, customer_page(browser, srv) as page:
        login(page, srv, "alex")
        page.goto(srv.url + "/room?room=study&view=tools")
        form = page.get_by_test_id("experiment-form")
        form.get_by_role("textbox", name="Improvement name").fill("UAT persisted improvement")
        form.get_by_role("button", name="Record improvement").click()
        expect(page.get_by_test_id("toast")).to_contain_text("Experiment recorded")
        assert_page_healthy(page, srv)
    restarted = create_lab(data_dir)
    with running_lab(restarted) as srv, customer_page(browser, srv) as page:
        login(page, srv, "alex")
        page.goto(srv.url + "/room?room=study&view=tools")
        expect(page.get_by_test_id("room-experiments")).to_contain_text("UAT persisted improvement")
        evidence = restarted.evidence()
        assert evidence["run_mode"] == "shadow" and evidence["kill_switch"] == "off"
        assert evidence["climate_service_calls"] == evidence["blocked_climate_requests"] == []
        assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CFG-1")
def test_customer_room_tools_explain_missing_evidence_and_persist_experiment(
    page: Page,
    srv: Srv,
    lab: Lab,
) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    login(page, srv, "alex")
    page.goto(srv.url + "/room?room=study&view=tools")
    before = lab.runtime.ctx.settings.model_dump()
    expect(page.get_by_test_id("readiness-model")).to_have_attribute("data-ready", "false")
    expect(page.get_by_test_id("schedule-suggestions")).to_contain_text(
        "No repeatable schedule mismatch"
    )
    for kind in ("setback", "tariff"):
        form = page.get_by_test_id(f"simulation-{kind}")
        form.get_by_role("button").click()
        expect(form.locator("[data-simulation-result]")).to_contain_text(
            "Unavailable: Fit and validate the thermal model"
            if kind == "setback"
            else "Unavailable: Configure distinct peak/off-peak tariffs"
        )
        expect(form.locator(".errors")).to_be_empty()
        assert lab.runtime.ctx.settings.model_dump() == before
    experiment = page.get_by_test_id("experiment-form")
    experiment.get_by_role("textbox", name="Improvement name").fill("UAT filter change")
    experiment.get_by_role("button", name="Record improvement").click()
    expect(page.get_by_test_id("toast")).to_contain_text("Experiment recorded")
    page.reload()
    expect(page.get_by_test_id("room-experiments")).to_contain_text("UAT filter change")
    expect(page.get_by_test_id("room-experiments")).to_contain_text(
        "collecting comparable evidence"
    )
    assert lab.runtime.conn.execute("SELECT COUNT(*) FROM room_experiments").fetchone()[0] == 1
    assert lab.runtime.ctx.settings.model_dump() == before
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-USR-2")
def test_customer_member_room_cannot_mutate_other_rooms_or_preferences(
    page: Page,
    srv: Srv,
    lab: Lab,
) -> None:
    login(page, srv, "jordan")
    assert page.request.get(srv.url + "/room?room=study&view=tools").status == 403
    page.goto(srv.url + "/room?view=tools")
    expect(page.locator("h2")).to_have_text("bedroom_1")
    expect(page.get_by_test_id("experiment-form")).to_have_count(0)
    assert page.request.get(srv.url + "/api/rooms/study/insights").status == 403
    csrf = page.locator("meta[name=csrf-token]").get_attribute("content")
    assert csrf is not None
    response = page.request.post(
        srv.url + "/api/rooms/study/experiments",
        headers={
            "X-CSRF-Token": csrf,
        },
        data={"name": "UAT change", "change_day": lab.runtime.clock().date().isoformat()},
    )
    assert response.status == 403
    assert page.request.get(srv.url + "/settings").status == 403
    assert lab.runtime.conn.execute("SELECT COUNT(*) FROM room_experiments").fetchone()[0] == 0
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-MET-1")
@pytest.mark.brd("FR-CMF-2")
@pytest.mark.brd("NFR-REL-2")
def test_customer_qualified_history_approval_and_offline_experiment_restore(
    browser: Browser, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    data_dir = tmp_path / "history-customer"
    original = create_lab(data_dir)
    backup = tmp_path / "generic-backup.db"
    future = original.runtime.clock() + timedelta(days=21)
    with running_lab(original) as srv, customer_page(browser, srv) as page:
        login(page, srv, "alex")
        page.goto(srv.url + "/settings/people")
        room = page.locator("[data-home-room=study]")
        room.locator("[data-r=humidity]").select_option("sensor.study_humidity")
        room.locator("[data-r=energy]").select_option("sensor.study_energy")
        page.get_by_test_id("settings-save").click()
        expect(page.get_by_test_id("toast")).to_contain_text("Saved")
        page.goto(srv.url + "/settings/ha")
        _open(page, "sensors")
        page.fill(
            "[data-path='sensors.auxiliary_heat']", "binary_sensor.living_room_auxiliary_heat"
        )
        page.get_by_test_id("settings-save").click()
        expect(page.get_by_test_id("toast")).to_contain_text("Saved")
        before = original.runtime.ctx.settings.model_dump()
        response = page.request.post(
            srv.url + "/uat/api/scenario",
            headers={"X-Thermabot-UAT": "1"},
            data={"name": "room-history"},
            timeout=600_000,
        )
        assert response.ok, response.text()
        evidence = response.json()
        assert evidence["replay"]["complete"] and evidence["historical_replay_minutes"] == 14401
        assert len(evidence["replay"]["days"]) == 10
        rows = original.runtime.conn.execute(
            "SELECT heat_min, hdd, coverage_pct FROM efficiency_daily WHERE n_samples=1440"
        ).fetchall()
        assert len(rows) == 10
        assert sorted(row["heat_min"] for row in rows) == [96] * 5 + [120] * 5
        assert all(row["hdd"] == 20 and row["coverage_pct"] == 100 for row in rows)
        assert original.runtime.ctx.settings.model_dump() == before
        page.goto(srv.url + "/room?room=study")
        result = page.request.get(srv.url + "/api/rooms/study/insights").json()
        assert result["quality"]["persistent_disagreement"]
        assert result["quality"]["paired_minutes"] >= 120
        assert result["humidity"]["outside_rh_minutes"] >= 120
        assert result["equipment"]["cycling_alert"] and result["equipment"]["aux_minutes"] > 0
        assert result["weather"]["available"] and result["weather"]["change_pct"] == 25
        expect(page.get_by_test_id("sensor-diagnostics")).to_contain_text("Persistent room")
        actions = page.get_by_test_id("weekly-room-actions")
        for title in (
            "Check sensor placement",
            "Review short cycling",
            "Review humidity",
            "Investigate weather-normalized runtime increase",
            "Review auxiliary-heat use",
        ):
            expect(actions).to_contain_text(title)
        page.get_by_role("navigation", name="Room sections").get_by_role(
            "link", name="Efficiency", exact=True
        ).click()
        expect(page.get_by_test_id("weather-performance")).to_contain_text("25.0% change")
        expect(page.get_by_test_id("heat-pump-diagnostics")).to_contain_text(
            "Repeated short cycling"
        )
        record_evidence(page, original, request, "qualified-weather-diagnostics")
        page.goto(srv.url + "/room?room=study&view=tools")
        experiment = page.get_by_test_id("experiment-form")
        experiment.get_by_role("textbox", name="Improvement name").fill("UAT filter comparison")
        change = original.runtime.clock().astimezone(active_zone()).date() - timedelta(days=14)
        experiment.locator("input[name=change_day]").fill(change.isoformat())
        experiment.get_by_role("button", name="Record improvement").click()
        expect(page.get_by_test_id("room-experiments")).to_contain_text(
            "25.0% central runtime/DD change"
        )
        expect(page.get_by_test_id("room-experiments")).not_to_contain_text("Settings changed")
        old = page.request.get(srv.url + "/api/rooms/study/insights").json()
        candidate = next(s for s in old["suggestions"] if s["id"] == "weekday-10")
        assert candidate["days"] == 10 and candidate["away_pct"] >= 80
        with customer_page(browser, srv) as member:
            login(member, srv, "jordan")
            member.goto(srv.url + "/room?view=tools")
            expect(member.get_by_test_id("schedule-suggestions")).to_contain_text("10:00")
            expect(member.get_by_test_id("schedule-approval")).to_have_count(0)
            csrf = member.locator("meta[name=csrf-token]").get_attribute("content")
            assert csrf
            denied = member.request.post(
                srv.url + "/api/rooms/bedroom_1/schedule-approval",
                headers={"X-CSRF-Token": csrf},
                data={"suggestion_id": candidate["id"], "fingerprint": old["fingerprint"]},
            )
            assert denied.status == 403
            assert_page_healthy(member, srv)
        with customer_page(browser, srv) as other:
            login(other, srv, "alex")
            other.goto(srv.url + "/room?room=study&view=tools")
            form = page.get_by_test_id("schedule-approval").filter(
                has=page.locator("input[value='weekday-10']")
            )
            form.get_by_role("button").click()
            page.get_by_test_id("confirm-sheet").get_by_role("button", name="Cancel").click()
            assert original.runtime.ctx.settings.model_dump() == before
            form.get_by_role("button").click()
            with page.expect_response("**/api/rooms/study/schedule-approval") as approved:
                page.get_by_test_id("confirm-yes").click()
            assert approved.value.status == 200
            stale = other.get_by_test_id("schedule-approval").filter(
                has=other.locator("input[value='weekday-10']")
            )
            stale.get_by_role("button").click()
            other.get_by_test_id("confirm-yes").click()
            expect(stale.locator(".errors")).to_contain_text("changed")
            accept_expected_http_error(srv, 409, "Conflict")
        apply_scenario(page, srv, "healthy")
        saved = original.runtime.controller.settings.model_dump()
        assert all(saved[field] == value for field, value in before.items() if field != "schedule")
        assert ("10:00", "away") in [
            (block.start, block.block)
            for block in original.runtime.controller.settings.schedule.weekday
        ]
        page.reload()
        expect(page.get_by_test_id("room-experiments")).to_contain_text(
            "Settings changed after this experiment"
        )
        record_evidence(page, original, request, "qualified-approved-experiment")
        page.goto(srv.url + "/recovery")
        with page.expect_download() as downloaded:
            page.get_by_test_id("backup-create").click()
        downloaded.value.save_as(backup)
        with sqlite3.connect(backup) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT COUNT(*) FROM room_experiments").fetchone()[0] == 1
        assert_page_healthy(page, srv)
        assert original.evidence()["climate_service_calls"] == []
        assert original.evidence()["blocked_climate_requests"] == []
    # Original app has shut down. Restore only its generic copy into a new marked lab.
    restored_dir = tmp_path / "offline-restored-customer"
    restored_dir.mkdir()
    shutil.copyfile(data_dir / MARKER, restored_dir / MARKER)
    shutil.copyfile(backup, restored_dir / "thermabot.db")
    restored = create_lab(restored_dir, clock=lambda: future)
    with running_lab(restored) as srv, customer_page(browser, srv) as page:
        login(page, srv, "alex")
        page.goto(srv.url + "/room?room=study&view=tools")
        expect(page.get_by_test_id("room-experiments")).to_contain_text("UAT filter comparison")
        expect(page.get_by_test_id("room-experiments")).to_contain_text(
            "25.0% central runtime/DD change"
        )
        expect(page.get_by_test_id("room-experiments")).to_contain_text("Settings changed")
        assert restored.runtime.ctx.settings.model_dump() == saved
        evidence = restored.evidence()
        assert evidence["run_mode"] == "shadow" and evidence["kill_switch"] == "off"
        assert evidence["climate_service_calls"] == evidence["blocked_climate_requests"] == []
        record_evidence(page, restored, request, "offline-restored-experiment-21-simulated-days")
        assert_page_healthy(page, srv)


@pytest.mark.brd("FR-PLN-3")
@pytest.mark.brd("FR-MET-1")
def test_customer_fits_replayed_model_and_compares_read_only_tools(
    page: Page, srv: Srv, lab: Lab, request: pytest.FixtureRequest
) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    login(page, srv, "alex")
    response = page.request.post(
        srv.url + "/uat/api/scenario",
        headers={"X-Thermabot-UAT": "1"},
        data={"name": "room-model"},
        timeout=600_000,
    )
    assert response.ok, response.text()
    evidence = response.json()
    assert evidence["replay"]["complete"] and evidence["historical_replay_minutes"] == 4321
    page.goto(srv.url + "/model")
    page.get_by_test_id("refit").click()
    expect(page.get_by_test_id("toast")).to_contain_text("Refit started")
    page.reload()
    expect(page.get_by_test_id("model-meta")).to_contain_text("Version")
    model = lab.runtime.conn.execute(
        "SELECT mae_json,n_samples FROM models WHERE active=1 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    assert model is not None and model["n_samples"] >= 192
    assert json.loads(model["mae_json"])["thermostat"] < 0.1
    page.goto(srv.url + "/settings/advanced")
    _open(page, "insights")
    for field, value in (("off_peak_rate", "0.1"), ("peak_rate", "0.1"), ("rated_power_kw", "3")):
        page.fill(f"[data-path='insights.{field}']", value)
    page.get_by_test_id("settings-save").click()
    expect(page.get_by_test_id("toast")).to_contain_text("Saved")
    flat = lab.runtime.ctx.settings.model_dump()
    page.goto(srv.url + "/room?room=study&view=tools")
    expect(page.get_by_test_id("readiness-tariff")).to_have_attribute("data-ready", "false")
    form = page.get_by_test_id("simulation-tariff")
    form.get_by_role("button").click()
    expect(form.locator("[data-simulation-result]")).to_contain_text(
        "Unavailable: Configure distinct peak/off-peak tariffs"
    )
    assert lab.runtime.ctx.settings.model_dump() == flat
    page.goto(srv.url + "/settings/advanced")
    _open(page, "insights")
    page.fill("[data-path='insights.peak_rate']", "0.4")
    page.get_by_test_id("settings-save").click()
    expect(page.get_by_test_id("toast")).to_contain_text("Saved")
    page.goto(srv.url + "/room?room=study&view=tools")
    for key in ("room", "thermostat", "forecast", "model", "tariff", "power"):
        expect(page.get_by_test_id(f"readiness-{key}")).to_have_attribute("data-ready", "true")
    before = lab.runtime.ctx.settings.model_dump()
    for kind in ("setback", "tariff"):
        form = page.get_by_test_id(f"simulation-{kind}")
        with page.expect_response("**/api/rooms/study/what-if") as simulated:
            form.get_by_role("button").click()
        result = simulated.value.json()
        assert result["available"] and result["simulated"]
        assert result["baseline"]["runtime_min"] > 0
        assert result["proposed"]["runtime_min"] >= 0
        assert result["baseline"]["cost"] > 0 and result["proposed"]["cost"] >= 0
        assert result["cost_saving"] == pytest.approx(
            result["baseline"]["cost"] - result["proposed"]["cost"]
        )
        assert result["saved_min"] == pytest.approx(
            result["baseline"]["runtime_min"] - result["proposed"]["runtime_min"]
        )
        table = form.get_by_test_id("simulation-comparison")
        expect(table.locator("caption")).to_contain_text("4 hours + 4 hours recovery")
        expect(table.locator("thead")).to_contain_text("Baseline")
        expect(table.locator("thead")).to_contain_text("Proposed")
        expect(table).to_contain_text("Central HVAC runtime")
        expect(table).to_contain_text("Estimated central HVAC cost")
        runtime = table.locator("tr").filter(
            has=page.get_by_role("rowheader", name="Central HVAC runtime", exact=True)
        )
        for index, scenario in enumerate(("baseline", "proposed")):
            expect(runtime.locator("td span").nth(index)).to_have_text(
                f"{result[scenario]['runtime_min']:.1f} min"
            )
        expect(form.locator("[data-simulation-result]")).to_contain_text("SIMULATED")
        expect(form.locator(".errors")).to_be_empty()
        assert lab.runtime.ctx.settings.model_dump() == before
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    record_evidence(page, lab, request, "validated-model-tool-comparisons")
    assert_page_healthy(page, srv)
