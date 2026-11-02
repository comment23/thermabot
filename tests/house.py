"""Generic test house (v0.2.0): the code defaults are 'not configured', so tests that need a
configured bot use these settings. Fake entities only; no real house data."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from thermabot.config import Settings

TSTAT = "climate.living_room"
BLOWER = "binary_sensor.living_room_blower_active"
COMPRESSOR = "sensor.living_room_compressor_speed"
OUTDOOR = "sensor.outdoor_temperature"
SENSORS = {"blower": BLOWER, "compressor": COMPRESSOR, "outdoor": OUTDOOR}


def house_dict() -> dict[str, Any]:
    return {
        "thermostat": {"entity_id": TSTAT},
        "sensors": dict(SENSORS),  # v0.6.1: no built-in sensor ids any more
        "rooms": {
            "thermostat": {"temp": TSTAT, "stale_min": 10, "dead_min": 10},
            "study": {"temp": "sensor.study_temperature"},
            "bedroom_1": {
                "temp": "sensor.bedroom_1_temperature",
                "tol": 2,
                "stale_min": 90,
                "aux_heat_detect": True,
            },
            "bedroom_2": {"temp": "sensor.bedroom_2_temperature", "tol": 2},
            "den": {"temp": "sensor.den_temperature", "tol": 3},
        },
        "weights": {
            "sleep": {"bedroom_1": 0.4, "bedroom_2": 0.4, "thermostat": 0.2},
            "home": {"thermostat": 0.4, "study": 0.3, "bedroom_1": 0.15, "bedroom_2": 0.15},
            "away": {"thermostat": 1.0},
        },
        "presence": {
            "people": {
                "alex": {
                    "life360": "device_tracker.life360_alex",
                    "companion": "device_tracker.alex_phone",
                },
                "sam": {
                    "life360": "device_tracker.life360_sam",
                    "companion": "device_tracker.sam_phone",
                },
                "jordan": {"life360": "device_tracker.life360_jordan"},
            },
            "excluded": ["casey", "riley"],
        },
    }


def house_settings(**overrides: Any) -> Settings:
    return Settings.model_validate({**house_dict(), **overrides})


HOUSE = house_settings()


def seed_house(data_dir: Path, settings: Settings | None = None) -> None:
    """Write a `settings` row (as a configured install has) before build()/create_app()."""
    from thermabot.adapters import db

    data_dir.mkdir(parents=True, exist_ok=True)
    conn: sqlite3.Connection = db.open_db(data_dir / "thermabot.db", now=datetime.now(UTC))
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by)"
        " VALUES (?,?,?,?)",
        (
            "settings",
            json.dumps((settings or HOUSE).model_dump(mode="json")),
            "2026-10-01T00:00:00+00:00",
            "test",
        ),
    )
    conn.close()
