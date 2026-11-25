"""T1.39 / v0.3.0: P3 Settings as forms. Save -> PUT api/settings -> replan; validation errors
are shown per field; safety edits need an Owner; members get 403; the JSON view is read-only
and Owner-only; °C display converts at the edge only."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from tests.house import TSTAT
from thermabot.adapters import db
from thermabot.core.statemachine import Persisted


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


@pytest.mark.brd("FR-CFG-1")
def test_home_editor_removes_room_weights_and_protects_assigned_rooms(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings/people")
    page.locator('[data-home-room="study"] [data-room-remove]').click()
    expect(page.locator("[data-testid=toast]")).to_contain_text("Reassign users")
    expect(page.locator('[data-home-room="study"]')).to_be_visible()
    page.locator('[data-home-room="den"] [data-room-remove]').click()
    page.click("[data-testid=confirm-yes]")
    page.click("[data-testid=settings-save]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    assert "den" not in srv.ctx.settings.rooms
    assert all("den" not in weights for weights in srv.ctx.settings.weights.values())
    assert srv.ctx.mock_ha.calls == []  # type: ignore[attr-defined]
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CFG-1")
def test_home_editor_adds_floor_and_persists_room_assignment(page: Page, srv: Srv) -> None:
    before = srv.ctx.settings.model_dump(mode="json")
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings/people")
    page.click("[data-home-add=floor]")
    page.fill("[data-home-new=name]", "Main floor")
    page.click("[data-home-dialog-save]")
    expect(page.locator('[data-home-floor="Main floor"]')).to_contain_text("0 rooms")
    expect(page.locator("[data-testid=settings-save]")).to_be_disabled()
    page.locator('[data-home-room="study"] [data-r=floor]').fill("Main floor")
    expect(page.locator('[data-home-floor="Main floor"]')).to_contain_text("Study")
    page.click("[data-testid=settings-save]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    assert srv.ctx.settings.rooms["study"].floor == "Main floor"
    assert srv.ctx.settings.weights == before["weights"]
    assert srv.ctx.mock_ha.calls == []  # type: ignore[attr-defined]
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CFG-1")
def test_home_editor_preserves_unused_weight_blocks(page: Page, srv: Srv) -> None:
    srv.ctx.settings = srv.ctx.settings.model_copy(
        update={"weights": {**srv.ctx.settings.weights, "away": {}}}
    )
    before = srv.ctx.settings.weights
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings/people")
    expect(page.locator("[data-testid=settings-save]")).to_be_disabled()
    page.locator('[data-home-room="study"] [data-r=floor]').fill("Main floor")
    page.click("[data-testid=settings-save]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    assert srv.ctx.settings.weights == before
    assert srv.ctx.mock_ha.calls == []  # type: ignore[attr-defined]
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-CFG-1")
def test_home_discovery_failure_preserves_saved_mappings(page: Page, srv: Srv) -> None:
    before = srv.ctx.settings.model_dump(mode="json")
    page.route(
        "**/api/setup/discover**",
        lambda route: route.fulfill(status=503, json={"detail": "Home Assistant unavailable"}),
    )
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings/people")
    expect(page.locator("[data-testid=home-discovery-status]")).to_contain_text("Discovery failed")
    expect(page.locator('[data-home-room="study"] [data-r=temp]')).to_have_value(
        "sensor.study_temperature"
    )
    expect(page.locator("[data-testid=settings-save]")).to_be_disabled()
    assert srv.ctx.settings.model_dump(mode="json") == before
    srv.errors.clear()  # expected mocked 503 response
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
    dest = {"safety": "/settings/safety", "schedule": "/settings/schedule"}.get(
        section, "/settings"
    )
    page.goto(f"{srv.url}{dest}")
    if section != "schedule":
        _open(page, section)
    page.fill(f"[data-path='{path}']", value)
    page.click("[data-testid=settings-save]")
    if section == "safety":
        page.fill("[data-testid=confirm-typed]", "SAVE")
        page.click("[data-testid=confirm-yes]")
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
    assert page.locator("[data-testid=advanced]").count() == 0  # JSON view: Owner only
    assert page.locator("[data-testid=open-wizard]").count() == 0
    denied = page.goto(f"{srv.url}/settings/safety")
    assert denied is not None and denied.status == 403
    page.goto(f"{srv.url}/settings/ha")
    expect(page.locator("[data-path='thermostat.entity_id']")).to_be_disabled()
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


@pytest.mark.brd("NFR-SAFE-5")
def test_active_thermostat_change_explains_that_control_must_stop(page: Page, srv: Srv) -> None:
    now = srv.ctx.clock()
    db.write_bot_state(
        srv.ctx.conn, Persisted(kill_switch="on", run_mode="active"), by="test", now=now
    )
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings/ha")
    _open(page, "thermostat")
    page.fill("[data-path='thermostat.entity_id']", "climate.other")
    page.click("[data-testid=settings-save]")

    expect(page.locator("[data-testid=errors]")).to_contain_text("Stop Control")
    assert srv.ctx.settings.thermostat.entity_id == TSTAT
    srv.errors.clear()  # the rejected 409 request is expected
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


@pytest.mark.brd("FR-CFG-1")
@pytest.mark.parametrize("width", [390, 1440])
def test_home_editor_adds_floor_room_and_read_only_equipment(
    page: Page, srv: Srv, width: int
) -> None:
    srv.ctx.mock_ha.states["fan.study"] = {  # type: ignore[attr-defined]
        "entity_id": "fan.study",
        "state": "off",
        "attributes": {"friendly_name": "Study fan"},
    }
    before = srv.ctx.settings.model_dump(mode="json")
    page.set_viewport_size({"width": width, "height": 900})
    login(page, srv, "alex")
    page.goto(f"{srv.url}/settings/people")
    expect(page.locator("[data-testid=home-discovery-status]")).to_contain_text("loaded")
    page.click("[data-home-add=room]")
    page.fill("[data-home-new=name]", "Guest room")
    page.fill("[data-home-new=floor]", "Upper floor")
    page.select_option("[data-home-new=temp]", "sensor.den_temperature")
    page.click("[data-home-dialog-save]")
    room = page.locator('[data-home-room="guest_room"]')
    room.locator("[data-room-devices] summary").click()
    room.locator("[data-room-device-picker]").select_option("fan.study")
    room.locator("[data-room-device-add]").click()
    expect(page.locator('[data-home-floor="Upper floor"]')).to_contain_text("Guest Room")
    assert srv.ctx.settings.model_dump(mode="json") == before
    page.click("[data-testid=settings-save]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Saved")
    page.wait_for_event("load")
    added = srv.ctx.settings.rooms["guest_room"]
    assert (added.floor, added.temp, added.devices) == (
        "Upper floor",
        "sensor.den_temperature",
        ["fan.study"],
    )
    assert srv.ctx.settings.weights["home"]["guest_room"] == 1
    assert (
        srv.ctx.settings.rooms["bedroom_1"].model_dump(mode="json") == before["rooms"]["bedroom_1"]
    )
    assert srv.ctx.settings.thermostat.model_dump(mode="json") == before["thermostat"]
    assert srv.ctx.mock_ha.calls == []  # type: ignore[attr-defined]
    expect(page.locator('[data-home-room="guest_room"]')).to_contain_text("Study fan")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert_page_healthy(page, srv)
