"""T1.28: controller = single write path (D§1.3, D§6.2-6.3) against the mock HA."""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tests.house import HOUSE
from tests.integration.mock_ha import BOT_USER_ID, GOOD_TOKEN, MockHA
from thermabot.adapters import db
from thermabot.adapters.ha_rest import HARest
from thermabot.app.controller import RETRY_AFTER_S, Controller, diff
from thermabot.core.statemachine import Live, Persisted
from thermabot.core.types import PlanStep, Snapshot, ThermostatState

TSTAT = "climate.living_room"
NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)
LIVE = Live(ha_mirror="on", tstat_age_min=0)
STEP = PlanStep(t=NOW, hvac_mode="heat_cool", heat_sp=67, cool_sp=75)


def snap(mode: str = "heat_cool", lo: float = 68, hi: float = 76) -> Snapshot:
    return Snapshot(
        t=NOW,
        thermostat=ThermostatState(
            hvac_mode=mode,
            current_temp=71,
            heat_sp=lo,
            cool_sp=hi,
            fan_mode="auto",
            preset_mode="none",
            last_reported=NOW,
        ),
    )


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def setup(
    tmp: Path, run_mode: str = "active", kill: str = "on", token: str = GOOD_TOKEN
) -> tuple[Controller, MockHA, Sleeps, sqlite3.Connection]:
    conn = db.open_db(tmp / "t.db", now=NOW)
    db.write_bot_state(
        conn,
        Persisted(
            kill_switch=kill,
            run_mode=run_mode,  # type: ignore[arg-type]
            shadow_started_at=NOW,
        ),
        by="test",
        now=NOW,
    )
    ha = MockHA()
    rest = HARest(
        "http://ha", token, transport=httpx.ASGITransport(app=ha.app), writable_entity=TSTAT
    )
    sl = Sleeps()
    c = Controller(conn, rest, settings=HOUSE, bot_user_id=BOT_USER_ID, sleep=sl)
    return c, ha, sl, conn


