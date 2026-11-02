"""FR-CFG-1: configurable thermostat entity; both write layers use it.

v0.2.0: the code default is empty = not configured (setup needed). Every write path denies
while it is unset: core guard allowlist, safety restore, controller, HA adapter check_write."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from tests.conftest import local
from tests.house import HOUSE, house_dict
from thermabot.adapters.ha_rest import WriteRefused, check_write
from thermabot.adapters.ha_ws import in_read_set
from thermabot.config import Settings, ThermostatCfg
from thermabot.core import safety
from thermabot.core import statemachine as sm
from thermabot.core.guards import GuardCtx, allowlist, evaluate
from thermabot.core.types import Action, Snapshot, ThermostatState

OLD = "climate.living_room"
OTHER = "climate.hallway"
NOW = local(2026, 10, 5, 12, 0)


def _ctx(entity: str | None = None, temp: float = 70, mode: str = "heat_cool") -> GuardCtx:
    s = HOUSE if entity is None else Settings(thermostat=ThermostatCfg(entity_id=entity))
    th = ThermostatState(
        hvac_mode=mode,
        current_temp=temp,
        heat_sp=68,
        cool_sp=76,
        fan_mode="auto",
        last_reported=NOW - timedelta(minutes=1),
    )
    return GuardCtx(
        kill_switch="on",
        ha_mirror="on",
        run_mode="active",
        snapshot=Snapshot(t=NOW, thermostat=th),
        settings=s,
        now=NOW,
    )


def _act(entity: str) -> Action:
    return Action(
        service="set_temperature",
        entity_id=entity,
        data={"target_temp_low": 67, "target_temp_high": 75},
    )


@pytest.mark.brd("FR-CFG-1")
def test_default_is_not_configured() -> None:
    s = Settings()
    assert s.thermostat.entity_id == "" and not s.thermostat.configured
    assert set(s.rooms) == {"thermostat"} and s.presence.people == {}
    assert HOUSE.thermostat.configured


@pytest.mark.brd("FR-CFG-1")
def test_saved_row_without_thermostat_key_is_not_configured() -> None:
    # A pre-v0.1.11 row lacks the key. v0.1.11 always persists it on first start, so a live
    # row has it; without it v0.2.0 lands in 'setup needed' (never writes), not a guess.
    saved = house_dict()
    del saved["thermostat"]
    s = Settings.model_validate_json(json.dumps(saved))
    assert not s.thermostat.configured


@pytest.mark.brd("FR-CFG-1")
@pytest.mark.parametrize(
    "bad", ["sensor.x", "climate.", "climate.Living", "climate.a-b", "climate.a b", " climate.x"]
)
def test_bad_entity_id_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        ThermostatCfg(entity_id=bad)
    with pytest.raises(ValidationError):
        Settings.model_validate({"thermostat": {"entity_id": bad}})


@pytest.mark.brd("FR-CFG-1")
def test_guard_allows_only_the_configured_entity() -> None:
    c = _ctx(OTHER)
    ok, note = allowlist(_act(OLD), c)
    assert not ok and "not writable" in note
    assert allowlist(_act(OTHER), c) == (True, "")
    d = evaluate(_act(OLD), c)
    assert not d.allowed and d.guard == "allowlist"
    assert evaluate(_act(OTHER), c).allowed
    assert allowlist(_act(OLD), _ctx()) == (True, "")
    assert not allowlist(_act(OTHER), _ctx())[0]


@pytest.mark.brd("NFR-SAFE-5")
@pytest.mark.parametrize("entity", ["", OLD, "climate.anything"])
def test_unconfigured_guard_denies_every_entity(entity: str) -> None:
    c = _ctx("")
    ok, note = allowlist(_act(entity) if entity else _act(OLD), c)
    assert not ok and "not configured" in note
    d = evaluate(_act(entity or OLD), c)
    assert not d.allowed and d.guard == "allowlist"
    restore = _act(entity or OLD).model_copy(update={"safety_restore": True})
    assert not evaluate(restore, c).allowed  # not even a safety restore


@pytest.mark.brd("NFR-SAFE-3")
def test_unconfigured_no_safety_restore() -> None:
    c = _ctx("", temp=55, mode="off")  # below the floor, system off
    assert c.snapshot is not None
    assert safety.check(c.snapshot, c.settings, "home", NOW) is None
    configured = _ctx(OLD, temp=55, mode="off")
    assert configured.snapshot is not None
    assert safety.check(configured.snapshot, configured.settings, "home", NOW) is not None


@pytest.mark.brd("FR-CFG-1")
def test_safety_restore_targets_configured_entity() -> None:
    c = _ctx(OTHER, temp=61, mode="off")
    assert c.snapshot is not None
    r = safety.check(c.snapshot, c.settings, "home", NOW)
    assert r is not None and r.action.entity_id == OTHER


@pytest.mark.brd("FR-CFG-1")
def test_check_write_custom_writable_entity() -> None:
    check_write("climate", "set_temperature", {"entity_id": OTHER}, writable_entity=OTHER)
    with pytest.raises(WriteRefused, match="not writable"):
        check_write("climate", "set_temperature", {"entity_id": OLD}, writable_entity=OTHER)
    with pytest.raises(WriteRefused, match="not allowed"):
        check_write("climate", "turn_off", {"entity_id": OTHER}, writable_entity=OTHER)


@pytest.mark.brd("NFR-SAFE-5")
@pytest.mark.parametrize("eid", [OLD, OTHER, ""])
def test_check_write_unconfigured_refuses_all_climate(eid: str) -> None:
    for kw in ({}, {"writable_entity": ""}):
        with pytest.raises(WriteRefused, match="no thermostat configured"):
            check_write("climate", "set_temperature", {"entity_id": eid}, **kw)  # type: ignore[arg-type]
    # the kill-switch mirror (not a climate write) is unaffected
    check_write("input_boolean", "turn_on", {"entity_id": "input_boolean.thermabot_enabled"})


@pytest.mark.brd("FR-CFG-1")
def test_ws_read_set_follows_configured_entity() -> None:
    assert in_read_set(OLD, thermostat=OLD) and not in_read_set(OTHER, thermostat=OLD)
    assert in_read_set(OTHER, thermostat=OTHER) and not in_read_set(OLD, thermostat=OTHER)
    assert not in_read_set(OLD) and not in_read_set("")  # unconfigured: no climate read


@pytest.mark.brd("FR-CFG-1")
def test_setup_needed_state_blocks_all_writes() -> None:
    p = sm.Persisted(kill_switch="on", run_mode="active", shadow_started_at=NOW)
    e = sm.effective(p, sm.Live(setup_needed=True))
    assert e.state == "DISABLED" and e.label == "SETUP NEEDED"
    assert not e.plan_writes and not e.restore_writes
    assert sm.effective(p, sm.Live()).state == "ACTIVE"  # configured: unchanged


@pytest.mark.brd("FR-CFG-1")
def test_sensors_missing_is_degraded_without_plan_writes() -> None:
    """v0.6.1: configured thermostat but no HVAC sensors -> DEGRADED(sensors), not SETUP NEEDED."""
    for mode in ("shadow", "active"):
        p = sm.Persisted(kill_switch="on", run_mode=mode, shadow_started_at=NOW)  # type: ignore[arg-type]
        e = sm.effective(p, sm.Live(ha_mirror="on", sensors_missing=True))
        assert e.state == "DEGRADED" and e.degraded_reason == "sensors"
        assert e.label != "SETUP NEEDED" and not e.plan_writes
        assert e.restore_writes is (mode == "active")
    s = Settings.model_validate({**house_dict(), "sensors": {}})
    assert s.sensors_missing and not HOUSE.sensors_missing
    assert not Settings().sensors_missing  # unconfigured: SETUP NEEDED wins
