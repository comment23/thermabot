"""T1.28: controller = single write path (D§1.3, D§6.2-6.3) against the mock HA."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import httpx
import pytest

from tests.house import HOUSE
from tests.integration.mock_ha import BOT_USER_ID, GOOD_TOKEN, MockHA
from thermabot.adapters import db
from thermabot.adapters.ha_rest import HARest, ServiceResult
from thermabot.app import controller as controller_module
from thermabot.app.controller import READBACK_POLL_S, RETRY_AFTER_S, Controller, diff, readback_ok
from thermabot.core.overrides import override_command_key
from thermabot.core.statemachine import Live, Persisted
from thermabot.core.types import Action, PlanStep, Snapshot, ThermostatState

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
            min_temp=45,
            max_temp=95,
            target_temp_step=1,
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
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    rest = HARest(
        "http://ha", token, transport=httpx.ASGITransport(app=ha.app), writable_entity=TSTAT
    )
    sl = Sleeps()
    c = Controller(conn, rest, settings=HOUSE, bot_user_id=BOT_USER_ID, sleep=sl, clock=lambda: NOW)
    return c, ha, sl, conn


def outcomes(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT outcome FROM writes ORDER BY id")]


def queue_override(
    conn: sqlite3.Connection, payload: dict[str, object], kind: str = "ui_target"
) -> int:
    cur = conn.execute(
        "INSERT INTO overrides (kind, source, payload_json, started_at, expires_at)"
        " VALUES (?, 'ui:alex', ?, ?, ?)",
        (kind, json.dumps(payload), NOW.isoformat(), NOW.replace(hour=19).isoformat()),
    )
    assert cur.lastrowid is not None
    override_id = cur.lastrowid
    db.write_kv(conn, override_command_key(override_id), "pending", now=NOW)
    return override_id


@pytest.mark.brd("FR-HA-4")
def test_diff_mode_plus_setpoint_is_one_action() -> None:
    acts = diff(PlanStep(t=NOW, hvac_mode="cool", cool_sp=75), snap(), TSTAT)
    assert len(acts) == 1 and acts[0].service == "set_temperature"
    assert acts[0].data == {"hvac_mode": "cool", "temperature": 75}
    assert diff(PlanStep(t=NOW, hvac_mode="heat_cool", heat_sp=68, cool_sp=76), snap(), TSTAT) == []


@pytest.mark.brd("FR-HA-4")
@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_readback_is_not_confirmed(value: float) -> None:
    action = Action(service="set_temperature", entity_id=TSTAT, data={"temperature": 70})
    assert not readback_ok(action, {"attributes": {"temperature": value}})


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


@pytest.mark.brd("FR-OVR-2")
@pytest.mark.parametrize(
    ("mode", "expected_data", "calls"),
    [
        ("heat_cool", {"target_temp_low": 68, "target_temp_high": 72}, 1),
        ("heat", {"temperature": 70}, 2),
        ("cool", {"temperature": 70}, 2),
    ],
)
async def test_ui_temperature_override_is_sent_and_read_back(
    tmp_path: Path, mode: str, expected_data: dict[str, float], calls: int
) -> None:
    c, ha, _, conn = setup(tmp_path)
    override_id = queue_override(
        conn,
        {
            "target_f": 70,
            "mode": None if mode == "heat_cool" else mode,
            "pause": False,
            "duration": "1h",
            "until": None,
        },
    )

    decisions = await c.tick(snap(mode=mode), STEP, LIVE, NOW)

    assert len(decisions) == 1 and decisions[0].allowed
    assert len(ha.calls) == calls
    assert ha.calls[-1]["data"] == {"entity_id": TSTAT, **expected_data}
    row = conn.execute("SELECT outcome, readback_ok FROM writes").fetchone()
    assert tuple(row) == ("sent", 1)
    status = db.read_kv(conn, override_command_key(override_id))
    assert status is not None and status.startswith("applied:")


@pytest.mark.brd("FR-OVR-2")
async def test_ui_override_respects_thermostat_setpoint_step(tmp_path: Path) -> None:
    from thermabot.app.main import thermostat

    c, ha, _, conn = setup(tmp_path)
    snapshot = Snapshot(t=NOW, thermostat=thermostat(ha.states[TSTAT], NOW))
    override_id = queue_override(conn, {"target_f": 70})

    await c.tick(snapshot, STEP, LIVE, NOW)

    assert ha.calls[-1]["data"] == {
        "entity_id": TSTAT,
        "target_temp_low": 68,
        "target_temp_high": 72,
    }
    status = db.read_kv(conn, override_command_key(override_id))
    assert (
        status is not None and status.startswith("applied:") and "68" in status and "72" in status
    )


@pytest.mark.brd("NFR-SAFE-5")
@pytest.mark.parametrize(
    ("attribute", "value", "reason"),
    [
        ("target_temp_step", None, "thermostat setpoint step unavailable"),
        ("target_temp_step", 0, "thermostat setpoint step unavailable"),
        ("min_temp", None, "thermostat setpoint range unavailable"),
        ("temperature_unit", "°C", "thermostat temperature unit is not Fahrenheit"),
    ],
)
async def test_ui_override_blocks_when_capabilities_are_unsafe(
    tmp_path: Path, attribute: str, value: object, reason: str
) -> None:
    from thermabot.app.main import thermostat

    c, ha, _, conn = setup(tmp_path)
    ha.states[TSTAT]["attributes"][attribute] = value
    snapshot = Snapshot(t=NOW, thermostat=thermostat(ha.states[TSTAT], NOW))
    override_id = queue_override(conn, {"target_f": 70})

    await c.tick(snapshot, STEP, LIVE, NOW)

    assert ha.calls == []
    assert db.read_kv(conn, override_command_key(override_id)) == f"blocked:{reason}"


@pytest.mark.brd("NFR-SAFE-5")
async def test_ui_override_blocks_if_guard_clamp_breaks_device_step(tmp_path: Path) -> None:
    from thermabot.app.main import thermostat

    c, ha, _, conn = setup(tmp_path)
    ha.states[TSTAT]["attributes"]["target_temp_step"] = 2
    snapshot = Snapshot(t=NOW, thermostat=thermostat(ha.states[TSTAT], NOW))
    override_id = queue_override(conn, {"target_f": 60})

    await c.tick(snapshot, STEP, LIVE, NOW)

    assert ha.calls == []
    assert db.read_kv(conn, override_command_key(override_id)) == (
        "blocked:setpoints incompatible with thermostat capabilities"
    )


@pytest.mark.brd("FR-OVR-2")
async def test_single_mode_override_reports_rounded_device_target(tmp_path: Path) -> None:
    from thermabot.app.main import thermostat

    c, ha, _, conn = setup(tmp_path)
    ha.states[TSTAT]["state"] = "heat"
    ha.states[TSTAT]["attributes"]["temperature"] = 68
    snapshot = Snapshot(t=NOW, thermostat=thermostat(ha.states[TSTAT], NOW))
    override_id = queue_override(conn, {"target_f": 70.5})

    await c.tick(snapshot, STEP, LIVE, NOW)

    assert ha.calls[-1]["data"] == {"entity_id": TSTAT, "temperature": 71}
    assert db.read_kv(conn, override_command_key(override_id)) == "applied:target 71F"


@pytest.mark.brd("FR-HA-4")
async def test_ui_override_does_not_confirm_a_different_device_setpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from thermabot.app.main import thermostat

    c, ha, _, conn = setup(tmp_path)
    ha.states[TSTAT]["attributes"]["target_temp_step"] = 0.5
    snapshot = Snapshot(t=NOW, thermostat=thermostat(ha.states[TSTAT], NOW))
    override_id = queue_override(conn, {"target_f": 70})
    call = ha.call

    async def alter_setpoint(
        domain: str, service: str, data: dict[str, Any], user: str = BOT_USER_ID
    ) -> list[dict[str, Any]]:
        result = await call(domain, service, data, user)
        if service == "set_temperature":
            await ha.external_change(target_temp_low=69)
        return result

    monkeypatch.setattr(ha, "call", alter_setpoint)
    await c.tick(snapshot, STEP, LIVE, NOW)

    assert len(ha.calls) == 1
    assert db.read_kv(conn, override_command_key(override_id)) == "failed"


@pytest.mark.brd("FR-KILL-2")
async def test_ui_temperature_override_still_respects_kill_switch(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path, kill="off")
    override_id = queue_override(conn, {"target_f": 70, "duration": "1h"})

    decisions = await c.tick(snap(), STEP, LIVE, NOW)

    assert len(decisions) == 1 and decisions[0].guard == "kill_switch"
    assert ha.calls == []
    assert db.read_kv(conn, override_command_key(override_id)) == "blocked:kill switch off"


@pytest.mark.brd("FR-KILL-2")
async def test_ui_temperature_override_in_shadow_is_blocked(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path, run_mode="shadow")
    override_id = queue_override(conn, {"target_f": 70})

    decisions = await c.tick(snap(), STEP, LIVE, NOW)

    assert len(decisions) == 1 and decisions[0].guard == "shadow"
    assert ha.calls == []
    assert db.read_kv(conn, override_command_key(override_id)) == "blocked:shadow mode"


@pytest.mark.brd("NFR-SAFE-5")
async def test_ui_override_rechecks_capabilities_before_changing_mode(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path)
    ha.states[TSTAT]["attributes"]["target_temp_step"] = 2
    override_id = queue_override(conn, {"target_f": 70, "mode": "heat"}, kind="ui_mode")

    await c.tick(snap(), STEP, LIVE, NOW)

    assert ha.calls == []
    assert db.read_kv(conn, override_command_key(override_id)) == "failed"


@pytest.mark.brd("FR-HA-4")
async def test_failed_ui_override_does_not_retry_until_replaced(tmp_path: Path) -> None:
    c, ha, sl, conn = setup(tmp_path)
    ha.ignore_writes = True
    override_id = queue_override(conn, {"target_f": 70})

    await c.tick(snap(), STEP, LIVE, NOW)
    assert len(ha.calls) == 1 and outcomes(conn) == ["failed"]
    assert db.read_kv(conn, override_command_key(override_id)) == "failed"
    c = Controller(conn, c.ha, settings=HOUSE, bot_user_id=BOT_USER_ID, sleep=sl, clock=lambda: NOW)
    await c.tick(snap(), STEP, LIVE, NOW)
    assert len(ha.calls) == 1 and outcomes(conn) == ["failed"]

    conn.execute("UPDATE overrides SET ended_at=? WHERE ended_at IS NULL", (NOW.isoformat(),))
    queue_override(conn, {"target_f": 70})
    ha.ignore_writes = False
    await c.tick(snap(), STEP, LIVE, NOW)
    assert len(ha.calls) == 2 and outcomes(conn) == ["failed", "sent"]


@pytest.mark.brd("FR-OVR-2")
@pytest.mark.parametrize(("seconds", "allowed"), [(179.999, True), (180, True), (180.001, False)])
async def test_ui_override_pending_timeout_boundary(
    tmp_path: Path, seconds: float, allowed: bool
) -> None:
    c, ha, _, conn = setup(tmp_path)
    override_id = queue_override(conn, {"target_f": 70})
    later = NOW + timedelta(seconds=seconds)
    c._clock = lambda: later

    await c.tick(snap().model_copy(update={"t": later}), STEP, LIVE, later)

    assert len(ha.calls) == int(allowed)
    expected = "applied:heat 68F, cool 72F" if allowed else "blocked:request timed out"
    assert db.read_kv(conn, override_command_key(override_id)) == expected


@pytest.mark.brd("FR-OVR-2")
async def test_ui_override_with_invalid_start_time_is_not_sent(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path)
    override_id = queue_override(conn, {"target_f": 70})
    conn.execute("UPDATE overrides SET started_at='invalid' WHERE id=?", (override_id,))

    await c.tick(snap(), STEP, LIVE, NOW)

    assert ha.calls == []
    assert db.read_kv(conn, override_command_key(override_id)) == "blocked:request timed out"


@pytest.mark.brd("FR-KILL-2")
@pytest.mark.parametrize("interrupt", ["cancel", "kill"])
async def test_ui_override_does_not_retry_after_interruption(
    tmp_path: Path, interrupt: str
) -> None:
    c, ha, _, conn = setup(tmp_path)
    ha.ignore_writes = True
    override_id = queue_override(conn, {"target_f": 70})
    interrupted = False

    async def interrupt_during_readback(delay: float) -> None:
        nonlocal interrupted
        if delay != READBACK_POLL_S or interrupted:
            return
        interrupted = True
        if interrupt == "cancel":
            conn.execute(
                "UPDATE overrides SET ended_at=? WHERE id=?", (NOW.isoformat(), override_id)
            )
        else:
            db.write_bot_state(conn, Persisted(kill_switch="off", run_mode="active"), by="test")

    c._sleep = interrupt_during_readback
    await c.tick(snap(), STEP, LIVE, NOW)

    assert interrupted
    assert len(ha.calls) == 1
    assert db.read_kv(conn, override_command_key(override_id)) == "failed"


@pytest.mark.brd("FR-KILL-2")
@pytest.mark.parametrize(
    "interrupt", ["cancel", "kill", "shadow", "mirror", "expired", "step", "pending-timeout"]
)
async def test_ui_override_stops_before_setpoint_if_interrupted_during_mode_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: str
) -> None:
    c, ha, _, conn = setup(tmp_path)
    override_id = queue_override(conn, {"target_f": 70, "mode": "heat"}, kind="ui_mode")
    call = ha.call
    clock = [NOW]
    c._clock = lambda: clock[0]

    async def interrupt_after_mode(
        domain: str, service: str, data: dict[str, Any], user: str = BOT_USER_ID
    ) -> list[dict[str, Any]]:
        result = await call(domain, service, data, user)
        if service == "set_hvac_mode":
            if interrupt == "cancel":
                conn.execute(
                    "UPDATE overrides SET ended_at=? WHERE id=?", (NOW.isoformat(), override_id)
                )
            elif interrupt == "kill":
                db.write_bot_state(conn, Persisted(kill_switch="off", run_mode="active"), by="test")
            elif interrupt == "shadow":
                db.write_bot_state(conn, Persisted(kill_switch="on", run_mode="shadow"), by="test")
            elif interrupt == "mirror":
                ha.states["input_boolean.thermabot_enabled"]["state"] = "off"
            elif interrupt == "expired":
                clock[0] = NOW + timedelta(hours=1)
            elif interrupt == "pending-timeout":
                clock[0] = NOW + timedelta(minutes=4)
            else:
                ha.states[TSTAT]["attributes"]["target_temp_step"] = 2
        return result

    monkeypatch.setattr(ha, "call", interrupt_after_mode)
    await c.tick(snap(), STEP, LIVE, NOW)

    assert [call["service"] for call in ha.calls] == ["set_hvac_mode"]
    assert db.read_kv(conn, override_command_key(override_id)) == "failed"


@pytest.mark.brd("FR-OVR-3")
async def test_ui_override_does_not_undo_later_manual_changes_after_restart(tmp_path: Path) -> None:
    c, ha, sl, conn = setup(tmp_path)
    queue_override(conn, {"target_f": 70})
    await c.tick(snap(), STEP, LIVE, NOW)
    assert len(ha.calls) == 1

    await ha.external_change(target_temp_low=66, target_temp_high=76)
    later = NOW + timedelta(minutes=11)
    changed = snap(lo=66, hi=76).model_copy(
        update={
            "t": later,
            "thermostat": snap(lo=66, hi=76).thermostat.model_copy(update={"last_reported": later}),
        }
    )
    c = Controller(conn, c.ha, settings=HOUSE, bot_user_id=BOT_USER_ID, sleep=sl, clock=lambda: NOW)
    await c.tick(changed, STEP, LIVE, later)
    assert len(ha.calls) == 1


@pytest.mark.brd("FR-OVR-3")
async def test_matching_ui_override_does_not_reassert_later(tmp_path: Path) -> None:
    c, ha, sl, conn = setup(tmp_path)
    override_id = queue_override(conn, {"target_f": 70})
    await ha.external_change(target_temp_low=68, target_temp_high=72)
    await c.tick(snap(lo=68, hi=72), STEP, LIVE, NOW)
    assert db.read_kv(conn, override_command_key(override_id)) == "matched:heat 68F, cool 72F"

    await ha.external_change(target_temp_low=66, target_temp_high=76)
    later = NOW + timedelta(minutes=11)
    changed = snap(lo=66, hi=76).model_copy(
        update={
            "t": later,
            "thermostat": snap(lo=66, hi=76).thermostat.model_copy(update={"last_reported": later}),
        }
    )
    c = Controller(conn, c.ha, settings=HOUSE, bot_user_id=BOT_USER_ID, sleep=sl, clock=lambda: NOW)
    await c.tick(changed, STEP, LIVE, later)
    assert ha.calls == []


@pytest.mark.brd("FR-OVR-2")
@pytest.mark.parametrize("mode", ["heat", "cool"])
async def test_single_mode_matching_target_does_not_send(tmp_path: Path, mode: str) -> None:
    from thermabot.app.main import thermostat

    c, ha, _, conn = setup(tmp_path)
    ha.states[TSTAT]["state"] = mode
    ha.states[TSTAT]["attributes"]["temperature"] = 70
    snapshot = Snapshot(t=NOW, thermostat=thermostat(ha.states[TSTAT], NOW))
    override_id = queue_override(conn, {"target_f": 70})

    await c.tick(snapshot, STEP, LIVE, NOW)

    assert ha.calls == []
    assert db.read_kv(conn, override_command_key(override_id)) == "matched:target 70F"


@pytest.mark.brd("FR-OVR-2")
async def test_matching_rounded_override_reports_actual_setpoint(tmp_path: Path) -> None:
    from thermabot.app.main import thermostat

    c, ha, _, conn = setup(tmp_path)
    ha.states[TSTAT]["state"] = "heat"
    ha.states[TSTAT]["attributes"]["temperature"] = 70
    snapshot = Snapshot(t=NOW, thermostat=thermostat(ha.states[TSTAT], NOW))
    override_id = queue_override(conn, {"target_f": 70.4})

    await c.tick(snapshot, STEP, LIVE, NOW)

    assert ha.calls == []
    assert db.read_kv(conn, override_command_key(override_id)) == "matched:target 70F"


@pytest.mark.brd("NFR-SAFE-9")
@pytest.mark.parametrize("state", [None, "sending"])
async def test_unconfirmed_or_legacy_override_is_not_replayed(
    tmp_path: Path, state: str | None
) -> None:
    c, ha, _, conn = setup(tmp_path)
    if state is None:
        conn.execute(
            "INSERT INTO overrides (kind, source, payload_json, started_at, expires_at)"
            " VALUES ('ui_target', 'ui:alex', ?, ?, ?)",
            (json.dumps({"target_f": 70}), NOW.isoformat(), NOW.replace(hour=19).isoformat()),
        )
    else:
        override_id = queue_override(conn, {"target_f": 70})
        db.write_kv(conn, override_command_key(override_id), state, now=NOW)

    assert await c.tick(snap(), STEP, LIVE, NOW) == []
    assert ha.calls == []


@pytest.mark.brd("NFR-SAFE-9")
def test_concurrent_controllers_claim_override_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, ha, sl, conn = setup(tmp_path)
    override_id = queue_override(conn, {"target_f": 70})
    conn2 = db.open_db(tmp_path / "t.db", now=NOW)
    rest2 = HARest(
        "http://ha", GOOD_TOKEN, transport=httpx.ASGITransport(app=ha.app), writable_entity=TSTAT
    )
    second = Controller(
        conn2, rest2, settings=HOUSE, bot_user_id=BOT_USER_ID, sleep=sl, clock=lambda: NOW
    )
    ready = Barrier(2)
    get_actions = Controller._override_actions
    send = controller_module._call_service

    def synchronized_actions(
        controller: Controller, snapshot: Snapshot, now: datetime
    ) -> tuple[int, list[Action] | None] | None:
        result = get_actions(controller, snapshot, now)
        ready.wait(timeout=5)
        return result

    async def delayed_send(rest: HARest, action: Action) -> ServiceResult:
        await asyncio.sleep(0.05)
        return await send(rest, action)

    monkeypatch.setattr(Controller, "_override_actions", synchronized_actions)
    monkeypatch.setattr(controller_module, "_call_service", delayed_send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(asyncio.run, controller.tick(snap(), STEP, LIVE, NOW))
            for controller in (first, second)
        ]
        for future in futures:
            future.result(timeout=10)

    assert len(ha.calls) == 1
    assert db.read_kv(conn, override_command_key(override_id)) == "applied:heat 68F, cool 72F"
    assert sorted(outcomes(conn)) == ["denied", "sent"]
    conn2.close()


@pytest.mark.brd("FR-HA-4")
async def test_override_does_not_send_setpoint_until_mode_confirmed(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path)
    ha.ignore_writes = True
    queue_override(conn, {"target_f": 70, "mode": "heat"}, kind="ui_mode")

    await c.tick(snap(), STEP, LIVE, NOW)

    assert [call["service"] for call in ha.calls] == ["set_hvac_mode"]
    assert outcomes(conn) == ["failed"]


@pytest.mark.brd("NFR-SAFE-3")
async def test_safety_restore_precedes_invalid_override_payload(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path)
    queue_override(conn, {"mode": "invalid"})
    cold = snap("off").model_copy(
        update={"thermostat": snap("off").thermostat.model_copy(update={"current_temp": 58})}
    )

    decisions = await c.tick(cold, STEP, LIVE, NOW)

    assert decisions[0].allowed and decisions[0].action.safety_restore
    assert len(ha.calls) == 1


@pytest.mark.brd("NFR-SAFE-9")
@pytest.mark.parametrize("payload", ['{"mode": "invalid"}', "{", "{}"])
async def test_invalid_pending_override_is_blocked_without_repeated_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, payload: str
) -> None:
    c, ha, _, conn = setup(tmp_path)
    override_id = queue_override(conn, {"target_f": 70})
    conn.execute("UPDATE overrides SET payload_json=? WHERE id=?", (payload, override_id))

    await c.tick(snap(), STEP, LIVE, NOW)
    await c.tick(snap(), STEP, LIVE, NOW)

    assert ha.calls == []
    assert db.read_kv(conn, override_command_key(override_id)) == "blocked:invalid override request"
    assert caplog.text.count("invalid override payload") == 1


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


@pytest.mark.brd("FR-KILL-2")
@pytest.mark.parametrize("interrupt", ["kill", "shadow", "mirror", "override"])
async def test_planned_write_does_not_retry_after_interruption(
    tmp_path: Path, interrupt: str
) -> None:
    c, ha, _, conn = setup(tmp_path)
    ha.ignore_writes = True

    async def interrupt_before_retry(delay: float) -> None:
        if delay != RETRY_AFTER_S:
            return
        if interrupt == "kill":
            db.write_bot_state(conn, Persisted(kill_switch="off", run_mode="active"), by="test")
        elif interrupt == "shadow":
            db.write_bot_state(conn, Persisted(kill_switch="on", run_mode="shadow"), by="test")
        elif interrupt == "mirror":
            ha.states["input_boolean.thermabot_enabled"]["state"] = "off"
        else:
            queue_override(conn, {"pause": True}, kind="ui_pause")

    c._sleep = interrupt_before_retry
    await c.tick(snap(), STEP, LIVE, NOW)

    assert len(ha.calls) == 1
    assert outcomes(conn) == ["failed"]


@pytest.mark.brd("FR-KILL-2")
async def test_planned_write_stops_after_kill_during_mode_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, ha, _, conn = setup(tmp_path)
    call = ha.call

    async def turn_off_after_mode(
        domain: str, service: str, data: dict[str, Any], user: str = BOT_USER_ID
    ) -> list[dict[str, Any]]:
        result = await call(domain, service, data, user)
        if service == "set_hvac_mode":
            db.write_bot_state(conn, Persisted(kill_switch="off", run_mode="active"), by="test")
        return result

    monkeypatch.setattr(ha, "call", turn_off_after_mode)
    step = PlanStep(t=NOW, hvac_mode="heat", heat_sp=70)
    await c.tick(snap(), step, LIVE, NOW)

    assert [call["service"] for call in ha.calls] == ["set_hvac_mode"]
    assert outcomes(conn) == ["failed"]


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
