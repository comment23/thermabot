"""v0.4.0 weather settings: provider default, legacy rows keep NWS, lat/lon validation."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from tests.house import house_dict
from thermabot.config import Settings


@pytest.mark.brd("FR-WX-2")
def test_fresh_install_defaults_to_open_meteo() -> None:
    s = Settings()
    assert s.weather.provider == "open_meteo"
    assert s.weather.lat is None and s.weather.lon is None and s.weather.units == "auto"


@pytest.mark.brd("FR-WX-2")
def test_legacy_row_without_weather_keeps_nws() -> None:
    d = house_dict()
    assert "weather" not in d
    assert Settings.model_validate(d).weather.provider == "nws"


@pytest.mark.brd("FR-WX-2")
def test_legacy_row_with_weather_but_no_provider_keeps_nws_and_values() -> None:
    d = {**house_dict(), "weather": {"stale_h": 2, "min_cover_h": 20, "bias_correct_above_f": 1.5}}
    w = Settings.model_validate(d).weather
    assert (w.provider, w.stale_h, w.min_cover_h, w.bias_correct_above_f) == ("nws", 2, 20, 1.5)
    assert w.lat is None and w.lon is None


@pytest.mark.brd("FR-WX-2")
def test_legacy_row_round_trips_identically_apart_from_new_keys() -> None:
    """A v0.3.0 row (json dump) loads; old keys unchanged; only new weather keys appear."""
    old = json.loads(json.dumps({**house_dict(), "weather": {"stale_h": 3.0}}))
    new = Settings.model_validate(old).model_dump(mode="json")
    assert new["weather"]["stale_h"] == 3.0
    added = set(new["weather"]) - {"stale_h", "min_cover_h", "bias_correct_above_f"}
    assert added == {"provider", "lat", "lon", "units"}
    assert (
        new["thermostat"] == old["thermostat"]
        and new["rooms"]["study"]["temp"] == (old["rooms"]["study"]["temp"])
    )


@pytest.mark.brd("FR-WX-2")
def test_explicit_provider_kept() -> None:
    d = {**house_dict(), "weather": {"provider": "open_meteo", "lat": 51.5, "lon": -0.12}}
    w = Settings.model_validate(d).weather
    assert (w.provider, w.lat, w.lon) == ("open_meteo", 51.5, -0.12)


@pytest.mark.brd("FR-WX-2")
@pytest.mark.parametrize(
    "w",
    [
        {"lat": 91, "lon": 0},
        {"lat": -91, "lon": 0},
        {"lat": 0, "lon": 181},
        {"lat": 0, "lon": -180.5},
        {"lat": 40},
        {"lon": -75},
        {"provider": "darksky"},
    ],
)
def test_bad_weather_settings_rejected(w: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"weather": w})


@pytest.mark.brd("FR-WX-2")
def test_saved_v040_row_reloads_unchanged() -> None:
    s = Settings.model_validate({**house_dict(), "weather": {"provider": "nws"}})
    again = Settings.model_validate_json(s.model_dump_json())
    assert again == s and again.weather.provider == "nws"
