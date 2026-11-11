from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import local
from tests.house import house_settings
from tests.integration.mock_ha import BOT_USER_ID, MockHA
from tests.integration.test_controller import LIVE, TSTAT, outcomes, queue_override, setup
from tests.integration.test_jobs import NOW, Clock, rt_of
from thermabot.adapters import db
from thermabot.app import jobs
from thermabot.app.main import thermostat
from thermabot.app.routes_api import _kv_put
from thermabot.config import Settings
from thermabot.core.statemachine import Persisted
from thermabot.core.types import HouseState, Plan, PlanStep, Presence, Snapshot


def seasonal_settings() -> Settings:
    data = house_settings().model_dump(mode="json")
    data["seasonal_control"] = {"enabled": True, "home": [70, 73], "sleep": [68, 70]}
    return Settings.model_validate(data)


@pytest.mark.parametrize(
    ("old_mode", "indoor", "new_mode", "target"),
    [("cool", 69, "heat", 70), ("heat", 74, "cool", 73)],
)
@pytest.mark.brd("FR-PLN-1")
async def test_tick_replans_before_a_stale_mode_can_worsen_comfort(
    tmp_path: Path, old_mode: str, indoor: float, new_mode: str, target: float
) -> None:
    ha = MockHA()
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    ha.states[TSTAT]["attributes"]["current_temperature"] = indoor
    clock = Clock(NOW)
    rt = rt_of(tmp_path, clock, ha=ha)
    _kv_put(rt.ctx, "settings", seasonal_settings().model_dump(mode="json"), "test")
    db.write_bot_state(rt.conn, Persisted(kill_switch="on", run_mode="active"), by="test", now=NOW)
    rt.ctx.plan = Plan(
        kind="fallback",
        created_at=NOW,
        steps=[
            PlanStep(
                t=NOW,
                hvac_mode=old_mode,
                heat_sp=70 if old_mode == "heat" else None,
                cool_sp=73 if old_mode == "cool" else None,
            )
        ],
    )
    rt.loops._replan_due = False

    await rt.tick(NOW)
    assert rt.loops._replan_due
    assert not ha.calls
    later = NOW + timedelta(minutes=1)
    clock.t = later
    await rt.replan(later)
    await rt.tick(later)
    assert [call["service"] for call in ha.calls] == ["set_hvac_mode", "set_temperature"]
    assert ha.calls[0]["data"]["hvac_mode"] == new_mode
    assert ha.calls[1]["data"]["temperature"] == target
    await rt.aclose()


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("hvac_modes", ["off", "heat_cool", "cool"]),
        ("target_temp_step", 2),
        ("min_temp", None),
        ("temperature_unit", "°C"),
    ],
)
@pytest.mark.brd("FR-HA-4")
async def test_seasonal_plan_refuses_incompatible_device_capabilities(
    tmp_path: Path, attribute: str, value: object
) -> None:
    c, ha, _, conn = setup(tmp_path)
    c.settings = seasonal_settings()
    ha.states[TSTAT]["attributes"][attribute] = value
    snap = Snapshot(t=NOW, thermostat=thermostat(ha.states[TSTAT], NOW))
    step = PlanStep(t=NOW, hvac_mode="heat", heat_sp=70)
    decisions = await c.tick(snap, step, LIVE, NOW)
    assert decisions[0].guard == "device"
    assert not decisions[0].allowed and not ha.calls
    assert outcomes(conn) == ["denied"]


@pytest.mark.brd("FR-HA-4")
async def test_seasonal_plan_rechecks_capabilities_before_changing_mode(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path)
    c.settings = seasonal_settings()
    snap = Snapshot(t=NOW, thermostat=thermostat(ha.states[TSTAT], NOW))
    ha.states[TSTAT]["attributes"]["hvac_modes"] = ["off", "heat_cool", "cool"]
    step = PlanStep(t=NOW, hvac_mode="heat", heat_sp=70)
    await c.tick(snap, step, LIVE, NOW)
    assert not ha.calls
    assert outcomes(conn) == ["failed"]


@pytest.mark.brd("FR-HA-4")
async def test_seasonal_plan_rechecks_capabilities_after_changing_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, ha, _, conn = setup(tmp_path)
    c.settings = seasonal_settings()
    snap = Snapshot(t=NOW, thermostat=thermostat(ha.states[TSTAT], NOW))
    call = ha.call

    async def change_capabilities(
        domain: str, service: str, data: dict[str, Any], user: str = BOT_USER_ID
    ) -> list[dict[str, Any]]:
        result = await call(domain, service, data, user)
        if service == "set_hvac_mode":
            ha.states[TSTAT]["attributes"]["target_temp_step"] = 2
        return result

    monkeypatch.setattr(ha, "call", change_capabilities)
    await c.tick(snap, PlanStep(t=NOW, hvac_mode="heat", heat_sp=70), LIVE, NOW)

    assert [call["service"] for call in ha.calls] == ["set_hvac_mode"]
    assert outcomes(conn) == ["failed"]


@pytest.mark.brd("FR-HA-4")
async def test_seasonal_plan_requires_exact_setpoint_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, ha, _, conn = setup(tmp_path)
    c.settings = seasonal_settings()
    snap = Snapshot(t=NOW, thermostat=thermostat(ha.states[TSTAT], NOW))
    call = ha.call

    async def rounded_readback(
        domain: str, service: str, data: dict[str, Any], user: str = BOT_USER_ID
    ) -> list[dict[str, Any]]:
        result = await call(domain, service, data, user)
        if service == "set_temperature":
            ha.states[TSTAT]["attributes"]["temperature"] = 69.5
        return result

    monkeypatch.setattr(ha, "call", rounded_readback)
    await c.tick(snap, PlanStep(t=NOW, hvac_mode="heat", heat_sp=70), LIVE, NOW)

    assert outcomes(conn) == ["failed"]
    assert c.alerts[0].key == "write_failed"


