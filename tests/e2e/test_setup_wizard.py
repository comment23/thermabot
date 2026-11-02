"""v0.3.0 setup wizard e2e (phone 390x844 and desktop 1440x900), against the generic mock HA
test house only (climate.living_room ...). Screenshots for docs/screenshots/ are written only
when THERMABOT_SCREENSHOTS=1, so a normal test run never touches the tree."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login
from tests.e2e.test_mobile import TAP, TAP_SEL
from thermabot.config import Settings

SHOTS = Path(__file__).resolve().parents[2] / "docs" / "screenshots"
PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1440, "height": 900}
NEW_PAGES = ["/setup", "/settings"]


def _shot(page: Page, name: str) -> None:
    if os.environ.get("THERMABOT_SCREENSHOTS") == "1":
        SHOTS.mkdir(parents=True, exist_ok=True)
        # phone: viewport only (a full-page shot smears the fixed bottom tab bar)
        full = page.viewport_size is not None and page.viewport_size["width"] > 600
        page.screenshot(path=str(SHOTS / name), full_page=full)


def _ctx_page(browser: Browser, srv: Srv, vp: dict[str, int], dark: bool = False) -> Page:
    kw: dict[str, object] = {"viewport": vp}
    if vp is PHONE:
        kw.update(is_mobile=True, has_touch=True)
    if dark:
        kw["color_scheme"] = "dark"
    pg = browser.new_context(**kw).new_page()  # type: ignore[arg-type]
    pg.set_default_timeout(5000)
    pg.on("console", lambda m: m.type == "error" and srv.errors.append(f"console: {m.text}"))
    pg.on("pageerror", lambda e: srv.errors.append(f"pageerror: {e}"))
    pg.on(
        "request",
        lambda r: (
            None
            if r.url.startswith((srv.url, "data:", "about:"))
            else srv.errors.append(f"external request: {r.url}")
        ),
    )
    return pg


@pytest.fixture
def phone(browser: Browser, srv: Srv) -> Page:
    return _ctx_page(browser, srv, PHONE)


@pytest.fixture
def desktop(browser: Browser, srv: Srv) -> Page:
    return _ctx_page(browser, srv, DESKTOP)


def _fresh(srv: Srv) -> None:
    """A fresh install: no thermostat, no settings row, nothing running."""
    srv.ctx.settings = Settings()
    srv.ctx.running_entity = ""
    srv.ctx.mock_ha.states["binary_sensor.front_door"] = {  # type: ignore[attr-defined]
        "entity_id": "binary_sensor.front_door",
        "state": "off",
        "attributes": {"device_class": "door", "friendly_name": "Front door"},
    }


def _next(page: Page) -> None:
    page.click("[data-testid=setup-next]")


def run_wizard(page: Page, srv: Srv, shots: str | None = None) -> None:
    page.goto(f"{srv.url}/setup")
    expect(page.locator("[data-testid=step-ha]")).to_be_visible()
    _next(page)  # not tested yet -> stays
    expect(page.locator("[data-testid=setup-errors]")).to_contain_text("Test the Home Assistant")
    page.click("[data-testid=ha-test]")
    expect(page.locator("[data-testid=ha-result]")).to_contain_text("OK: Home Assistant 2026.10.0")
    if shots:
        _shot(page, f"setup-1-connect-{shots}.png")
    _next(page)
    # 2: thermostat picker lists climate.* with name/state/modes
    lst = page.locator("[data-testid=climate-list]")
    expect(lst).to_contain_text("climate.living_room")
    expect(lst).to_contain_text("Living Room")
    expect(lst).to_contain_text("heat_cool")
    expect(lst).to_contain_text("circulate")
    _next(page)
    expect(page.locator("[data-testid=setup-errors]")).to_contain_text("Pick the thermostat")
    page.click("label.pick[data-entity='climate.living_room']")
    if shots:
        _shot(page, f"setup-2-thermostat-{shots}.png")
    _next(page)
    # 3: sensors pre-filled from suggestions (fresh install), overridable
    expect(page.locator("[data-testid=sensor-blower]")).to_have_value(
        "binary_sensor.living_room_blower_active"
    )
    expect(page.locator("[data-testid=sensor-outdoor]")).to_have_value("sensor.outdoor_temperature")
    expect(page.locator("[data-testid=suggest-compressor]")).to_contain_text("%")
    expect(page.locator("[data-testid=home-location]")).to_contain_text("Test House")
    expect(page.locator("[data-testid=home-context]")).to_contain_text("does not control equipment")
    page.fill("[data-testid=sensor-humidity]", "sensor.nope")
    _next(page)
    expect(page.locator("[data-testid=setup-errors]")).to_contain_text("not a Home Assistant")
    page.fill("[data-testid=sensor-humidity]", "")
    rows = page.locator("[data-testid=room-row]")
    study = rows.filter(has=page.locator("[value='sensor.study_temperature']"))
    study.locator("[data-r=on]").check()
    study.locator("[data-r=floor]").fill("Second")
    door = page.locator(".opening-rule").filter(has_text="binary_sensor.front_door")
    door.locator("[data-opening=use]").check()
    door.locator("[data-opening=role]").select_option("exterior")
    door.locator("[data-opening=rooms]").fill("study")
    page.locator("[data-testid=openings-enabled]").check()
    if shots:
        _shot(page, f"setup-3-sensors-{shots}.png")
    _next(page)
    # 4: people
    alex = page.locator("[data-testid=person-row]").filter(
        has=page.locator("[value='person.alex']")
    )
    alex.locator("[data-p=on]").check()
    alex.locator("[data-p=name]").fill("Alex")
    _next(page)
    # 5: comfort bands with validation, °C display
    page.select_option("[data-testid=units]", "C")
    expect(page.locator("[data-testid=band-heat-home-0]")).to_have_value("20")
    page.fill("[data-testid=band-heat-home-1]", "23.5")  # 74.5 F > cool low 73
    _next(page)
    expect(page.locator("[data-testid=setup-errors]")).to_contain_text("heat high")
    page.fill("[data-testid=band-heat-home-1]", "21.5")  # 70.5 F
    if shots:
        _shot(page, f"setup-5-comfort-{shots}.png")
    _next(page)
    # 6: weather (v0.4.0): fresh install defaults to Open-Meteo; HA home location; test fetch
    expect(page.locator("[data-testid=step-weather]")).to_be_visible()
    expect(page.locator("[data-testid=weather-provider]")).to_have_value("open_meteo")
    page.fill("[data-testid=weather-lat]", "95")
    page.fill("[data-testid=weather-lon]", "0")
    _next(page)
    expect(page.locator("[data-testid=setup-errors]")).to_contain_text("Latitude")
    page.click("[data-testid=weather-ha-location]")
    expect(page.locator("[data-testid=weather-lat]")).to_have_value("40")
    expect(page.locator("[data-testid=weather-lon]")).to_have_value("-75")
    page.click("[data-testid=weather-test]")
    expect(page.locator("[data-testid=weather-result]")).to_contain_text("OK: open_meteo")
    expect(page.locator("[data-testid=weather-preview]")).to_be_visible()
    if shots:
        _shot(page, f"setup-6-weather-{shots}.png")
    _next(page)
    # 7: review
    expect(page.locator("[data-testid=review-weather]")).to_contain_text("Open-Meteo, 40, -75")
    expect(page.locator("[data-testid=review-thermostat]")).to_have_text("climate.living_room")
    expect(page.locator("[data-testid=review-people]")).to_contain_text("alex = person.alex")
    expect(page.locator("[data-testid=review-rooms]")).to_contain_text("study")
    expect(page.locator("[data-testid=restart-note]")).to_be_visible()
    if shots:
        _shot(page, f"setup-7-review-{shots}.png")


def _assert_saved(srv: Srv) -> None:
    s = srv.ctx.settings
    assert s.thermostat.entity_id == "climate.living_room"
    assert s.rooms["thermostat"].temp == "climate.living_room"
    assert s.rooms["study"].temp == "sensor.study_temperature"
    assert s.rooms["study"].floor == "Second"
    assert s.openings.enabled
    assert s.openings.sensors["binary_sensor.front_door"].role == "exterior"
    assert s.openings.sensors["binary_sensor.front_door"].affected_rooms == ["study"]
    assert s.weights["home"]["study"] == 1
    assert s.sensors.blower == "binary_sensor.living_room_blower_active"
    assert s.sensors.humidity == ""
    assert s.presence.people["alex"].companion == "person.alex"
    assert s.display.units == "C"
    assert s.bands.heat.home == (68, 70.5)  # stored °F
    assert s.bands.cool.home == (73, 76)  # untouched: exact
    assert (s.weather.provider, s.weather.lat, s.weather.lon) == ("open_meteo", 40, -75)
    row = srv.ctx.conn.execute("SELECT updated_by FROM settings WHERE key='settings'").fetchone()
    assert row["updated_by"] == "alex"
    st = srv.ctx.conn.execute("SELECT kill_switch, run_mode FROM bot_state").fetchone()
    assert (st["kill_switch"], st["run_mode"]) == ("on", "shadow")  # unchanged by save
    assert srv.ctx.mock_ha.calls == []  # type: ignore[attr-defined]  # no HA service call


@pytest.mark.brd("FR-CFG-1")
def test_wizard_phone(phone: Page, srv: Srv) -> None:
    _fresh(srv)
    login(phone, srv, "alex", destination="/setup")
    phone.goto(f"{srv.url}/")
    expect(phone.locator("[data-testid=setup-link]")).to_be_visible()
    phone.click("[data-testid=setup-link]")
    phone.wait_for_url("**/setup")
    run_wizard(phone, srv, shots="phone")
    phone.click("[data-testid=setup-save]")
    expect(phone.locator("[data-testid=toast]")).to_contain_text("Restart thermabot")
    _assert_saved(srv)
    phone.goto(f"{srv.url}/")
    expect(phone.locator("[data-testid=restart-banner]")).to_contain_text("climate.living_room")
    w = phone.evaluate("document.documentElement.scrollWidth")
    assert w <= PHONE["width"]
    assert_page_healthy(phone, srv)


@pytest.mark.brd("FR-CFG-1")
def test_wizard_desktop(desktop: Page, srv: Srv) -> None:
    _fresh(srv)
    login(desktop, srv, "alex", destination="/setup")
    run_wizard(desktop, srv, shots="desktop")
    desktop.click("[data-testid=setup-save]")
    expect(desktop.locator("[data-testid=toast]")).to_contain_text("Saved")
    _assert_saved(srv)
    desktop.goto(f"{srv.url}/settings")
    _shot(desktop, "settings-desktop.png")
    assert_page_healthy(desktop, srv)


@pytest.mark.brd("FR-CFG-1")
def test_wizard_reachable_from_settings_and_preloads(phone: Page, srv: Srv) -> None:
    """Configured house: the wizard opens from Settings with the current values; leaving
    without saving changes nothing and needs no restart."""
    login(phone, srv, "alex")
    phone.goto(f"{srv.url}/settings")
    _shot(phone, "settings-phone.png")
    phone.click("[data-testid=open-wizard]")
    phone.wait_for_url("**/setup")
    phone.click("[data-testid=ha-test]")
    expect(phone.locator("[data-testid=ha-result]")).to_contain_text("OK")
    _next(phone)
    expect(phone.locator("input[name=tstat][value='climate.living_room']")).to_be_checked()
    assert srv.ctx.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    assert srv.ctx.mock_ha.calls == []  # type: ignore[attr-defined]
    assert phone.locator("[data-testid=restart-banner]").count() == 0
    assert_page_healthy(phone, srv)


@pytest.mark.brd("FR-USR-2")
@pytest.mark.parametrize("user", ["ann", "jordan"])
def test_wizard_owner_only(page: Page, srv: Srv, user: str) -> None:
    _fresh(srv)
    login(page, srv, user)
    r = page.goto(f"{srv.url}/setup")
    assert r is not None and r.status == 403
    page.goto(f"{srv.url}/")
    assert page.locator("[data-testid=setup-link]").count() == 0
    assert page.locator("a[href='setup']").count() == 0
    srv.errors.clear()


ABS = re.compile(r"""(?:href|src|action)\s*=\s*["']\s*/(?!/)""", re.I)
ABS_JS = re.compile(r"""(?:fetch|api|EventSource)\(\s*(?:'[A-Z]+',\s*)?['"`]/""")


