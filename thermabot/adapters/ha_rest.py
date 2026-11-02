"""HA REST client (D§7.2-7.4): read fallback, sensor publish, kill-switch mirror, and the
private climate write `_call_service` (imported only by app/controller.py, T1.29).

Defence in depth: HA has no per-entity permissions (the bot token could also drive
any other `climate.*` entity), so every service POST passes `check_write` BEFORE any HTTP request.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from thermabot.core.guards import ALLOWED_SERVICES
from thermabot.core.types import Action

log = logging.getLogger(__name__)
SENSOR_PREFIX = "sensor.thermabot_"
MIRROR_ENTITY = "input_boolean.thermabot_enabled"
MIRROR_SERVICES = ("turn_on", "turn_off")
_BEARER = re.compile(r"(Bearer\s+)\S{20,}")


class WriteRefused(ValueError):
    """A write outside the adapter allowlist. Raised before any network I/O."""


class HAError(Exception):
    def __init__(self, status: int | None, msg: str) -> None:
        super().__init__(msg)
        self.status = status


@dataclass(frozen=True)
class ServiceResult:
    status: int
    ms: int


def redact(text: str) -> str:
    return _BEARER.sub(r"\1***", text)


_installed = False


def install_redaction() -> None:
    """Mask `Bearer <20+ chars>` in every log record, whatever logger emits it."""
    global _installed
    if _installed:
        return
    base = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        rec = base(*args, **kwargs)
        try:
            msg = rec.getMessage()
        except Exception:  # noqa: BLE001 - bad format args; leave untouched
            return rec
        if "Bearer" in msg:
            rec.msg, rec.args = redact(msg), ()
        return rec

    logging.setLogRecordFactory(factory)
    _installed = True


def check_write(domain: str, service: str, data: dict[str, Any], writable_entity: str = "") -> None:
    """Adapter-layer allowlist (second layer behind core.guards.allowlist). Exactly one
    writable climate entity: `writable_entity` (settings.thermostat.entity_id, FR-CFG-1).
    Empty (not configured, v0.2.0) = no climate write is allowed at all."""
    eid = data.get("entity_id")
    if not isinstance(eid, str):
        raise WriteRefused("entity_id must be a single string")
    if domain == "climate":
        if not writable_entity:
            raise WriteRefused("no thermostat configured: climate writes disabled")
        if eid != writable_entity:
            raise WriteRefused(f"climate entity {eid} not writable")
        if service not in ALLOWED_SERVICES:
            raise WriteRefused(f"climate.{service} not allowed")
        return
    if domain == "input_boolean" and eid == MIRROR_ENTITY and service in MIRROR_SERVICES:
        return
    raise WriteRefused(f"{domain}.{service} on {eid} not allowed")


class HARest:
    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
        writable_entity: str = "",
    ) -> None:
        install_redaction()
        self.writable_entity = writable_entity
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            r = await self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise HAError(None, f"{method} {path}: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise HAError(r.status_code, f"{method} {path}: HTTP {r.status_code}")
        return r

    # ---- reads -------------------------------------------------------------------
    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        try:
            r = await self._send("GET", f"/api/states/{entity_id}")
        except HAError as e:
            if e.status == 404:
                return None
            raise
        out: dict[str, Any] = r.json()
        return out

    async def get_states(self, entity_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Read-set fallback while WS is down (polled every 60 s by the loop)."""
        out: dict[str, dict[str, Any]] = {}
        for eid in entity_ids:
            s = await self.get_state(eid)
            if s is not None:
                out[eid] = s
        return out

    async def get_all_states(self) -> list[dict[str, Any]]:
        """GET /api/states (setup wizard discovery, v0.3.0). Read only."""
        r = await self._send("GET", "/api/states")
        out: list[dict[str, Any]] = r.json()
        return out

    async def get_config(self) -> dict[str, Any]:
        """GET /api/config: HA version and location name (setup wizard connection test)."""
        r = await self._send("GET", "/api/config")
        out: dict[str, Any] = r.json()
        return out

    # ---- non-device writes -------------------------------------------------------
    async def publish_sensor(
        self, entity_id: str, state: str, attributes: dict[str, Any] | None = None
    ) -> None:
        if not entity_id.startswith(SENSOR_PREFIX):
            raise WriteRefused(f"publish restricted to {SENSOR_PREFIX}*, got {entity_id}")
        await self._send(
            "POST",
            f"/api/states/{entity_id}",
            json={"state": state, "attributes": attributes or {}},
        )

    async def set_mirror(self, on: bool) -> None:
        """input_boolean.thermabot_enabled (D§7.4). Caller writes the DB row first."""
        await self._post_service(
            "input_boolean", "turn_on" if on else "turn_off", {"entity_id": MIRROR_ENTITY}
        )

    # ---- the one device write ----------------------------------------------------
    async def _post_service(self, domain: str, service: str, data: dict[str, Any]) -> ServiceResult:
        check_write(domain, service, data, self.writable_entity)  # before any HTTP
        t0 = time.monotonic()
        r = await self._send("POST", f"/api/services/{domain}/{service}", json=data)
        return ServiceResult(status=r.status_code, ms=int((time.monotonic() - t0) * 1000))


async def _call_service(client: HARest, action: Action) -> ServiceResult:
    """PRIVATE. Only app/controller.py may import this (T1.29). Guards already ran in core;
    the adapter allowlist re-checks entity + service before any HTTP."""
    data: dict[str, Any] = {"entity_id": action.entity_id, **action.data}
    if action.service == "set_preset_mode" and data.get("preset_mode") in (None, "None"):
        data["preset_mode"] = "none"
    return await client._post_service("climate", action.service, data)
