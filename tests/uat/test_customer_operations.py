"""Customer operations through the UI and actual runtime, without climate actuation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from scripts.uat import Lab
from tests.e2e.conftest import PW, Srv, assert_page_healthy, login
from tests.e2e.test_p3_settings import _open
from tests.uat.conftest import customer_page
from tests.uat.test_customer_journeys import apply_scenario


@pytest.mark.brd("FR-PRS-1")
def test_customer_guest_mode_changes_actual_presence(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.click("[data-testid=guest-toggle]")
    expect(page.locator("[data-testid=guest-toggle]")).to_contain_text("ON")
    evidence = apply_scenario(page, srv, "away")
    assert evidence["snapshot"]["presence"]["house"] == "OCCUPIED"
    page.reload()
    expect(page.locator("[data-testid=guest-toggle]")).to_contain_text("ON")
    page.click("[data-testid=guest-toggle]")
    expect(page.locator("[data-testid=guest-toggle]")).to_contain_text("OFF")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-OVR-2")
def test_customer_shadow_override_is_reported_blocked_then_cancelled(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.locator("[data-watch-action]").click()
    expect(page.locator("[data-testid=state-badge]")).to_have_attribute("data-state", "SHADOW")
    page.fill("[data-testid=override-target]", "72")
    page.select_option("select[name=duration]", "1h")
    page.click("[data-testid=override-start]")
    expect(page.locator("[data-testid=state-badge]")).to_have_attribute("data-state", "OVERRIDE")
    apply_scenario(page, srv, "healthy")
    page.reload()
    expect(page.locator("[data-testid=override-command-status]")).to_contain_text(
        "Not applied: shadow mode"
    )
    page.click("[data-testid=override-cancel]")
    expect(page.locator("[data-testid=state-badge]")).to_have_attribute("data-state", "SHADOW")
    assert srv.ctx.open_override() is None
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-OVR-1")
def test_customer_plan_pause_persists_and_can_be_cancelled(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.click("[data-testid=control-pause]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Plan paused")
    pause = srv.ctx.open_override()
    assert pause is not None and pause.kind == "ui_pause"
    apply_scenario(page, srv, "healthy")
    page.reload()
    expect(page.locator("[data-testid=override-cancel]")).to_be_visible()
    page.click("[data-testid=override-cancel]")
    expect(page.locator("[data-testid=override-cancel]")).to_be_hidden()
    assert srv.ctx.open_override() is None
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CMF-1")
def test_customer_saved_comfort_band_reaches_controller_on_next_tick(
    page: Page, srv: Srv, lab: Lab
) -> None:
    login(page, srv, "ann")
    page.goto(srv.url + "/settings/advanced")
    _open(page, "bands")
    page.fill("[data-path='bands.heat.home.0']", "67")
    page.click("[data-testid=settings-save]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    apply_scenario(page, srv, "healthy")
    assert lab.runtime.controller.settings.bands.heat.home[0] == 67
    page.reload()
    _open(page, "bands")
    assert (
        page.locator("[data-path='bands.heat.home.0']").evaluate("input => input.valueAsNumber")
        == 67
    )
    expect(page.locator("[data-testid=settings-save]")).to_be_disabled()
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CFG-1")
def test_customer_preview_import_and_backup_are_real_and_leave_bot_stopped(
    page: Page, srv: Srv, lab: Lab, tmp_path: Path
) -> None:
    login(page, srv, "alex")
    page.goto(srv.url + "/recovery")
    response = page.request.get(srv.url + "/api/config/export")
    assert response.ok
    document = response.json()
    assert document["format"] == "thermabot-config"
    assert "HA_TOKEN" not in json.dumps(document)
    assert "environment" not in document
    document["settings"]["display"]["units"] = "C"
    before = lab.runtime.ctx.settings.model_dump(mode="json")
    page.set_input_files(
        "[data-testid=config-file]",
        {
            "name": "generic-config.json",
            "mimeType": "application/json",
            "buffer": json.dumps(document).encode(),
        },
    )
    page.click("[data-testid=config-preview]")
    expect(page.locator("[data-testid=import-changes]")).to_contain_text("display")
    assert lab.runtime.ctx.settings.model_dump(mode="json") == before
    page.click("[data-testid=config-apply]")
    page.fill("[data-testid=confirm-typed]", "RESTORE")
    page.click("[data-testid=confirm-yes]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Configuration imported")
    assert lab.runtime.ctx.settings.display.units == "C"
    assert lab.evidence()["kill_switch"] == "off"
    assert list((Path(lab.runtime.env.DATA_DIR) / "backup").glob("*pre-import*"))
    with page.expect_download() as downloaded:
        page.click("[data-testid=backup-create]")
    copy = tmp_path / "downloaded-generic-backup.db"
    downloaded.value.save_as(copy)
    with sqlite3.connect(copy) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 4
    expect(page.locator("[data-testid=backup-list]")).to_contain_text("manual")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-USR-1")
@pytest.mark.brd("FR-USR-2")
def test_customer_user_lifecycle_changes_real_session_permissions(
    page: Page, srv: Srv, browser: Browser
) -> None:
    login(page, srv, "alex")
    page.goto(srv.url + "/users")
    form = page.locator("[data-testid=user-form]")
    form.locator("input[name=username]").fill("sam")
    form.locator("select[name=role]").select_option("member")
    form.locator("select[name=room]").select_option("bedroom_1")
    form.locator("input[name=password]").fill(PW)
    form.locator("button[type=submit]").click()
    expect(page.locator("tr[data-user=sam]")).to_contain_text("member")
    with customer_page(browser, srv) as member:
        login(member, srv, "sam")
        assert member.request.get(srv.url + "/settings").status == 403
        change = page.locator("[data-testid=role-form]")
        change.locator("[data-testid=role-user]").select_option(label="sam")
        change.locator("select[name=role]").select_option("admin")
        change.locator("button[type=submit]").click()
        expect(page.locator("tr[data-user=sam]")).to_contain_text("admin")
        assert member.request.get(srv.url + "/settings").status == 200
        page.click("[data-testid=delete-sam]")
        page.fill("[data-testid=confirm-typed]", "sam")
        page.click("[data-testid=confirm-yes]")
        expect(page.locator("tr[data-user=sam]")).to_have_count(0)
        member.goto(srv.url + "/")
        expect(member).to_have_url(srv.url + "/login")
    assert_page_healthy(page, srv)
