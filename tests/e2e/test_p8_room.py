"""T1.43: P8 My Room. A Member sees and nudges only their own room (±2°F, <=4 h, 3/day);
another room is 403 on both the page and the API."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from thermabot.core.types import RoomReading, Snapshot, ThermostatState


def _api(page: Page, body: str) -> int:
    return int(page.evaluate(f"async () => (await tb.api('POST', '/api/nudge', {body})).status"))


@pytest.mark.brd("FR-CMF-6")
def test_member_nudges_own_room(page: Page, srv: Srv) -> None:
    replans: list[int] = []
    srv.ctx.replan = lambda: replans.append(1)
    srv.ctx.snapshot = Snapshot(
        t=datetime.now(UTC),
        thermostat=ThermostatState(hvac_mode="heat"),
        rooms={"bedroom_1": RoomReading(key="bedroom_1", temp=66.5)},
    )
    login(page, srv, "jordan")
    page.goto(f"{srv.url}/room")
    expect(page.locator("h2")).to_have_text("bedroom_1")
    expect(page.locator("[data-testid=reading]")).to_contain_text("66.5°F")
    expect(page.locator("[data-testid=limits]")).to_contain_text("±2°F for up to 4 h")
    expect(page.locator("[data-testid=limits]")).to_contain_text("0 of 3")
    opts = page.locator("[data-testid=delta] option").all_text_contents()
    assert opts == ["-2°F", "-1°F", "+1°F", "+2°F"]
    assert page.locator("[data-testid=hours] option").count() == 4
    page.select_option("[data-testid=delta]", "2")
    page.click("[data-testid=nudge-submit]")
    expect(page.locator("[data-testid=nudge]")).to_contain_text("+2°F")
    expect(page.locator("[data-testid=limits]")).to_contain_text("1 of 3")
    row = srv.ctx.conn.execute("SELECT room, delta_f FROM nudges").fetchone()
    assert (row["room"], row["delta_f"]) == ("bedroom_1", 2) and replans == [1]
    page.click("[data-testid=nudge-cancel]")
    expect(page.locator("[data-testid=nudge]")).to_have_count(0)
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CMF-6")
def test_member_other_room_403(page: Page, srv: Srv) -> None:
    login(page, srv, "jordan")
    r = page.goto(f"{srv.url}/room?room=study")
    assert r is not None and r.status == 403
    page.goto(f"{srv.url}/room")
    assert _api(page, "{room: 'study', delta_f: 1, hours: 1}") == 403
    assert srv.ctx.conn.execute("SELECT COUNT(*) FROM nudges").fetchone()[0] == 0
    srv.errors.clear()  # deliberate 403s are logged by the browser
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CMF-6")
@pytest.mark.parametrize("body", ["{delta_f: 3, hours: 1}", "{delta_f: 1, hours: 5}"])
def test_limits_enforced_by_api(page: Page, srv: Srv, body: str) -> None:
    login(page, srv, "jordan")
    page.goto(f"{srv.url}/room")
    assert _api(page, body) == 422
    srv.errors.clear()
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CMF-6")
def test_three_per_day(page: Page, srv: Srv) -> None:
    login(page, srv, "jordan")
    page.goto(f"{srv.url}/room")
    assert [_api(page, "{delta_f: 1, hours: 1}") for _ in range(4)] == [200, 200, 200, 422]
    page.reload()
    expect(page.locator("[data-testid=limits]")).to_contain_text("3 of 3")
    expect(page.locator("[data-testid=nudge-submit]")).to_be_disabled()
    srv.errors.clear()
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CMF-6")
def test_admin_can_pick_any_room(page: Page, srv: Srv) -> None:
    login(page, srv, "ann")
    page.goto(f"{srv.url}/room?room=study")
    expect(page.locator("h2")).to_have_text("study")
    page.click("[data-testid=nudge-submit]")
    expect(page.locator("[data-testid=nudge]")).to_contain_text("+1°F")
    assert srv.ctx.conn.execute("SELECT room FROM nudges").fetchone()[0] == "study"
    assert_page_healthy(page, srv)
