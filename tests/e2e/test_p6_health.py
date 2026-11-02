"""T1.42a: P6 Health (Owner/Admin): sources, write stats + lag, watchdog helper, Life360
status, audit."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from thermabot.core.types import (
    HouseState,
    PersonPresence,
    PersonState,
    Presence,
    Snapshot,
    ThermostatState,
)


def _seed(srv: Srv) -> None:
    now = datetime.now(UTC)
    for ms, outcome in ((800, "shadow"), (1200, "shadow"), (None, "failed")):
        srv.ctx.conn.execute(
            "INSERT INTO writes (ts, service, entity_id, data_json, reason, outcome, readback_ms)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                now.isoformat(),
                "set_temperature",
                "climate.t",
                "{}",
                "plan" if ms else "ha timeout",
                outcome,
                ms,
            ),
        )
    srv.ctx.watchdog_helper = "on"
    srv.ctx.weather_health = "stale"
    srv.ctx.snapshot = Snapshot(
        t=now,
        thermostat=ThermostatState(hvac_mode="heat"),
        presence=Presence(
            house=HouseState.OCCUPIED,
            since=now,
            people={
                "alex": PersonPresence(state=PersonState.HOME, source="life360", age_min=3),
                "sam": PersonPresence(state=PersonState.AWAY, source="unknown"),
            },
        ),
    )


@pytest.mark.brd("FR-PRS-4")
def test_health_page(page: Page, srv: Srv) -> None:
    _seed(srv)
    login(page, srv, "ann")
    page.goto(f"{srv.url}/health")
    expect(page.locator("[data-testid=sources] tr[data-key=weather]")).to_contain_text("stale")
    expect(page.locator("[data-testid=watchdog]")).to_have_text("on")
    expect(page.locator("[data-testid=writes]")).to_contain_text("shadow 2")
    expect(page.locator("[data-testid=writes]")).to_contain_text("failed 1")
    expect(page.locator("[data-testid=lag]")).to_contain_text("p50 1000 ms")
    expect(page.locator("[data-testid=errors-table]")).to_contain_text("ha timeout")
    expect(page.locator("[data-testid=presence] tr[data-key=alex]")).to_contain_text("life360")
    expect(page.locator("[data-testid=presence] tr[data-key=sam]")).to_contain_text(
        "no fresh source"
    )
    assert_page_healthy(page, srv)


@pytest.mark.brd("NFR-SEC-5")
def test_audit_shows_actions(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.click("[data-testid=kill-open]")
    page.click("[data-testid=kill-off]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Bot stopped")
    page.goto(f"{srv.url}/health")
    expect(page.locator("[data-testid=audit-row]").first).to_contain_text("kill_off")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-USR-2")
def test_member_forbidden(page: Page, srv: Srv) -> None:
    login(page, srv, "jordan")
    r = page.goto(f"{srv.url}/health")
    assert r is not None and r.status == 403
