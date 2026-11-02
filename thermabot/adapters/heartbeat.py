"""Heartbeat to the HA webhook automation `thermabot_heartbeat` (D\u00a713, B3 rev 2026-10-05).

POST a small JSON body to HEARTBEAT_WEBHOOK_URL with NO auth header (HA webhook triggers are
unauthenticated, local_only). The HA watchdog reads the automation's `last_triggered`, so the
bot token stays non-admin and never writes /api/states. Unset URL -> no-op (warned once).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class HeartbeatWebhook:
    def __init__(self, url: str | None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = (url or "").strip() or None
        self._warned = False
        self._http = httpx.AsyncClient(timeout=5, transport=transport)

    async def publish(self, entity_id: str, state: str, attributes: dict[str, Any]) -> None:
        """Loops.Publish signature. Raises on transport error / non-2xx (Loops logs it)."""
        if self.url is None:
            if not self._warned:
                log.warning("HEARTBEAT_WEBHOOK_URL not set: heartbeat disabled")
                self._warned = True
            return
        r = await self._http.post(self.url, json={"ts": state, **attributes})
        r.raise_for_status()

    async def aclose(self) -> None:
        await self._http.aclose()
