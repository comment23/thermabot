"""Weather staleness, source choice and bias (D§8.5, FR-WX-2/3). Pure: `now` passed in."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

from thermabot.config import SeasonCfg, WeatherCfg
from thermabot.core.schedule import season_from_forecast
from thermabot.core.types import Forecast, ForecastPoint, Season


@runtime_checkable
class WeatherProvider(Protocol):
    """Port for hourly forecast adapters (v0.4.0): thermabot.adapters.weather.NWS and
    OpenMeteo. `hourly` returns °F points; on a failed fetch an adapter returns its last good
    forecast with `stale=True`, or raises when it has none. Callers never let it crash a loop."""

    name: str

    async def hourly(self, now: datetime | None = None) -> Forecast: ...

    async def aclose(self) -> None: ...


def coverage_h(fc: Forecast, now: datetime) -> float:
    """Hours of forecast ahead of now (latest point - now), 0 if none."""
    future = [p.t for p in fc.points if p.t >= now]
    if not future:
        return 0.0
    return (max(future) - now).total_seconds() / 3600.0


def is_stale(fc: Forecast | None, now: datetime, cfg: WeatherCfg | None = None) -> bool:
    """Stale when missing, fetched > stale_h ago, or covering < min_cover_h ahead."""
    cfg = cfg or WeatherCfg()
    if fc is None:
        return True
    if now - fc.fetched_at > timedelta(hours=cfg.stale_h):
        return True
    return coverage_h(fc, now) < cfg.min_cover_h


def forecast_season(
    fc: Forecast | None, now: datetime, season_cfg: SeasonCfg, weather_cfg: WeatherCfg
) -> Season | None:
    if fc is None or is_stale(fc, now, weather_cfg):
        return None
    if not any(now <= point.t < now + timedelta(hours=24) for point in fc.points):
        return None
    return season_from_forecast(season_cfg, fc, now)


def choose_source(
    primary: Forecast | None,
    nws: Forecast | None,
    now: datetime,
    cfg: WeatherCfg | None = None,
) -> tuple[Forecast | None, Literal["ha", "nws", "none"]]:
    """HA weather.home if fresh, else NWS if fresh, else (None, 'none') -> DEGRADED(weather)."""
    if not is_stale(primary, now, cfg):
        return primary, "ha"
    if not is_stale(nws, now, cfg):
        return nws, "nws"
    return None, "none"


def forecast_at(fc: Forecast, t: datetime) -> float | None:
    """Temperature of the latest point at or before t (hourly step function)."""
    best: ForecastPoint | None = None
    for p in fc.points:
        if p.t <= t and (best is None or p.t > best.t):
            best = p
    return best.temp_f if best else None


def rolling_bias(
    pairs: Sequence[tuple[datetime, float, float]], now: datetime, days: float = 7
) -> float | None:
    """Mean of (forecast_at_hour − observed outdoor) over pairs within the last `days`.
    pairs = (t, forecast_f, observed_f). None when there's no data."""
    cut = now - timedelta(days=days)
    diffs = [f - o for t, f, o in pairs if cut <= t <= now]
    return sum(diffs) / len(diffs) if diffs else None


def apply_bias(fc: Forecast, bias: float | None, cfg: WeatherCfg | None = None) -> Forecast:
    """Subtract bias from every point when |bias| > bias_correct_above_f (FR-WX-3)."""
    cfg = cfg or WeatherCfg()
    if bias is None or abs(bias) <= cfg.bias_correct_above_f:
        return fc
    pts = [p.model_copy(update={"temp_f": p.temp_f - bias}) for p in fc.points]
    return fc.model_copy(update={"points": pts})