@pytest.mark.brd("FR-CMF-2")
async def test_schedule_boundary_replans_before_applying_the_previous_block(tmp_path: Path) -> None:
    now = local(2026, 11, 10, 21, 59)
    ha = MockHA()
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    ha.states[TSTAT]["state"] = "heat"
    ha.states[TSTAT]["attributes"]["temperature"] = 70
    clock = Clock(now)
    rt = rt_of(tmp_path, clock, ha=ha)
    _kv_put(rt.ctx, "settings", seasonal_settings().model_dump(mode="json"), "test")
    db.write_bot_state(rt.conn, Persisted(kill_switch="on", run_mode="active"), by="test", now=now)
    rt.ctx.plan = Plan(
        kind="fallback",
        created_at=now,
        steps=[PlanStep(t=now, hvac_mode="heat", heat_sp=70)],
    )
    rt.loops._replan_due = False
    await rt.tick(now)
    assert not rt.loops._replan_due and not ha.calls

    clock.t = now + timedelta(minutes=1)
    ha.states[TSTAT]["attributes"]["current_temperature"] = 69
    await rt.tick(clock.t)
    assert rt.loops._replan_due and not ha.calls
    await rt.replan(clock.t)
    assert rt.ctx.plan is not None and rt.ctx.plan.steps[0].heat_sp == 68
    await rt.tick(clock.t)
    assert ha.calls[-1]["data"]["temperature"] == 68
    await rt.aclose()


@pytest.mark.brd("FR-CMF-1")
async def test_away_boundary_does_not_apply_a_future_occupied_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = local(2026, 11, 10, 22)
    ha = MockHA()
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    ha.states[TSTAT]["state"] = "heat"
    ha.states[TSTAT]["attributes"]["temperature"] = 62
    ha.states[TSTAT]["attributes"]["current_temperature"] = 71
    rt = rt_of(tmp_path, Clock(now), ha=ha)
    _kv_put(rt.ctx, "settings", seasonal_settings().model_dump(mode="json"), "test")
    db.write_bot_state(rt.conn, Persisted(kill_switch="on", run_mode="active"), by="test", now=now)
    away = Presence(house=HouseState.EMPTY, since=now - timedelta(hours=1))
    rt.prev_presence = away
    rt.seasonal_block = "away"

    async def still_away(_now: object) -> Presence:
        return away

    monkeypatch.setattr(rt, "read_presence", still_away)
    rt.ctx.plan = Plan(
        kind="fallback",
        created_at=now - timedelta(minutes=5),
        steps=[PlanStep(t=now, hvac_mode="heat", heat_sp=68)],
    )
    rt.loops._replan_due = False

    await rt.tick(now)

    assert rt.loops._replan_due
    assert not ha.calls
    await rt.replan(now)
    assert rt.ctx.plan is not None
    assert rt.ctx.plan.steps[0].heat_sp == 62
    await rt.aclose()


@pytest.mark.brd("FR-OVR-2")
async def test_open_override_blocks_the_weather_aware_plan(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path)
    c.settings = seasonal_settings()
    queue_override(conn, {"pause": True}, kind="ui_pause")
    snap = Snapshot(t=NOW, thermostat=thermostat(ha.states[TSTAT], NOW))

    decisions = await c.tick(snap, PlanStep(t=NOW, hvac_mode="heat", heat_sp=70), LIVE, NOW)

    assert not ha.calls
    assert decisions == [] or all(not decision.allowed for decision in decisions)


@pytest.mark.brd("NFR-SAFE-3")
async def test_safety_restore_still_precedes_an_open_override(tmp_path: Path) -> None:
    c, ha, _, conn = setup(tmp_path)
    c.settings = seasonal_settings()
    queue_override(conn, {"pause": True}, kind="ui_pause")
    ha.states[TSTAT]["state"] = "off"
    ha.states[TSTAT]["attributes"]["current_temperature"] = 58
    snap = Snapshot(t=NOW, thermostat=thermostat(ha.states[TSTAT], NOW))

    decisions = await c.tick(snap, PlanStep(t=NOW, hvac_mode="heat", heat_sp=70), LIVE, NOW)

    assert decisions[0].allowed and decisions[0].action.safety_restore
    assert [call["service"] for call in ha.calls] == ["set_hvac_mode", "set_temperature"]


@pytest.mark.brd("FR-PLN-7")
async def test_replan_error_fallback_still_corrects_the_active_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ha = MockHA()
    ha.states[TSTAT]["state"] = "heat"
    ha.states[TSTAT]["attributes"]["temperature"] = 70
    ha.states[TSTAT]["attributes"]["current_temperature"] = 74
    rt = rt_of(tmp_path, Clock(NOW), ha=ha)
    _kv_put(rt.ctx, "settings", seasonal_settings().model_dump(mode="json"), "test")
    await rt.tick(NOW)

    def fail_model(*_args: object) -> None:
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(jobs, "active_model", fail_model)
    await rt.replan(NOW)

    assert rt.ctx.plan is not None
    assert rt.ctx.plan.kind == "fallback"
    assert "replan error" in rt.ctx.plan.reason_banner
    assert (rt.ctx.plan.steps[0].hvac_mode, rt.ctx.plan.steps[0].cool_sp) == ("cool", 73)
    assert not ha.calls
    await rt.aclose()
