"""Config (D§14): env Settings (pydantic-settings) + tunable Settings with BRD defaults.

Safety, off and writes fields are tighten-only: validators reject any value looser than
the BRD default (D§14.3 last paragraph).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from thermabot.core.limits import COOL_SP_MAX, HEAT_SP_MIN, INDOOR_MAX

BlockName = Literal["home", "sleep", "away"]


class Env(BaseSettings):
    """Deployment + secrets from environment (D§14.1)."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    HA_URL: str = "http://homeassistant.local:8123"
    HA_TOKEN: SecretStr
    INFLUX_URL: str = "http://influxdb:8086"
    INFLUX_ORG: str = "home"
    INFLUX_READ_TOKEN: SecretStr | None = None
    INFLUX_WRITE_TOKEN: SecretStr | None = None
    INFLUX_READ_BUCKET: str = "ha_hvac"
    INFLUX_WRITE_BUCKET: str = "thermabot"
    ALERT_WEBHOOK_URL: str | None = None
    # HA webhook automation thermabot_heartbeat (no auth; the HA watchdog reads last_triggered)
    HEARTBEAT_WEBHOOK_URL: str | None = None
    ALERT_WEBHOOK_TOKEN: SecretStr | None = None
    BOB_TOKEN: SecretStr | None = None
    TZ: str = "America/New_York"
    DATA_DIR: str = "/data"
    PORT: int = 8095
    LOG_LEVEL: str = "INFO"
    # v0.6.1: one-time seed for a configured settings row that has no `sensors` section yet
    # (written before v0.3.0). Applied once at startup; after that the row is the source of
    # truth and these are ignored. Empty = not set.
    THERMABOT_SENSOR_BLOWER: str = ""
    THERMABOT_SENSOR_COMPRESSOR: str = ""
    THERMABOT_SENSOR_OUTDOOR: str = ""


Range = tuple[float, float]


class SeasonBands(BaseModel):
    home: Range
    sleep: Range
    away: Range

    @model_validator(mode="after")
    def _not_inverted(self) -> SeasonBands:
        for name in ("home", "sleep", "away"):
            lo, hi = getattr(self, name)
            if lo > hi:
                raise ValueError(f"{name}: band inverted ({lo} > {hi})")
        return self


class Bands(BaseModel):
    """FR-CMF-1 (°F)."""

    heat: SeasonBands = SeasonBands(home=(68, 71), sleep=(66, 69), away=(62, 66))
    cool: SeasonBands = SeasonBands(home=(73, 76), sleep=(71, 74), away=(78, 80))


class SeasonCfg(BaseModel):
    heat_below_f: float = 60
    cool_above_f: float = 68


class BlockStart(BaseModel):
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    block: BlockName


class ScheduleCfg(BaseModel):
    """FR-CMF-2, local time. Weekend = Sat, Sun."""

    weekday: list[BlockStart] = [
        BlockStart(start="06:30", block="home"),
        BlockStart(start="22:00", block="sleep"),
    ]
    weekend: list[BlockStart] = [
        BlockStart(start="08:00", block="home"),
        BlockStart(start="22:00", block="sleep"),
    ]

    @field_validator("weekday", "weekend")
    @classmethod
    def _sorted_unique(cls, v: list[BlockStart]) -> list[BlockStart]:
        starts = [b.start for b in v]
        if not v or starts != sorted(set(starts)):
            raise ValueError("schedule blocks must be non-empty, sorted and non-overlapping")
        return v


class RoomCfg(BaseModel):
    temp: str
    floor: str = Field(default="", max_length=64, pattern=r"^[a-zA-Z0-9 _-]*$")
    tol: float = Field(default=0, ge=0)
    stale_min: float = 60
    dead_min: float = 360
    aux_heat_detect: bool = False

    @model_validator(mode="after")
    def _dead_after_stale(self) -> RoomCfg:
        if self.dead_min < self.stale_min:
            raise ValueError("dead_min must be >= stale_min")
        return self


class ThermostatCfg(BaseModel):
    """FR-CFG-1. The one writable climate entity (Owner: safety section).

    v0.2.0: empty by default = not configured (setup needed). Every write layer (core guard,
    controller, HA adapter) denies all writes while it is empty."""

    entity_id: str = Field(default="", pattern=r"^(climate\.[a-z0-9_]+)?$")

    @property
    def configured(self) -> bool:
        return bool(self.entity_id)


def _default_rooms() -> dict[str, RoomCfg]:
    # v0.2.0: generic. Only the thermostat itself (read from settings.thermostat); add rooms
    # with their temperature sensors in Settings.
    return {"thermostat": RoomCfg(temp="", stale_min=10, dead_min=10)}


def _default_weights() -> dict[str, dict[str, float]]:
    return {
        "sleep": {"thermostat": 1.0},
        "home": {"thermostat": 1.0},
        "away": {"thermostat": 1.0},
    }


