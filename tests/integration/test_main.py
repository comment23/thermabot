"""App entry point: env fail-fast, lifespan loops, heartbeat, 0 climate calls, summary kv."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.house import TSTAT, seed_house
from tests.integration.mock_ha import GOOD_TOKEN, MockHA
from thermabot.adapters import db
from thermabot.app.main import ConfigError, build, create_app, load_env
from thermabot.app.routes_ui import SUMMARY_KEY
from thermabot.core.statemachine import Persisted

NOW = datetime(2026, 10, 4, 22, 0, tzinfo=UTC)


def env(tmp: Path, **extra: str) -> dict[str, str]:
    return {
        "HEARTBEAT_WEBHOOK_URL": "http://ha/api/webhook/hb-test",
        "HA_URL": "http://ha",
        "HA_TOKEN": GOOD_TOKEN,
        "DATA_DIR": str(tmp / "data"),
        **extra,
    }


@pytest.mark.brd("NFR-REL-1")
def test_missing_env_fails_fast_and_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="HA_TOKEN"):
        load_env({"DATA_DIR": str(tmp_path / "data")})
    with pytest.raises(ConfigError, match="non-empty"):
        load_env({"HA_TOKEN": " ", "DATA_DIR": str(tmp_path / "data")})
    assert not (tmp_path / "data").exists()


@pytest.mark.brd("NFR-SAFE-8")
def test_app_starts_kill_off_heartbeat_no_climate_calls(tmp_path: Path) -> None:
    ha = MockHA()
    app = create_app(load_env(env(tmp_path)), ha_transport=httpx.ASGITransport(app=ha.app))
    with TestClient(app) as c:
        deadline = time.monotonic() + 20
        r = c.get("/healthz")
        # The first probe can land before the startup replan finishes. That is
        # "starting", not a stale 503. Wait until the first tick reports DISABLED.
        while r.json().get("state") == "starting" and time.monotonic() < deadline:
            assert r.status_code == 200, r.text
            time.sleep(0.05)
            r = c.get("/healthz")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "DISABLED"
    beats = [p["body"] for p in ha.webhook_posts]
    assert beats and beats[0]["state"] == "DISABLED"
    assert beats[-1]["state"] == "stopped"  # lifespan shutdown
    assert ha.state_posts == []  # no /api/states publish (token stays non-admin)
    assert {p["auth"] for p in ha.webhook_posts} == {None}  # webhook POST carries no auth
    assert [x for x in ha.calls if x["domain"] == "climate"] == []


@pytest.mark.brd("FR-MODE-2")
@pytest.mark.parametrize(
    ("kill", "mode", "expected_mode"),
    [("on", "active", "shadow"), ("on", "shadow", "shadow"), ("off", "active", "active")],
)
def test_restart_never_resumes_control(
    tmp_path: Path, kill: str, mode: str, expected_mode: str
) -> None:
    data = tmp_path / "data"
    seed_house(data)
    conn = db.open_db(data / "thermabot.db", now=NOW)
    db.write_bot_state(
        conn,
        Persisted.model_validate(
            {
                "kill_switch": kill,
                "run_mode": mode,
                "shadow_started_at": NOW - timedelta(days=8),
            }
        ),
        by="test",
        now=NOW,
    )
    conn.close()
    ha = MockHA()
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    ha.states[TSTAT]["state"] = "off"
    ha.states[TSTAT]["attributes"]["current_temperature"] = 50
    app = create_app(
        load_env(env(tmp_path)), clock=lambda: NOW, ha_transport=httpx.ASGITransport(app=ha.app)
    )
    with TestClient(app):
        state = db.read_bot_state(app.state.runtime.conn)
        assert state is not None
        assert state.kill_switch == kill and state.run_mode == expected_mode
        notice = db.read_kv(app.state.runtime.conn, "restart_shadow_notice")
        if kill == "on" and mode == "active":
            assert notice and "Watch" in notice
            assert state.shadow_started_at == NOW
        else:
            assert notice is None
            assert state.shadow_started_at == NOW - timedelta(days=8)
        deadline = time.monotonic() + 20
        while app.state.runtime.ctx.last_tick is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert app.state.runtime.ctx.last_tick is not None
        assert not [call for call in ha.calls if call["domain"] == "climate"]


@pytest.mark.brd("FR-MODE-2")
def test_restart_downgrade_and_notice_are_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    seed_house(data)
    conn = db.open_db(data / "thermabot.db", now=NOW)
    db.write_bot_state(
        conn,
        Persisted(kill_switch="on", run_mode="active", shadow_started_at=NOW - timedelta(days=8)),
        by="test",
        now=NOW,
    )
    conn.close()
    app = create_app(
        load_env(env(tmp_path)),
        clock=lambda: NOW,
        ha_transport=httpx.ASGITransport(app=MockHA().app),
    )
    original = db.write_kv

    def fail_notice(
        conn: sqlite3.Connection, key: str, value: str, now: datetime | None = None
    ) -> None:
        if key == "restart_shadow_notice":
            raise sqlite3.OperationalError("simulated notice failure")
        original(conn, key, value, now)

    monkeypatch.setattr(db, "write_kv", fail_notice)
    try:
        with pytest.raises(sqlite3.OperationalError, match="simulated notice failure"):
            with TestClient(app):
                pass
        state = db.read_bot_state(app.state.runtime.conn)
        assert state is not None and state.run_mode == "active"
        assert db.read_kv(app.state.runtime.conn, "restart_shadow_notice") is None
    finally:
        app.state.runtime.conn.close()


@pytest.mark.brd("FR-ALR-2")
async def test_summary_send_writes_summary_latest(tmp_path: Path) -> None:
    hook = httpx.MockTransport(lambda req: httpx.Response(200))
    rt = build(
        load_env(env(tmp_path, ALERT_WEBHOOK_URL="http://hook/x")),
        clock=lambda: NOW,
        ha_transport=httpx.ASGITransport(app=MockHA().app),
        notify_transport=hook,
    )
    assert await rt.send_summary("Last week\n  hello", NOW) == "ok"
    assert db.read_kv(rt.conn, SUMMARY_KEY) == "Last week\n  hello"
    await rt.aclose()


@pytest.mark.brd("FR-ALR-2")
async def test_summary_not_written_when_delivery_fails(tmp_path: Path) -> None:
    rt = build(
        load_env(env(tmp_path)),
        clock=lambda: NOW,
        ha_transport=httpx.ASGITransport(app=MockHA().app),
    )  # no webhook url
    assert (await rt.send_summary("x", NOW)).startswith("failed")
    assert db.read_kv(rt.conn, SUMMARY_KEY) is None
    await rt.aclose()


@pytest.mark.brd("FR-ALR-2")
async def test_summary_job_renders_and_sends(tmp_path: Path) -> None:
    seen: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(req.content))
        return httpx.Response(200)

    rt = build(
        load_env(env(tmp_path, ALERT_WEBHOOK_URL="http://hook/x")),
        clock=lambda: NOW,
        ha_transport=httpx.ASGITransport(app=MockHA().app),
        notify_transport=httpx.MockTransport(handler),
    )
    await rt.summary(NOW)
    for t in list(rt.pending):
        await t
    assert seen and seen[0]["kind"] == "weekly_summary"
    assert "Last week" in str(db.read_kv(rt.conn, SUMMARY_KEY))
    await rt.aclose()
