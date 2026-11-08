"""T1.37: P1 Dashboard. Live readings, state banners, override/guest via the API, member
read-only view (no override/kill controls; read-only settings summary)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from thermabot.adapters import db
from thermabot.config import RoomCfg
from thermabot.core.openings import OpeningFinding
from thermabot.core.overrides import override_command_key
from thermabot.core.types import Plan, PlanStep, RoomReading, Snapshot, ThermostatState


def _seed(srv: Srv) -> None:
    now = datetime.now(UTC)
    srv.ctx.settings.rooms["bedroom_2"] = RoomCfg(temp="sensor.bedroom_2", floor="Second")
    srv.ctx.snapshot = Snapshot(
        t=now,
        thermostat=ThermostatState(
            hvac_mode="heat", hvac_action="heating", current_temp=69.5, heat_sp=68, cool_sp=76
        ),
        rooms={
            "jordan": RoomReading(key="jordan", temp=67.0, age_min=4, score=82),
            "bedroom_2": RoomReading(key="bedroom_2", temp=None, stale=True, age_min=95),
            "loft": RoomReading(key="loft", temp=70.1, estimated=True),
        },
    )
    srv.ctx.score = 87.4
    srv.ctx.plan = Plan(
        kind="optimised",
        created_at=now,
        steps=[
            PlanStep(
                t=now + timedelta(minutes=30),
                hvac_mode="heat",
                heat_sp=67,
                reason="sleep block setback",
            )
        ],
    )


@pytest.mark.brd("FR-UI-1")
def test_dashboard_shows_live_state(page: Page, srv: Srv) -> None:
    _seed(srv)
    login(page, srv, "alex")
    expect(page.locator("[data-testid=tstat-temp]")).to_have_text("69.5°F")
    expect(page.locator("[data-testid=hvac-mode]")).to_have_text("heat")
    expect(page.locator("[data-testid=score]")).to_have_text("87")
    expect(page.locator("[data-testid=next-change]")).to_contain_text("sleep block setback")
    expect(page.locator("[data-testid=shadow-days]")).to_contain_text("3.0 of 7")
    expect(page.locator("tr[data-room=bedroom_2]")).to_contain_text("(stale)")
    expect(page.locator("tr[data-room=bedroom_2]")).to_have_attribute("data-floor", "Second")
    expect(page.locator("tr[data-room=bedroom_2]")).to_contain_text("Second")
    expect(page.locator("tr[data-room=loft]")).to_contain_text("(est)")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-1")
def test_dashboard_shows_read_only_opening_assessment(page: Page, srv: Srv) -> None:
    srv.ctx.opening_assessment = [
        OpeningFinding(
            status="sustained",
            entity_ids=["binary_sensor.front_door"],
            rooms=["living_room"],
            duration_min=12,
            message="SHADOW observation only; no action was sent.",
        )
    ]
    login(page, srv, "alex")
    assessment = page.locator("[data-testid=opening-assessment]")
    expect(assessment).to_be_visible()
    expect(assessment).to_contain_text("Sustained")
    expect(assessment).to_contain_text("Observations only")
    expect(assessment).to_contain_text("binary_sensor.front_door")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-MODE-1")
@pytest.mark.parametrize(
    ("setup", "state", "testid"),
    [("tstat", "DEGRADED", "degraded"), ("kill", "DISABLED", "disabled")],
)
def test_dashboard_state_banners(page: Page, srv: Srv, setup: str, state: str, testid: str) -> None:
    if setup == "tstat":
        srv.ctx.live = srv.ctx.live.model_copy(update={"tstat_unavailable": True})
    else:
        srv.ctx.conn.execute("UPDATE bot_state SET kill_switch='off'")  # bad checksum too
    login(page, srv, "ann")
    expect(page.locator("[data-testid=state-badge]")).to_have_attribute("data-state", state)
    expect(page.locator(f"[data-testid={testid}]")).to_be_visible()
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-OVR-2")
def test_override_start_and_cancel_via_api(page: Page, srv: Srv) -> None:
    replans: list[int] = []
    srv.ctx.replan = lambda: replans.append(1)
    login(page, srv, "ann")
    page.fill("input[name=target_f]", "71")
    page.select_option("select[name=duration]", "1h")
    page.click("[data-testid=override-start]")
    expect(page.locator("[data-testid=state-badge]")).to_have_attribute("data-state", "OVERRIDE")
    expect(page.locator("[data-testid=override-countdown]")).to_contain_text("Override")
    row = srv.ctx.conn.execute("SELECT id, kind, source FROM overrides").fetchone()
    assert (row["kind"], row["source"]) == ("ui_target", "ui:ann")
    status = page.locator("[data-testid=override-command-status]")
    expect(status).to_contain_text("Awaiting the next control cycle")
    expect(status).to_contain_text("Requests older than three minutes will be blocked")
    key = override_command_key(row["id"])
    for raw, message in (
        ("sending", "in progress or unconfirmed"),
        ("applied:heat 68F, cool 72F", "heat 68F, cool 72F"),
        ("matched:target 70F", "target 70F"),
        ("blocked:shadow mode", "Not applied: shadow mode"),
        ("failed", "Thermostat change was not confirmed"),
    ):
        db.write_kv(srv.ctx.conn, key, raw)
        page.reload()
        expect(status).to_contain_text(message)
    srv.ctx.conn.execute("DELETE FROM bot_kv WHERE key=?", (key,))
    page.reload()
    expect(status).to_contain_text("request is unconfirmed")
    page.click("[data-testid=override-cancel]")
    expect(page.locator("[data-testid=state-badge]")).to_have_attribute("data-state", "SHADOW")
    assert len(replans) == 2
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-PRS-1")
def test_guest_toggle(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.click("[data-testid=guest-toggle]")
    expect(page.locator("[data-testid=guest-toggle]")).to_contain_text("ON")
    acts = [r[0] for r in srv.ctx.conn.execute("SELECT action FROM audit WHERE actor='alex'")]
    assert "guest" in acts
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-USR-2")
def test_member_read_only(page: Page, srv: Srv) -> None:
    login(page, srv, "jordan")
    for tid in ("override-controls", "guest-toggle", "kill-open"):
        assert page.locator(f"[data-testid={tid}]").count() == 0
    expect(page.locator("[data-testid=settings-summary]")).to_contain_text("68")
    # The API stays the authority even if a member crafts the request by hand.
    st = page.evaluate(
        "async () => (await tb.api('POST', '/api/override', {target_f: 75, duration: '1h'})).status"
    )
    assert st == 403
    srv.errors.clear()  # the browser logs the deliberate 403 as a console error
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CFG-1")
def test_dashboard_setup_needed_banner(page: Page, srv: Srv) -> None:
    from thermabot.config import Settings

    srv.ctx.settings = Settings()  # a fresh install: no thermostat configured
    login(page, srv, "alex", destination="/setup")
    page.goto(f"{srv.url}/")
    expect(page.locator("[data-testid=state-badge]")).to_have_text("SETUP NEEDED")
    banner = page.locator("[data-testid=setup-needed]")
    expect(banner).to_contain_text("no thermostat is configured")
    assert banner.locator("a").get_attribute("href") == "setup"  # relative (ingress)
    assert_page_healthy(page, srv)
