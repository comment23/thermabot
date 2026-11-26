from __future__ import annotations

import pytest

from tests.house import house_settings
from thermabot.core.comfort import block_band, hold_setpoints
from thermabot.core.preferences import Priority, propose, saved_priority


@pytest.mark.brd("FR-CMF-1")
@pytest.mark.parametrize(
    ("priority", "band"),
    [
        ("comfort", (72, 74)),
        ("balanced", (71, 75)),
        ("efficiency", (70, 76)),
    ],
)
def test_preferences_drive_all_seasons_and_preserve_unrelated_settings(
    priority: Priority,
    band: tuple[float, float],
) -> None:
    original = house_settings()
    before = original.model_dump()
    updated = propose(original, 73, priority, ["study"], ["bedroom_1"])
    for season in ("heat", "cool", "shoulder"):
        assert block_band(updated, season, "home") == band
        assert block_band(updated, season, "sleep") == band
    expected = original.model_dump()
    expected["seasonal_control"] = {"enabled": True, "home": band, "sleep": band}
    expected["insights"].update(ideal_temp_f=73, tolerance_f=band[1] - 73)
    expected["weights"].update(home={"study": 1}, sleep={"bedroom_1": 1})
    assert updated.model_dump() == expected and original.model_dump() == before
    assert saved_priority(updated) == priority
    automatic = hold_setpoints(band, original.writes.deadband_f)
    assert automatic[1] - automatic[0] >= original.writes.deadband_f


@pytest.mark.brd("NFR-SAFE-1")
@pytest.mark.parametrize(
    ("ideal", "rooms", "message"),
    [
        (79, ["study"], "safety"),
        (73, [], "at least one"),
        (73, ["unknown"], "unknown room"),
        (73, ["study", "study"], "duplicate"),
    ],
)
def test_preferences_reject_unsafe_or_incomplete_choices(
    ideal: float,
    rooms: list[str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        propose(house_settings(), ideal, "balanced", rooms, ["bedroom_1"])


@pytest.mark.brd("FR-CFG-1")
@pytest.mark.parametrize("problem", ["missing", "climate", "thermostat", "hvac"])
def test_missing_sensor_and_custom_ranges_are_not_silently_replaced(problem: str) -> None:
    original = house_settings()
    rooms = ["study"]
    if problem == "missing":
        original.rooms["study"].temp = ""
    elif problem == "climate":
        original.rooms["study"].temp = "climate.not_a_room_sensor"
    elif problem == "thermostat":
        original.rooms["thermostat"].temp = "climate.other"
        rooms = ["thermostat"]
    else:
        original = house_settings(sensors={})
    before = original.model_dump()
    assert saved_priority(original) is None
    with pytest.raises(ValueError, match="Confirm|confirm"):
        propose(original, 73, "balanced", rooms, ["bedroom_1"])
    assert original.model_dump() == before


@pytest.mark.brd("NFR-SAFE-1")
def test_automatic_deadband_must_fit_safety_even_when_reference_fits() -> None:
    settings = house_settings()
    assert 77 < 79 < settings.safety.ceil_tstat_f
    assert hold_setpoints((77, 79), settings.writes.deadband_f) == (77, 80)
    with pytest.raises(ValueError, match="safety limits or the thermostat deadband"):
        propose(settings, 78, "comfort", ["study"], ["bedroom_1"])
