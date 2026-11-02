"""NWS adapter (D\x158.5, US only): points -> forecastHourly / forecast, cached, User-Agent
required. v0.4.0: a WeatherProvider (adapters/weather.py: timeouts, retries, cache, stale)."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

import httpx

from thermabot.adapters.weather import HOURLY_TTL, CachedHourly, Sleep
from thermabot.core.types import Forecast, ForecastPoint

BASE = "https://api.weather.gov"
USER_AGENT = "thermabot (https://github.com/comment23/thermabot)"
__all__ = ["BASE", "HOURLY_TTL", "NWS", "USER_AGENT"]


class NWS(CachedHourly):
    name = "nws"

    def __init__(
        self,
        lat: float,
        lon: float,
        base_url: str = BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        super().__init__(
            base_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
            transport=transport,
            timeout=20,
            sleep=sleep,
        )
        self.lat, self.lon = round(lat, 4), round(lon, 4)
        self._points: dict[str, Any] | None = None  # cached for the process lifetime

    async def points(self) -> dict[str, Any]:
        if self._points is None:
            self._points = (await self._get(f"/points/{self.lat},{self.lon}"))["properties"]
        return self._points

    async def _fetch(self, now: datetime) -> Forecast:
        data = await self._get(str((await self.points())["forecastHourly"]))
        return Forecast(
            source="nws",
            fetched_at=now,
            points=[
                ForecastPoint(
                    t=datetime.fromisoformat(p["startTime"]),
                    temp_f=_to_f(p),
                    humidity=_qv(p.get("relativeHumidity")),
                    conditions=p.get("shortForecast") or None,
                    wind_mph=_wind(p.get("windSpeed")),
                )
                for p in data["properties"]["periods"]
            ],
        )

    async def daily(self) -> list[dict[str, Any]]:
        """12-hour periods for the weekly summary fallback."""
        data = await self._get(str((await self.points())["forecast"]))
        return [
            {
                "start": p["startTime"],
                "name": p.get("name", ""),
                "temp_f": _to_f(p),
                "is_day": p.get("isDaytime", True),
                "short": p.get("shortForecast", ""),
            }
            for p in data["properties"]["periods"]
        ]


def _to_f(p: dict[str, Any]) -> float:
    v = p["temperature"]
    if isinstance(v, dict):  # newer quantitative-value shape
        val = float(v["value"])
        return val * 9 / 5 + 32 if str(v.get("unitCode", "")).endswith("degC") else val
    val = float(v)
    return val * 9 / 5 + 32 if p.get("temperatureUnit") == "C" else val


def _qv(v: Any) -> float | None:
    """NWS quantitative value {'unitCode': ..., 'value': x} or a bare number."""
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _wind(v: Any) -> float | None:
    """'10 mph' / '5 to 10 mph' -> the upper number in mph; km/h converted."""
    if isinstance(v, dict):
        x = _qv(v)
        return None if x is None else round(x / 1.609, 1)
    nums = re.findall(r"\d+(?:\.\d+)?", str(v or ""))
    if not nums:
        return None
    x = float(nums[-1])
    return round(x / 1.609, 1) if "km" in str(v) else x
