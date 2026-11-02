"""v0.2.0: a fresh install (no `settings` row) is generic and lands in SETUP NEEDED. It must
never read or write a climate entity, and SHADOW stays the default run mode."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.house import HOUSE, TSTAT, seed_house
from tests.integration.mock_ha import BOT_USER_ID, GOOD_TOKEN, MockHA
from tests.integration.test_controller import LIVE, NOW, STEP, Sleeps, snap
from tests.integration.test_main import env
from thermabot.adapters import db
from thermabot.adapters.ha_rest import HARest
from thermabot.app.controller import Controller
from thermabot.app.main import create_app, load_env
from thermabot.config import Settings
from thermabot.core.statemachine import Persisted


class Recorder(httpx.AsyncBaseTransport):
    """Wraps the mock HA transport and records every request path."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.paths: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(f"{request.method} {request.url.path}")
        return await self.inner.handle_async_request(request)


def _run(tmp: Path, ha: MockHA, t: Recorder, wait_beats: int = 1) -> dict[str, object]:
    app = create_app(load_env(env(tmp)), ha_transport=t)
    with TestClient(app) as c:
        deadline = time.monotonic() + 5
        while len(ha.webhook_posts) < wait_beats and time.monotonic() < deadline:
            time.sleep(0.02)
        body: dict[str, object] = c.get("/healthz").json()
    return body


@pytest.mark.brd("FR-CFG-1")
def test_fresh_install_is_setup_needed_and_touches_no_climate(tmp_path: Path) -> None:
    ha = MockHA()
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"  # even with the mirror on
    t = Recorder(httpx.ASGITransport(app=ha.app))
    h = _run(tmp_path, ha, t)
    assert h["state"] == "DISABLED"
    assert [x for x in ha.calls if x["domain"] == "climate"] == []
    assert not [p for p in t.paths if "climate." in p]  # no climate read either
    assert any(p.endswith("/api/webhook/hb-test") for p in t.paths)  # heartbeat still posts
    conn = db.open_db(tmp_path / "data" / "thermabot.db")
    p = db.read_bot_state(conn)
    assert p is not None and p.run_mode == "shadow" and p.kill_switch == "off"
    assert conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM settings WHERE key='settings'").fetchone()[0] == 0
    conn.close()


@pytest.mark.brd("FR-CFG-1")
def test_configured_row_loads_and_reads_its_thermostat(tmp_path: Path) -> None:
    seed_house(tmp_path / "data")
    ha = MockHA()
    t = Recorder(httpx.ASGITransport(app=ha.app))
    _run(tmp_path, ha, t)
    assert any(p == f"GET /api/states/{TSTAT}" for p in t.paths)
    assert [x for x in ha.calls if x["domain"] == "climate"] == []  # kill switch off
    conn = db.open_db(tmp_path / "data" / "thermabot.db")
    s = Settings.model_validate_json(
        conn.execute("SELECT value_json FROM settings WHERE key='settings'").fetchone()[0]
    )
    assert s == HOUSE
    conn.close()


def _controller(tmp: Path, settings: Settings) -> tuple[Controller, MockHA]:
    conn = db.open_db(tmp / "t.db", now=NOW)
    db.write_bot_state(
        conn,
        Persisted(kill_switch="on", run_mode="active", shadow_started_at=NOW),
        by="test",
        now=NOW,
    )
    ha = MockHA()
    rest = HARest(
        "http://ha",
        GOOD_TOKEN,
        transport=httpx.ASGITransport(app=ha.app),
        writable_entity=settings.thermostat.entity_id,
    )
    c = Controller(conn, rest, settings=settings, bot_user_id=BOT_USER_ID, sleep=Sleeps())
    return c, ha


@pytest.mark.brd("NFR-SAFE-5")
async def test_unconfigured_controller_never_writes(tmp_path: Path) -> None:
    c, ha = _controller(tmp_path, Settings())
    assert await c.tick(snap(), STEP, LIVE, NOW) == []  # ACTIVE + plan step: still nothing
    assert await c.tick(snap(mode="off"), STEP, LIVE, NOW) == []  # off: no safety restore
    assert await c.kill_restore(NOW) == "skipped"
    assert ha.calls == []
    assert c.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0


@pytest.mark.brd("NFR-SAFE-5")
async def test_unconfigured_execute_denies_even_a_forced_decision(tmp_path: Path) -> None:
    from thermabot.core.types import Action, Decision

    c, ha = _controller(tmp_path, Settings())
    act = Action(service="set_temperature", entity_id=TSTAT, data={"temperature": 70})
    d = Decision(t=NOW, action=act, allowed=True, reason="forced")
    assert await c.execute(d, NOW) is False
    assert ha.calls == []
    rows = [r[0] for r in c.conn.execute("SELECT outcome FROM writes")]
    assert rows == ["denied"]


@pytest.mark.brd("NFR-SAFE-5")
async def test_configured_controller_still_writes(tmp_path: Path) -> None:
    c, ha = _controller(tmp_path, HOUSE)
    ds = await c.tick(snap(), STEP, LIVE, NOW)
    assert [d.allowed for d in ds] == [True]
    assert {x["data"]["entity_id"] for x in ha.calls} == {TSTAT}
