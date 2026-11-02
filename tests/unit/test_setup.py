"""v0.3.0 setup wizard: pure discovery heuristics, °F/°C round trip, and the optional new
settings sections (old rows load unchanged; legacy rows keep the old sensor ids)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.house import TSTAT, house_dict
from tests.integration.mock_ha.fixtures import initial_states
from thermabot.app import forms
from thermabot.app.units import c_to_f, f_to_c, from_display, to_display
from thermabot.config import Settings
from thermabot.core import discovery


def _states(extra: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    raw = {**initial_states(), **(extra or {})}
    return [{"entity_id": k, **v} for k, v in raw.items()]


EXTRA = {
    "sensor.kitchen_temperature": {
        "state": "70.1",
        "attributes": {"device_class": "temperature", "unit_of_measurement": "°F"},
    },
    "sensor.kitchen_humidity": {
        "state": "45",
        "attributes": {"device_class": "humidity", "unit_of_measurement": "%"},
    },
    "sensor.outdoor_humidity": {
        "state": "80",
        "attributes": {"device_class": "humidity", "unit_of_measurement": "%"},
    },
    "sensor.nas_cpu_temperature": {
        "state": "50",
        "attributes": {"device_class": "temperature", "unit_of_measurement": "°C"},
    },
    "device_tracker.alex_phone": {"state": "home", "attributes": {"friendly_name": "Alex phone"}},
    "fan.bedroom_ceiling": {"state": "off", "attributes": {"friendly_name": "Bedroom fan"}},
    "cover.living_room_blind": {"state": "closed", "attributes": {"friendly_name": "Living blind"}},
    "binary_sensor.front_door": {
        "state": "on",
        "attributes": {"device_class": "door", "friendly_name": "Front door"},
    },
    "climate.garage_heater": {
        "state": "off",
        "attributes": {"hvac_modes": ["off", "heat"], "friendly_name": "Garage heater"},
    },
    "sun.sun": {
        "state": "above_horizon",
        "attributes": {
            "azimuth": 182.3,
            "elevation": 36.2,
            "next_setting": "2026-10-29T22:00:00+00:00",
        },
    },
}


@pytest.mark.brd("FR-CFG-1")
def test_entities_lists_climate_with_modes() -> None:
    es = discovery.entities(_states(), ["climate"])
    assert [e.entity_id for e in es] == ["climate.garage_heater", TSTAT]
    t = es[1]
    assert t.name == "Living Room" and t.state == "heat_cool"
    assert t.hvac_modes == ["off", "heat", "cool", "heat_cool"]
    assert t.fan_modes == ["auto", "on", "circulate"] and t.current_temperature == 71
    assert (t.min_temp, t.max_temp, t.target_temp_step) == (45, 95, 1)
    assert (t.target_temp_low, t.target_temp_high) == (68, 76)


@pytest.mark.brd("FR-CFG-1")
def test_discover_suggests_sensors_with_confidence() -> None:
    d = discovery.discover(_states(EXTRA), TSTAT)
    assert d.blower[0].entity_id == "binary_sensor.living_room_blower_active"
    assert d.compressor[0].entity_id == "sensor.living_room_compressor_speed"
    assert d.compressor[0].confidence >= 0.9
    assert d.outdoor[0].entity_id == "sensor.outdoor_temperature"
    assert [c.entity_id for c in d.humidity] == ["sensor.kitchen_humidity"]  # not outdoor
    rooms = {c.entity_id: c for c in d.rooms}
    assert rooms["sensor.kitchen_temperature"].room == "kitchen"
    assert (
        rooms["sensor.kitchen_temperature"].confidence > rooms["sensor.den_temperature"].confidence
    )
    assert "sensor.nas_cpu_temperature" not in rooms  # hardware, not a room
    assert "sensor.outdoor_temperature" not in rooms
    assert TSTAT not in rooms
    people = [c.entity_id for c in d.people]
    assert people[0] == "person.alex" and "device_tracker.alex_phone" in people
    assert [c.entity_id for c in d.fans] == ["fan.bedroom_ceiling"]
    assert [c.entity_id for c in d.covers] == ["cover.living_room_blind"]
    assert [c.entity_id for c in d.supplemental_heat] == ["climate.garage_heater"]
    assert [c.entity_id for c in d.openings] == ["binary_sensor.front_door"]
    for group in (d.blower, d.compressor, d.outdoor, d.humidity, d.rooms, d.people):
        assert all(0 < c.confidence <= 1 and c.reason for c in group)
    sun = discovery.sun_position(_states(EXTRA))
    assert sun is not None and sun.azimuth == 182.3 and sun.elevation == 36.2
    assert sun.next_setting == "2026-10-29T22:00:00+00:00"


def test_sun_position_rejects_invalid_angles() -> None:
    sun = discovery.sun_position(
        [
            {
                "entity_id": "sun.sun",
                "state": "above_horizon",
                "attributes": {"azimuth": 361, "elevation": float("nan")},
            }
        ]
    )
    assert sun is not None and sun.azimuth is None and sun.elevation is None


@pytest.mark.brd("FR-CFG-1")
def test_discover_handles_empty_and_garbage() -> None:
    assert discovery.discover([]).rooms == []
    d = discovery.discover([{"entity_id": "sensor.x"}, {"state": "1"}, {"entity_id": "light.a"}])
    assert d.rooms == [] and d.blower == []


# ---- units -------------------------------------------------------------------------------
@pytest.mark.brd("FR-CFG-1")
def test_units_known_points() -> None:
    assert f_to_c(32) == 0 and f_to_c(212) == 100 and f_to_c(68) == 20
    assert c_to_f(20) == 68 and c_to_f(21.5) == 70.5 and c_to_f(22.2) == 72.0
    assert to_display(70, "F") == 70 and from_display(70, "F") == 70


@pytest.mark.brd("FR-CFG-1")
def test_units_round_trip_every_half_degree() -> None:
    """Every stored 0.5 °F value from 40 to 100 survives F -> C (display) -> F unchanged."""
    for i in range(80, 201):
        f = i / 2
        assert from_display(to_display(f, "C"), "C") == f, f


@pytest.mark.brd("FR-CFG-1")
def test_units_display_only_core_stays_f() -> None:
    s = Settings.model_validate({**house_dict(), "display": {"units": "C"}})
    assert s.bands.heat.home == (68, 71)  # stored in °F regardless
    secs = {x.name: x for x in forms.sections(s, owner=True)}
    f = {x.path: x for x in secs["bands"].fields}
    assert f["bands.heat.home.0"].kind == "temp" and f["bands.heat.home.0"].value == 20.0
    assert f["bands.heat.home.0"].f_value == 68
    tol = {x.path: x for x in secs["rooms"].fields}["rooms.bedroom_1.tol"]
    assert tol.kind == "delta" and tol.value == round(2 * 5 / 9, 1)


# ---- settings schema ---------------------------------------------------------------------
@pytest.mark.brd("FR-CFG-1")
def test_pre_v030_row_loads_unchanged_with_no_builtin_sensors() -> None:
    """A row written by v0.1.11-v0.2.0 (no sensors/display key) loads with the same effective
    values. v0.6.1: no built-in sensor ids; it reads none (sensors_missing) until they are
    saved or seeded from THERMABOT_SENSOR_* (app.main.migrate_env_sensors)."""
    old = Settings.model_validate(house_dict()).model_dump(mode="json")
    old.pop("sensors")
    old.pop("display")
    s = Settings.model_validate_json(json.dumps(old))
    assert s.sensors.model_dump() == {"blower": "", "compressor": "", "outdoor": "", "humidity": ""}
    assert s.sensors_missing
    assert s.display.units == "F"
    for k in old:
        assert s.model_dump(mode="json")[k] == old[k], k


@pytest.mark.brd("FR-CFG-1")
def test_fresh_install_has_no_sensors() -> None:
    s = Settings()
    assert not s.thermostat.configured
    assert s.sensors.model_dump() == {"blower": "", "compressor": "", "outdoor": "", "humidity": ""}


@pytest.mark.brd("FR-CFG-1")
def test_saved_sensors_are_kept_including_empty() -> None:
    d = {**house_dict(), "sensors": {"outdoor": "sensor.outside_temp"}}
    s = Settings.model_validate(d)
    assert s.sensors.outdoor == "sensor.outside_temp" and s.sensors.blower == ""
    s2 = Settings.model_validate_json(s.model_dump_json())
    assert s2 == s


def test_room_floor_mapping_is_optional_and_validated() -> None:
    s = Settings.model_validate(
        {
            **house_dict(),
            "rooms": {
                **house_dict()["rooms"],
                "study": {"temp": "sensor.study_temperature", "floor": "Second"},
            },
        }
    )
    assert s.rooms["study"].floor == "Second"
    with pytest.raises(ValueError):
        Settings.model_validate(
            {
                **house_dict(),
                "rooms": {
                    **house_dict()["rooms"],
                    "study": {"temp": "sensor.study_temperature", "floor": "Upper/Attic"},
                },
            }
        )


def test_opening_mappings_require_known_rooms_and_garage_entry_contacts() -> None:
    room = next(iter(house_dict()["rooms"]))
    base = {
        **house_dict(),
        "openings": {
            "enabled": True,
            "sensors": {
                "binary_sensor.garage_door": {
                    "role": "garage_exterior",
                    "affected_rooms": [room],
                    "required_openings": ["binary_sensor.garage_entry"],
                },
                "binary_sensor.garage_entry": {
                    "role": "garage_to_house",
                    "affected_rooms": [room],
                },
            },
        },
    }
    assert Settings.model_validate(base).openings.enabled

    bad_room = json.loads(json.dumps(base))
    bad_room["openings"]["sensors"]["binary_sensor.garage_entry"]["affected_rooms"] = ["not_a_room"]
    with pytest.raises(ValueError, match="unknown affected rooms"):
        Settings.model_validate(bad_room)

    missing_link = json.loads(json.dumps(base))
    missing_link["openings"]["sensors"].pop("binary_sensor.garage_entry")
    with pytest.raises(ValueError, match="must be a mapped garage_to_house contact"):
        Settings.model_validate(missing_link)


@pytest.mark.brd("FR-CFG-1")
@pytest.mark.parametrize("bad", ["Sensor.X", "sensor", "sensor.a b", "sensor.a;drop"])
def test_sensor_ids_validated(bad: str) -> None:
    with pytest.raises(ValueError):
        Settings.model_validate({**house_dict(), "sensors": {"outdoor": bad}})


@pytest.mark.brd("FR-CMF-1")
@pytest.mark.parametrize(
    ("block", "heat", "cool", "msg"),
    [
        ("home", (70, 74), (73, 76), "overlap"),  # heat.high > cool.low
        ("sleep", (70, 71), (71, 72), "deadband"),  # cool.high - heat.low = 2 < 3
    ],
)
def test_band_rules(block: str, heat: tuple[int, int], cool: tuple[int, int], msg: str) -> None:
    d = house_dict()
    base = Settings().bands.model_dump()
    base["heat"][block], base["cool"][block] = list(heat), list(cool)
    with pytest.raises(ValueError, match=msg):
        Settings.model_validate({**d, "bands": base})


@pytest.mark.brd("FR-CFG-1")
def test_forms_cover_every_leaf_and_lock_owner_sections() -> None:
    s = Settings.model_validate(house_dict())
    secs = forms.sections(s, owner=False)
    assert [x.name for x in secs] == list(forms.ORDER)
    assert set(forms.ORDER) == set(Settings.model_fields) - {"openings"}
    locked = {x.name for x in secs if x.locked}
    assert locked == {"thermostat", "safety", "off"}
    paths = {f.path for x in secs for f in x.fields}
    for p in (
        "thermostat.entity_id",
        "bands.cool.away.1",
        "schedule.weekday.0.start",
        "schedule.weekday.1.block",
        "rooms.study.temp",
        "weights.home.study",
        "presence.people.alex.life360",
        "presence.excluded",
        "override.choices",
        "sensors.outdoor",
        "display.units",
        "summary.enabled",
    ):
        assert p in paths, p
    kinds = {f.path: f.kind for x in secs for f in x.fields}
    assert kinds["schedule.weekday.0.block"] == "select"
    assert kinds["summary.enabled"] == "bool"
    assert kinds["display.units"] == "select"
    assert kinds["writes.max_per_day"] == "int"
    assert all(x.locked is False for x in forms.sections(s, owner=True))
