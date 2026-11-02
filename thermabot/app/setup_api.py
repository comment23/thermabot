"""v0.3.0 first-run setup wizard API (Owner only).

Read-only towards Home Assistant: the wizard only ever GETs /api/config and /api/states through
the `HARead` protocol, which has no service method, so it cannot call a HA service. Secrets
stay in the environment; nothing here returns the token. Save writes the `settings` row only;
run mode and the kill switch live in `bot_state` and are never touched.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from thermabot.app.auth import Principal, require
from thermabot.app.routes_api import Ctx, _has_row, _kv_put, ctx_of, load_settings
from thermabot.config import Settings
from thermabot.core import discovery
from thermabot.core.types import ForecastPoint
from thermabot.core.weather import coverage_h

HA_TIMEOUT_S = 5.0
CACHE_S = 15.0
DOMAINS = ("climate", "sensor", "binary_sensor", "person", "device_tracker", "fan", "cover")

CtxD = Annotated[Ctx, Depends(ctx_of)]
Owner = Annotated[Principal, Depends(require("edit_safety"))]
Comfort = Annotated[Principal, Depends(require("edit_comfort"))]
router = APIRouter(prefix="/api/setup")
WEATHER_TEST_TIMEOUT_S = 15.0


class HATestOut(BaseModel):
    ok: bool
    version: str | None = None
    location_name: str | None = None
    url: str | None = None
    ms: int | None = None
    error: str | None = None


class HomeContextOut(BaseModel):
    location_available: bool
    latitude: float | None = None
    longitude: float | None = None
    location_name: str | None = None
    time_zone: str | None = None
    location_error: str | None = None
    sun: discovery.SunPosition | None = None


class SaveOut(BaseModel):
    settings: Settings
    restart_required: bool
    running_entity: str


def _coordinate(value: Any, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) and minimum <= result <= maximum else None


def _err(e: BaseException) -> str:
    if isinstance(e, TimeoutError):
        return f"Home Assistant did not answer within {HA_TIMEOUT_S:g} s"
    status = getattr(e, "status", None)
    if status == 401:
        return "Home Assistant rejected the token (HTTP 401): check HA_TOKEN"
    if status:
        return f"Home Assistant returned HTTP {status}"
    return f"cannot reach Home Assistant ({type(e).__name__}): check HA_URL"


async def _states(c: Ctx) -> list[dict[str, Any]]:
    """GET /api/states through the adapter, cached for CACHE_S, bounded by HA_TIMEOUT_S."""
    if c.ha_read is None:
        raise HTTPException(503, "Home Assistant client not available")
    hit = c.setup_cache.get("states")
    now = time.monotonic()
    if hit is not None and now - hit[0] < CACHE_S:
        out: list[dict[str, Any]] = hit[1]
        return out
    try:
        states = await asyncio.wait_for(c.ha_read.get_all_states(), HA_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - shown to the Owner, never raised raw
        raise HTTPException(502, _err(e)) from e
    c.setup_cache["states"] = (now, states)
    return states


@router.get("/ha-test", response_model=HATestOut)
async def ha_test(c: CtxD, _: Owner) -> HATestOut:
    if c.ha_read is None:
        return HATestOut(ok=False, url=c.ha_url or None, error="Home Assistant client not set")
    t0 = time.monotonic()
    try:
        cfg = await asyncio.wait_for(c.ha_read.get_config(), HA_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        return HATestOut(ok=False, url=c.ha_url or None, error=_err(e))
    c.setup_cache.pop("states", None)  # a fresh test also refreshes discovery
    return HATestOut(
        ok=True,
        version=str(cfg.get("version") or "") or None,
        location_name=cfg.get("location_name"),
        url=c.ha_url or None,
        ms=int((time.monotonic() - t0) * 1000),
    )


@router.get("/home-context", response_model=HomeContextOut)
async def home_context(c: CtxD, _: Owner) -> HomeContextOut:
    """Read HA's configured location and sun state; never uses browser geolocation."""
    if c.ha_read is None:
        raise HTTPException(503, "Home Assistant client not available")
    try:
        cfg, states = await asyncio.wait_for(
            asyncio.gather(c.ha_read.get_config(), _states(c)), HA_TIMEOUT_S
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - expose a bounded error, never raw response data
        raise HTTPException(502, _err(e)) from e

    lat = _coordinate(cfg.get("latitude"), -90, 90)
    lon = _coordinate(cfg.get("longitude"), -180, 180)
    paired = lat is not None and lon is not None
    location_error = None
    if not paired:
        location_error = (
            "Home Assistant must provide valid latitude and longitude for sun-aware features"
        )
    return HomeContextOut(
        location_available=paired,
        latitude=round(lat, 4) if lat is not None and lon is not None else None,
        longitude=round(lon, 4) if lat is not None and lon is not None else None,
        location_name=str(cfg["location_name"]) if cfg.get("location_name") else None,
        time_zone=str(cfg["time_zone"]) if cfg.get("time_zone") else None,
        location_error=location_error,
        sun=discovery.sun_position(states),
    )


@router.get("/entities", response_model=list[discovery.Entity])
async def entities(
    c: CtxD,
    _: Owner,
    domain: Literal[
        "climate", "sensor", "binary_sensor", "person", "device_tracker", "fan", "cover"
    ] = "climate",
) -> list[discovery.Entity]:
    return discovery.entities(await _states(c), [domain])


class DiscoverOut(BaseModel):
    thermostat: str
    climate: list[discovery.Entity]
    suggestions: discovery.Discovery
    all: list[discovery.Entity]  # every candidate-domain entity, for manual override


@router.get("/discover", response_model=DiscoverOut)
async def discover(c: CtxD, _: Owner, thermostat: str = "") -> DiscoverOut:
    states = await _states(c)
    tstat = thermostat or load_settings(c).thermostat.entity_id
    return DiscoverOut(
        thermostat=tstat,
        climate=discovery.entities(states, ["climate"]),
        suggestions=discovery.discover(states, tstat),
        all=discovery.entities(states, DOMAINS[1:]),
    )


@router.post("/save", response_model=SaveOut)
async def save(body: dict[str, Any], request: Request, c: CtxD, p: Owner) -> SaveOut:
    """Validate through the Settings model and write the row (updated_by = the Owner).
    A thermostat change only takes effect after a restart (both write layers are bound at
    startup), so the response says so; nothing else about the running bot changes here."""
    try:
        new = Settings.model_validate(body)
    except ValidationError as e:
        errs = [{"loc": list(x["loc"]), "msg": x["msg"]} for x in e.errors()]
        raise HTTPException(422, errs) from e
    old = load_settings(c)
    restart = new.thermostat.entity_id != (c.running_entity or "")
    if new == old and _has_row(c):  # v0.6.1: nothing differs -> no row write, no audit
        return SaveOut(
            settings=old, restart_required=restart, running_entity=c.running_entity or ""
        )
    _kv_put(c, "settings", new.model_dump(mode="json"), p.username)
    c.settings = new
    c.audit(
        p,
        "setup_save",
        request,
        thermostat=new.thermostat.entity_id,
        changed=[k for k in Settings.model_fields if getattr(old, k) != getattr(new, k)],
        restart_required=restart,
    )
    c.replan()
    return SaveOut(settings=new, restart_required=restart, running_entity=c.running_entity or "")


# ---- v0.4.0 weather step (wizard + Settings; Owner and Admin, read-only) --------------------
class HALocationOut(BaseModel):
    ok: bool
    lat: float | None = None
    lon: float | None = None
    location_name: str | None = None
    error: str | None = None


@router.get("/ha-location", response_model=HALocationOut)
async def ha_location(c: CtxD, _: Comfort) -> HALocationOut:
    """HA's home location from GET /api/config (read-only; no service call)."""
    if c.ha_read is None:
        return HALocationOut(ok=False, error="Home Assistant client not set")
    try:
        cfg = await asyncio.wait_for(c.ha_read.get_config(), HA_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        return HALocationOut(ok=False, error=_err(e))
    lat, lon = cfg.get("latitude"), cfg.get("longitude")
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return HALocationOut(ok=False, error="Home Assistant has no home location set")
    return HALocationOut(
        ok=True,
        lat=round(float(lat), 4),
        lon=round(float(lon), 4),
        location_name=cfg.get("location_name"),
    )


class WeatherTestIn(BaseModel):
    provider: Literal["nws", "open_meteo"]
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class WeatherTestOut(BaseModel):
    ok: bool
    provider: str
    points: int = 0
    hours: float = 0
    preview: list[ForecastPoint] = []
    ms: int | None = None
    error: str | None = None


@router.post("/weather-test", response_model=WeatherTestOut)
async def weather_test(body: WeatherTestIn, c: CtxD, _: Comfort) -> WeatherTestOut:
    """Fetch a forecast with the given provider/location and preview the next 12 hours.
    Saves nothing and does not touch the running bot's forecast."""
    from thermabot.adapters.weather import make_provider

    t0 = time.monotonic()
    prov = make_provider(body.provider, body.lat, body.lon, c.weather_transports)
    prov.retries = 0  # interactive: one attempt, bounded by the timeout below
    now = c.clock()
    try:
        fc = await asyncio.wait_for(prov.hourly(now=now), WEATHER_TEST_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - shown to the user
        status = getattr(getattr(e, "response", None), "status_code", None)
        msg = (
            f"{body.provider} returned HTTP {status}"
            + (" (NWS covers the US only)" if body.provider == "nws" and status == 404 else "")
            if status
            else f"{body.provider} fetch failed ({type(e).__name__})"
        )
        return WeatherTestOut(ok=False, provider=body.provider, error=msg)
    finally:
        await prov.aclose()
    ahead = sorted((p for p in fc.points if p.t >= now - timedelta(hours=1)), key=lambda p: p.t)
    return WeatherTestOut(
        ok=bool(ahead),
        provider=body.provider,
        points=len(fc.points),
        hours=round(coverage_h(fc, now), 1),
        preview=ahead[:12],
        ms=int((time.monotonic() - t0) * 1000),
        error=None if ahead else "the forecast has no future hours",
    )
