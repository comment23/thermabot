"""Phone layout (Alex uses an iPhone, 390x844). v0.1.3: no page scrolls sideways; action
buttons full width under 600 px. v0.1.8: bottom tab bar + More menu, tables as cards,
44 px tap targets (header Stop/Start accepted at 36 px)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser, Page

from tests.e2e.conftest import Srv, assert_page_healthy, login
from tests.e2e.test_layout import _pages

W, H = 390, 844


@pytest.fixture
def phone(browser: Browser, srv: Srv) -> Page:
    ctx = browser.new_context(viewport={"width": W, "height": H}, is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    pg.set_default_timeout(5000)
    pg.on("console", lambda m: m.type == "error" and srv.errors.append(f"console: {m.text}"))
    pg.on("pageerror", lambda e: srv.errors.append(f"pageerror: {e}"))
    return pg


@pytest.mark.brd("FR-UI-1")
def test_pages_fit_phone_width(phone: Page, srv: Srv) -> None:
    login(phone, srv, "alex")
    for href in _pages(srv):
        r = phone.goto(f"{srv.url}{href}")
        assert r is not None and r.status == 200, href
        assert_page_healthy(phone, srv)
        page_w = phone.evaluate("document.documentElement.scrollWidth")
        assert page_w <= W, f"{href}: page scrolls sideways ({page_w}px > {W}px)"
        # every nav link reachable without sideways scrolling
        for box in (a.bounding_box() for a in phone.locator("[data-testid=nav] a").all()):
            assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= W, href
        # tables never wider than the screen (they scroll inside themselves)
        for t in phone.locator("main table").all():
            box = t.bounding_box()
            if box is not None:
                assert box["x"] + box["width"] <= W + 1, f"{href}: table overflows"


@pytest.mark.brd("FR-UI-1")
def test_buttons_full_width_on_phone(phone: Page, srv: Srv) -> None:
    login(phone, srv, "alex")
    phone.goto(f"{srv.url}/users")
    content_w = phone.evaluate("document.querySelector('main').clientWidth")
    # row actions inside tables stay compact; standalone actions go full width
    btns = [
        b
        for b in phone.locator("main > :not(header) button:visible").all()
        if not b.evaluate("e => !!e.closest('table, dialog')")
    ]
    assert btns, "expected action buttons on /users"
    for b in btns:
        box = b.bounding_box()
        assert box is not None and box["width"] >= 0.8 * content_w, b.inner_text()


TAP = 44
TAP_SEL = ", ".join(
    [
        "main button:visible",
        "main input:visible:not([type=hidden]):not([type=checkbox]):not([type=radio])",
        "main select:visible",
        "nav.tabs > ul > li > a:visible",
        "nav.tabs summary:visible",
        "nav.seg a:visible",
    ]
)


@pytest.mark.brd("FR-UI-1")
def test_bottom_tab_bar_on_phone(phone: Page, srv: Srv) -> None:
    login(phone, srv, "alex")
    for href in _pages(srv):
        phone.goto(f"{srv.url}{href}")
        nav = phone.locator("[data-testid=nav]").bounding_box()
        assert nav is not None, href
        # pinned to the bottom of the viewport, full width
        assert abs(nav["y"] + nav["height"] - H) <= 2, f"{href}: tab bar not at bottom ({nav})"
        assert nav["width"] >= W - 2, href
        tabs = phone.locator("nav.tabs > ul > li:visible").all()
        assert 3 <= len(tabs) <= 5, f"{href}: {len(tabs)} tabs"


@pytest.mark.brd("FR-UI-1")
def test_tap_targets_at_least_44px(phone: Page, srv: Srv) -> None:
    login(phone, srv, "alex")
    for href in _pages(srv):
        phone.goto(f"{srv.url}{href}")
        for el in phone.locator(TAP_SEL).all():
            # header Stop/Start is accepted at 36 px (Alex, 2026-10-06)
            if el.evaluate("e => !!e.closest('dialog, #kill-open')"):
                continue
            box = el.bounding_box()
            if box is None or box["width"] == 0:
                continue
            what = el.evaluate("e => e.outerHTML.slice(0, 120)")
            assert box["height"] >= TAP - 0.5, f"{href}: {what} is {box['height']}px tall"


@pytest.mark.brd("FR-UI-1")
@pytest.mark.parametrize(
    ("user", "want", "hidden"),
    [
        ("alex", ["/settings", "/setup", "/model", "/health", "/users", "/account"], []),
        ("jordan", ["/account"], ["/settings", "/setup", "/users"]),
    ],
)
def test_more_menu_role_gated(
    phone: Page, srv: Srv, user: str, want: list[str], hidden: list[str]
) -> None:
    login(phone, srv, user)
    more = phone.locator("[data-testid=nav-more]")
    links = more.locator("ul a")
    assert not links.first.is_visible(), "More menu should start closed"
    more.locator("summary").click()
    # v0.3.0: nav links are relative (ingress); compare as site paths
    hrefs = ["/" + (a.get_attribute("href") or "") for a in links.all() if a.is_visible()]
    for h in want:
        assert h in hrefs, f"{user}: {h} missing from More ({hrefs})"
    for h in hidden:
        assert h not in hrefs, f"{user}: {h} should be hidden"
    assert phone.locator("[data-testid=logout]").is_visible()
    box = more.locator("ul").bounding_box()
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= W and box["y"] >= 0
    phone.click(f"[data-testid=nav-more] a[href='{want[-1].lstrip('/')}']")
    phone.wait_for_url(f"**{want[-1]}")
    assert_page_healthy(phone, srv)
