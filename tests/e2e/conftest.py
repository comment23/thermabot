"""E2E harness: real uvicorn server (thread, 127.0.0.1, random port) + headless Chromium.

No network beyond loopback: every request to another host fails `assert_page_healthy`.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from playwright.sync_api import Browser, Page, sync_playwright

from tests.house import HOUSE
from thermabot.adapters import db
from thermabot.app.auth import Auth, create_user
from thermabot.app.routes_api import Ctx, create_app
from thermabot.core.statemachine import Live, Persisted

PW = "correct horse"
USERS = (
    ("alex", "owner", None),
    ("ann", "admin", None),
    ("jordan", "member", "bedroom_1"),
    ("bea", "member", "study"),
)


@dataclass
class Srv:
    ctx: Ctx
    url: str
    errors: list[str] = field(default_factory=list)


def _weather_mocks() -> dict[str, Any]:
    """v0.4.0: weather test fetches answer from fixtures; no live network in e2e. Both
    providers' fixtures are shifted to the real wall-clock `now` (e2e's Ctx has no fake
    clock), so they never go stale as real time passes the fixtures' originally-recorded
    dates (2026-10-05)."""
    from tests.integration.test_jobs import nws_ok
    from tests.integration.test_open_meteo import om_body

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    om = httpx.MockTransport(lambda r: httpx.Response(200, json=om_body(start=now)))
    return {"open_meteo": om, "nws": nws_ok(start=now)}


def make_ctx(tmp: Path, **kw: Any) -> Ctx:
    from tests.integration.mock_ha import GOOD_TOKEN, MockHA
    from thermabot.adapters.ha_rest import HARest

    now = datetime.now(UTC)
    conn = db.open_db(tmp / "e2e.db", now=now)
    db.write_bot_state(
        conn,
        Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=now - timedelta(days=3)),
        by="test",
        now=now,
    )
    for name, role, room in USERS:
        create_user(conn, name, role, PW, room=room, now=now)  # type: ignore[arg-type]
    kw.setdefault("settings", HOUSE)
    kw.setdefault("weather_transports", _weather_mocks())
    ha = MockHA()  # generic test house only (climate.living_room ...); never real data
    ctx = Ctx(
        conn=conn,
        auth=Auth(conn, bob_token="bob-e2e-token-0123456789abcdef"),
        live=Live(ha_mirror="on", tstat_age_min=0),
        last_tick=now,
        ha_health="ok",
        ha_read=HARest("http://ha.test", GOOD_TOKEN, transport=httpx.ASGITransport(app=ha.app)),
        ha_url="http://homeassistant.local:8123",
        **kw,
    )
    ctx.mock_ha = ha  # type: ignore[attr-defined]  # tests inspect ha.calls
    return ctx


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")  # module, not session: sync Playwright holds a running
def browser() -> Iterator[Browser]:  # event loop that breaks later pytest-asyncio tests
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def srv(tmp_path: Path) -> Iterator[Srv]:
    ctx = make_ctx(tmp_path)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(ctx),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            timeout_graceful_shutdown=1,
        )
    )
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    yield Srv(ctx=ctx, url=f"http://127.0.0.1:{port}")
    server.should_exit = True
    th.join(5)


@pytest.fixture
def page(browser: Browser, srv: Srv) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 375, "height": 800})
    pg = context.new_page()
    pg.set_default_timeout(5000)
    pg.on("console", lambda m: m.type == "error" and srv.errors.append(f"console: {m.text}"))
    pg.on("pageerror", lambda e: srv.errors.append(f"pageerror: {e}"))

    def _req(r: Any) -> None:
        if not r.url.startswith((srv.url, "data:", "about:")):
            srv.errors.append(f"external request: {r.url}")

    pg.on("request", _req)
    yield pg
    context.close()


def login(page: Page, srv: Srv, user: str, destination: str = "/") -> None:
    page.goto(f"{srv.url}/login")
    page.fill("input[name=username]", user)
    page.fill("input[name=password]", PW)
    page.click("button[type=submit]")
    page.wait_for_url(f"{srv.url}{destination}")


def capture_chart_labels(page: Page) -> None:
    page.add_init_script(
        """(() => {
            const draw = CanvasRenderingContext2D.prototype.fillText;
            window.chartLabels = [];
            CanvasRenderingContext2D.prototype.fillText = function(text, ...args) {
                if (this.canvas.closest('.chart'))
                    window.chartLabels.push(
                        `${String(text)}|${Math.round(args[0])}|${Math.round(args[1])}`
                    );
                return draw.call(this, text, ...args);
            };
        })()"""
    )


def assert_page_healthy(page: Page, srv: Srv) -> None:
    """Header present, no console errors, no external requests (D§11 page budget)."""
    assert page.locator("[data-testid=header]").count() == 1
    assert page.locator("[data-testid=state-badge]").count() == 1
    page.wait_for_load_state("load")
    assert srv.errors == [], srv.errors
