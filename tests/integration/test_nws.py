"""T1.25: NWS adapter against recorded-shape JSON fixtures (no network)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from thermabot.adapters.nws import NWS, USER_AGENT
from thermabot.core.weather import is_stale

FIX = Path(__file__).parent / "fixtures" / "nws"
T0 = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)


def transport(log: list[httpx.Request]) -> httpx.MockTransport:
    def h(req: httpx.Request) -> httpx.Response:
        log.append(req)
        p = req.url.path
        name = (
            "points"
            if p.startswith("/points/")
            else "hourly"
            if p.endswith("/forecast/hourly")
            else "forecast"
        )
        return httpx.Response(200, json=json.loads((FIX / f"{name}.json").read_text()))

    return httpx.MockTransport(h)


@pytest.mark.brd("FR-WX-2")
async def test_hourly_via_points_with_user_agent() -> None:
    log: list[httpx.Request] = []
    n = NWS(40.0, -75.0, transport=transport(log))
    fc = await n.hourly(now=T0)
    assert fc.source == "nws" and len(fc.points) == 156
    assert fc.points[0].t == datetime(2026, 10, 5, 18, 0, tzinfo=UTC)
    assert not is_stale(fc, T0)
    assert [r.url.path for r in log] == [
        "/points/40.0,-75.0",
        "/gridpoints/PHI/50,70/forecast/hourly",
    ]
    assert all(r.headers["User-Agent"] == USER_AGENT for r in log)
    await n.aclose()


@pytest.mark.brd("FR-WX-2")
async def test_points_and_hourly_cached() -> None:
    log: list[httpx.Request] = []
    n = NWS(40.0, -75.0, transport=transport(log))
    await n.hourly(now=T0)
    await n.hourly(now=T0 + timedelta(minutes=10))
    assert len(log) == 2
    await n.hourly(now=T0 + timedelta(minutes=45))  # TTL expired, points still cached
    assert len(log) == 3 and log[-1].url.path.endswith("/forecast/hourly")
    await n.daily()
    assert len(log) == 4
    await n.aclose()


@pytest.mark.brd("FR-WX-1")
async def test_daily_periods() -> None:
    n = NWS(40.0, -75.0, transport=transport([]))
    d = await n.daily()
    assert len(d) == 14 and d[0]["temp_f"] == 64 and d[1]["is_day"] is False
    await n.aclose()


@pytest.mark.brd("FR-WX-2")
async def test_http_error_raises() -> None:
    n = NWS(40.0, -75.0, transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(httpx.HTTPStatusError):
        await n.hourly(now=T0)
    await n.aclose()
