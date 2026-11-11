"""Comfort score (D§5.2), staleness weighting (D§5.3), space-heater detection (D§5.4). Pure."""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from datetime import datetime

from pydantic import BaseModel

from thermabot.config import AuxHeatCfg, BlockName, ComfortCfg, RoomCfg, Settings
from thermabot.core.limits import COOL_SP_MAX
from thermabot.core.types import HvacMode, Season

Band = tuple[float, float]


def block_band(s: Settings, season: Season, block: BlockName) -> Band:
    """[L, H] for the block. Shoulder: heating low edge + cooling high edge (D§5.2)."""
    if s.seasonal_control.enabled and block in ("home", "sleep"):
        return seasonal_band(s, block)
    heat: Band = getattr(s.bands.heat, block)
    cool: Band = getattr(s.bands.cool, block)
    if season == "heat":
        return heat
    if season == "cool":
        return cool
    return (heat[0], cool[1])


def hold_setpoints(band: Band, deadband: float) -> Band:
    """Keep automatic-mode setpoints at least the configured deadband apart."""
    lo, hi = band
    hi = max(hi, lo + deadband)
    if hi > COOL_SP_MAX:
        hi, lo = float(COOL_SP_MAX), COOL_SP_MAX - deadband
    return float(lo), float(hi)


def seasonal_band(s: Settings, block: BlockName) -> Band:
    if block == "away":
        return s.bands.heat.away[0], s.bands.cool.away[1]
    if block == "sleep":
        return s.seasonal_control.sleep
    return s.seasonal_control.home


def seasonal_mode(
    band: Band, indoor_f: float | None, season: Season | None, current_mode: str | None
) -> HvacMode:
    if indoor_f is not None and math.isfinite(indoor_f):
        lo, hi = band
        if indoor_f < lo:
            return "heat"
        if indoor_f > hi:
            return "cool"
        if season in ("heat", "cool"):
            return season
        if current_mode in ("heat", "cool"):
            return "heat" if current_mode == "heat" else "cool"
        return "heat" if indoor_f <= (lo + hi) / 2 else "cool"
    if season in ("heat", "cool"):
        return season
    if current_mode in ("heat", "cool"):
        return "heat" if current_mode == "heat" else "cool"
    return "heat_cool"


def room_band(
    band: Band,
    tol: float,
    nudge_f: float = 0.0,
    *,
    season: Season = "heat",
    rh: float | None = None,
    cfg: ComfortCfg | None = None,
) -> Band:
    """lo = L - tol + δ, hi = H + tol + δ; in cooling season hi drops by humid_tighten_f
    when RH > humid_rh (FR-PLN-8)."""
    cfg = cfg or ComfortCfg()
    lo = band[0] - tol + nudge_f
    hi = band[1] + tol + nudge_f
    if season == "cool" and rh is not None and rh > cfg.humid_rh:
        hi -= cfg.humid_tighten_f
    return lo, hi


def room_score(temp: float, lo: float, hi: float, falloff_f: float = 3.0) -> float:
    """100 inside [lo, hi], linear to 0 at falloff_f outside. hi may be +inf (aux heat)."""
    dist = max(0.0, lo - temp, temp - hi)
    return 100.0 * max(0.0, 1.0 - dist / falloff_f)


def house_score(
    scores: Mapping[str, float],
    weights: Mapping[str, float],
    *,
    occupied: bool = True,
    floor_margin: float = 40.0,
) -> float | None:
    """Σ w·score / Σ w; when occupied, capped at min(weighted room score) + floor_margin.
    Returns None when no room carries weight."""
    used = {k: w for k, w in weights.items() if w > 0 and k in scores}
    total = sum(used.values())
    if total <= 0:
        return None
    house = sum(w * scores[k] for k, w in used.items()) / total
    if occupied:
        house = min(house, min(scores[k] for k in used) + floor_margin)
    return house


def violation_minutes(prev_zero_min: float, score: float, dt_min: float) -> float:
    """Consecutive minutes a room has sat at score 0 (reset on any non-zero score)."""
    return prev_zero_min + dt_min if score <= 0 else 0.0


def is_violation(
    zero_min: float, weight: float, occupied: bool, cfg: ComfortCfg | None = None
) -> bool:
    """FR-CMF-3: weighted, occupied room at score 0 for > violation_min consecutive minutes."""
    cfg = cfg or ComfortCfg()
    return occupied and weight > 0 and zero_min > cfg.violation_min


def base_weights(s: Settings, block: BlockName, nudged: set[str] | None = None) -> dict[str, float]:
    """Block weights × nudge boost for rooms with an active nudge (FR-CMF-6)."""
    nudged = nudged or set()
    return {
        k: w * (s.nudge.weight_boost if k in nudged else 1.0)
        for k, w in s.weights.get(block, {}).items()
    }


