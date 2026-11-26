"""Customer UAT lab: the real application/runtime, simulated HA, and no live services.

Run from the repository root: python -m scripts.uat --data-dir PATH [--fresh].
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from tests.e2e.conftest import PW, USERS
from tests.house import house_settings, seed_house
from tests.integration.mock_ha import GOOD_TOKEN, MockHA
from tests.integration.test_jobs import nws_ok
from tests.integration.test_open_meteo import om_body
from tests.integration.test_shadow_learning import TRUE
from thermabot.adapters import db
from thermabot.adapters.ha_rest import HAError
from thermabot.app.auth import create_user
from thermabot.app.main import Runtime, create_app, load_env
from thermabot.core.thermal import step

log = logging.getLogger(__name__)
MARKER = ".thermabot-uat"
MARKER_TEXT = "thermabot simulated UAT v1\n"
Scenario = Literal[
    "healthy",
    "celsius",
    "stale-room",
    "missing-room",
    "away",
    "ha-outage",
    "room-evidence",
    "room-history",
    "room-model",
]


class ScenarioIn(BaseModel):
    name: Scenario


class SimulatedHA(httpx.AsyncBaseTransport):
    """All HA traffic stays in-process; even an accidental climate write is refused."""

    def __init__(self) -> None:
        self.ha = MockHA()
        self.scenario: Scenario = "healthy"
        self.reads: list[str] = []
        self.blocked_writes: list[str] = []
        self.meter_start: datetime | None = None
        self.history_split: datetime | None = None
        self.model_start: datetime | None = None
        self.model_bucket = -1
        self.model_temp = 69.0
        self.model_heating = False
        self.model_outdoor = 45.0
        self.model_hour = 0.0
        self.ha._set(
            "sensor.study_humidity",
            "65",
            {
                "unit_of_measurement": "%",
                "device_class": "humidity",
                "friendly_name": "Study humidity",
            },
            None,
        )
        self.ha._set(
            "sensor.study_energy",
            "10",
            {
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "total_increasing",
                "friendly_name": "Study energy",
            },
            None,
        )
        self.ha._set(
            "binary_sensor.living_room_auxiliary_heat",
            "off",
            {"friendly_name": "Central auxiliary heat", "device_class": "heat"},
            None,
        )
        self.transport = httpx.ASGITransport(app=self.ha.app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "ha.test":
            raise RuntimeError("UAT refused a request outside simulated Home Assistant")
        path = request.url.path
        if request.method == "POST" and path.startswith("/api/services/climate/"):
            self.blocked_writes.append(path)
            log.error("UAT blocked a climate service request: %s", path)
            return httpx.Response(403, json={"detail": "UAT forbids climate writes"})
        if request.method == "GET":
            self.reads.append(path)
        if self.scenario == "ha-outage":
            return httpx.Response(503, json={"detail": "Simulated Home Assistant outage"})
        return await self.transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self.transport.aclose()

    def report(self, now: datetime) -> None:
        """Simulate regular device reports, while preserving the selected fault."""
        for state in self.ha.states.values():
            state["last_reported"] = state["last_updated"] = now.isoformat()
        study = self.ha.states["sensor.study_temperature"]
        study["state"] = "20" if self.scenario == "celsius" else "70.5"
        study["attributes"]["unit_of_measurement"] = "°C" if self.scenario == "celsius" else "°F"
        if self.scenario in ("room-evidence", "room-history"):
            study["state"] = "73"
            if self.meter_start is None:
                self.meter_start = now
            minutes = max(0, (now - self.meter_start).total_seconds() / 60)
            self.ha.states["sensor.study_energy"]["state"] = str(10 + minutes * 0.1)
        elif self.scenario == "stale-room":
            study["last_reported"] = study["last_updated"] = (now - timedelta(hours=2)).isoformat()
        elif self.scenario == "missing-room":
            study["state"] = "unavailable"
        away = self.scenario == "away"
        if self.scenario == "room-history":
            if self.history_split is None:
                raise RuntimeError("Room history requires an identified replay window")
            minute = now.hour * 60 + now.minute
            period = 30 if now < self.history_split else 24
            heating = minute % period < 2
            attrs = self.ha.states["climate.living_room"]["attributes"]
            attrs.update(current_temperature=70, hvac_action="heating" if heating else "idle")
            self.ha.states["binary_sensor.living_room_blower_active"]["state"] = (
                "on" if heating else "off"
            )
            self.ha.states["sensor.outdoor_temperature"]["state"] = "45"
            self.ha.states["binary_sensor.living_room_auxiliary_heat"]["state"] = (
                "on" if minute % period == 1 else "off"
            )
            away = 9 <= now.hour < 11
        elif self.scenario == "room-model":
            if self.model_start is None:
                raise RuntimeError("Room model requires an identified replay window")
            bucket = int((now - self.model_start).total_seconds() // 900)
            if bucket != self.model_bucket:
                if self.model_bucket >= 0:
                    self.model_temp = step(
                        self.model_temp,
                        self.model_outdoor,
                        self.model_hour,
                        "heat" if self.model_heating else "idle",
                        TRUE,
                    )
                self.model_bucket = bucket
                self.model_hour = now.hour + now.minute / 60
                self.model_outdoor = 45 + 8 * (
                    (self.model_hour - 9) / 12 if 3 <= self.model_hour <= 21 else 0
                )
                self.model_heating = self.model_temp < 68 or (
                    self.model_heating and self.model_temp < 70
                )
            self.ha.states["climate.living_room"]["attributes"].update(
                current_temperature=self.model_temp,
                hvac_action="heating" if self.model_heating else "idle",
            )
            self.ha.states["binary_sensor.living_room_blower_active"]["state"] = (
                "on" if self.model_heating else "off"
            )
            study["state"] = str(self.model_temp)
            self.ha.states["sensor.outdoor_temperature"]["state"] = str(self.model_outdoor)
        for eid, state in self.ha.states.items():
            if eid.startswith(("person.", "device_tracker.")):
                state["state"] = "not_home" if away else "home"
        # A temperature can be unchanged for days while its reports remain fresh.
        study["last_changed"] = (now - timedelta(days=2)).isoformat()


def _deny_external(request: httpx.Request) -> httpx.Response:
    raise RuntimeError(f"UAT refused external {request.method} traffic")


def _nws(request: httpx.Request) -> httpx.Response:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return nws_ok(start=now).handle_request(request)


class Lab:
    def __init__(self, app: FastAPI, transport: SimulatedHA) -> None:
        self.app = app
        self.runtime: Runtime = app.state.runtime
        self.transport = transport
        self.lock = asyncio.Lock()
        self.replaying = False
        self.replay_evidence: dict[str, Any] | None = None

    async def tick(self, now: datetime, *, scenario: Scenario | None = None) -> None:
        async with self.lock:
            await self._tick(now, scenario=scenario)

    async def _tick(self, now: datetime, *, scenario: Scenario | None = None) -> None:
        if scenario is not None:
            self.transport.scenario = scenario
            self.runtime.ctx.setup_cache.clear()
        state = db.read_bot_state(self.runtime.conn)
        if state is None or state.run_mode != "shadow":
            raise RuntimeError("UAT requires a valid SHADOW bot state")
        self.transport.report(now)
        try:
            await self.runtime.tick(now)
        except HAError as exc:
            if self.transport.scenario != "ha-outage" or exc.status != 503:
                raise
            log.warning("UAT injected HA outage; runtime reported disconnected")

    async def replay(self, name: Scenario) -> dict[str, Any]:
        async with self.lock:
            state = db.read_bot_state(self.runtime.conn)
            if state is None or state.kill_switch != "off" or state.run_mode != "shadow":
                raise HTTPException(409, "Stop Watching before replaying simulated room history.")
            now = self.runtime.clock()
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            self.replaying = True
            self.replay_evidence = {"scenario": name, "complete": False, "minutes": 0, "days": []}
            log.info("Starting stopped-only simulated replay: %s", name)
            try:
                if name == "room-history":
                    self.transport.history_split = day - timedelta(days=14)
                    dates = []
                    for window in (range(28, 14, -1), range(1, 15)):
                        weekdays = [day - timedelta(days=d) for d in window]
                        dates.extend([t for t in weekdays if t.weekday() < 5][:5])
                    # Five completed weekdays in each weather window, not continuous Watch.
                    dates.sort()
                    self.transport.meter_start = dates[0]
                    for start in dates:
                        for minute in range(1440):
                            await self._tick(start + timedelta(minutes=minute), scenario=name)
                            self.replay_evidence["minutes"] += 1
                        await self.runtime.rollup(start + timedelta(days=1, minutes=5))
                        self.replay_evidence["days"].append(start.date().isoformat())
                elif name == "room-model":
                    start = now.replace(second=0, microsecond=0) - timedelta(days=3)
                    self.transport.model_start = start
                    self.transport.model_bucket = -1
                    self.transport.model_temp = 69
                    self.transport.model_heating = False
                    for minute in range(4320):
                        await self._tick(start + timedelta(minutes=minute), scenario=name)
                        self.replay_evidence["minutes"] += 1
                elif name == "room-evidence":
                    self.transport.meter_start = now - timedelta(minutes=2)
                    for minute in (2, 1):
                        await self._tick(now - timedelta(minutes=minute), scenario=name)
                        self.replay_evidence["minutes"] += 1
                else:
                    raise ValueError("Not a historical replay scenario")
                await self._tick(
                    now if name == "room-evidence" else self.runtime.clock(), scenario=name
                )
                self.replay_evidence["minutes"] += 1
                self.replay_evidence["complete"] = True
                return self.evidence()
            finally:
                self.replaying = False
                log.info("Simulated replay result: %s", self.replay_evidence)

    def evidence(self) -> dict[str, Any]:
        rt = self.runtime
        state = db.read_bot_state(rt.conn)
        latest = rt.conn.execute(
            "SELECT snapshot_json FROM samples ORDER BY ts DESC, rowid DESC LIMIT 1"
        ).fetchone()
        model = rt.conn.execute(
            "SELECT version,fitted_at,mae_json,n_samples FROM models"
            " WHERE active=1 ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return {
            "environment": "simulated-uat",
            "scenario": self.transport.scenario,
            "historical_replay_minutes": 0
            if self.replay_evidence is None
            else self.replay_evidence["minutes"],
            "replay": self.replay_evidence,
            "model": None
            if model is None
            else {
                "version": model["version"],
                "fitted_at": model["fitted_at"],
                "mae": json.loads(model["mae_json"]),
                "training_samples": model["n_samples"],
            },
            "state": rt.ctx.eff().state,
            "run_mode": None if state is None else state.run_mode,
            "kill_switch": None if state is None else state.kill_switch,
            "ha_health": rt.ctx.ha_health,
            "snapshot": None
            if rt.ctx.snapshot is None
            else rt.ctx.snapshot.model_dump(mode="json"),
            "sample_count": rt.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0],
            "latest_sample": None if latest is None else json.loads(latest[0]),
            "climate_service_calls": [
                call for call in self.transport.ha.calls if call["domain"] == "climate"
            ],
            "blocked_climate_requests": list(self.transport.blocked_writes),
            "climate_reads": [path for path in self.transport.reads if "/states/climate." in path],
        }


def create_lab(
    data_dir: Path,
    *,
    fresh: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Lab:
    """Never open an existing directory unless it belongs to this disposable harness."""
    data_dir = data_dir.resolve()
    marker = data_dir / MARKER
    new = not marker.exists()
    if new:
        if data_dir.exists() and any(data_dir.iterdir()):
            raise ValueError("UAT needs an empty, dedicated data directory; existing data refused")
        data_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(MARKER_TEXT, encoding="ascii")
        if not fresh:
            seed_house(
                data_dir,
                house_settings(presence={"people": {"alex": {"companion": "person.alex"}}}),
            )
    elif marker.read_text(encoding="ascii") != MARKER_TEXT:
        raise ValueError("Not a recognized UAT directory")

    # Supply every Env field, so even process environment credentials cannot leak in.
    from thermabot.config import Env

    values = {name: field.get_default() for name, field in Env.model_fields.items()}
    values.update(
        HA_URL="http://ha.test",
        HA_TOKEN=GOOD_TOKEN,
        DATA_DIR=str(data_dir),
        TZ="UTC",
        HEARTBEAT_WEBHOOK_URL="http://ha.test/api/webhook/uat-heartbeat",
    )
    transport = SimulatedHA()
    weather: dict[str, httpx.AsyncBaseTransport | None] = {
        "nws": httpx.MockTransport(_nws),
        "open_meteo": httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=om_body(start=datetime.now(UTC).replace(minute=0, second=0, microsecond=0)),
            )
        ),
    }
    app = create_app(
        load_env(values),
        clock=clock,
        ha_transport=transport,
        nws_transport=weather["nws"],
        open_meteo_transport=weather["open_meteo"],
        influx_transport=httpx.MockTransport(_deny_external),
        notify_transport=httpx.MockTransport(_deny_external),
    )
    lab = Lab(app, transport)
    lab.runtime.ctx.weather_transports = weather
    lab.runtime.loops.jobs.tick = lab.tick
    # The mock discovery includes a generic opening for the normal setup journey.
    transport.ha.states["binary_sensor.front_door"] = {
        "entity_id": "binary_sensor.front_door",
        "state": "off",
        "attributes": {"device_class": "door", "friendly_name": "Front door"},
    }
    if new and not fresh:
        for name, role, room in USERS:
            create_user(lab.runtime.conn, name, role, PW, room=room)  # type: ignore[arg-type]

    @app.middleware("http")
    async def shadow_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method == "POST" and request.url.path == "/api/kill":
            try:
                body = await request.json()
            except json.JSONDecodeError:
                return JSONResponse({"detail": "Invalid request JSON"}, status_code=422)
            if isinstance(body, dict) and body.get("mode") == "active":
                return JSONResponse(
                    {"detail": "This simulated UAT environment permits Watch/SHADOW only"},
                    status_code=403,
                )
            if isinstance(body, dict) and body.get("enabled") and lab.replaying:
                return JSONResponse(
                    {"detail": "Wait for the stopped-only simulated replay to finish"},
                    status_code=409,
                )
        return await call_next(request)

    @app.get("/uat", include_in_schema=False)
    async def panel() -> FileResponse:
        return FileResponse(Path(__file__).with_name("uat.html"))

    @app.get("/uat/api/status", include_in_schema=False)
    async def evidence() -> dict[str, Any]:
        return lab.evidence()

    @app.post("/uat/api/scenario", include_in_schema=False)
    async def scenario(body: ScenarioIn, request: Request) -> dict[str, Any]:
        # No CORS is enabled; cross-origin forms cannot supply this custom header.
        if request.headers.get("X-Thermabot-UAT") != "1":
            raise HTTPException(403, "UAT scenario requests require the local harness header")
        if body.name in ("room-evidence", "room-history", "room-model"):
            return await lab.replay(body.name)
        await lab.tick(lab.runtime.clock(), scenario=body.name)
        return lab.evidence()

    return lab


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--data-dir", required=True, type=Path, help="Dedicated disposable UAT directory"
    )
    ap.add_argument(
        "--fresh", action="store_true", help="New directory: start with first-run setup"
    )
    ap.add_argument("--port", type=int, default=8096)
    args = ap.parse_args()
    if not 1 <= args.port <= 65535:
        ap.error("--port must be between 1 and 65535")
    try:
        lab = create_lab(args.data_dir, fresh=args.fresh)
    except ValueError as exc:
        ap.error(str(exc))
    print(f"SIMULATED UAT ONLY: http://127.0.0.1:{args.port}/uat", flush=True)
    print("No live HA, no ACTIVE, no climate writes. Stop with Ctrl+C.", flush=True)
    uvicorn.run(lab.app, host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
