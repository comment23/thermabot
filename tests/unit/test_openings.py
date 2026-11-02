from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest

from thermabot.config import OpeningPolicyCfg
from thermabot.core.openings import assess

NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)


def _state(opened: bool, minutes_ago: int, *, reported: datetime = NOW) -> dict[str, Any]:
    changed = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "state": "on" if opened else "off",
        "last_changed": changed,
        "last_updated": reported.isoformat(),
        "last_reported": reported.isoformat(),
    }


def _exterior_policy(
    *, active_state: Literal["on", "off"] = "on", stale_after_min: float = 0
) -> OpeningPolicyCfg:
    return OpeningPolicyCfg(
        enabled=True,
        min_open_min=10,
        recovery_min=5,
        stale_after_min=stale_after_min,
        sensors={
            "binary_sensor.front_door": {
                "role": "exterior",
                "affected_rooms": ["living_room"],
                "active_state": active_state,
            }
        },
    )


@pytest.mark.parametrize(
    ("minutes_open", "status"),
    [(3, "brief"), (12, "sustained")],
)
def test_exterior_open_threshold(minutes_open: int, status: str) -> None:
    finding = assess(
        _exterior_policy(),
        {"binary_sensor.front_door": _state(True, minutes_open)},
        NOW,
    )[0]
    assert finding.status == status
    assert finding.rooms == ["living_room"]
    assert "action was sent" in finding.message


def test_unknown_stale_and_inverted_contacts_are_not_assumed_open() -> None:
    policy = _exterior_policy(active_state="off")
    missing = assess(policy, {"binary_sensor.front_door": None}, NOW)[0]
    assert missing.status == "unknown"

    stale_policy = _exterior_policy(stale_after_min=5)
    stale_state = _state(True, 12, reported=NOW - timedelta(minutes=6))
    stale = assess(stale_policy, {"binary_sensor.front_door": stale_state}, NOW)[0]
    assert stale.status == "unknown" and "stale" in stale.message

    inverted = assess(
        policy,
        {"binary_sensor.front_door": _state(False, 12)},
        NOW,
    )[0]
    assert inverted.status == "sustained"


def test_closed_contact_stabilizes_before_becoming_inactive() -> None:
    policy = _exterior_policy()
    states = {"binary_sensor.front_door": _state(False, 2)}
    assert assess(policy, states, NOW)[0].status == "stabilizing"
    states["binary_sensor.front_door"] = _state(False, 6)
    assert assess(policy, states, NOW)[0].status == "inactive"


def _garage_policy() -> OpeningPolicyCfg:
    return OpeningPolicyCfg(
        enabled=True,
        min_open_min=10,
        recovery_min=5,
        sensors={
            "binary_sensor.garage_door": {
                "role": "garage_exterior",
                "affected_rooms": ["garage"],
                "required_openings": ["binary_sensor.garage_entry"],
            },
            "binary_sensor.garage_entry": {
                "role": "garage_to_house",
                "affected_rooms": ["living_room"],
            },
        },
    )


def test_garage_requires_both_exterior_and_house_door_open() -> None:
    policy = _garage_policy()
    garage_only = assess(
        policy,
        {
            "binary_sensor.garage_door": _state(True, 20),
            "binary_sensor.garage_entry": _state(False, 20),
        },
        NOW,
    )[0]
    assert garage_only.status == "inactive"
    assert garage_only.rooms == ["garage", "living_room"]

    path_open = assess(
        policy,
        {
            "binary_sensor.garage_door": _state(True, 20),
            "binary_sensor.garage_entry": _state(True, 12),
        },
        NOW,
    )[0]
    assert path_open.status == "sustained"
    assert path_open.duration_min == 12


def test_unknown_garage_contact_keeps_path_unknown() -> None:
    finding = assess(
        _garage_policy(),
        {
            "binary_sensor.garage_door": _state(True, 20),
            "binary_sensor.garage_entry": None,
        },
        NOW,
    )[0]
    assert finding.status == "unknown"


def test_garage_path_duration_starts_when_last_required_contact_opens() -> None:
    policy = OpeningPolicyCfg(
        enabled=True,
        min_open_min=10,
        sensors={
            "binary_sensor.garage_door": {
                "role": "garage_exterior",
                "affected_rooms": ["garage"],
                "required_openings": [
                    "binary_sensor.garage_entry",
                    "binary_sensor.garage_side_entry",
                ],
            },
            "binary_sensor.garage_entry": {
                "role": "garage_to_house",
                "affected_rooms": ["living_room"],
            },
            "binary_sensor.garage_side_entry": {
                "role": "garage_to_house",
                "affected_rooms": ["kitchen"],
            },
        },
    )
    finding = assess(
        policy,
        {
            "binary_sensor.garage_door": _state(True, 20),
            "binary_sensor.garage_entry": _state(True, 15),
            "binary_sensor.garage_side_entry": _state(True, 3),
        },
        NOW,
    )[0]
    assert finding.status == "brief"
    assert finding.duration_min == 3


def test_disabled_policy_has_no_findings() -> None:
    policy = _exterior_policy().model_copy(update={"enabled": False})
    assert assess(policy, {}, NOW) == []