# --- Staleness (D§5.3) -------------------------------------------------------------

NEVER_STALE = "thermostat"  # thermostat staleness means DEGRADED, not weight loss
ESTIMATED_WEIGHT_FACTOR = 0.5


def remote_rooms(s: Settings) -> tuple[str, ...]:
    """Rooms weighted in the sleep block (bedrooms, usually far from the thermostat). Used for
    the all-dead estimate (D§5.3) and the fan-circulation check (FR-FAN-1)."""
    return tuple(
        k
        for k, w in s.weights.get("sleep", {}).items()
        if w > 0 and k != NEVER_STALE and k in s.rooms
    )


def room_age_min(
    now: datetime, last_reported: datetime | None, last_updated: datetime | None = None
) -> float | None:
    """age = now - newer of last_reported / last_updated, in minutes; None if neither."""
    stamps = [t for t in (last_reported, last_updated) if t is not None]
    if not stamps:
        return None
    return (now - max(stamps)).total_seconds() / 60.0


def staleness_factor(age_min: float | None, stale_min: float, dead_min: float) -> float:
    """1 while fresh, linear to 0 at dead_min, 0 beyond. Unknown age counts as dead."""
    if age_min is None:
        return 0.0
    if age_min <= stale_min:
        return 1.0
    if age_min >= dead_min:
        return 0.0
    return 1.0 - (age_min - stale_min) / (dead_min - stale_min)


def apply_staleness(
    weights: Mapping[str, float],
    ages: Mapping[str, float | None],
    rooms: Mapping[str, RoomCfg],
    predicted: Mapping[str, float] | None = None,
    remote: Collection[str] = (),
) -> tuple[dict[str, float], set[str]]:
    """Weights × staleness factor. Renormalisation happens in house_score (Σw divides).

    If every remote room (`remote`, see remote_rooms) is dead, remote rooms with base
    weight get 0.5× weight and are
    flagged estimated; the caller scores them on `predicted` temps (thermal model, §4.1).
    Returns (effective weights, estimated room keys)."""
    out: dict[str, float] = {}
    for k, w in weights.items():
        if k == NEVER_STALE or k not in rooms:
            out[k] = w
            continue
        cfg = rooms[k]
        out[k] = w * staleness_factor(ages.get(k), cfg.stale_min, cfg.dead_min)
    estimated: set[str] = set()
    up = [k for k in remote if k in rooms]
    if (
        up
        and predicted is not None
        and all(
            staleness_factor(ages.get(k), rooms[k].stale_min, rooms[k].dead_min) == 0 for k in up
        )
    ):
        for k in up:
            if weights.get(k, 0) > 0 and k in predicted:
                out[k] = weights[k] * ESTIMATED_WEIGHT_FACTOR
                estimated.add(k)
    return out, estimated


# --- Space-heater detection (D§5.4, FR-CMF-4) ----------------------------------------


class AuxHeatState(BaseModel):
    flag: bool = False
    below_since: datetime | None = None  # start of the current "excess < clear_below_f" run


def expected_room_temp(t_tstat: float, t_out: float, offset_f: float, coupling: float) -> float:
    """expected = T + d_r,u + e_r,u·(To − T)  (§4.1 room offset model)."""
    return t_tstat + offset_f + coupling * (t_out - t_tstat)


def aux_heat_step(
    prev: AuxHeatState,
    now: datetime,
    *,
    season: Season,
    fresh: bool,
    t_room: float,
    t_room_30m_ago: float | None,
    expected: float,
    furnace_idle_30m: bool,
    cfg: AuxHeatCfg | None = None,
) -> AuxHeatState:
    """One evaluation of the space-heater flag with hysteresis.

    Only heating season + fresh room can raise the flag. Clearing needs excess below
    clear_below_f continuously for clear_min minutes."""
    cfg = cfg or AuxHeatCfg()
    excess = t_room - expected
    if not prev.flag:
        if season != "heat" or not fresh:
            return prev
        spike = (
            t_room_30m_ago is not None and t_room - t_room_30m_ago > cfg.rise_f and furnace_idle_30m
        )
        if excess > cfg.above_expected_f or spike:
            return AuxHeatState(flag=True)
        return prev
    if excess >= cfg.clear_below_f:
        return AuxHeatState(flag=True)
    since = prev.below_since or now
    if (now - since).total_seconds() / 60.0 >= cfg.clear_min:
        return AuxHeatState(flag=False)
    return AuxHeatState(flag=True, below_since=since)


def aux_adjust(
    weight: float, band: Band, flag: bool, cfg: AuxHeatCfg | None = None
) -> tuple[float, Band]:
    """While flagged: weight = cfg.weight and hi = +inf (too-cold still penalised)."""
    if not flag:
        return weight, band
    cfg = cfg or AuxHeatCfg()
    return cfg.weight, (band[0], math.inf)
