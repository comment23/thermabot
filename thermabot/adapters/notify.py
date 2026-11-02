"""Alert webhook (D§12.1): POST the envelope to ALERT_WEBHOOK_URL with Bearer token;
on non-2xx/transport error retry after 1, 5 and 15 min, then report 'failed:<err>'.
Delivery failure never affects control (callers fire-and-forget)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from thermabot.core.types import AlertEvent

log = logging.getLogger(__name__)
RETRY_DELAYS_S = (60.0, 300.0, 900.0)
_SEV = {"info": "info", "warn": "warning", "critical": "critical"}


def envelope(
    kind: Literal["alert", "weekly_summary"],
    key: str,
    severity: str,
    status: Literal["open", "closed"],
    title: str,
    text: str,
    ts: datetime,
    url: str | None = None,
) -> dict[str, Any]:
    return {
        "source": "thermabot",
        "kind": kind,
        "key": key,
        "severity": _SEV.get(severity, severity),
        "status": status,
        "title": title,
        "text": text,
        "ts": ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "url": url,
    }


def alert_envelope(ev: AlertEvent, url: str | None = None) -> dict[str, Any]:
    status: Literal["open", "closed"] = "closed" if ev.clear else "open"
    title = f"thermabot {ev.key}" + (" cleared" if ev.clear else "")
    return envelope("alert", ev.key, ev.severity, status, title, ev.text, ev.t, url)


class Notifier:
    def __init__(
        self,
        url: str | None,
        token: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.url, self._token, self._sleep = url, token, sleep
        self._http = httpx.AsyncClient(timeout=10, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, body: dict[str, Any]) -> str | None:
        """None on success, else a short error string."""
        assert self.url is not None
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            r = await self._http.post(self.url, json=body, headers=headers)
        except httpx.HTTPError as e:
            return type(e).__name__
        return None if 200 <= r.status_code < 300 else f"HTTP {r.status_code}"

    async def deliver(self, body: dict[str, Any]) -> str:
        """Returns the `alerts.delivery` value: 'ok' or 'failed:<err>'. Never raises."""
        if not self.url:
            return "failed:no webhook url"
        err = await self._post(body)
        for delay in RETRY_DELAYS_S:
            if err is None:
                return "ok"
            log.warning("alert webhook %s; retry in %.0f s", err, delay)
            await self._sleep(delay)
            err = await self._post(body)
        return "ok" if err is None else f"failed:{err}"
