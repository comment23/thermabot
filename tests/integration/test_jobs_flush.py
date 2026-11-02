"""Influx flush job (D§8.2, NFR-REL-2): buffered write-back, bounded, never affects control."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from thermabot.adapters import db
from thermabot.adapters.influx import Influx
from thermabot.app.jobs import Background
from thermabot.app.loops import HEARTBEAT_ENTITY, Jobs, Loops

T0 = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)


def influx(handler: httpx.MockTransport, write_tok: str | None = "wtok") -> Influx:
    return Influx("http://influx", "home", "ha_hvac", "thermabot", "rtok", write_tok, handler)


def bg(tmp: Path, ix: Influx) -> Background:
    return Background(conn=db.open_db(tmp / "t.db", now=T0), influx=ix, state_fn=lambda: "SHADOW")


def _write_row(conn: object, ts: datetime, outcome: str = "shadow") -> None:
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO writes (ts, service, entity_id, data_json, reason, outcome)"
        " VALUES (?,?,?,?,?,?)",
        (ts.isoformat(), "set_temperature", "climate.x", "{}", "plan", outcome),
    )


@pytest.mark.brd("NFR-REL-2")
async def test_flush_writes_state_and_new_write_rows_once(tmp_path: Path) -> None:
    bodies: list[bytes] = []

    def h(req: httpx.Request) -> httpx.Response:
        bodies.append(req.content)
        return httpx.Response(204)

    b = bg(tmp_path, influx(httpx.MockTransport(h)))
    _write_row(b.conn, T0 - timedelta(hours=1))  # pre-start history: not replayed
    b.score_fn = lambda: 91.5
    await b.flush(T0)
    assert b'thermabot_state state="SHADOW",score=91.5' in bodies[-1]
    assert b"thermabot_write" not in bodies[-1]
    _write_row(b.conn, T0 + timedelta(seconds=30))
    await b.flush(T0 + timedelta(minutes=1))
    assert bodies[-1].count(b"thermabot_write,outcome=shadow,service=set_temperature") == 1
    await b.flush(T0 + timedelta(minutes=2))
    assert b"thermabot_write" not in bodies[-1]
    await b.influx.aclose()


@pytest.mark.brd("NFR-REL-2")
async def test_flush_noop_without_write_token(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    b = bg(
        tmp_path,
        influx(httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(204)), None),
    )
    await b.flush(T0)
    assert calls == [] and not b.influx.buffer
    await b.influx.aclose()


@pytest.mark.brd("NFR-REL-2")
async def test_buffer_bounded_drop_oldest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from thermabot.adapters import influx as mod

    monkeypatch.setattr(mod, "BUFFER_MAX_LINES", 3)
    ix = influx(httpx.MockTransport(lambda r: httpx.Response(503)))
    ix.enqueue([f"m v={i}i {i}" for i in range(5)], now=T0)
    assert [ln for _, ln in ix.buffer] == ["m v=2i 2", "m v=3i 3", "m v=4i 4"]
    assert await ix.flush(now=T0) is False and len(ix.buffer) == 3
    await ix.aclose()


@pytest.mark.brd("NFR-REL-2")
async def test_flush_failure_or_hang_never_stops_tick_or_heartbeat(tmp_path: Path) -> None:
    """Influx down (raises) or hung: control tick and heartbeat run every iteration."""
    from thermabot.app import loops as loops_mod

    ticks: list[datetime] = []
    posts: list[str] = []

    async def tick(now: datetime) -> None:
        ticks.append(now)

    async def boom(_: datetime) -> None:
        raise RuntimeError("influx exploded")

    async def hang(_: datetime) -> None:
        await asyncio.sleep(3600)

    async def publish(eid: str, st: str, attrs: dict[str, object]) -> None:
        posts.append(eid)

    t = [T0]
    lp = Loops(
        publish=publish,
        state_fn=lambda: "SHADOW",
        jobs=Jobs(tick=tick, flush=boom),
        clock=lambda: t[0],
    )
    for _ in range(3):
        await lp.run_once()
        t[0] += timedelta(minutes=1)
    old = loops_mod.JOB_TIMEOUT_S["flush"]
    loops_mod.JOB_TIMEOUT_S["flush"] = 0.05
    try:
        lp.jobs.flush = hang
        await lp.run_once()
    finally:
        loops_mod.JOB_TIMEOUT_S["flush"] = old
    assert len(ticks) == 4 and posts.count(HEARTBEAT_ENTITY) == 4


@pytest.mark.brd("NFR-REL-2")
async def test_runtime_wires_flush_job(tmp_path: Path) -> None:
    from tests.integration.mock_ha import GOOD_TOKEN, MockHA
    from thermabot.app.main import build, load_env

    env = load_env({"HA_URL": "http://ha", "HA_TOKEN": GOOD_TOKEN, "DATA_DIR": str(tmp_path)})
    rt = build(env, clock=lambda: T0, ha_transport=httpx.ASGITransport(app=MockHA().app))
    assert rt.loops.jobs.flush == rt.bg.flush
    await rt.aclose()
