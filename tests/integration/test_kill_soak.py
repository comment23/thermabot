"""T1.30: kill-switch soak. 24 simulated hours with kill OFF (fake clock), including floor
and ceiling crossings, restart, corrupt state and mirror disagreement -> 0 HA service calls."""

from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from tests.house import HOUSE, TSTAT
from tests.integration.mock_ha import BOT_USER_ID, GOOD_TOKEN, MockHA
from tests.integration.test_controller import NOW, STEP, Sleeps
from thermabot.adapters import db
from thermabot.adapters.ha_rest import HARest
from thermabot.app.controller import Controller
from thermabot.core.statemachine import Live, Persisted
from thermabot.core.types import PlanStep, Snapshot, ThermostatState

TICK = timedelta(seconds=60)
TICKS_24H = 24 * 60


def temp_at(i: int) -> float:
    """Sinusoid 50..90 °F: crosses the floor and the ceiling twice a day."""
    return 70 + 20 * math.sin(2 * math.pi * i / TICKS_24H)


def snap_at(i: int) -> Snapshot:
    t = NOW + i * TICK
    return Snapshot(
        t=t,
        thermostat=ThermostatState(
            hvac_mode="off",
            current_temp=temp_at(i),
            heat_sp=68,
            cool_sp=76,
            fan_mode="auto",
            preset_mode="none",
            last_reported=t,
        ),
    )


def step_at(i: int) -> PlanStep:
    return STEP.model_copy(update={"t": NOW + i * TICK})


def make(tmp: Path, ha: MockHA) -> Controller:
    conn = db.open_db(tmp / "t.db", now=NOW)
    rest = HARest(
        "http://ha", GOOD_TOKEN, transport=httpx.ASGITransport(app=ha.app), writable_entity=TSTAT
    )
    return Controller(conn, rest, settings=HOUSE, bot_user_id=BOT_USER_ID, sleep=Sleeps())


def kill_off(c: Controller, run_mode: str = "active") -> None:
    db.write_bot_state(
        c.conn,
        Persisted(
            kill_switch="off",
            run_mode=run_mode,  # type: ignore[arg-type]
            shadow_started_at=NOW - timedelta(days=30),
            disabled_since=NOW,
        ),
        by="test",
        now=NOW,
    )


async def soak(c: Controller, start: int, n: int, live: Live) -> None:
    for i in range(start, start + n):
        await c.tick(snap_at(i), step_at(i), live, NOW + i * TICK)


@pytest.mark.brd("FR-KILL-2")
@pytest.mark.parametrize("run_mode", ["active", "shadow"])
async def test_24h_kill_off_zero_service_calls(tmp_path: Path, run_mode: str) -> None:
    ha = MockHA()
    c = make(tmp_path, ha)
    kill_off(c, run_mode)
    temps = [temp_at(i) for i in range(TICKS_24H)]
    assert min(temps) < 55 and max(temps) > 85  # floor + ceiling crossed
    await soak(c, 0, TICKS_24H, Live(ha_mirror="off", tstat_age_min=0))
    assert ha.calls == []
    sent = c.conn.execute("SELECT COUNT(*) FROM writes WHERE outcome='sent'").fetchone()[0]
    assert sent == 0


@pytest.mark.brd("FR-KILL-2")
async def test_restart_keeps_kill_off(tmp_path: Path) -> None:
    ha = MockHA()
    c = make(tmp_path, ha)
    kill_off(c)
    await soak(c, 0, 120, Live(ha_mirror="off", tstat_age_min=0))
    c.conn.close()
    c2 = make(tmp_path, ha)  # restart: same DB file, fresh process state
    await soak(c2, 120, 600, Live(ha_mirror="off", tstat_age_min=0))
    assert ha.calls == []


@pytest.mark.brd("FR-KILL-2")
async def test_corrupt_state_means_off(tmp_path: Path) -> None:
    ha = MockHA()
    c = make(tmp_path, ha)
    db.write_bot_state(
        c.conn,
        Persisted(kill_switch="on", run_mode="active", shadow_started_at=NOW - timedelta(days=30)),
        by="test",
        now=NOW,
    )
    c.conn.execute("UPDATE bot_state SET checksum='tampered' WHERE id=1")
    assert db.read_bot_state(c.conn) is None
    await soak(c, 0, 600, Live(ha_mirror="on", tstat_age_min=0))
    assert ha.calls == []


@pytest.mark.brd("FR-KILL-2")
@pytest.mark.parametrize(("kill", "mirror"), [("on", "off"), ("on", None), ("off", "on")])
async def test_mirror_disagreement_means_off(tmp_path: Path, kill: str, mirror: str | None) -> None:
    ha = MockHA()
    c = make(tmp_path, ha)
    db.write_bot_state(
        c.conn,
        Persisted(
            kill_switch=kill,
            run_mode="active",  # type: ignore[arg-type]
            shadow_started_at=NOW - timedelta(days=30),
        ),
        by="test",
        now=NOW,
    )
    await soak(c, 0, 600, Live(ha_mirror=mirror, tstat_age_min=0))  # type: ignore[arg-type]
    assert ha.calls == []
