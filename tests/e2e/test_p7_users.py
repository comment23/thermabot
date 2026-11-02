"""T1.42b: P7 Users (Owner only): create, role change, delete; last Owner protected."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login


def _role(srv: Srv, name: str) -> str | None:
    r = srv.ctx.conn.execute("SELECT role FROM users WHERE username=?", (name,)).fetchone()
    return None if r is None else str(r[0])


@pytest.mark.brd("FR-USR-1")
def test_create_change_role_delete(page: Page, srv: Srv) -> None:
    page.on("dialog", lambda d: d.accept())
    login(page, srv, "alex")
    page.goto(f"{srv.url}/users")
    f = page.locator("[data-testid=user-form]")
    f.locator("input[name=username]").fill("zed")
    f.locator("select[name=role]").select_option("member")
    f.locator("select[name=room]").select_option("bedroom_1")
    f.locator("input[name=password]").fill("longenough")
    f.locator("button[type=submit]").click()
    expect(page.locator("tr[data-user=zed]")).to_contain_text("bedroom_1")
    g = page.locator("[data-testid=role-form]")
    g.locator("[data-testid=role-user]").select_option(label="zed")
    g.locator("select[name=role]").select_option("admin")
    g.locator("button[type=submit]").click()
    expect(page.locator("tr[data-user=zed]")).to_contain_text("admin")
    page.click("[data-testid=delete-zed]")
    expect(page.locator("tr[data-user=zed]")).to_have_count(0)
    assert _role(srv, "zed") is None
    acts = {r[0] for r in srv.ctx.conn.execute("SELECT action FROM audit WHERE actor='alex'")}
    assert {"user_create", "user_patch", "user_delete"} <= acts
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-USR-1")
def test_last_owner_cannot_be_demoted(page: Page, srv: Srv) -> None:
    login(page, srv, "alex")
    page.goto(f"{srv.url}/users")
    assert page.locator("[data-testid=delete-alex]").count() == 0
    g = page.locator("[data-testid=role-form]")
    g.locator("[data-testid=role-user]").select_option(label="alex")
    g.locator("select[name=role]").select_option("admin")
    g.locator("button[type=submit]").click()
    expect(g.locator("[data-testid=errors]")).to_contain_text("last Owner")
    assert _role(srv, "alex") == "owner"
    srv.errors.clear()  # the 409 is logged by the browser
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-USR-2")
@pytest.mark.parametrize("user", ["ann", "jordan"])
def test_users_owner_only(page: Page, srv: Srv, user: str) -> None:
    login(page, srv, user)
    r = page.goto(f"{srv.url}/users")
    assert r is not None and r.status == 403
