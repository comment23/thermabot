from datetime import timedelta

import pytest

from tests.conftest import local
from tests.house import HOUSE
from tests.house import TSTAT as WRITABLE_ENTITY
from thermabot.core.guards import (
    GuardCtx,
    WriteRecord,
    evaluate,
    hard_limits,
)
from thermabot.core.types import Action, Snapshot, ThermostatState

NOW = local(2026, 10, 5, 12, 0)


def snap(
    mode: str = "heat_cool",
    temp: float = 70,
    lo: float = 68,
    hi: float = 76,
    age_min: float = 1,
    **kw: object,
) -> Snapshot:
    th = ThermostatState(
        hvac_mode=mode,
        current_temp=temp,
        heat_sp=lo,
        cool_sp=hi,
        fan_mode="auto",
        last_reported=NOW - timedelta(minutes=age_min),
        **kw,
    )  # type: ignore[arg-type]
    return Snapshot(t=NOW, thermostat=th)


def ctx(**kw: object) -> GuardCtx:
    base: dict[str, object] = dict(
        kill_switch="on",
        ha_mirror="on",
        run_mode="active",
        snapshot=snap(),
        now=NOW,
        settings=HOUSE,
    )
    base.update(kw)
    return GuardCtx(**base)  # type: ignore[arg-type]


def sp(
    lo: float = 67,
    hi: float = 75,
    restore: bool = False,
    entity: str = WRITABLE_ENTITY,
    **extra: float | str,
) -> Action:
    return Action(
        service="set_temperature",
        entity_id=entity,
        data={"target_temp_low": lo, "target_temp_high": hi, **extra},
        safety_restore=restore,
    )


def mode(m: str, restore: bool = False) -> Action:
    return Action(
        service="set_hvac_mode",
        entity_id=WRITABLE_ENTITY,
        data={"hvac_mode": m},
        safety_restore=restore,
    )


def off_ok(**kw: object) -> GuardCtx:
    base: dict[str, object] = dict(off_score_proven=True, off_forecast_f=[60, 70])
    base.update(kw)
    return ctx(**base)


class TestReadable:
    @pytest.mark.brd("NFR-SAFE-6")
    def test_missing_unavailable_old_deny_even_restore(self) -> None:
        for c in (
            ctx(snapshot=None),
            ctx(snapshot=snap(mode="unavailable")),
            ctx(snapshot=snap(age_min=11)),
        ):
            d = evaluate(sp(restore=True), c)
            assert not d.allowed and d.guard == "readable"

    @pytest.mark.brd("NFR-SAFE-6")
    def test_fresh_ok(self) -> None:
        assert evaluate(sp(), ctx(snapshot=snap(age_min=9))).allowed


class TestKillSwitch:
    @pytest.mark.brd("FR-KILL-2")
    def test_off_missing_or_mirror_disagree_denies_restore(self) -> None:
        for c in (
            ctx(kill_switch="off"),
            ctx(kill_switch=None),
            ctx(ha_mirror="off"),
            ctx(ha_mirror=None),
        ):
            d = evaluate(sp(restore=True), c)
            assert not d.allowed and d.guard == "kill_switch"

    @pytest.mark.brd("FR-MODE-2")
    def test_shadow_logged_not_sent(self) -> None:
        d = evaluate(sp(), ctx(run_mode="shadow"))
        assert not d.allowed and d.guard == "shadow" and d.would_write


class TestOverride:
    @pytest.mark.brd("FR-OVR-3")
    def test_override_blocks_plan_not_restore(self) -> None:
        d = evaluate(sp(), ctx(override_open=True))
        assert not d.allowed and d.guard == "override"
        assert evaluate(sp(restore=True), ctx(override_open=True)).allowed


class TestAllowlist:
    @pytest.mark.brd("FR-HA-2")
    def test_foreign_refused(self) -> None:
        d = evaluate(sp(entity="climate.garage_ac"), ctx())
        assert not d.allowed and d.guard == "allowlist"
        d = evaluate(sp(entity="climate.garage_ac", restore=True), ctx())
        assert not d.allowed and d.guard == "allowlist"

    @pytest.mark.brd("FR-HA-8")
    def test_preset_only_none_once(self) -> None:
        a = Action(
            service="set_preset_mode", entity_id=WRITABLE_ENTITY, data={"preset_mode": "none"}
        )
        c = ctx(snapshot=snap(preset_mode="away"))
        assert evaluate(a, c).allowed
        d = evaluate(a, ctx(snapshot=snap(preset_mode="away"), preset_cleared=True))
        assert not d.allowed and d.guard == "allowlist"
        eco = a.model_copy(update={"data": {"preset_mode": "eco"}})
        assert evaluate(eco, c).guard == "allowlist"


