"""HEARTBEAT_WEBHOOK_URL heartbeat (B3 rev 2026-10-05): POST, no auth, no /api/states."""

from __future__ import annotations

import json

import httpx
import pytest

from thermabot.adapters.heartbeat import HeartbeatWebhook

URL = "http://ha/api/webhook/hb-test"


@pytest.mark.brd("NFR-SAFE-7")
async def test_posts_json_without_auth() -> None:
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200)

    hb = HeartbeatWebhook(URL, transport=httpx.MockTransport(h))
    await hb.publish("sensor.thermabot_heartbeat", "2026-10-05T18:00:00+00:00", {"state": "X"})
    await hb.aclose()
    assert len(seen) == 1 and seen[0].method == "POST" and str(seen[0].url) == URL
    assert "authorization" not in seen[0].headers
    assert json.loads(seen[0].content) == {"ts": "2026-10-05T18:00:00+00:00", "state": "X"}


@pytest.mark.brd("NFR-SAFE-7")
async def test_non_2xx_raises_so_loops_log_it() -> None:
    hb = HeartbeatWebhook(URL, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(httpx.HTTPStatusError):
        await hb.publish("x", "t", {})
    await hb.aclose()


@pytest.mark.brd("NFR-SAFE-7")
async def test_unset_url_is_noop() -> None:
    called: list[httpx.Request] = []
    t = httpx.MockTransport(lambda r: called.append(r) or httpx.Response(200))
    for url in (None, "", "  "):
        hb = HeartbeatWebhook(url, transport=t)
        await hb.publish("x", "t", {})
        await hb.aclose()
    assert called == []
