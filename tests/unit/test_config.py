import pytest
from pydantic import ValidationError

from tests.house import HOUSE
from thermabot.config import Env, OffCfg, SafetyCfg, Settings, WritesCfg
from thermabot.core import guards


@pytest.mark.brd("FR-CMF-1")
def test_band_defaults() -> None:
    s = Settings()
    assert s.bands.heat.home == (68, 71)
    assert s.bands.heat.sleep == (66, 69)
    assert s.bands.heat.away == (62, 66)
    assert s.bands.cool.home == (73, 76)
    assert s.bands.cool.sleep == (71, 74)
    assert s.bands.cool.away == (78, 80)


@pytest.mark.brd("FR-CMF-2")
def test_schedule_defaults() -> None:
    s = Settings()
    assert [(b.start, b.block) for b in s.schedule.weekday] == [
        ("06:30", "home"),
        ("22:00", "sleep"),
    ]
    assert [(b.start, b.block) for b in s.schedule.weekend] == [
        ("08:00", "home"),
        ("22:00", "sleep"),
    ]


@pytest.mark.brd("FR-CMF-3")
def test_room_and_weight_defaults() -> None:
    s = HOUSE
    assert s.rooms["bedroom_1"].tol == 2
    assert s.rooms["bedroom_1"].stale_min == 90
    assert s.rooms["den"].tol == 3
    assert s.weights["sleep"] == {"bedroom_1": 0.4, "bedroom_2": 0.4, "thermostat": 0.2}
    assert s.weights["home"]["thermostat"] == 0.4
    assert s.comfort.target_score == 80


@pytest.mark.brd("NFR-SAFE-1")
def test_safety_defaults() -> None:
    s = Settings()
    assert (s.safety.floor_tstat_f, s.safety.floor_room_f) == (62, 60)
    assert (s.safety.ceil_tstat_f, s.safety.ceil_room_f) == (80, 82)
    assert (s.off.outdoor_min_f, s.off.outdoor_max_f, s.off.reevaluate_h) == (50, 78, 8)
    assert s.writes.max_per_day == 30
    assert s.presence.excluded == []


@pytest.mark.brd("FR-CMF-5")
def test_hard_limit_constants() -> None:
    assert guards.HEAT_SP_MIN == 60
    assert guards.COOL_SP_MIN == 68
    assert guards.INDOOR_MAX == 82
    assert guards.COOL_SP_MAX == 80
    assert not hasattr(guards, "WRITABLE_ENTITY")  # v0.2.0: no house entity in code
    assert guards.PRECOND_MAX_BEYOND_BAND == 2
    assert (guards.OFF_ABS_CAP_H, guards.OFF_FORCED_ON_H) == (24, 1)


@pytest.mark.brd("NFR-SAFE-1")
def test_floor_loosen_raises() -> None:
    with pytest.raises(ValidationError):
        SafetyCfg(floor_tstat_f=61)


@pytest.mark.brd("NFR-SAFE-1")
def test_floor_tighten_ok() -> None:
    assert SafetyCfg(floor_tstat_f=63).floor_tstat_f == 63


@pytest.mark.brd("NFR-SAFE-1")
@pytest.mark.parametrize(
    "cfg, kw",
    [
        (SafetyCfg, {"ceil_tstat_f": 81}),
        (SafetyCfg, {"floor_room_f": 59}),
        (SafetyCfg, {"ceil_room_f": 83}),
        (OffCfg, {"outdoor_min_f": 49}),
        (OffCfg, {"outdoor_max_f": 79}),
        (OffCfg, {"reevaluate_h": 9}),
        (WritesCfg, {"max_per_day": 31}),
        (WritesCfg, {"min_setpoint_gap_min": 5}),
        (WritesCfg, {"deadband_f": 2}),
    ],
)
def test_loosening_raises(cfg: type, kw: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        cfg(**kw)


@pytest.mark.brd("D§11.2")
def test_settings_rejects_bad_values() -> None:
    d = Settings().model_dump()
    d["bands"]["heat"]["home"] = (71, 68)
    with pytest.raises(ValidationError):
        Settings.model_validate(d)
    d = Settings().model_dump()
    d["weights"]["home"]["study"] = -0.1
    with pytest.raises(ValidationError):
        Settings.model_validate(d)


@pytest.mark.brd("D§14.3")
def test_settings_json_round_trip() -> None:
    s = Settings()
    assert Settings.model_validate_json(s.model_dump_json()) == s


@pytest.mark.brd("D§14.1")
def test_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_TOKEN", "fake-token")
    e = Env()
    assert e.HA_URL == "http://homeassistant.local:8123"
    assert e.PORT == 8095
    assert e.HA_TOKEN.get_secret_value() == "fake-token"
    assert "fake-token" not in repr(e)


@pytest.mark.brd("FR-CMF-1")
def test_brd_default_bands_pass_both_band_rules() -> None:
    Settings.model_validate(Settings().model_dump())


@pytest.mark.brd("FR-CMF-1")
def test_band_overlap_rejected() -> None:
    d = Settings().model_dump()
    d["bands"]["heat"]["home"] = (68, 74)  # heat.high 74 > cool.low 73
    with pytest.raises(ValidationError, match="overlap"):
        Settings.model_validate(d)


@pytest.mark.brd("FR-CMF-1")
def test_band_touching_allowed() -> None:
    d = Settings().model_dump()
    d["bands"]["heat"]["home"] = (68, 73)  # heat.high == cool.low is not overlap
    Settings.model_validate(d)


@pytest.mark.brd("FR-CMF-1")
def test_band_span_below_deadband_rejected() -> None:
    d = Settings().model_dump()
    d["bands"]["heat"]["home"] = (70, 71)
    d["bands"]["cool"]["home"] = (71, 72)  # cool.high 72 - heat.low 70 = 2 < 3
    with pytest.raises(ValidationError, match="deadband"):
        Settings.model_validate(d)