class TestHardLimits:
    @pytest.mark.brd("FR-CMF-5")
    def test_clamps_floor_and_ceiling(self) -> None:
        a, _ = hard_limits(sp(lo=55, hi=85), ctx())
        assert a is not None
        assert a.data["target_temp_low"] == 60 and a.data["target_temp_high"] == 80
        a, _ = hard_limits(
            Action(
                service="set_temperature",
                entity_id=WRITABLE_ENTITY,
                data={"temperature": 60, "hvac_mode": "cool"},
            ),
            ctx(),
        )
        assert a is not None and a.data["temperature"] == 68

    @pytest.mark.brd("FR-HA-3")
    def test_deadband(self) -> None:
        a, note = hard_limits(sp(lo=72, hi=73), ctx())
        assert a is not None and a.data["target_temp_high"] == 75 and "deadband" in note
        a, _ = hard_limits(sp(lo=79, hi=80), ctx())
        assert a is not None and (a.data["target_temp_low"], a.data["target_temp_high"]) == (77, 80)

    @pytest.mark.brd("FR-CMF-5")
    def test_no_valid_clamp_denies(self) -> None:
        d = evaluate(sp(), ctx(device_min_f=79, device_max_f=80))
        assert not d.allowed and d.guard == "hard_limits"


class TestOffGuardrails:
    @pytest.mark.brd("NFR-SAFE-1")
    def test_allowed_when_all_hold(self) -> None:
        assert evaluate(mode("off"), off_ok()).allowed

    @pytest.mark.brd("NFR-SAFE-1")
    def test_outdoor_window(self) -> None:
        for f in ([49, 60], [60, 79], []):
            d = evaluate(mode("off"), off_ok(off_forecast_f=f))
            assert not d.allowed and d.guard == "off_guardrails"
        assert evaluate(mode("off"), off_ok(off_forecast_f=[50, 78])).allowed

    @pytest.mark.brd("NFR-SAFE-1")
    def test_score_not_proven(self) -> None:
        assert evaluate(mode("off"), ctx(off_forecast_f=[60])).guard == "off_guardrails"

    @pytest.mark.brd("NFR-SAFE-2")
    def test_cap_used_and_floor_crossed_and_hold(self) -> None:
        assert evaluate(mode("off"), off_ok(off_used_h=24)).guard == "off_guardrails"
        assert evaluate(mode("off"), off_ok(snapshot=snap(temp=62))).guard == "off_guardrails"
        assert evaluate(mode("off"), off_ok(snapshot=snap(temp=80))).guard == "off_guardrails"
        held = off_ok(safety_hold_until=NOW + timedelta(minutes=5))
        assert evaluate(mode("off"), held).guard == "off_guardrails"


def w(min_ago: float, service: str = "set_temperature", **data: float | str) -> WriteRecord:
    return WriteRecord(t=NOW - timedelta(minutes=min_ago), service=service, data=data)


class TestRateLimit:
    @pytest.mark.brd("FR-HA-5")
    def test_no_change(self) -> None:
        d = evaluate(sp(lo=68, hi=76), ctx())
        assert not d.allowed and d.guard == "rate_limit"

    @pytest.mark.brd("NFR-SAFE-10")
    def test_setpoint_gap(self) -> None:
        assert evaluate(sp(), ctx(recent_writes=[w(9)])).guard == "rate_limit"
        assert evaluate(sp(), ctx(recent_writes=[w(10)])).allowed

    @pytest.mark.brd("NFR-SAFE-10")
    def test_mode_gap(self) -> None:
        rw = [w(14, "set_hvac_mode", hvac_mode="heat_cool")]
        assert evaluate(mode("heat"), ctx(recent_writes=rw)).guard == "rate_limit"
        rw = [w(15, "set_hvac_mode", hvac_mode="heat_cool")]
        assert evaluate(mode("heat"), ctx(recent_writes=rw)).allowed

    @pytest.mark.brd("FR-HA-3")
    def test_max_per_day(self) -> None:
        rw = [w(11 + i, "set_fan_mode", fan_mode="auto") for i in range(30)]
        assert evaluate(sp(), ctx(recent_writes=rw)).guard == "rate_limit"

    @pytest.mark.brd("FR-HA-3")
    def test_flips(self) -> None:
        rw = [
            w(100, "set_hvac_mode", hvac_mode="heat"),
            w(80, "set_hvac_mode", hvac_mode="cool"),
            w(60, "set_hvac_mode", hvac_mode="heat"),
        ]
        c = ctx(snapshot=snap(mode="heat"), recent_writes=rw)
        assert evaluate(mode("cool"), c).guard == "rate_limit"

    @pytest.mark.brd("NFR-SAFE-3")
    def test_restore_bypasses_rate_limit_not_kill_switch(self) -> None:
        rw = [w(1), w(2, "set_hvac_mode", hvac_mode="off")]
        c = ctx(snapshot=snap(mode="off", temp=61), recent_writes=rw)
        assert evaluate(sp(restore=True, hvac_mode="heat_cool"), c).allowed
        c2 = c.model_copy(update={"kill_switch": "off"})
        assert evaluate(sp(restore=True, hvac_mode="heat_cool"), c2).guard == "kill_switch"