@pytest.mark.brd("FR-UI-1")
def test_new_pages_have_no_absolute_links(page: Page, srv: Srv) -> None:
    """Ingress: no root-relative URL in the rendered setup/settings pages or their JS."""
    login(page, srv, "alex")
    for href in NEW_PAGES:
        r = page.goto(f"{srv.url}{href}")
        assert r is not None and r.status == 200
        html = page.content()
        assert not ABS.findall(html), (href, ABS.findall(html))
        attrs = page.evaluate(
            """() => [...document.querySelectorAll('[href],[src],[action],[data-api]')]
                .map(e => e.getAttribute('href') || e.getAttribute('src')
                       || e.getAttribute('action') || e.getAttribute('data-api'))"""
        )
        bad = [a for a in attrs if a and re.match(r"^(?:[A-Z]+ )?/(?!/)", a)]
        assert bad == [], (href, bad)
    static = Path(__file__).resolve().parents[2] / "thermabot" / "app" / "static"
    for js in ("setup.js", "settings.js", "app.js"):
        text = (static / js).read_text(encoding="utf-8")
        assert not ABS_JS.findall(text), js
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-UI-1")
def test_wizard_under_ingress_prefix(page: Page, srv: Srv) -> None:
    """Served behind a path prefix (as HA ingress does) the wizard still loads its assets and
    talks to the API through relative URLs."""
    prefix = "/api/hassio_ingress/tok123"

    def route(r: object) -> None:
        req = r.request  # type: ignore[attr-defined]
        url = req.url.replace(f"{srv.url}{prefix}", srv.url)
        hdrs = {**req.headers, "x-ingress-path": prefix}
        resp = r.fetch(url=url, headers=hdrs, max_redirects=0)  # type: ignore[attr-defined]
        r.fulfill(response=resp)  # type: ignore[attr-defined]

    page.add_init_script(  # poll instead of SSE: an endless stream can't be proxied by route
        "new MutationObserver((_, o) => { if (document.documentElement) {"
        " document.documentElement.dataset.nosse = '1'; o.disconnect(); } })"
        ".observe(document, {childList: true})"
    )
    page.route(re.compile("^" + re.escape(f"{srv.url}{prefix}/")), route)
    bad: list[str] = []
    page.on("response", lambda r: r.status >= 400 and bad.append(f"{r.status} {r.url}"))
    page.goto(f"{srv.url}{prefix}/login")
    page.fill("input[name=username]", "alex")
    page.fill("input[name=password]", "correct horse")
    page.click("button[type=submit]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Welcome")
    # (the post-login 303 is relative, './'; covered by test_setup_api. Playwright does not
    # route a browser-followed redirect hop, so navigate explicitly.)
    page.goto(f"{srv.url}{prefix}/")
    expect(page.locator("[data-testid=header]")).to_be_visible()
    page.goto(f"{srv.url}{prefix}/setup")
    page.click("[data-testid=ha-test]")
    expect(page.locator("[data-testid=ha-result]")).to_contain_text("OK")
    reqs: list[str] = []
    page.on("request", lambda r: reqs.append(r.url))
    page.reload()
    page.wait_for_load_state("load")
    assert reqs and all(u.startswith(f"{srv.url}{prefix}/") for u in reqs), reqs
    assert bad == [], bad
    assert_page_healthy(page, srv)
    page.unroute_all(behavior="ignoreErrors")


