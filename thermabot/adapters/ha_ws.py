"""HA WebSocket client (D§7.1): auth, get_states seed, state_changed subscribe, ping,
backoff reconnect, weather.get_forecasts, auth-failed handling (no tight loop)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import websockets
from websockets.asyncio.client import ClientConnection

log = logging.getLogger(__name__)

Health = Literal["connecting", "ok", "down", "auth_failed"]
# v0.6.1: no house-specific ids here; configured HVAC/room sensors are passed in `extra`.
READ_PREFIXES = ("person.", "device_tracker.")
READ_EXACT = (
    "weather.home",
    "zone.home",
    "input_boolean.thermabot_enabled",
    "input_boolean.thermabot_watchdog",
    "input_datetime.thermabot_watchdog_last",
)  # read-only (D§13); plus the configured thermostat entity (FR-CFG-1)
BACKOFF_MAX_S = 60.0
AUTH_FAIL_BACKOFF_S = 300.0


class AuthFailed(Exception):
    pass


def in_read_set(eid: str, extra: frozenset[str] = frozenset(), thermostat: str = "") -> bool:
    return (
        (bool(thermostat) and eid == thermostat)
        or eid in READ_EXACT
        or eid in extra
        or eid.startswith(READ_PREFIXES)
    )


def backoff_s(attempt: int, rng: random.Random | None = None) -> float:
    """1, 2, 4 ... 60 s plus up to 10% jitter."""
    base = min(BACKOFF_MAX_S, 2.0 ** max(0, attempt))
    return base + (rng or random).uniform(0, base * 0.1)


class HAWebSocket:
    def __init__(
        self,
        url: str,
        token: str,
        extra_entities: frozenset[str] = frozenset(),
        thermostat_entity: str = "",
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        ping_s: float = 30.0,
        pong_timeout_s: float = 10.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.url, self._token = url, token
        self.extra = extra_entities
        self.thermostat = thermostat_entity  # settings.thermostat.entity_id (FR-CFG-1)
        self.on_event = on_event
        self.ping_s, self.pong_timeout_s = ping_s, pong_timeout_s
        self._sleep = sleep
        self.cache: dict[str, dict[str, Any]] = {}
        self.health: Health = "connecting"
        self.connects = 0
        self.seeds = 0
        self._ws: ClientConnection | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._stop = asyncio.Event()

    # ---- public --------------------------------------------------------------------
    async def run(self) -> None:
        """Connect forever with backoff until stop()."""
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._session()
                attempt = 0
            except AuthFailed:
                self.health = "auth_failed"
                log.error("HA WS auth_invalid; backing off %ss", AUTH_FAIL_BACKOFF_S)
                await self._sleep(AUTH_FAIL_BACKOFF_S)
                continue
            except (OSError, websockets.WebSocketException, TimeoutError) as e:
                log.warning("HA WS down: %s", type(e).__name__)
            if self._stop.is_set():
                break
            self.health = "down"
            await self._sleep(backoff_s(attempt))
            attempt += 1

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            asyncio.ensure_future(self._ws.close())

    async def call(self, msg: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        if self._ws is None:
            raise ConnectionError("HA WS not connected")
        self._id += 1
        mid = self._id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps({"id": mid, **msg}))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    async def get_forecasts(
        self, kind: Literal["hourly", "daily"] = "hourly", entity: str = "weather.home"
    ) -> list[dict[str, Any]]:
        r = await self.call(
            {
                "type": "call_service",
                "domain": "weather",
                "service": "get_forecasts",
                "service_data": {"type": kind},
                "target": {"entity_id": entity},
                "return_response": True,
            }
        )
        if not r.get("success"):
            raise RuntimeError(f"get_forecasts failed: {r.get('error')}")
        fc: list[dict[str, Any]] = r["result"]["response"][entity]["forecast"]
        return fc

    # ---- internals -----------------------------------------------------------------
    async def _session(self) -> None:
        async with websockets.connect(self.url, ping_interval=None, close_timeout=2) as ws:
            first = json.loads(await ws.recv())
            if first.get("type") != "auth_required":
                raise websockets.WebSocketException("unexpected greeting")
            await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            res = json.loads(await ws.recv())
            if res.get("type") == "auth_invalid":
                raise AuthFailed(res.get("message", ""))
            if res.get("type") != "auth_ok":
                raise websockets.WebSocketException("auth not ok")
            self._ws, self._id = ws, 0
            self.connects += 1
            reader = asyncio.create_task(self._reader(ws))
            pinger = asyncio.create_task(self._pinger())
            try:
                await self._seed()
                await self.call({"type": "subscribe_events", "event_type": "state_changed"})
                self.health = "ok"
                done, _ = await asyncio.wait({reader, pinger}, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception() is not None:
                        raise t.exception()  # type: ignore[misc]
            finally:
                for t in (reader, pinger):
                    t.cancel()
                    with contextlib.suppress(BaseException):
                        await t
                for f in self._pending.values():
                    f.cancel()
                self._pending.clear()
                self._ws = None

    async def _seed(self) -> None:
        r = await self.call({"type": "get_states"})
        self.cache = {
            s["entity_id"]: s
            for s in r.get("result") or []
            if in_read_set(s["entity_id"], self.extra, self.thermostat)
        }
        self.seeds += 1

    async def _reader(self, ws: ClientConnection) -> None:
        try:
            await self._read_loop(ws)
        finally:  # wake any caller waiting on a reply from a dead socket
            for f in self._pending.values():
                if not f.done():
                    f.set_exception(ConnectionError("ws closed"))

    async def _read_loop(self, ws: ClientConnection) -> None:
        async for raw in ws:
            m = json.loads(raw)
            if m.get("type") == "event":
                await self._handle_event(m["event"])
                continue
            fut = self._pending.get(int(m.get("id", -1)))
            if fut is not None and not fut.done():
                fut.set_result(m)

    async def _handle_event(self, ev: dict[str, Any]) -> None:
        data = ev.get("data", {})
        eid = str(data.get("entity_id", ""))
        if not in_read_set(eid, self.extra, self.thermostat):
            return
        new = data.get("new_state")
        if new is None:
            self.cache.pop(eid, None)
        else:
            self.cache[eid] = new
        if self.on_event is not None:
            await self.on_event(ev)

    async def _pinger(self) -> None:
        while True:
            await asyncio.sleep(self.ping_s)
            try:
                await self.call({"type": "ping"}, timeout=self.pong_timeout_s)
            except TimeoutError as e:
                raise websockets.WebSocketException("pong missed") from e
