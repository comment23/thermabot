"""Fix: watchdog attribution via input_datetime.thermabot_watchdog_last (D§13)."""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path

import pytest

from tests.integration.test_controller import NOW, TSTAT, setup
from thermabot.app.controller import WATCHDOG_LAST_ENTITY, parse_last_changed
from thermabot.core.statemachine import classify_change


def wd_state(t: str | None, state: str = "2026-10-05 17:59:00") -> dict[str, object]:
    return {"entity_id": WATCHDOG_LAST_ENTITY, "state": state, "last_changed": t}


async def _change(tmp: Path, wd: dict[str, object] | None) -> tuple[str, object, object]:
    c, ha, _, _ = setup(tmp, kill="off")
    c.on_watchdog_last(wd)
    old = copy.deepcopy(ha.states[TSTAT])
    old["state"] = "off"  # watchdog restores from off
    await ha.external_change(hvac_mode="heat_cool", target_temp_low=68, target_temp_high=76)
    kind = c.on_thermostat_change(old, ha.states[TSTAT], NOW)
    return kind, c, ha


@pytest.mark.brd("NFR-SAFE-8")
async def test_change_within_2min_is_watchdog(tmp_path: Path) -> None:
    kind, c, _ = await _change(tmp_path, wd_state((NOW - timedelta(seconds=90)).isoformat()))
    assert kind == "watchdog"
    assert not c.override_open(NOW)  # type: ignore[attr-defined]
    keys = [(a.key, a.severity) for a in c.alerts]  # type: ignore[attr-defined]
    assert keys == [("watchdog_fired", "critical")]
    assert c.hold_until == NOW + timedelta(minutes=60)  # type: ignore[attr-defined]


@pytest.mark.brd("NFR-SAFE-8")
async def test_change_at_2min_plus_is_external(tmp_path: Path) -> None:
    kind, c, _ = await _change(tmp_path, wd_state((NOW - timedelta(minutes=2)).isoformat()))
    assert kind == "external"
    assert c.override_open(NOW)  # type: ignore[attr-defined]


@pytest.mark.brd("NFR-SAFE-8")
@pytest.mark.parametrize(
    "wd", [None, wd_state(None), wd_state(NOW.isoformat(), "unavailable"), wd_state("garbage")]
)
async def test_watchdog_last_unknown_is_external(
    tmp_path: Path, wd: dict[str, object] | None
) -> None:
    kind, c, _ = await _change(tmp_path, wd)
    assert kind == "external"
    assert c.override_open(NOW)  # type: ignore[attr-defined]
    assert all(a.key != "watchdog_fired" for a in c.alerts)  # type: ignore[attr-defined]


@pytest.mark.brd("NFR-SAFE-8")
def test_classify_change_window_pure() -> None:
    old, new = {"hvac_mode": "off"}, {"hvac_mode": "heat_cool"}
    w = NOW - timedelta(seconds=30)
    assert classify_change(old, new, "c", None, set(), None, watchdog_last=w, now=NOW) == "watchdog"
    assert classify_change(old, new, "c", None, {"c"}, None, watchdog_last=w, now=NOW) == "bot"
    future = NOW + timedelta(seconds=30)
    assert (
        classify_change(old, new, "c", None, set(), None, watchdog_last=future, now=NOW)
        == "external"
    )
    assert parse_last_changed(None) is None