class ComfortCfg(BaseModel):
    target_score: float = 80
    falloff_f: float = Field(default=3, gt=0)
    violation_min: float = 30
    floor_margin: float = 40
    humid_rh: float = 60
    humid_tighten_f: float = 1
    room_dead_alert_h: float = 12


class AuxHeatCfg(BaseModel):
    above_expected_f: float = 3
    rise_f: float = 2
    rise_window_min: float = 30
    clear_below_f: float = 1.5
    clear_min: float = 60
    weight: float = Field(default=0.1, ge=0)


class NudgeCfg(BaseModel):
    max_delta_f: float = Field(default=2, le=2)
    max_hours: float = Field(default=4, le=4)
    per_day: int = Field(default=3, le=3)
    weight_boost: float = 1.5


OverrideDuration = Literal["1h", "2h", "4h", "next_block", "until"]


class OverrideCfg(BaseModel):
    default: OverrideDuration = "2h"
    choices: list[OverrideDuration] = ["1h", "2h", "4h", "next_block", "until"]
    external_until: OverrideDuration = "next_block"
    notify: bool = False


def _tighten(name: str, v: float, brd: float, direction: Literal["min", "max"]) -> float:
    """Reject values looser than the BRD value. 'min': v must be >= brd; 'max': v <= brd."""
    if (direction == "min" and v < brd) or (direction == "max" and v > brd):
        op = ">=" if direction == "min" else "<="
        raise ValueError(f"{name}={v} loosens BRD limit; must be {op} {brd}")
    return v


class OffCfg(BaseModel):
    """NFR-SAFE-1/2 (Owner only). Tighten = narrower outdoor window, sooner re-evaluation."""

    outdoor_min_f: float = 50
    outdoor_max_f: float = 78
    reevaluate_h: float = 8

    @model_validator(mode="after")
    def _tighten_only(self) -> OffCfg:
        _tighten("outdoor_min_f", self.outdoor_min_f, 50, "min")
        _tighten("outdoor_max_f", self.outdoor_max_f, 78, "max")
        _tighten("reevaluate_h", self.reevaluate_h, 8, "max")
        if self.outdoor_min_f > self.outdoor_max_f:
            raise ValueError("off: outdoor_min_f > outdoor_max_f")
        return self


class SafetyCfg(BaseModel):
    floor_tstat_f: float = 62
    floor_room_f: float = 60
    ceil_tstat_f: float = 80
    ceil_room_f: float = 82
    hold_after_restore_min: float = 60

    @model_validator(mode="after")
    def _tighten_only(self) -> SafetyCfg:
        _tighten("floor_tstat_f", self.floor_tstat_f, 62, "min")
        _tighten("floor_room_f", self.floor_room_f, max(60, HEAT_SP_MIN), "min")
        _tighten("ceil_tstat_f", self.ceil_tstat_f, min(80, COOL_SP_MAX), "max")
        _tighten("ceil_room_f", self.ceil_room_f, min(82, INDOOR_MAX), "max")
        _tighten("hold_after_restore_min", self.hold_after_restore_min, 60, "min")
        if self.floor_tstat_f >= self.ceil_tstat_f:
            raise ValueError("safety: floor_tstat_f must be < ceil_tstat_f")
        return self


class WritesCfg(BaseModel):
    min_setpoint_gap_min: float = 10
    min_mode_gap_min: float = 15
    max_per_day: int = 30
    max_heat_cool_flips: int = 2
    deadband_f: float = 3
    readback_timeout_s: float = 120

    @model_validator(mode="after")
    def _tighten_only(self) -> WritesCfg:
        _tighten("min_setpoint_gap_min", self.min_setpoint_gap_min, 10, "min")
        _tighten("min_mode_gap_min", self.min_mode_gap_min, 15, "min")
        _tighten("max_per_day", self.max_per_day, 30, "max")
        _tighten("max_heat_cool_flips", self.max_heat_cool_flips, 2, "max")
        _tighten("deadband_f", self.deadband_f, 3, "min")
        _tighten("readback_timeout_s", self.readback_timeout_s, 120, "max")
        return self


class FanCfg(BaseModel):
    max_min_per_hour: float = 30
    max_session_h: float = 2
    gap_trigger_f: float = 3
    min_proven_gain_f: float = 0.5


class PlannerCfg(BaseModel):
    horizon_h: float = 48
    step_min: int = 15
    replan_min: int = 60
    mae_ok_tstat_f: float = 1.0
    mae_ok_room_f: float = 2.0
    mode_change_penalty_min: float = 5
    fan_cost_weight: float = 0.05
    # v0.1.9 (design decision): with no confident model, 'thermostat' holds
    # the thermostat's current manual setpoints when valid, else the band edges; 'bands' =
    # band edges only (pre-0.1.9 behaviour).
    fallback_source: Literal["thermostat", "bands"] = "thermostat"


