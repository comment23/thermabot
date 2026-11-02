"""T1.39 / v0.3.0: P3 Settings as forms. Save -> PUT api/settings -> replan; validation errors
are shown per field; safety edits need an Owner; members get 403; the JSON view is read-only
and Owner-only; °C display converts at the edge only."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login


def _field(page: Page, path: str) -> object:
    return page.locator(f"[data-path='{path}']")


def _open(page: Page, section: str) -> None:
    d = page.locator(f"[data-testid=section-{section}]")
    if d.get_attribute("open") is None:
        d.locator("summary").click()


@pytest.mark.brd("FR-CMF-1")
def test_save_valid_settings_replans(page: Page, srv: Srv) -> None:
    replans: list[int] = []
    srv.ctx.replan = lambda: replans.append(1)
    before = srv.ctx.settings.model_dump(mode="json")
    login(page, srv, "ann")
    page.goto(f"{srv.url}/settings")
    _open(page, "bands")
    page.fill("[data-path='bands.heat.home.0']", "67")
    page.fill("[data-path='bands.heat.home.1']", "70")
    page.click("[data-testid=settings-save]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    expect(page.locator("[data-testid=bands-summary]")).to_contain_text("67-70")
    assert replans == [1] and srv.ctx.settings.bands.heat.home == (67, 70)
    after = srv.ctx.settings.model_dump(mode="json")
    before["bands"]["heat"]["home"] = [67, 70]
    assert after == before  # nothing else changed
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CMF-1")
def test_unchanged_save_is_disabled_and_writes_nothing(page: Page, srv: Srv) -> None:
    """v0.6.1: with nothing changed, Save is disabled and says 'No changes'; no row is written."""
    before = srv.ctx.settings.model_dump(mode="json")
    row = srv.ctx.conn.execute("SELECT updated_at, updated_by FROM settings").fetchall()
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings")
    btn = page.locator("[data-testid=settings-save]")
    expect(btn).to_be_disabled()
    expect(btn).to_have_text("No changes")
    _open(page, "bands")
    page.fill("[data-path='bands.heat.home.0']", "67")
    expect(btn).to_be_enabled()
    expect(btn).to_have_text("Save")
    page.fill("[data-path='bands.heat.home.0']", "68")  # back to the stored value
    expect(btn).to_be_disabled()
    assert srv.ctx.settings.model_dump(mode="json") == before
    assert srv.ctx.conn.execute("SELECT updated_at, updated_by FROM settings").fetchall() == row
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CMF-1")
@pytest.mark.parametrize(
    ("section", "path", "value", "msg"),
    [
        ("bands", "bands.heat.home.0", "72", "inverted"),
        ("safety", "safety.floor_tstat_f", "55", "loosens"),
        ("schedule", "schedule.weekday.0.start", "25:00", "pattern"),
    ],
)
def test_validation_errors_shown_per_field(
    page: Page, srv: Srv, section: str, path: str, value: str, msg: str
) -> None:
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings")
    _open(page, section)
    page.fill(f"[data-path='{path}']", value)
    page.click("[data-testid=settings-save]")
    expect(page.locator("[data-testid=errors]")).to_contain_text(msg)
    err = page.locator(f"[data-testid=section-{section}] .field-error:not(:empty)")
    expect(err.first).to_be_visible()
    assert srv.ctx.settings.bands.heat.home == (68, 71)
    assert srv.ctx.settings.safety.floor_tstat_f == 62
    srv.errors.clear()  # the 422 response is logged by the browser
    assert_page_healthy(page, srv)


@pytest.mark.brd("NFR-SEC-3")
def test_admin_cannot_change_safety(page: Page, srv: Srv) -> None:
    login(page, srv, "ann")
    page.goto(f"{srv.url}/settings")
    expect(page.locator("[data-testid=safety-note]")).to_be_visible()
    _open(page, "safety")
    expect(page.locator("[data-path='safety.floor_tstat_f']")).to_be_disabled()
    expect(page.locator("[data-path='thermostat.entity_id']")).to_be_disabled()
    assert page.locator("[data-testid=advanced]").count() == 0  # JSON view: Owner only
    assert page.locator("[data-testid=open-wizard]").count() == 0
    # a forged request is still refused by the API
    res = page.evaluate(
        """async () => {
          const s = JSON.parse(document.querySelector('[data-settings-form]').dataset.settings);
          s.safety.floor_tstat_f = 64;
          const r = await window.tb.api('PUT', 'api/settings', s);
          return [r.status, JSON.stringify(r.data)];
        }"""
    )
    assert res[0] == 403 and "edit_safety" in res[1]
    assert srv.ctx.settings.safety.floor_tstat_f == 62
    srv.errors.clear()
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CFG-1")
def test_owner_json_view_is_read_only(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings")
    page.locator("[data-testid=advanced] summary").click()
    box = page.locator("[data-testid=settings-json]")
    expect(box).to_be_visible()
    assert box.get_attribute("readonly") is not None
    assert '"thermostat"' in box.input_value()
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CFG-1")
def test_celsius_display_round_trip(page: Page, srv: Srv) -> None:
    """Switch to °C; untouched temperatures save back to the exact stored °F; an edited one
    is converted to °F at the edge (21.5 °C -> 70.5 °F)."""
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings")
    _open(page, "display")
    page.select_option("[data-path='display.units']", "C")
    page.click("[data-testid=settings-save]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    page.wait_for_load_state("load")
    page.goto(f"{srv.url}/settings")
    s0 = srv.ctx.settings
    assert s0.display.units == "C" and s0.bands.heat.home == (68, 71)
    expect(page.locator("[data-path='bands.heat.home.0']")).to_have_value("20.0")
    # pure round trip: the untouched form in C equals the stored F (Save stays disabled)
    expect(page.locator("[data-testid=settings-save]")).to_be_disabled()
    s1 = srv.ctx.settings
    assert s1.model_dump() == s0.model_dump()
    page.goto(f"{srv.url}/settings")
    page.fill("[data-path='bands.heat.home.1']", "21.5")
    page.click("[data-testid=settings-save]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    assert srv.ctx.settings.bands.heat.home == (68, 70.5)
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-USR-2")
def test_member_forbidden(page: Page, srv: Srv) -> None:
    login(page, srv, "jordan")
    r = page.goto(f"{srv.url}/settings")
    assert r is not None and r.status == 403
    assert "/settings" not in page.goto(f"{srv.url}/").text()  # type: ignore[union-attr]
