from datetime import timedelta

import pytest

from tests.conftest import local
from thermabot.core.alerts import SEVERITY, Conditions, evaluate, event, severity

NOW = local(2026, 10, 5, 12, 0)

LEVEL_CASES = {
    "degraded": Conditions(degraded_min=16),
    "comfort_violation": Conditions(comfort_zero_min={"bedroom_1": 31}),
    "room_stale:study": Conditions(room_dead_h={"study": 13}),
    "life360_stale": Conditions(life360_all_stale_h=2.5),
    "ha_auth": Conditions(ha_auth_rejected=True),
}


@pytest.mark.brd("FR-ALR-1")
@pytest.mark.parametrize("key", sorted(LEVEL_CASES))
def test_each_level_key_fires_once_and_closes(key: str) -> None:
    ev, op = evaluate(LEVEL_CASES[key], {}, NOW)
    assert [e.key for e in ev] == [key] and not ev[0].clear
    ev2, op = evaluate(LEVEL_CASES[key], op, NOW + timedelta(hours=1))
    assert ev2 == []  # deduplicated
    ev3, op = evaluate(Conditions(), op, NOW + timedelta(hours=2))
    assert [e.key for e in ev3] == [key] and ev3[0].clear and ev3[0].text.startswith("resolved")
    assert op == {}


@pytest.mark.brd("FR-ALR-1")
def test_thresholds_not_crossed() -> None:
    c = Conditions(
        degraded_min=15, comfort_zero_min={"x": 30}, room_dead_h={"x": 12}, life360_all_stale_h=2
    )
    assert evaluate(c, {}, NOW)[0] == []


@pytest.mark.brd("FR-ALR-1")
def test_reminder_after_24h() -> None:
    c = LEVEL_CASES["degraded"]
    _, op = evaluate(c, {}, NOW)
    assert evaluate(c, op, NOW + timedelta(hours=23, minutes=59))[0] == []
    ev, op2 = evaluate(c, op, NOW + timedelta(hours=24))
    assert len(ev) == 1 and ev[0].text.startswith("still")
    assert op2["degraded"].opened_at == NOW


@pytest.mark.brd("FR-ALR-1")
@pytest.mark.parametrize(
    "key", ["write_failed", "safety_restore", "watchdog_fired", "kill_switch", "container_restart"]
)
def test_event_keys(key: str) -> None:
    e = event(key, "x", NOW)
    assert e is not None and e.severity == SEVERITY[key]


@pytest.mark.brd("FR-OVR-4")
def test_override_alert_off_by_default() -> None:
    assert event("override", "x", NOW) is None
    assert event("override", "x", NOW, override_notify=True) is not None


@pytest.mark.brd("FR-ALR-1")
def test_severities() -> None:
    assert severity("safety_restore") == "critical" and severity("room_stale:study") == "info"
    assert severity("degraded") == "warn"