@pytest.mark.brd("FR-UI-1")
def test_wizard_tap_targets_and_dark_mode(browser: Browser, srv: Srv) -> None:
    _fresh(srv)
    pg = _ctx_page(browser, srv, PHONE, dark=True)
    login(pg, srv, "alex", destination="/setup")
    pg.goto(f"{srv.url}/setup")
    pg.click("[data-testid=ha-test]")
    expect(pg.locator("[data-testid=ha-result]")).to_contain_text("OK")
    for step in range(1, 6):
        for el in pg.locator(TAP_SEL + ", label.pick:visible").all():
            if el.evaluate("e => !!e.closest('dialog, #kill-open')"):
                continue
            box = el.bounding_box()
            if box is None or box["width"] == 0:
                continue
            what = el.evaluate("e => e.outerHTML.slice(0, 100)")
            assert box["height"] >= TAP - 0.5, f"step {step}: {what} is {box['height']}px"
        if step == 2:
            pg.click("label.pick[data-entity='climate.living_room']")
        if step < 5:
            _next(pg)
    bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
    rgb = [int(x) for x in re.findall(r"\d+", bg)[:3]]
    assert sum(rgb) < 3 * 80, bg  # dark background
    _shot(pg, "setup-5-comfort-phone-dark.png")
    assert pg.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]
    assert_page_healthy(pg, srv)


@pytest.mark.brd("FR-UI-1")
def test_settings_desktop_layout(desktop: Page, srv: Srv) -> None:
    login(desktop, srv, "alex")
    desktop.goto(f"{srv.url}/settings")
    assert desktop.evaluate("document.documentElement.scrollWidth") <= DESKTOP["width"]
    # forms per section, multi-column at desktop width
    fs = desktop.locator("[data-testid=section-bands] fieldset")
    cols = fs.evaluate("e => getComputedStyle(e).gridTemplateColumns.split(' ').length")
    assert cols >= 3
    assert_page_healthy(desktop, srv)
