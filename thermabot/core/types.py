"""Pure data shapes shared by core, adapters and API (D§1.2, D§11.2). No I/O."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

HvacMode = Literal["off", "heat", "cool", "heat_cool"]
FanMode = Literal["auto", "on"]
Block = Literal["home", "sleep", "away"]
Season = Literal["heat", "cool", "shoulder"]


class HouseState(StrEnum):
    OCCUPIED = "OCCUPIED"
    EMPTY = "EMPTY"


class PersonState(StrEnum):
    HOME = "HOME"
    AWAY = "AWAY"


class RoomReading(BaseModel):
    key: str
    temp: float | None
    humidity: float | None = None
    last_reported: datetime | None = None
    age_min: float | None = None
    stale: bool = False
    estimated: bool = False
    score: float | None = None
    aux_flag: bool = False


class ForecastPoint(BaseModel):
    """One hourly forecast point. temp_f is always °F. v0.4.0: optional extras (None when the
    provider doesn't give them)."""

    t: datetime
    temp_f: float
    humidity: float | None = None  # relative humidity, %
    conditions: str | None = None  # short text, e.g. "Partly cloudy"
    wind_mph: float | None = None
    cloud_pct: float | None = None


ForecastSource = Literal["ha", "nws", "open_meteo"]


class Forecast(BaseModel):
    source: ForecastSource
    fetched_at: datetime
    points: list[ForecastPoint] = Field(default_factory=list)
    # v0.4.0: True when an adapter serves its last good forecast because a fetch failed.
    # Staleness for planning is still decided by core.weather.is_stale (fetched_at, coverage).
    stale: bool = False


class ThermostatState(BaseModel):
    hvac_mode: str
    hvac_modes: list[str] = Field(default_factory=list)
    hvac_action: str | None = None
    current_temp: float | None = None
    heat_sp: float | None = None
    cool_sp: float | None = None
    min_temp: float | None = None
    max_temp: float | None = None
    target_temp_step: float | None = None
    temperature_unit: str | None = None
    fan_mode: str | None = None
    preset_mode: str | None = None
    humidity: float | None = None
    outdoor_temp: float | None = None
    last_reported: datetime | None = None

    @field_validator("hvac_mode", "hvac_action", "fan_mode", "preset_mode", mode="before")
    @classmethod
    def _lower(cls, v: object) -> object:
        """HA/Trane casing varies ('Auto', 'None'); every compare in the bot is on lowercase
        (v0.1.10: a case-sensitive fan compare dry-wrote set_fan_mode every minute)."""
        return v.lower() if isinstance(v, str) else v


class PersonPresence(BaseModel):
    state: PersonState
    source: Literal["life360", "companion", "unknown"]
    age_min: float | None = None
    away_since: datetime | None = None


class Presence(BaseModel):
    house: HouseState
    since: datetime
    people: dict[str, PersonPresence] = Field(default_factory=dict)
    guest_until: datetime | None = None


class Snapshot(BaseModel):
    """Everything core needs for one tick, built by adapters (D§1.3 step 1)."""

    t: datetime
    thermostat: ThermostatState
    rooms: dict[str, RoomReading] = Field(default_factory=dict)
    presence: Presence | None = None
    forecast: Forecast | None = None
    # last_changed of input_datetime.thermabot_watchdog_last (read-only; D§13). None = unknown.
    watchdog_last: datetime | None = None


class PlanStep(BaseModel):
    t: datetime
    hvac_mode: HvacMode
    heat_sp: float | None = None
    cool_sp: float | None = None
    fan_mode: FanMode = "auto"
    pred_t: float | None = None
    pred_rooms: dict[str, float] = Field(default_factory=dict)
    pred_u: float | None = None
    score: float | None = None
    reason: str = ""


class Plan(BaseModel):
    kind: Literal["optimised", "fallback"]
    created_at: datetime
    reason_banner: str = ""
    steps: list[PlanStep] = Field(default_factory=list)


class Action(BaseModel):
    """One HA service call against the single writable entity (D§6)."""

    service: Literal["set_hvac_mode", "set_temperature", "set_fan_mode", "set_preset_mode"]
    entity_id: str
    data: dict[str, float | str | None] = Field(default_factory=dict)
    safety_restore: bool = False


class Decision(BaseModel):
    t: datetime
    action: Action
    allowed: bool
    reason: str
    would_write: bool = False
    guard: str | None = None  # first guard that denied (or 'shadow'), None when allowed


class AlertEvent(BaseModel):
    key: str
    severity: Literal["info", "warn", "critical"]
    text: str
    t: datetime
    clear: bool = False


# --- JSON API models (D§11.2). Backend owns the schema. ---------------------------------


class HealthzOut(BaseModel):
    ok: bool
    state: str
    ha: str
    weather: str
    last_tick: datetime | None
    version: str


class OverrideOut(BaseModel):
    id: int
    kind: str
    started_at: datetime
    expires_at: datetime
    command_status: (
        Literal["pending", "sending", "applied", "failed", "blocked", "matched", "unconfirmed"]
        | None
    ) = None
    command_detail: str | None = None


class StatusOut(BaseModel):
    state: str
    label: str
    ha_health: str = "connecting"
    kill_switch: Literal["on", "off"] | None
    run_mode: Literal["shadow", "active"] | None
    shadow_days: float
    override: OverrideOut | None = None
    snapshot: ThermostatState | None = None
    score: float | None = None
    rooms: list[RoomReading] = Field(default_factory=list)
    next_change: str | None = None


class SavingsOut(BaseModel):
    saved_h: float | None = None
    ci_h: float | None = None
    pct: float | None = None
    counted: bool = False


class HistoryOut(BaseModel):
    days: list[dict[str, object]]
    savings: SavingsOut


class KillIn(BaseModel):
    enabled: bool
    restore: Literal["band", "leave"] = "band"
    mode: Literal["shadow", "active"] | None = None


class KillOut(BaseModel):
    state: str
    kill_switch: Literal["on", "off"]
    run_mode: Literal["shadow", "active"]
    restore: Literal["sent", "failed", "not_needed", "skipped"] | None = None
    mirror_ok: bool


class OverrideIn(BaseModel):
    target_f: float | None = Field(default=None, ge=60, le=82, allow_inf_nan=False)
    mode: HvacMode | None = None
    pause: bool = False
    duration: Literal["1h", "2h", "4h", "next_block", "until"] = "2h"
    until: datetime | None = None


class NudgeIn(BaseModel):
    room: str | None = None
    delta_f: int
    hours: float


class NudgeOut(BaseModel):
    id: int
    room: str
    delta_f: float
    expires_at: datetime


class GuestIn(BaseModel):
    on: bool
    until: datetime | None = None


class UserOut(BaseModel):
    id: int
    username: str
    role: Literal["owner", "admin", "member"]
    room: str | None = None


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    role: Literal["owner", "admin", "member"]
    password: str = Field(min_length=8)
    room: str | None = None


class UserPatch(BaseModel):
    role: Literal["owner", "admin", "member"] | None = None
    room: str | None = None
    password: str | None = Field(default=None, min_length=8)


class OkOut(BaseModel):
    ok: bool = True
    detail: str = ""
