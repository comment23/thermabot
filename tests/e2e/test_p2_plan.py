"""T1.38: P2 Plan & Timeline. Chart, HVAC bar track aligned with the change table, presence
track, fallback banner, estimated rooms drawn dashed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from thermabot.core.types import Plan, PlanStep, RoomReading, Snapshot, ThermostatState


def _plan(kind: str = "optimised", banner: str = "") -> Plan:
    t0 = datetime.now(UTC).replace(second=0, microsecond=0)
    steps = []
    for i in range(16):  # 4 h of 15-min steps: heat 68 x4, heat 66 x4, off x4, fan x4
        mode, sp, fan, why = [
            ("heat", 68, "auto", "home block"),
            ("heat", 66, "auto", "setback"),
            ("off", None, "auto", "mild: off window"),
            ("off", None, "on", "fan mixing"),
        ][i // 4]
        steps.append(
            PlanStep(
                t=t0 + timedelta(minutes=15 * i),
                hvac_mode=mode,
                heat_sp=sp,
                fan_mode=fan,
                pred_t=68 - i * 0.1,
                pred_rooms={"jordan": 67.0, "loft": 69.0},
                reason=why if i % 4 == 0 else "",
            )
        )
    return Plan(kind=kind, created_at=t0, reason_banner=banner, steps=steps)  # type: ignore[arg-type]


@pytest.mark.brd("FR-PLN-6")
def test_bars_line_up_with_change_table(page: Page, srv: Srv) -> None:
    srv.ctx.plan = _plan()
    login(page, srv, "jordan")
    page.goto(f"{srv.url}/plan")
    bars = page.locator("[data-testid=bar]")
    rows = page.locator("[data-testid=change]")
    expect(bars).to_have_count(4)
    assert [bars.nth(i).get_attribute("data-start") for i in range(4)] == [
        rows.nth(i).get_attribute("data-start") for i in range(4)
    ]
    assert [bars.nth(i).get_attribute("data-kind") for i in range(4)] == [
        "heat",
        "heat",
        "off",
        "fan",
    ]
    expect(rows.nth(1)).to_contain_text("setback")
    expect(page.locator("[data-testid=presence-track] [data-testid=block]").first).to_be_visible()
    assert page.locator("[data-testid=fallback-banner]").count() == 0
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-PLN-7")
def test_fallback_banner(page: Page, srv: Srv) -> None:
    srv.ctx.plan = _plan("fallback", "model MAE too high")
    login(page, srv, "alex")
    page.goto(f"{srv.url}/plan")
    expect(page.locator("[data-testid=fallback-banner]")).to_contain_text("model MAE too high")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-PLN-6")
def test_chart_renders_estimated_room_dashed(page: Page, srv: Srv) -> None:
    srv.ctx.plan = _plan()
    srv.ctx.snapshot = Snapshot(
        t=datetime.now(UTC),
        thermostat=ThermostatState(hvac_mode="heat"),
        rooms={"loft": RoomReading(key="loft", temp=69, estimated=True)},
    )
    login(page, srv, "alex")
    page.goto(f"{srv.url}/plan")
    chart = page.locator("[data-testid=chart]")
    expect(chart.locator("canvas")).to_have_count(1)
    expect(chart).to_have_attribute("data-series", "5")
    expect(chart).to_have_attribute("data-dashed", "1")
    expect(chart).to_contain_text("loft (est)")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-1")
def test_no_plan_yet(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.goto(f"{srv.url}/plan")
    expect(page.locator("[data-testid=no-plan]")).to_be_visible()
    assert_page_healthy(page, srv)