WeatherProviderName = Literal["nws", "open_meteo"]


class WeatherCfg(BaseModel):
    """v0.4.0: pluggable provider. `nws` = US National Weather Service; `open_meteo` = global,
    keyless (the default for new installs). A configured row from before v0.4.0 has no
    `provider` and keeps NWS (see Settings._legacy_defaults). lat/lon empty = use Home
    Assistant's home location (zone.home), as before. `units` only affects how the Forecast
    page shows temperatures (auto = display.units); provider data and the core stay in °F."""

    provider: WeatherProviderName = "open_meteo"
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    units: Literal["auto", "F", "C"] = "auto"
    stale_h: float = 3
    min_cover_h: float = 24
    bias_correct_above_f: float = 1

    @model_validator(mode="after")
    def _lat_lon_pair(self) -> WeatherCfg:
        if (self.lat is None) != (self.lon is None):
            raise ValueError("weather: set both lat and lon, or neither (= HA home location)")
        return self


class PersonCfg(BaseModel):
    life360: str | None = None
    companion: str | None = None


class PresenceCfg(BaseModel):
    # v0.2.0: generic. No people configured = the house always counts as OCCUPIED.
    people: dict[str, PersonCfg] = Field(default_factory=dict)
    excluded: list[str] = []
    empty_after_min: float = 30
    life360_stale_min: float = 30
    companion_stale_min: float = 120

    @model_validator(mode="after")
    def _excluded_not_read(self) -> PresenceCfg:
        both = set(self.people) & set(self.excluded)
        if both:
            raise ValueError(f"excluded people must not be configured: {sorted(both)}")
        return self


_ENTITY = r"^([a-z_]+\.[a-z0-9_]+)?$"


class SensorsCfg(BaseModel):
    """v0.3.0 (setup wizard): optional HVAC sensors, read only. Empty = not read.
    blower: binary_sensor/sensor (on/off or 0/1); compressor: speed sensor (%);
    outdoor: temperature sensor (°F); humidity: indoor RH sensor that replaces the
    thermostat's own current_humidity when set."""

    blower: str = Field(default="", pattern=_ENTITY)
    compressor: str = Field(default="", pattern=_ENTITY)
    outdoor: str = Field(default="", pattern=_ENTITY)
    humidity: str = Field(default="", pattern=_ENTITY)


class OpeningSensorCfg(BaseModel):
    role: Literal["unknown", "exterior", "garage_exterior", "garage_to_house"] = "unknown"
    affected_rooms: list[str] = Field(default_factory=list)
    required_openings: list[str] = Field(default_factory=list)
    active_state: Literal["on", "off"] = "on"


class OpeningPolicyCfg(BaseModel):
    """Read-only SHADOW assessment for explicitly mapped door contacts."""

    enabled: bool = False
    min_open_min: float = Field(default=10, ge=1, le=1440)
    recovery_min: float = Field(default=5, ge=0, le=120)
    # 0 disables age-based staleness because many contact sensors are event-driven.
    stale_after_min: float = Field(default=0, ge=0, le=1440)
    sensors: dict[str, OpeningSensorCfg] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _opening_links(self) -> OpeningPolicyCfg:
        for entity_id, cfg in self.sensors.items():
            if not re.fullmatch(r"binary_sensor\.[a-z0-9_]+", entity_id):
                raise ValueError(f"openings.sensors.{entity_id}: expected a binary_sensor entity")
            if len(cfg.affected_rooms) != len(set(cfg.affected_rooms)):
                raise ValueError(f"openings.sensors.{entity_id}: duplicate affected room")
            if len(cfg.required_openings) != len(set(cfg.required_openings)):
                raise ValueError(f"openings.sensors.{entity_id}: duplicate required opening")
            if cfg.role == "garage_exterior":
                if not cfg.required_openings:
                    raise ValueError(
                        f"openings.sensors.{entity_id}: garage exterior needs a house-door contact"
                    )
                for required in cfg.required_openings:
                    linked = self.sensors.get(required)
                    if linked is None or linked.role != "garage_to_house":
                        raise ValueError(
                            f"openings.sensors.{entity_id}: {required} must be a mapped "
                            "garage_to_house contact"
                        )
            elif cfg.required_openings:
                raise ValueError(
                    f"openings.sensors.{entity_id}: required openings only apply to garage doors"
                )
        return self


class DisplayCfg(BaseModel):
    """v0.3.0: UI display only. The core and the stored settings always stay in °F."""

    units: Literal["F", "C"] = "F"


class MetricsCfg(BaseModel):
    hdd_base_f: float = 65
    baseline_min_hours: float = 20


class AlertsCfg(BaseModel):
    degraded_after_min: float = 15
    remind_after_h: float = 24