def outcomes(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT outcome FROM writes ORDER BY id")]


@pytest.mark.brd("FR-HA-4")
def test_diff_mode_plus_setpoint_is_one_action() -> None:
    acts = diff(PlanStep(t=NOW, hvac_mode="cool", cool_sp=75), snap(), TSTAT)
    assert len(acts) == 1 and acts[0].service == "set_temperature"
    assert acts[0].data == {"hvac_mode": "cool", "temperature": 75}
    assert diff(PlanStep(t=NOW, hvac_mode="heat_cool", heat_sp=68, cool_sp=76), snap(), TSTAT) == []


@pytest.mark.brd("FR-HA-4")
async def test_active_write_sent_and_read_back(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path)
    ds = await c.tick(snap(), STEP, LIVE, NOW)
    assert [d.allowed for d in ds] == [True] and len(ha.calls) == 1
    assert ha.calls[0]["data"] == {
        "entity_id": TSTAT,
        "target_temp_low": 67,
        "target_temp_high": 75,
    }  # mode already heat_cool -> setpoints only (T1.44 split)
    row = conn.execute("SELECT outcome, readback_ok FROM writes").fetchone()
    assert tuple(row) == ("sent", 1)


@pytest.mark.brd("FR-HA-4")
async def test_readback_mismatch_retry_then_write_failed(tmp_path: Path) -> None:
    c, ha, sl, conn = setup(tmp_path)
    ha.ignore_writes = True
    await c.tick(snap(), STEP, LIVE, NOW)
    assert len(ha.calls) == 2 and RETRY_AFTER_S in sl.calls
    assert outcomes(conn) == ["failed"]
    assert [a.key for a in c.alerts] == ["write_failed"]
    await c.tick(snap(), STEP, LIVE, NOW)  # same plan step: no retry loop
    assert len(ha.calls) == 2


@pytest.mark.brd("NFR-SAFE-5")
async def test_adapter_error_no_write(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path, token="bad-token-xxxxxxxxxxxxxxxxxxxx")
    step = PlanStep(t=NOW, hvac_mode="heat_cool", heat_sp=67, cool_sp=75, fan_mode="on")
    await c.tick(snap(), step, LIVE, NOW)
    assert outcomes(conn) == ["failed"]  # fan write not attempted after failure
    assert ha.states[TSTAT]["attributes"]["target_temp_low"] == 68


@pytest.mark.brd("FR-OVR-3")
async def test_external_change_opens_override(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path)
    old = copy.deepcopy(ha.states[TSTAT])
    await ha.external_change(hvac_mode="heat", temperature=72)
    assert c.on_thermostat_change(old, ha.states[TSTAT], NOW) == "external"
    ds = await c.tick(snap(), STEP, LIVE, NOW)
    assert ds[0].guard == "override" and ha.calls == []
    assert outcomes(conn) == ["denied"]


@pytest.mark.brd("FR-OVR-3")
async def test_own_write_not_external(tmp_path: Path) -> None:
    c, ha, _, _ = setup(tmp_path)
    old = copy.deepcopy(ha.states[TSTAT])
    await c.tick(snap(), STEP, LIVE, NOW)
    assert c.on_thermostat_change(old, ha.states[TSTAT], NOW) == "bot"
    assert not c.override_open(NOW)


@pytest.mark.brd("FR-KILL-2")
async def test_kill_off_and_shadow_never_write(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path, kill="off")
    cold = snap().model_copy(
        update={
            "thermostat": snap().thermostat.model_copy(
                update={"current_temp": 58, "hvac_mode": "off"}
            )
        }
    )
    await c.tick(cold, STEP, LIVE, NOW)  # safety restore wanted, kill switch denies
    assert ha.calls == [] and outcomes(conn) == ["denied"]
    sub = tmp_path / "s"
    sub.mkdir()
    c2, ha2, _, conn2 = setup(sub, run_mode="shadow")
    await c2.tick(snap(), STEP, LIVE, NOW)
    assert ha2.calls == [] and outcomes(conn2) == ["shadow"]


@pytest.mark.brd("FR-KILL-4")
async def test_corrupt_bot_state_no_write(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path)
    conn.execute("UPDATE bot_state SET run_mode='shadow'")  # checksum now wrong
    ds = await c.tick(snap(), STEP, LIVE, NOW)
    assert ds[0].guard == "kill_switch" and ha.calls == []


@pytest.mark.brd("NFR-SAFE-3")
async def test_safety_restore_bypasses_rate_limit(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path)
    conn.execute(
        "INSERT INTO writes (ts, service, entity_id, data_json, reason, outcome)"
        " VALUES (?,?,?,?,?,?)",
        (NOW.isoformat(), "set_temperature", TSTAT, json.dumps({"temperature": 70}), "x", "sent"),
    )
    cold = snap("off").model_copy(
        update={"thermostat": snap("off").thermostat.model_copy(update={"current_temp": 58})}
    )
    ds = await c.tick(cold, STEP, LIVE, NOW)
    assert ds[0].allowed and ds[0].action.safety_restore and len(ha.calls) == 1
    assert c.hold_until is not None and c.hold_until > NOW


@pytest.mark.brd("NFR-SAFE-3")
async def test_safety_restore_from_off_survives_trane_quirk(tmp_path: Path) -> None:
    """T1.44: the Trane ignores hvac_mode in set_temperature when leaving OFF. The controller
    sends set_hvac_mode first, then the setpoints, and the read-back confirms heat_cool."""
    c, ha, _, conn = setup(tmp_path)
    ha.trane_off_quirk = True
    ha.states[TSTAT]["state"] = "off"
    cold = snap("off").model_copy(
        update={"thermostat": snap("off").thermostat.model_copy(update={"current_temp": 58})}
    )
    ds = await c.tick(cold, STEP, LIVE, NOW)
    assert ds[0].allowed and ds[0].action.safety_restore
    cc = [x for x in ha.calls if x["domain"] == "climate"]
    assert [x["service"] for x in cc] == ["set_hvac_mode", "set_temperature"]
    assert cc[0]["data"]["hvac_mode"] == "heat_cool" and "hvac_mode" not in cc[1]["data"]
    assert ha.states[TSTAT]["state"] == "heat_cool"
    assert outcomes(conn) == ["sent"]


@pytest.mark.brd("FR-HA-4")
async def test_mode_already_matching_sends_setpoints_only(tmp_path: Path) -> None:
    c, ha, _, _ = setup(tmp_path)
    ds = await c.tick(snap(lo=69, hi=77), STEP, LIVE, NOW)
    assert ds[0].allowed
    cc = [x for x in ha.calls if x["domain"] == "climate"]
    assert [x["service"] for x in cc] == ["set_temperature"]
    assert "hvac_mode" not in cc[0]["data"]
