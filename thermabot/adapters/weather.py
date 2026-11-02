"""Weather provider adapters (v0.4.0), implementing core.weather.WeatherProvider.

Shared behaviour: per-request timeout, bounded retries with backoff on transport errors,
timeouts, 429 and 5xx; a 30-minute cache of the last good hourly forecast; and on a failed
fetch the last good forecast is served with `stale=True` (raise only when there is none).
Callers (main.Runtime.replan) still wrap every fetch, so a weather failure can never crash a
loop. All points are returned in °F.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from thermabot.core.types import Forecast

log = logging.getLogger(__name__)

HOURLY_TTL = timedelta(minutes=30)
TIMEOUT_S = 10.0
RETRIES = 2  # extra attempts after the first
BACKOFF_S = 0.5  # 0.5 s, then 1 s
RETRY_STATUS = {429, 500, 502, 503, 504}

Sleep = Callable[[float], Awaitable[Any]]


class CachedHourly:
    """Base for hourly providers. Subclasses set `name` and implement `_fetch`."""

    name = "base"

    def __init__(
        self,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT_S,
        retries: int = RETRIES,
        backoff_s: float = BACKOFF_S,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=transport, headers=dict(headers or {})
        )
        self.retries, self.backoff_s, self._sleep = retries, backoff_s, sleep
        self._hourly: tuple[datetime, Forecast] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(self.retries + 1):
            last = attempt == self.retries
            try:
                r = await self._http.get(url, params=params)
            except (httpx.TransportError, httpx.TimeoutException):
                if last:
                    raise
            else:
                if r.status_code not in RETRY_STATUS or last:
                    r.raise_for_status()
                    out: dict[str, Any] = r.json()
                    return out
            await self._sleep(self.backoff_s * (2**attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    async def _fetch(self, now: datetime) -> Forecast:
        raise NotImplementedError

    async def hourly(self, now: datetime | None = None) -> Forecast:
        t = now or datetime.now(UTC)
        if self._hourly is not None and t - self._hourly[0] < HOURLY_TTL:
            return self._hourly[1]
        try:
            fc = await self._fetch(t)
        except Exception as e:
            if self._hourly is None:
                raise
            log.warning("%s fetch failed (%s); serving the last good forecast", self.name, e)
            return self._hourly[1].model_copy(update={"stale": True})
        self._hourly = (t, fc)
        return fc


def make_provider(
    name: str,
    lat: float,
    lon: float,
    transports: Mapping[str, httpx.AsyncBaseTransport | None] | None = None,
    sleep: Sleep = asyncio.sleep,
) -> CachedHourly:
    """Provider by settings name ('nws' | 'open_meteo')."""
    from thermabot.adapters.nws import NWS
    from thermabot.adapters.open_meteo import OpenMeteo

    tr = (transports or {}).get(name)
    if name == "nws":
        return NWS(lat, lon, transport=tr, sleep=sleep)
    if name == "open_meteo":
        return OpenMeteo(lat, lon, transport=tr, sleep=sleep)
    raise ValueError(f"unknown weather provider {name!r}")
