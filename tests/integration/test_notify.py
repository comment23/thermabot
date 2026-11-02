"""T1.26: alert webhook envelope + retries (D§12.1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from thermabot.adapters.notify import RETRY_DELAYS_S, Notifier, alert_envelope, envelope
from thermabot.core.types import AlertEvent

T0 = datetime(2026, 10, 5, 18, 17, tzinfo=UTC)
KEYS = {"source", "kind", "key", "severity", "status", "title", "text", "ts", "url"}


class Rec:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses, self.reqs, self.sleeps = statuses, [], []  # type: ignore[var-annotated]

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.reqs.append(req)
        return httpx.Response(self.statuses[min(len(self.reqs) - 1, len(self.statuses) - 1)])

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)


def notifier(rec: Rec) -> Notifier:
    return Notifier(
        "http://hook/thermabot",
        "hooktoken",
        sleep=rec.sleep,
        transport=httpx.MockTransport(rec.handler),
    )


@pytest.mark.brd("FR-ALR-1")
def test_envelope_contract() -> None:
    ev = AlertEvent(key="degraded", severity="warn", text="HA down", t=T0)
    e = alert_envelope(ev, url="http://thermabot.local:8095/health")
    assert set(e) == KEYS
    assert e["source"] == "thermabot" and e["kind"] == "alert" and e["status"] == "open"
    assert e["severity"] == "warning" and e["ts"] == "2026-10-05T18:17:00Z"
    closed = alert_envelope(ev.model_copy(update={"clear": True}))
    assert closed["status"] == "closed"
    s = envelope("weekly_summary", "weekly", "info", "open", "Week", "line1\nline2", T0)
    assert s["kind"] == "weekly_summary" and "\n" in s["text"] and "\\n" not in s["text"]


@pytest.mark.brd("FR-ALR-1")
async def test_first_try_ok_with_bearer() -> None:
    rec = Rec([204])
    n = notifier(rec)
    body = alert_envelope(AlertEvent(key="k", severity="critical", text="x", t=T0))
    assert await n.deliver(body) == "ok"
    assert rec.sleeps == [] and rec.reqs[0].headers["Authorization"] == "Bearer hooktoken"
    assert json.loads(rec.reqs[0].content) == body
    await n.aclose()


@pytest.mark.brd("FR-ALR-1")
async def test_retries_1_5_15_then_ok() -> None:
    rec = Rec([500, 502, 503, 200])
    n = notifier(rec)
    assert await n.deliver({"x": 1}) == "ok"
    assert rec.sleeps == [60.0, 300.0, 900.0] == list(RETRY_DELAYS_S)
    await n.aclose()


@pytest.mark.brd("FR-ALR-1")
async def test_all_fail_reports_failed() -> None:
    rec = Rec([500])
    n = notifier(rec)
    assert await n.deliver({"x": 1}) == "failed:HTTP 500"
    assert len(rec.reqs) == 4
    await n.aclose()


@pytest.mark.brd("FR-ALR-1")
async def test_transport_error_and_no_url_never_raise() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    sleeps: list[float] = []

    async def sl(s: float) -> None:
        sleeps.append(s)

    n = Notifier("http://hook", None, transport=httpx.MockTransport(boom), sleep=sl)
    assert await n.deliver({}) == "failed:ConnectError" and len(sleeps) == 3
    assert await Notifier(None, None).deliver({}) == "failed:no webhook url"
    await n.aclose()
