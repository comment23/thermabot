"""v0.1.10: HA/Trane strings are compared case-insensitively. The Trane reports fan_mode
'Auto' and preset_mode 'None'; the bot wants 'auto'/'none'. A case-sensitive compare
dry-wrote set_fan_mode {'fan_mode': 'auto'} every minute in SHADOW (2026-10-06)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.house import HOUSE
from tests.house import TSTAT as WRITABLE_ENTITY
from thermabot.app.controller import device_value, diff, readback_ok
from thermabot.core.guards import _no_change
from thermabot.core.types import Action, PlanStep, Snapshot, ThermostatState

NOW = datetime(2026, 10, 7, 3, 0, tzinfo=UTC)


def snap(
    mode: str = "heat_cool", fan: str | None = "Auto", preset: str | None = "None"
) -> Snapshot:
    return Snapshot(
        t=NOW,
        thermostat=ThermostatState(
            hvac_mode=mode,
            hvac_action="Idle",
            current_temp=74,
            heat_sp=73,
            cool_sp=76,
            fan_mode=fan,
            preset_mode=preset,
            last_reported=NOW,
        ),
    )


@pytest.mark.brd("FR-HA-4")
def test_trane_casing_is_normalised_on_read() -> None:
    th = snap(mode="HEAT_COOL").thermostat
    assert (th.hvac_mode, th.hvac_action, th.fan_mode, th.preset_mode) == (
        "heat_cool",
        "idle",
        "auto",
        "none",
    )
    assert ThermostatState(hvac_mode="off", fan_mode=None).fan_mode is None


@pytest.mark.brd("FR-HA-4")
@pytest.mark.parametrize("fan", ["Auto", "AUTO", "auto"])
def test_fan_auto_vs_trane_auto_is_no_write(fan: str) -> None:
    step = PlanStep(t=NOW, hvac_mode="heat_cool", heat_sp=73, cool_sp=76, fan_mode="auto")
    assert diff(step, snap(fan=fan), WRITABLE_ENTITY) == []


@pytest.mark.brd("FR-HA-4")
def test_real_fan_change_still_written() -> None:
    step = PlanStep(t=NOW, hvac_mode="heat_cool", heat_sp=73, cool_sp=76, fan_mode="on")
    acts = diff(step, snap(fan="Auto"), WRITABLE_ENTITY)
    assert [a.service for a in acts] == ["set_fan_mode"]
    assert acts[0].data == {"fan_mode": "on"}


@pytest.mark.brd("FR-HA-4")
@pytest.mark.parametrize("mode", ["Heat_Cool", "HEAT_COOL"])
def test_hvac_mode_case_is_no_write(mode: str) -> None:
    step = PlanStep(t=NOW, hvac_mode="heat_cool", heat_sp=73, cool_sp=76)
    assert diff(step, snap(mode=mode), WRITABLE_ENTITY) == []
    off = PlanStep(t=NOW, hvac_mode="off")
    assert diff(off, snap(mode="Off"), WRITABLE_ENTITY) == []


@pytest.mark.brd("FR-HA-4")
def test_no_change_guard_case_insensitive() -> None:
    from thermabot.core.guards import GuardCtx

    ctx = GuardCtx(
        kill_switch="on",
        ha_mirror="on",
        run_mode="shadow",
        snapshot=snap(),
        now=NOW,
        settings=HOUSE,
    )
    fan = Action(service="set_fan_mode", entity_id=WRITABLE_ENTITY, data={"fan_mode": "auto"})
    assert _no_change(fan, ctx)
    on = fan.model_copy(update={"data": {"fan_mode": "on"}})
    assert not _no_change(on, ctx)


@pytest.mark.brd("FR-HA-4")
def test_device_value_uses_device_spelling_and_readback_ignores_case() -> None:
    assert device_value("auto", ["Auto", "On", "Circulate"]) == "Auto"
    assert device_value("none", ["None", "Home", "Away"]) == "None"
    assert device_value("auto", ["auto", "on"]) == "auto"
    assert device_value("auto", None) == "auto"
    act = Action(service="set_fan_mode", entity_id=WRITABLE_ENTITY, data={"fan_mode": "Auto"})
    assert readback_ok(act, {"state": "heat_cool", "attributes": {"fan_mode": "auto"}})
