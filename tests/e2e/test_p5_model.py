"""T1.41: P5 Model (Owner only). Params, MAE vs limits, offsets, fan results, refit button."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Srv, assert_page_healthy, login


def _seed(srv: Srv) -> None:
    srv.ctx.conn.execute(
        "INSERT INTO models (version, fitted_at, params_json, mae_json, n_samples, active)"
        " VALUES (3, '2026-10-05T07:15:00+00:00', ?, ?, 4321, 1)",
        (
            json.dumps({"tau_h": 9.5, "k_heat": 2.1, "offsets": {"jordan": -1.25, "loft": 0.5}}),
            json.dumps({"tstat": 0.6, "jordan": 2.4}),
        ),
    )
    srv.ctx.conn.execute(
        "INSERT INTO fan_experiments (started_at, ended_at, gap_before_f, gap_after_f,"
        " outdoor_f, season) VALUES ('2026-10-04T14:00:00+00:00',"
        " '2026-10-04T15:00:00+00:00', 3.5, 2.5, 55, 'heat')"
    )


@pytest.mark.brd("FR-FAN-2")
def test_model_page_shows_params_mae_offsets_fans(page: Page, srv: Srv) -> None:
    _seed(srv)
    login(page, srv, "alex")
    page.goto(f"{srv.url}/model")
    expect(page.locator("[data-testid=model-meta]")).to_contain_text("Version 3")
    expect(page.locator("[data-testid=mae] tr[data-key=tstat]")).not_to_contain_text("above")
    expect(page.locator("[data-testid=mae] tr[data-key=jordan]")).to_contain_text("above 2")
    expect(page.locator("[data-testid=params]")).to_contain_text("tau_h")
    expect(page.locator("[data-testid=params]")).not_to_contain_text("offsets")
    expect(page.locator("[data-testid=offsets] tr[data-key=jordan]")).to_contain_text("-1.25")
    expect(page.locator("[data-testid=fan-row]")).to_contain_text("1.0 ✓")
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-PLN-7")
def test_refit_button_calls_api(page: Page, srv: Srv) -> None:
    calls: list[int] = []

    async def refit() -> None:
        calls.append(1)

    srv.ctx.refit = refit
    login(page, srv, "alex")
    page.goto(f"{srv.url}/model")
    expect(page.locator("[data-testid=no-model]")).to_be_visible()
    page.click("[data-testid=refit]")
    expect(page.locator("[data-testid=toast]")).to_contain_text("Refit started")
    assert calls == [1]
    acts = [r[0] for r in srv.ctx.conn.execute("SELECT action FROM audit WHERE actor='alex'")]
    assert "refit" in acts
    assert_page_healthy(page, srv)


@pytest.mark.brd("FR-USR-2")
@pytest.mark.parametrize("user", ["ann", "jordan"])
def test_model_owner_only(page: Page, srv: Srv, user: str) -> None:
    login(page, srv, user)
    r = page.goto(f"{srv.url}/model")
    assert r is not None and r.status == 403
