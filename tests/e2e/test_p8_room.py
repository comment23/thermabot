"""T1.43: P8 My Room. A Member sees and nudges only their own room (±2°F, <=4 h, 3/day);
another room is 403 on both the page and the API."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from thermabot.app.jobs import save_sample
from thermabot.config import InsightsCfg
from thermabot.core.thermal import ThermalParams
from thermabot.core.types import Forecast, ForecastPoint, RoomReading, Snapshot, ThermostatState


def _api(page: Page, body: str) -> int:
    return int(page.evaluate(f"async () => (await tb.api('POST', '/api/nudge', {body})).status"))


def _view(page: Page, label: str) -> None:
    page.get_by_test_id("room-tabs").get_by_role("link", name=label, exact=True).click()
    expect(page.get_by_test_id("room-tabs").locator('[aria-current="page"]')).to_have_text(label)
    expect(page.get_by_test_id("room-summary")).to_be_visible()
    expect(page.get_by_test_id("room-nudge-controls").locator("summary")).to_be_visible()


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
    page.get_by_test_id("room-nudge-controls").locator("summary").click()
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
    page.get_by_test_id("room-nudge-controls").locator("summary").click()
    expect(page.locator("[data-testid=limits]")).to_contain_text("3 of 3")
    expect(page.locator("[data-testid=nudge-submit]")).to_be_disabled()
    srv.errors.clear()
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CMF-6")
def test_admin_can_pick_any_room(page: Page, srv: Srv) -> None:
    login(page, srv, "ann")
    page.goto(f"{srv.url}/room?room=study")
    expect(page.locator("h2")).to_have_text("study")
    page.get_by_test_id("room-nudge-controls").locator("summary").click()
    page.click("[data-testid=nudge-submit]")
    expect(page.locator("[data-testid=nudge]")).to_contain_text("+1°F")
    assert srv.ctx.conn.execute("SELECT room FROM nudges").fetchone()[0] == "study"
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-1")
@pytest.mark.parametrize("large_text", [False, True], ids=["standard-text", "large-text"])
@pytest.mark.parametrize("width", [375, 1440])
@pytest.mark.parametrize("units", ["F", "C"])
def test_room_insights_table_efficiency_and_ranges(
    page: Page, srv: Srv, width: int, units: Literal["F", "C"], large_text: bool
) -> None:
    if large_text:
        page.add_init_script(
            """document.addEventListener('DOMContentLoaded', () =>
                document.documentElement.style.setProperty('font-size', '150%'));"""
        )
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    srv.ctx.clock = lambda: now
    srv.ctx.settings = srv.ctx.settings.model_copy(
        update={"display": srv.ctx.settings.display.model_copy(update={"units": units})}
    )
    srv.ctx.snapshot = Snapshot(
        t=now,
        thermostat=ThermostatState(hvac_mode="heat", current_temp=73, last_reported=now),
        rooms={"bedroom_1": RoomReading(key="bedroom_1", temp=74, age_min=0)},
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(color_scheme="light" if width == 375 else "dark")
    for m in range(120):
        save_sample(
            srv.ctx.conn,
            now - timedelta(minutes=m),
            {
                "tstat": {"temp": 70, "hvac_action": "heating"},
                "rooms": {"bedroom_1": {"temp": 68 if m < 60 else 66, "age_min": 0}},
                "block": "home",
                "t_out": 40,
            },
            "SHADOW",
            90,
            [],
        )
    login(page, srv, "jordan")
    page.goto(f"{srv.url}/room")
    expect(page.get_by_test_id("room-mean")).to_have_text("19.4°C" if units == "C" else "67°F")
    expect(page.get_by_test_id("room-ideal-delta")).to_have_text(
        "+0.6°C" if units == "C" else "+1°F"
    )
    expect(page.get_by_test_id("room-ideal-comfort")).to_have_text("0.0%")
    expect(page.get_by_test_id("room-priority-action")).to_contain_text(
        "Restore reliable sensor coverage"
    )
    expect(page.get_by_test_id("room-offset")).to_contain_text("-1.7°C" if units == "C" else "-3°F")
    expect(page.get_by_test_id("room-trend")).to_contain_text("+1.1°C" if units == "C" else "+2°F")
    expect(page.get_by_test_id("room-coverage")).to_contain_text("120 measured minutes")
    _view(page, "Efficiency")
    expect(page.get_by_test_id("room-comfort")).to_have_text("100.0%")
    expect(page.get_by_test_id("room-runtime")).to_contain_text("not this room's runtime")
    expect(page.get_by_test_id("room-efficiency")).to_contain_text("24 h")
    expect(page.get_by_test_id("heat-pump-diagnostics")).to_contain_text("Central HVAC")
    _view(page, "Temperatures")
    expect(page.get_by_test_id("room-temperature-row")).to_have_count(24)
    for label, rows in (("7 days", 28), ("30 days", 30)):
        page.get_by_role("link", name=label, exact=True).click()
        expect(page.get_by_test_id("room-temperature-row")).to_have_count(rows)
        expect(page.locator("h2")).to_have_text("bedroom_1")
        chart = page.get_by_test_id("room-temperature-chart")
        expect(chart).to_have_attribute("data-state", "ready")
        expect(chart).to_have_attribute("data-points", str(rows))
        expect(chart).to_have_attribute("data-ideal", "22.8" if units == "C" else "73")
        expect(chart.locator("canvas")).to_have_count(1)
        data = json.loads(page.locator("#room-temperature-data").text_content() or "{}")
        assert None in data["temp"] and any(t is not None for t in data["temp"])
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert chart.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        expect(page.get_by_test_id("room-period-label")).to_contain_text("last 7 days")
    _view(page, "Tools")
    expect(page.get_by_test_id("room-setup")).to_be_visible()
    expect(page.get_by_test_id("room-setup").locator('a[href^="settings"]')).to_have_count(0)
    expect(page.get_by_test_id("room-setup")).to_contain_text("Ask an Owner")
    _view(page, "Temperatures")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    table_size = page.get_by_test_id("room-temperature-table").evaluate(
        "el => ({scroll: el.scrollWidth, width: el.clientWidth})"
    )
    assert table_size["scroll"] <= table_size["width"] + 1, table_size
    _view(page, "Efficiency")
    assert page.get_by_test_id("room-efficiency").evaluate(
        "el => el.scrollWidth <= el.clientWidth + 1"
    )
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-1")
def test_room_empty_and_stale_are_not_successful_measurements(page: Page, srv: Srv) -> None:
    now = datetime.now(UTC)
    srv.ctx.snapshot = Snapshot(
        t=now,
        thermostat=ThermostatState(hvac_mode="heat"),
        rooms={"bedroom_1": RoomReading(key="bedroom_1", temp=99, stale=True)},
    )
    login(page, srv, "jordan")
    page.goto(f"{srv.url}/room")
    expect(page.get_by_test_id("reading")).to_contain_text("current temperature unavailable")
    expect(page.get_by_test_id("reading")).not_to_contain_text("99")
    expect(page.get_by_test_id("room-history-empty")).to_be_visible()
    expect(page.get_by_test_id("room-ideal-delta")).to_have_text("—")
    _view(page, "Efficiency")
    expect(page.get_by_test_id("room-comfort")).to_have_text("—")
    expect(page.get_by_test_id("room-runtime")).to_contain_text("heating — min")
    _view(page, "Temperatures")
    expect(page.get_by_test_id("room-temperature-table")).to_contain_text("No samples")
    expect(page.get_by_test_id("room-temperature-chart")).to_have_attribute("data-state", "empty")
    _view(page, "Tools")
    for key in ("room", "thermostat", "forecast", "model", "meter", "tariff"):
        expect(page.get_by_test_id(f"readiness-{key}")).to_have_attribute("data-ready", "false")
    for scenario in ("setback", "tariff"):
        form = page.get_by_test_id(f"simulation-{scenario}")
        form.get_by_role("button").click()
        expect(form.locator("[data-simulation-result]")).to_contain_text("Unavailable:")
        expect(form.locator("[data-simulation-result]")).not_to_contain_text("SIMULATED")
        expect(form.get_by_role("button")).to_be_enabled()
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-1")
@pytest.mark.parametrize("units", ["F", "C"])
def test_research_tools_show_results_and_persist_improvements(
    page: Page, srv: Srv, units: Literal["F", "C"]
) -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    srv.ctx.clock = lambda: now
    srv.ctx.settings = srv.ctx.settings.model_copy(
        update={
            "display": srv.ctx.settings.display.model_copy(update={"units": units}),
            "insights": InsightsCfg(off_peak_rate=0.1, peak_rate=0.4, rated_power_kw=3),
        }
    )
    srv.ctx.snapshot = Snapshot(
        t=now,
        thermostat=ThermostatState(hvac_mode="heat", current_temp=73, last_reported=now),
        rooms={"bedroom_1": RoomReading(key="bedroom_1", temp=73, age_min=0)},
    )
    srv.ctx.forecast = Forecast(
        source="ha",
        fetched_at=now,
        points=[ForecastPoint(t=now + timedelta(hours=i), temp_f=45) for i in range(49)],
    )
    srv.ctx.conn.execute(
        "INSERT INTO models(version,fitted_at,params_json,mae_json,n_samples,active)"
        " VALUES(1,?,?,?,?,1)",
        (
            now.isoformat(),
            ThermalParams(a=0.03, s=0).model_dump_json(),
            json.dumps({"thermostat": 0.5}),
            2000,
        ),
    )
    login(page, srv, "alex")
    page.goto(f"{srv.url}/room?room=bedroom_1")
    expect(page.get_by_test_id("ideal-temperature")).to_contain_text(
        "22.8°C" if units == "C" else "73°F"
    )
    for section in (
        "sensor-diagnostics",
        "humidity-comfort",
        "weekly-room-actions",
    ):
        expect(page.get_by_test_id(section)).to_be_visible()
    _view(page, "Efficiency")
    for section in ("meter-energy", "weather-performance", "heat-pump-diagnostics"):
        expect(page.get_by_test_id(section)).to_be_visible()
    _view(page, "Tools")
    for key in ("room", "thermostat", "forecast", "model", "tariff", "power"):
        expect(page.get_by_test_id(f"readiness-{key}")).to_have_attribute("data-ready", "true")
    expect(page.get_by_test_id("simulation-readiness")).to_contain_text("Setback: ready")
    link = page.get_by_test_id("readiness-model").get_by_role("link")
    expect(link).to_have_attribute("href", "model")
    expect(page.get_by_test_id("readiness-tariff").get_by_role("link")).to_have_attribute(
        "href", "settings/advanced"
    )
    for scenario in ("setback", "tariff"):
        form = page.get_by_test_id(f"simulation-{scenario}")
        form.get_by_role("button").click()
        expect(form.locator("[data-simulation-result]")).to_contain_text("SIMULATED")
        comparison = form.get_by_test_id("simulation-comparison")
        expect(comparison).to_contain_text("Baseline")
        expect(comparison).to_contain_text("Proposed")
        expect(comparison).to_contain_text("Central HVAC runtime")
        expect(comparison.locator("meter")).to_have_count(4)
        assert comparison.locator("meter").evaluate_all(
            "els => els.every(el => el.value >= 0 && el.value <= el.max"
            " && el.getAttribute('aria-label'))"
        )
        assert comparison.locator("meter").evaluate_all(
            "els => els.every(el => el.getBoundingClientRect().width >= 60)"
        )
        assert comparison.locator("caption").evaluate(
            "el => el.getBoundingClientRect().width >= el.closest('table').clientWidth * 0.95"
        )
        assert comparison.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        assert comparison.locator("caption").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        expect(form.locator(".errors")).to_be_empty()
    form = page.get_by_test_id("experiment-form")
    form.locator("[name=name]").fill("Window sealing")
    form.get_by_role("button").click()
    expect(page.get_by_test_id("room-experiments")).to_contain_text("Window sealing")
    expect(page.get_by_test_id("room-tabs").locator('[aria-current="page"]')).to_have_text("Tools")
    page.get_by_test_id("room-nudge-controls").locator("summary").click()
    expect(page.get_by_test_id("nudge-form")).to_be_visible()
    assert srv.ctx.conn.execute("SELECT COUNT(*) FROM room_experiments").fetchone()[0] == 1
    assert srv.ctx.mock_ha.calls == []
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-1")
def test_room_tabs_and_table_work_without_javascript(
    page: Page, browser: Browser, srv: Srv
) -> None:
    login(page, srv, "ann")
    context = browser.new_context(java_script_enabled=False, viewport={"width": 375, "height": 900})
    try:
        context.add_cookies(page.context.cookies())
        plain = context.new_page()
        plain.goto(f"{srv.url}/room?room=study&range=7d")
        _view(plain, "Temperatures")
        expect(plain.get_by_test_id("room-temperature-row")).to_have_count(28)
        plain.get_by_role("link", name="30 days", exact=True).click()
        expect(plain.get_by_test_id("room-temperature-row")).to_have_count(30)
        expect(plain.locator("h2")).to_have_text("study")
        expect(plain).to_have_url(f"{srv.url}/room?room=study&range=30d&view=temperatures")
        plain.get_by_test_id("room-nudge-controls").locator("summary").click()
        expect(plain.get_by_test_id("nudge-form")).to_be_visible()
    finally:
        context.close()