class SummaryCfg(BaseModel):
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "sun"
    time: str = Field(default="18:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    enabled: bool = True


class Settings(BaseModel):
    """Tunables (D§14.3), stored in SQLite `settings`, seeded from these defaults."""

    thermostat: ThermostatCfg = Field(default_factory=ThermostatCfg)
    bands: Bands = Field(default_factory=Bands)
    season: SeasonCfg = Field(default_factory=SeasonCfg)
    schedule: ScheduleCfg = Field(default_factory=ScheduleCfg)
    rooms: dict[str, RoomCfg] = Field(default_factory=_default_rooms)
    weights: dict[str, dict[str, float]] = Field(default_factory=_default_weights)
    comfort: ComfortCfg = Field(default_factory=ComfortCfg)
    aux_heat: AuxHeatCfg = Field(default_factory=AuxHeatCfg)
    nudge: NudgeCfg = Field(default_factory=NudgeCfg)
    override: OverrideCfg = Field(default_factory=OverrideCfg)
    off: OffCfg = Field(default_factory=OffCfg)
    safety: SafetyCfg = Field(default_factory=SafetyCfg)
    writes: WritesCfg = Field(default_factory=WritesCfg)
    fan: FanCfg = Field(default_factory=FanCfg)
    planner: PlannerCfg = Field(default_factory=PlannerCfg)
    weather: WeatherCfg = Field(default_factory=WeatherCfg)
    presence: PresenceCfg = Field(default_factory=PresenceCfg)
    metrics: MetricsCfg = Field(default_factory=MetricsCfg)
    alerts: AlertsCfg = Field(default_factory=AlertsCfg)
    summary: SummaryCfg = Field(default_factory=SummaryCfg)
    sensors: SensorsCfg = Field(default_factory=SensorsCfg)  # v0.3.0
    openings: OpeningPolicyCfg = Field(default_factory=OpeningPolicyCfg)
    display: DisplayCfg = Field(default_factory=DisplayCfg)  # v0.3.0

    @model_validator(mode="before")
    @classmethod
    def _legacy_defaults(cls, data: Any) -> Any:
        """v0.6.1: there are no built-in sensor ids. A row without `sensors` reads no HVAC
        sensors until they are saved, or seeded once from THERMABOT_SENSOR_* at startup
        (app.main.migrate_env_sensors).
        v0.4.0: a configured row without `weather.provider` keeps NWS (the only provider
        before v0.4.0); a fresh install gets the Open-Meteo default."""
        if not isinstance(data, dict):
            return data
        t = data.get("thermostat")
        eid = t.get("entity_id") if isinstance(t, dict) else getattr(t, "entity_id", "")
        if not eid:
            return data
        w = data.get("weather")
        if w is None:
            data = {**data, "weather": {"provider": "nws"}}
        elif isinstance(w, dict) and "provider" not in w:
            data = {**data, "weather": {**w, "provider": "nws"}}
        return data

    @property
    def sensors_missing(self) -> bool:
        """v0.6.1: a configured thermostat with none of blower/compressor/outdoor set. The
        bot then runs DEGRADED (reason 'sensors': no plan writes) until they are saved."""
        x = self.sensors
        return self.thermostat.configured and not (x.blower or x.compressor or x.outdoor)

    @model_validator(mode="after")
    def _cross_checks(self) -> Settings:
        for block, ws in self.weights.items():
            for room, w in ws.items():
                if w < 0:
                    raise ValueError(f"weights.{block}.{room} < 0")
                if room not in self.rooms:
                    raise ValueError(f"weights.{block}.{room}: unknown room")
        for block in ("home", "sleep", "away"):
            heat_lo, heat_hi = getattr(self.bands.heat, block)
            cool_lo, cool_hi = getattr(self.bands.cool, block)
            # Owner decision 2026-10-05: heat and cool bands must not overlap.
            if heat_hi > cool_lo:
                raise ValueError(f"bands.{block}: heat.high > cool.low (overlap)")
            # Shoulder heat_cool uses heat low edge + cool high edge (D§5.2); that pair must
            # respect the thermostat deadband. (Literal D§11.2 cool.low-heat.high rejects
            # the BRD defaults: 73-71 < 3.)
            if cool_hi - heat_lo < self.writes.deadband_f:
                raise ValueError(f"bands.{block}: cool.high - heat.low < deadband")
            if heat_lo < HEAT_SP_MIN or cool_hi > COOL_SP_MAX:
                raise ValueError(f"bands.{block}: beyond hard limits")
        for entity_id, cfg in self.openings.sensors.items():
            unknown = set(cfg.affected_rooms) - set(self.rooms)
            if unknown:
                raise ValueError(
                    f"openings.sensors.{entity_id}: unknown affected rooms {sorted(unknown)}"
                )
        return self
