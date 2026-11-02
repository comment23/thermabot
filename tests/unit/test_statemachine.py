from datetime import timedelta

import pytest

from tests.conftest import local
from thermabot.core.statemachine import (
    Live,
    Persisted,
    classify_change,
    effective,
    reconcile,
    step,
    to_shadow,
    turn_off,
    turn_on,
)

NOW = local(2026, 10, 5, 12, 0)
ON_SHADOW = Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=NOW)
ON_ACTIVE = Persisted(
    kill_switch="on", run_mode="active", shadow_started_at=NOW - timedelta(days=20)
)


class TestPrecedence:
    @pytest.mark.brd("FR-KILL-2")
    def test_disabled_beats_all_and_missing_row(self) -> None:
        bad = Live(ha_down_s=999, override_open=True, weather_stale=True)
        for p in (None, turn_off(ON_ACTIVE, NOW)):
            e = effective(p, bad)
            assert e.state == "DISABLED" and not e.plan_writes and not e.restore_writes
        assert effective(ON_ACTIVE, Live(ha_mirror="off")).state == "DISABLED"
        assert effective(ON_ACTIVE, Live(ha_mirror=None)).state == "DISABLED"

    @pytest.mark.brd("NFR-SAFE-6")
    def test_degraded_ha_beats_override(self) -> None:
        e = effective(ON_ACTIVE, Live(ha_down_s=61, override_open=True))
        assert e.state == "DEGRADED" and e.degraded_reason == "ha" and not e.plan_writes
        assert e.restore_writes  # thermostat still readable
        e = effective(ON_ACTIVE, Live(tstat_age_min=11))
        assert e.state == "DEGRADED" and not e.restore_writes
        assert not effective(ON_ACTIVE, Live(tstat_unavailable=True)).restore_writes
        assert effective(ON_ACTIVE, Live(ha_down_s=60)).state == "ACTIVE"

    @pytest.mark.brd("FR-OVR-3")
    def test_override_beats_mode_restore_still_allowed(self) -> None:
        e = effective(ON_ACTIVE, Live(override_open=True))
        assert e.state == "OVERRIDE" and not e.plan_writes and e.restore_writes
        e = effective(ON_SHADOW, Live(override_open=True))
        assert e.label == "SHADOW · OVERRIDE" and not e.restore_writes

    @pytest.mark.brd("FR-WX-2")
    def test_weather_degraded_allows_fallback_writes(self) -> None:
        e = effective(ON_ACTIVE, Live(weather_stale=True))
        assert e.state == "DEGRADED" and e.degraded_reason == "weather" and e.plan_writes
        assert not effective(ON_SHADOW, Live(weather_stale=True)).plan_writes

    @pytest.mark.brd("FR-MODE-2")
    def test_shadow_writes_nothing(self) -> None:
        e = effective(ON_SHADOW, Live())
        assert e.state == "SHADOW" and not e.plan_writes and not e.restore_writes

    @pytest.mark.brd("FR-MODE-1")
    def test_step_emits_transition(self) -> None:
        e, tr = step(ON_ACTIVE, Live(ha_down_s=120), NOW, prev="ACTIVE")
        assert e.state == "DEGRADED" and tr[0].frm == "ACTIVE" and tr[0].to == "DEGRADED"
        assert step(ON_ACTIVE, Live(), NOW, prev="ACTIVE")[1] == []


class TestShadowGate:
    @pytest.mark.brd("FR-MODE-3")
    def test_active_needs_7_days(self) -> None:
        p = turn_on(None, "shadow", NOW)
        assert p.shadow_started_at == NOW
        with pytest.raises(ValueError):
            turn_on(p, "active", NOW + timedelta(days=6, hours=23))
        assert turn_on(p, "active", NOW + timedelta(days=7)).run_mode == "active"
        with pytest.raises(ValueError):
            turn_on(None, "active", NOW)

    @pytest.mark.brd("FR-MODE-3")
    def test_short_off_keeps_clock_long_off_resets(self) -> None:
        p = turn_on(None, "shadow", NOW)
        off = turn_off(p, NOW + timedelta(days=5))
        back = turn_on(off, "shadow", NOW + timedelta(days=5, hours=23))
        assert back.shadow_started_at == NOW
        back = turn_on(off, "shadow", NOW + timedelta(days=6, minutes=1))
        assert back.shadow_started_at == NOW + timedelta(days=6, minutes=1)

    @pytest.mark.brd("FR-MODE-3")
    def test_back_to_shadow_keeps_clock(self) -> None:
        assert to_shadow(ON_ACTIVE).shadow_started_at == ON_ACTIVE.shadow_started_at


@pytest.mark.brd("FR-OVR-3")
def test_external_change() -> None:
    old = {"hvac_mode": "heat_cool", "target_temp_low": 68, "current_temperature": 70}
    new = dict(old, target_temp_low=72)
    assert classify_change(old, new, "c1", "u", {"c1"}, "wd") == "bot"
    assert classify_change(old, new, "c2", "wd", {"c1"}, "wd") == "watchdog"
    assert classify_change(old, new, "c2", "alex", {"c1"}, "wd") == "external"
    same = dict(old, current_temperature=71)
    assert classify_change(old, same, "c2", None, set(), None) == "none"


class TestReconcile:
    @pytest.mark.brd("NFR-SAFE-9")
    def test_off_with_window_continues(self) -> None:
        assert reconcile(ON_ACTIVE, "off", None, True, False) == ["continue"]

    @pytest.mark.brd("NFR-SAFE-9")
    def test_off_without_window_is_external(self) -> None:
        assert reconcile(ON_ACTIVE, "off", None, False, False) == ["external_override"]

    @pytest.mark.brd("NFR-SAFE-9")
    def test_floor_wins(self) -> None:
        assert reconcile(ON_ACTIVE, "off", None, False, True) == ["safety_restore"]

    @pytest.mark.brd("FR-HA-8")
    def test_preset_cleared_once(self) -> None:
        assert reconcile(ON_ACTIVE, "heat_cool", "away", False, False) == ["clear_preset"]
        done = ON_ACTIVE.model_copy(update={"preset_cleared": True})
        assert reconcile(done, "heat_cool", "away", False, False) == ["continue"]
        assert reconcile(turn_off(ON_ACTIVE, NOW), "heat_cool", "away", False, False) == [
            "continue"
        ]
