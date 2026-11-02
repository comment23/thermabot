"""v0.1.9: Shadow report page at 390x844 (phone), reachable from More, member access = view."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import Srv, assert_page_healthy, login
from tests.e2e.test_mobile import W, phone  # noqa: F401 - fixture
from thermabot.app import jobs as J
from thermabot.core.shadow import ShadowDay


def seed(srv: Srv) -> None:
    now = datetime.now(UTC)
    for m in range(30):
        J.save_sample(srv.ctx.conn, now - timedelta(minutes=m), {"tstat": None}, "SHADOW", None, [])
    day = (now - timedelta(days=1)).date().isoformat()
    sd = ShadowDay(
        actual_heat_min=120,
        actual_cool_min=0,
        actual_comfort_pct=96,
        n_samples=1440,
        coverage_pct=100,
        note="no thermal model yet: bot estimate not available",
    )
    J.save_shadow_day(srv.ctx.conn, day, sd, None, now)


@pytest.mark.brd("FR-UI-1")
@pytest.mark.parametrize("user", ["alex", "jordan"])
def test_shadow_page_via_more_on_phone(phone: Page, srv: Srv, user: str) -> None:  # noqa: F811
    seed(srv)
    login(phone, srv, user)
    phone.locator("[data-testid=nav-more] summary").click()
    phone.click("[data-testid=nav-more] a[href='shadow']")
    phone.wait_for_url("**/shadow")
    assert_page_healthy(phone, srv)
    assert phone.evaluate("document.documentElement.scrollWidth") <= W
    assert phone.locator("[data-testid=samples-count]").inner_text() == "30"
    assert phone.locator("[data-testid=learning-since]").inner_text().startswith("Learning since")
    assert phone.locator("[data-testid=verdict]").inner_text().startswith("Not enough data yet")
    rows = phone.locator("[data-testid=shadow-row]")
    assert rows.count() == 1 and "no model" in rows.first.inner_text()
    r = phone.request.get(f"{srv.url}/api/shadow")
    assert r.status == 200 and r.json()["samples"]["count"] == 30


@pytest.mark.brd("FR-UI-1")
def test_shadow_requires_login(phone: Page, srv: Srv) -> None:  # noqa: F811
    phone.goto(f"{srv.url}/shadow")
    phone.wait_for_url("**/login")
    assert phone.request.get(f"{srv.url}/api/shadow").status == 401
