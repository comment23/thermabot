"""T1.48 crash tests (BRD §8.1 Crash, NFR-REL-2). A "kill" = abandon the runtime with no
lifespan shutdown, no aclose and no close of the SQLite connection (like `docker kill`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tests.house import HOUSE, seed_house
from tests.integration.mock_ha import BOT_USER_ID, GOOD_TOKEN, MockHA
from tests.integration.test_controller import TSTAT, Sleeps
from thermabot.adapters import db
from thermabot.adapters.ha_rest import HARest
from thermabot.app.controller import Controller
from thermabot.app.main import Runtime, build, load_env
from thermabot.core.planner import hold_setpoints
from thermabot.core.statemachine import Live, Persisted
from thermabot.core.types import PlanStep, Snapshot, ThermostatState

NOW = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)  # 18:00 local, before the sleep block


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def runtime(tmp: Path, ha: MockHA, clk: Clock) -> Runtime:
    env = load_env(
        {
            "HEARTBEAT_WEBHOOK_URL": "http://ha/api/webhook/hb-test",
            "HA_URL": "http://ha",
            "HA_TOKEN": GOOD_TOKEN,
            "DATA_DIR": str(tmp),
        }
    )
    seed_house(tmp)
    return build(env, clock=clk, ha_transport=httpx.ASGITransport(app=ha.app))


def mirror_on(ha: MockHA) -> MockHA:
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    return ha


def kill_on(conn: object, run_mode: str = "active") -> None:
    db.write_bot_state(
        conn,
        Persisted(  # type: ignore[arg-type]
            kill_switch="on",
            run_mode=run_mode,  # type: ignore[arg-type]
            shadow_started_at=NOW - timedelta(days=30),
        ),
        by="test",
        now=NOW,
    )


@pytest.mark.brd("NFR-REL-2")
@pytest.mark.parametrize(
    "season,block", [("heat", "sleep"), ("cool", "sleep"), ("heat", "home"), ("cool", "away")]
)
async def test_killed_mid_precondition_leaves_livable_setpoint(
    tmp_path: Path, season: str, block: str
) -> None:
    """The precondition write targets the next band (≤ 2 °F beyond its edge, NFR-SAFE-4); if
    the process dies right after it, the thermostat keeps that setpoint -> still livable."""
    s = HOUSE
    band = getattr(getattr(s.bands, season), block)
    lo, hi = hold_setpoints(band, s.writes.deadband_f)
    conn = db.open_db(tmp_path / "t.db", now=NOW)
    kill_on(conn)
    ha = MockHA()
    rest = HARest(
        "http://ha", GOOD_TOKEN, transport=httpx.ASGITransport(app=ha.app), writable_entity=TSTAT
    )
    c = Controller(conn, rest, settings=s, bot_user_id=BOT_USER_ID, sleep=Sleeps())
    th = ThermostatState(
        hvac_mode="heat_cool",
        current_temp=71,
        heat_sp=68,
        cool_sp=76,
        fan_mode="auto",
        preset_mode="none",
        last_reported=NOW,
    )
    step = PlanStep(t=NOW, hvac_mode="heat_cool", heat_sp=lo, cool_sp=hi)
    await c.tick(Snapshot(t=NOW, thermostat=th), step, Live(ha_mirror="on", tstat_age_min=0), NOW)
    # --- killed here: no further ticks, no restore, no shutdown ---
    a = ha.states[TSTAT]["attributes"]
    assert ha.states[TSTAT]["state"] == "heat_cool"
    assert band[0] - 2 <= a["target_temp_low"] <= band[1] + 2
    assert band[0] - 2 <= a["target_temp_high"] <= band[1] + 2
    assert 45 <= a["target_temp_low"] < a["target_temp_high"] <= 80
    assert len(ha.calls) == 1 and ha.calls[0]["domain"] == "climate"


@pytest.mark.brd("NFR-REL-2")
async def test_killed_while_off_heartbeat_stops(tmp_path: Path) -> None:
    """System `off`, process killed: no further heartbeats and no 'stopped' marker, so the
    HA watchdog sees it go stale (watchdog behaviour itself: T1.52)."""
    ha, clk = MockHA(), Clock(NOW)
    ha.states[TSTAT]["state"] = "off"
    rt = runtime(tmp_path, ha, clk)
    for _ in range(3):
        await rt.loops.run_once()
        clk.t += timedelta(seconds=60)
    beats = [p["body"] for p in ha.webhook_posts]
    assert len(beats) == 3
    # --- killed: the loop task is gone; time passes ---
    clk.t += timedelta(minutes=16)
    assert len(ha.webhook_posts) == 3
    assert all(p["state"] != "stopped" for p in beats)
    last = datetime.fromisoformat(beats[-1]["ts"])
    assert clk.t - last > timedelta(minutes=15)
    assert [x for x in ha.calls if x["domain"] == "climate"] == []


@pytest.mark.brd("NFR-REL-2")
@pytest.mark.brd("FR-MODE-1")
async def test_restart_restores_persisted_state(tmp_path: Path) -> None:
    """Host reboot / docker kill: bot_state, override and summary slot survive (WAL DB)."""
    ha, clk = mirror_on(MockHA()), Clock(NOW)
    rt = runtime(tmp_path, ha, clk)
    kill_on(rt.conn, run_mode="shadow")
    rt.conn.execute(
        "INSERT INTO overrides (kind, source, payload_json, started_at, expires_at)"
        " VALUES (?,?,?,?,?)",
        ("ui_pause", "alex", "{}", NOW.isoformat(), (NOW + timedelta(hours=2)).isoformat()),
    )
    rt.loops.save_slot(NOW - timedelta(hours=1))  # type: ignore[misc]
    await rt.loops.run_once()
    assert rt.ctx.eff().base == "SHADOW"
    # --- killed: no aclose, no conn.close ---
    clk.t += timedelta(minutes=5)
    rt2 = runtime(tmp_path, mirror_on(MockHA()), clk)
    p = db.read_bot_state(rt2.conn)
    assert p is not None and (p.kill_switch, p.run_mode) == ("on", "shadow")
    assert rt2.ctx.open_override() is not None
    assert rt2.loops.load_slot() == NOW - timedelta(hours=1)  # type: ignore[misc]
    await rt2.loops.run_once()
    assert rt2.ctx.eff().base == "SHADOW"
    assert rt2.loops.last_summary_slot == NOW - timedelta(hours=1)
    await rt2.aclose()
