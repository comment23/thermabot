"""v0.1.6: change own password from /account (every role) and /users (Owner reset), at 390x844."""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import PW, Srv, assert_page_healthy, login

W, H = 390, 844
NEW = "a brand new pw"


@pytest.fixture
def phone(browser: Browser, srv: Srv) -> Page:
    ctx = browser.new_context(viewport={"width": W, "height": H}, is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    pg.set_default_timeout(5000)
    pg.on("console", lambda m: m.type == "error" and srv.errors.append(f"console: {m.text}"))
    pg.on("pageerror", lambda e: srv.errors.append(f"pageerror: {e}"))
    return pg


def _relogin(page: Page, srv: Srv, user: str, pw: str) -> int:
    r = page.request.post(f"{srv.url}/login", data={"username": user, "password": pw})
    return r.status


@pytest.mark.brd("FR-USR-1")
@pytest.mark.parametrize("user", ["alex", "jordan"])
def test_change_own_password_on_phone(phone: Page, srv: Srv, user: str) -> None:
    login(phone, srv, user)
    phone.click("[data-testid=nav-more] > summary")  # Account lives under More on phones
    phone.click("[data-testid=nav-more] a[href='account']")
    f = phone.locator("[data-testid=password-form]")
    f.locator("input[name=current]").fill(PW)
    f.locator("input[name=new]").fill(NEW)
    f.locator("input[name=confirm]").fill(NEW + "x")
    f.locator("button[type=submit]").click()
    expect(f.locator("[data-testid=errors]")).to_contain_text("don't match")
    srv.errors.clear()  # the 422 is logged by the browser
    f.locator("input[name=confirm]").fill(NEW)
    f.locator("button[type=submit]").click()
    expect(phone.locator("[data-testid=toast]")).to_contain_text("Password changed")
    assert phone.evaluate("document.documentElement.scrollWidth") <= W
    box = f.locator("button[type=submit]").bounding_box()
    assert box is not None and box["width"] >= 0.8 * phone.evaluate(
        "document.querySelector('main').clientWidth"
    )
    assert_page_healthy(phone, srv)
    assert _relogin(phone, srv, user, PW) == 401
    assert _relogin(phone, srv, user, NEW) == 200


@pytest.mark.brd("FR-USR-1")
def test_owner_resets_password_on_users_page(phone: Page, srv: Srv) -> None:
    login(phone, srv, "alex")
    phone.goto(f"{srv.url}/users")
    g = phone.locator("[data-testid=reset-form]")
    assert g.locator("[data-testid=reset-user] option", has_text="alex").count() == 0
    g.locator("[data-testid=reset-user]").select_option(label="jordan")
    g.locator("input[name=password]").fill(NEW)
    g.locator("button[type=submit]").click()
    expect(phone.locator("[data-testid=toast]")).to_contain_text("Password reset")
    acts = {r[0] for r in srv.ctx.conn.execute("SELECT action FROM audit WHERE actor='alex'")}
    assert "password_reset" in acts
    assert phone.evaluate("document.documentElement.scrollWidth") <= W
    assert_page_healthy(phone, srv)
    assert _relogin(phone, srv, "jordan", NEW) == 200
