"""FastAPI mock of HA WS + REST (D§7.5). Knobs: latency_s, bad token, ignore_writes,
drop_ws(), external_change(); counts every service call in `calls`."""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect

from tests.integration.mock_ha.fixtures import hourly_forecast, initial_states

GOOD_TOKEN = "mock-good-token-0123456789abcdef"
BOT_USER_ID = "user-thermabot"
FOREIGN_USER_ID = "user-wall-panel"
CLIMATE_KEYS = ("temperature", "target_temp_low", "target_temp_high", "fan_mode", "preset_mode")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MockHA:
    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        for eid, s in initial_states().items():
            self._set(eid, s["state"], s["attributes"], None)
        self.token = GOOD_TOKEN
        self.latency_s = 0.0
        self.ignore_writes = False
        # T1.44 quirk: the real Trane ignores hvac_mode inside set_temperature when leaving OFF.
        self.trane_off_quirk = False
        self.admin = False  # non-admin bot user, like the real HA
        self.climate_forbidden = False  # knob: 403 on climate services (probe test)
        self.calls: list[dict[str, Any]] = []  # every service call (REST + WS)
        self.state_posts: list[dict[str, Any]] = []
        self.webhook_posts: list[dict[str, Any]] = []  # POST /api/webhook/<id> (heartbeat)
        self.config_reads = 0
        self.all_states_reads = 0
        self.ws_auth_attempts = 0
        self._subs: list[tuple[WebSocket, int]] = []
        self._sockets: list[WebSocket] = []
        self.app = self._build()

    # ---- state -------------------------------------------------------------------------
    def _set(self, eid: str, state: str, attrs: dict[str, Any], user: str | None) -> dict[str, Any]:
        ts = _now()
        new = {
            "entity_id": eid,
            "state": state,
            "attributes": copy.deepcopy(attrs),
            "last_changed": ts,
            "last_updated": ts,
            "last_reported": ts,
            "context": {"id": uuid.uuid4().hex, "user_id": user, "parent_id": None},
        }
        self.states[eid] = new
        return new

    async def _change(self, eid: str, state: str, attrs: dict[str, Any], user: str | None) -> None:
        old = copy.deepcopy(self.states.get(eid))
        new = self._set(eid, state, attrs, user)
        ev = {
            "event_type": "state_changed",
            "time_fired": new["last_updated"],
            "data": {"entity_id": eid, "old_state": old, "new_state": new},
            "context": new["context"],
        }
        for ws, sub_id in list(self._subs):
            try:
                await ws.send_json({"id": sub_id, "type": "event", "event": ev})
            except Exception:  # noqa: BLE001 - dead socket
                self._subs.remove((ws, sub_id))

    async def external_change(self, user: str = FOREIGN_USER_ID, **attrs: Any) -> None:
        """Simulate the wall panel / app changing the thermostat."""
        cur = self.states["climate.living_room"]
        merged = {**cur["attributes"], **{k: v for k, v in attrs.items() if k != "hvac_mode"}}
        await self._change(cur["entity_id"], attrs.get("hvac_mode", cur["state"]), merged, user)

    async def drop_ws(self) -> None:
        for ws in list(self._sockets):
            try:
                await ws.close(code=1012)
            except Exception:  # noqa: BLE001
                pass
        self._sockets.clear()
        self._subs.clear()

    # ---- services ----------------------------------------------------------------------
    async def call(
        self, domain: str, service: str, data: dict[str, Any], user: str = BOT_USER_ID
    ) -> list[dict[str, Any]]:
        self.calls.append({"domain": domain, "service": service, "data": copy.deepcopy(data)})
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        eid = str(data.get("entity_id", ""))
        if domain == "climate" and (self.climate_forbidden or not eid.startswith("climate.")):
            raise HTTPException(403 if self.climate_forbidden else 400, "unauthorized")
        if eid not in self.states:
            raise HTTPException(400, f"unknown entity {eid}")
        if self.ignore_writes:
            return []
        cur = self.states[eid]
        attrs, state = dict(cur["attributes"]), cur["state"]
        if domain == "climate":
            for k in CLIMATE_KEYS:
                if k in data:
                    attrs[k] = data[k]
            if service == "set_hvac_mode" or "hvac_mode" in data:
                quirk = self.trane_off_quirk and service == "set_temperature" and state == "off"
                if not quirk:
                    state = str(data["hvac_mode"])
        elif domain == "input_boolean":
            state = {"turn_on": "on", "turn_off": "off"}.get(service, state)
        await self._change(eid, state, attrs, user)
        return [self.states[eid]]

    # ---- FastAPI app -------------------------------------------------------------------
    def _auth(self, authorization: str | None) -> None:
        if authorization != f"Bearer {self.token}":
            raise HTTPException(401, "unauthorized")

    def _build(self) -> FastAPI:
        app = FastAPI()
        ha = self

        @app.get("/api/")
        async def api_root(authorization: str | None = Header(None)) -> dict[str, str]:
            ha._auth(authorization)
            return {"message": "API running."}

        @app.get("/api/config")
        async def config(authorization: str | None = Header(None)) -> dict[str, Any]:
            ha._auth(authorization)
            ha.config_reads += 1
            return {
                "version": "2026.10.0",
                "location_name": "Test House",
                "unit_system": {},
                "latitude": 40.0,
                "longitude": -75.0,
                "time_zone": "America/New_York",
            }

        @app.get("/api/states")
        async def all_states(authorization: str | None = Header(None)) -> list[dict[str, Any]]:
            ha._auth(authorization)
            ha.all_states_reads += 1
            if self.latency_s:
                await asyncio.sleep(self.latency_s)
            return list(ha.states.values())

        @app.get("/api/states/{eid}")
        async def one_state(eid: str, authorization: str | None = Header(None)) -> dict[str, Any]:
            ha._auth(authorization)
            if eid not in ha.states:
                raise HTTPException(404, "Entity not found.")
            return ha.states[eid]

        @app.post("/api/states/{eid}")
        async def post_state(
            eid: str, request: Request, authorization: str | None = Header(None)
        ) -> dict[str, Any]:
            ha._auth(authorization)
            body = await request.json()
            ha.state_posts.append({"entity_id": eid, **body})
            await ha._change(eid, str(body["state"]), body.get("attributes", {}), BOT_USER_ID)
            return ha.states[eid]

        @app.post("/api/webhook/{webhook_id}")
        async def webhook(webhook_id: str, request: Request) -> dict[str, Any]:
            """HA webhook trigger: unauthenticated; records the auth header to prove none sent."""
            body = await request.json()
            ha.webhook_posts.append(
                {
                    "webhook_id": webhook_id,
                    "auth": request.headers.get("authorization"),
                    "body": body,
                }
            )
            return {}

        @app.post("/api/services/{domain}/{service}")
        async def service(
            domain: str, service: str, request: Request, authorization: str | None = Header(None)
        ) -> list[dict[str, Any]]:
            ha._auth(authorization)
            return await ha.call(domain, service, await request.json())

        @app.websocket("/api/websocket")
        async def ws_endpoint(ws: WebSocket) -> None:
            await ws.accept()
            await ws.send_json({"type": "auth_required", "ha_version": "2026.10.0"})
            msg = await ws.receive_json()
            ha.ws_auth_attempts += 1
            if msg.get("type") != "auth" or msg.get("access_token") != ha.token:
                await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
                await ws.close()
                return
            await ws.send_json({"type": "auth_ok", "ha_version": "2026.10.0"})
            ha._sockets.append(ws)
            try:
                while True:
                    await ha._ws_msg(ws, await ws.receive_json())
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                ha._subs = [s for s in ha._subs if s[0] is not ws]
                if ws in ha._sockets:
                    ha._sockets.remove(ws)

        return app

    async def _ws_msg(self, ws: WebSocket, m: dict[str, Any]) -> None:
        mid, typ = m.get("id"), m.get("type")
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if typ == "ping":
            await ws.send_json({"id": mid, "type": "pong"})
        elif typ == "get_states":
            await ws.send_json(
                {"id": mid, "type": "result", "success": True, "result": list(self.states.values())}
            )
        elif typ == "subscribe_events":
            self._subs.append((ws, int(mid or 0)))
            await ws.send_json({"id": mid, "type": "result", "success": True, "result": None})
        elif typ == "call_service":
            data = {**m.get("service_data", {}), **m.get("target", {})}
            if m.get("domain") == "weather" and m.get("service") == "get_forecasts":
                self.calls.append({"domain": "weather", "service": "get_forecasts", "data": data})
                resp = {"weather.home": {"forecast": hourly_forecast()}}
                await ws.send_json(
                    {
                        "id": mid,
                        "type": "result",
                        "success": True,
                        "result": {"context": {}, "response": resp},
                    }
                )
                return
            try:
                await self.call(str(m["domain"]), str(m["service"]), data)
                await ws.send_json({"id": mid, "type": "result", "success": True, "result": {}})
            except HTTPException as e:
                await ws.send_json(
                    {
                        "id": mid,
                        "type": "result",
                        "success": False,
                        "error": {"code": "unauthorized", "message": e.detail},
                    }
                )
        else:
            await ws.send_json(
                {
                    "id": mid,
                    "type": "result",
                    "success": False,
                    "error": {"code": "unknown_command", "message": str(typ)},
                }
            )
