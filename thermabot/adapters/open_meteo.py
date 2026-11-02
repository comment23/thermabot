"""Open-Meteo adapter (v0.4.0): global, keyless hourly forecast (https://open-meteo.com).
One GET /v1/forecast per refresh; temperatures requested in °F, wind in mph, unix UTC times."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from thermabot.adapters.weather import CachedHourly, Sleep
from thermabot.core.types import Forecast, ForecastPoint

BASE = "https://api.open-meteo.com"
HOURLY = "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,cloud_cover"
FORECAST_DAYS = 4  # >= 48 h ahead at any time of day

# WMO weather interpretation codes (Open-Meteo docs), shortened.
WMO = {
    0: "Clear",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Rain showers",
    81: "Rain showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm, hail",
    99: "Thunderstorm, hail",
}


class OpenMeteo(CachedHourly):
    name = "open_meteo"

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
            headers={"User-Agent": "thermabot (https://github.com/comment23/thermabot)"},
            transport=transport,
            sleep=sleep,
        )
        self.lat, self.lon = round(lat, 4), round(lon, 4)

    async def _fetch(self, now: datetime) -> Forecast:
        data = await self._get(
            "/v1/forecast",
            params={
                "latitude": self.lat,
                "longitude": self.lon,
                "hourly": HOURLY,
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "timeformat": "unixtime",
                "timezone": "GMT",
                "forecast_days": FORECAST_DAYS,
            },
        )
        h: dict[str, list[Any]] = data["hourly"]
        n = len(h["time"])

        def col(k: str, i: int) -> Any:
            v = h.get(k)
            return v[i] if v is not None and i < len(v) else None

        pts: list[ForecastPoint] = []
        for i in range(n):
            temp = col("temperature_2m", i)
            if temp is None:
                continue
            code = col("weather_code", i)
            pts.append(
                ForecastPoint(
                    t=datetime.fromtimestamp(int(h["time"][i]), UTC),
                    temp_f=float(temp),
                    humidity=_f(col("relative_humidity_2m", i)),
                    conditions=None if code is None else WMO.get(int(code), f"code {code}"),
                    wind_mph=_f(col("wind_speed_10m", i)),
                    cloud_pct=_f(col("cloud_cover", i)),
                )
            )
        return Forecast(source="open_meteo", fetched_at=now, points=pts)


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
