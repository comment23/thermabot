"""v0.4.0 weather providers: Open-Meteo parsing, retries/backoff, cache, stale fallback, NWS
refactor (no network: httpx.MockTransport only)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from thermabot.adapters.nws import NWS
from thermabot.adapters.open_meteo import OpenMeteo
from thermabot.adapters.weather import make_provider
from thermabot.core.weather import WeatherProvider, is_stale

T0 = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)
NWS_FIX = Path(__file__).parent / "fixtures" / "nws"


def om_body(hours: int = 96, start: datetime = T0) -> dict[str, object]:
    ts = [int((start + timedelta(hours=h)).timestamp()) for h in range(hours)]
    return {
        "latitude": 51.5,
        "longitude": -0.12,
        "hourly": {
            "time": ts,
            "temperature_2m": [50 + (h % 24) / 2 for h in range(hours)],
            "relative_humidity_2m": [70] * hours,
            "weather_code": [3 if h % 2 else 61 for h in range(hours)],
            "wind_speed_10m": [8.5] * hours,
            "cloud_cover": [90] * hours,
        },
    }


class Seq:
    """Transport answering from a list of statuses/exceptions; then 200 with `body`."""

    def __init__(self, seq: list[int | Exception], body: dict[str, object]) -> None:
        self.seq, self.body, self.reqs = list(seq), body, []  # type: ignore[var-annotated]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.reqs.append(req)
        if self.seq:
            x = self.seq.pop(0)
            if isinstance(x, Exception):
                raise x
            return httpx.Response(x)
        return httpx.Response(200, json=self.body)


async def _nosleep(_: float) -> None:
    return None


@pytest.mark.brd("FR-WX-2")
async def test_open_meteo_parses_hourly() -> None:
    h = Seq([], om_body())
    om = OpenMeteo(51.5, -0.12, transport=httpx.MockTransport(h), sleep=_nosleep)
    assert isinstance(om, WeatherProvider)
    fc = await om.hourly(now=T0)
    assert fc.source == "open_meteo" and len(fc.points) == 96 and not fc.stale
    p = fc.points[1]
    assert p.t == T0 + timedelta(hours=1) and p.temp_f == 50.5
    assert (p.humidity, p.conditions, p.wind_mph, p.cloud_pct) == (70, "Overcast", 8.5, 90)
    assert fc.points[0].conditions == "Light rain"
    assert not is_stale(fc, T0)
    q = h.reqs[0].url.params
    assert h.reqs[0].url.path == "/v1/forecast"
    assert q["temperature_unit"] == "fahrenheit" and q["latitude"] == "51.5"
    assert "temperature_2m" in q["hourly"] and q["timeformat"] == "unixtime"
    await om.aclose()


@pytest.mark.brd("FR-WX-2")
async def test_open_meteo_skips_null_temps() -> None:
    b = om_body(4)
    b["hourly"]["temperature_2m"][2] = None  # type: ignore[index]
    om = OpenMeteo(0, 0, transport=httpx.MockTransport(Seq([], b)), sleep=_nosleep)
    assert len((await om.hourly(now=T0)).points) == 3
    await om.aclose()


@pytest.mark.brd("FR-WX-2")
async def test_retries_with_backoff_then_succeeds() -> None:
    h = Seq([503, httpx.ConnectError("boom")], om_body())
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)

    om = OpenMeteo(0, 0, transport=httpx.MockTransport(h), sleep=sleep)
    fc = await om.hourly(now=T0)
    assert len(fc.points) == 96 and len(h.reqs) == 3 and waits == [0.5, 1.0]
    await om.aclose()


@pytest.mark.brd("FR-WX-2")
async def test_no_retry_on_client_error() -> None:
    h = Seq([404], om_body())
    om = OpenMeteo(0, 0, transport=httpx.MockTransport(h), sleep=_nosleep)
    with pytest.raises(httpx.HTTPStatusError):
        await om.hourly(now=T0)
    assert len(h.reqs) == 1
    await om.aclose()


@pytest.mark.brd("FR-WX-2")
async def test_cache_then_stale_fallback_then_recovery() -> None:
    h = Seq([], om_body())
    om = OpenMeteo(0, 0, transport=httpx.MockTransport(h), sleep=_nosleep)
    a = await om.hourly(now=T0)
    assert await om.hourly(now=T0 + timedelta(minutes=10)) is a  # cached, no request
    assert len(h.reqs) == 1
    h.seq = [503, 503, 503]
    b = await om.hourly(now=T0 + timedelta(minutes=45))  # TTL expired, all retries fail
    assert b.stale and b.fetched_at == a.fetched_at and len(h.reqs) == 4
    c = await om.hourly(now=T0 + timedelta(minutes=50))  # retries again, recovers
    assert not c.stale and c.fetched_at == T0 + timedelta(minutes=50)
    await om.aclose()


@pytest.mark.brd("FR-WX-2")
async def test_failure_without_cache_raises() -> None:
    om = OpenMeteo(0, 0, transport=httpx.MockTransport(Seq([500, 500, 500], {})), sleep=_nosleep)
    with pytest.raises(httpx.HTTPStatusError):
        await om.hourly(now=T0)
    await om.aclose()


@pytest.mark.brd("FR-WX-2")
async def test_timeout_retried() -> None:
    h = Seq([httpx.ReadTimeout("slow")], om_body())
    om = OpenMeteo(0, 0, transport=httpx.MockTransport(h), sleep=_nosleep)
    assert len((await om.hourly(now=T0)).points) == 96
    await om.aclose()


def nws_handler(fail: list[int]) -> httpx.MockTransport:
    def h(req: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(fail.pop(0))
        name = "points" if req.url.path.startswith("/points/") else "hourly"
        return httpx.Response(200, json=json.loads((NWS_FIX / f"{name}.json").read_text()))

    return httpx.MockTransport(h)


@pytest.mark.brd("FR-WX-2")
async def test_nws_is_a_provider_with_retry_and_extras() -> None:
    n = NWS(40.0, -75.0, transport=nws_handler([502]), sleep=_nosleep)
    assert isinstance(n, WeatherProvider) and n.name == "nws"
    fc = await n.hourly(now=T0)
    assert fc.source == "nws" and len(fc.points) == 156
    assert fc.points[0].conditions == "Cloudy"
    await n.aclose()


def test_nws_value_helpers() -> None:
    from thermabot.adapters.nws import _qv, _to_f, _wind

    assert _qv({"unitCode": "wmoUnit:percent", "value": 61}) == 61 and _qv(None) is None
    assert _qv("x") is None and _qv(5) == 5
    assert _wind("5 to 10 mph") == 10 and _wind("16 km/h") == 9.9 and _wind("") is None
    assert _wind({"value": 16.09}) == 10.0
    assert _to_f({"temperature": 10, "temperatureUnit": "C"}) == 50
    assert _to_f({"temperature": {"value": 0, "unitCode": "wmoUnit:degC"}}) == 32


def test_make_provider() -> None:
    assert make_provider("nws", 40, -75).name == "nws"
    assert make_provider("open_meteo", 40, -75).name == "open_meteo"
    with pytest.raises(ValueError):
        make_provider("x", 0, 0)
