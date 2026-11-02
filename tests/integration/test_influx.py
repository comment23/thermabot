"""T1.24: Influx adapter against httpx.MockTransport in Bob's schema
(measurement = unit, tag entity_id without domain, field `value`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from thermabot.adapters.influx import Influx, flux_query, line, parse_csv, strip_domain

T0 = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)
HDR = (
    "#datatype,string,long,dateTime:RFC3339,double,string,string,string\n"
    "#group,false,false,false,false,true,true,true\n#default,_result,,,,,,\n"
    ",result,table,_time,_value,_field,_measurement,entity_id\n"
)
# Two tables: compressor speed under '%' and blower under its own entity-named measurement.
CSV = (
    HDR
    + ",,0,2026-10-05T17:00:00Z,35,value,%,living_room_compressor_speed\n"
    + ",,0,2026-10-05T17:05:00Z,100,value,%,living_room_compressor_speed\n"
    + "\n"
    + HDR
    + ",,1,2026-10-05T17:00:00Z,1,value,binary_sensor.living_room_blower_active,"
    "living_room_blower_active\n"
)


def make(handler: httpx.MockTransport) -> Influx:
    return Influx(
        "http://influx", "home", "ha_hvac", "thermabot", "rtok", "wtok", transport=handler
    )


@pytest.mark.brd("FR-PLN-3")
def test_flux_strips_domain_and_ignores_measurement() -> None:
    q = flux_query("ha_hvac", "climate.living_room", ["value"])
    assert 'r.entity_id == "living_room"' in q
    assert "_measurement" not in q and 'bucket:"ha_hvac"' in q
    assert strip_domain("binary_sensor.living_room_blower_active") == "living_room_blower_active"


@pytest.mark.brd("FR-PLN-3")
def test_parse_multi_table_csv() -> None:
    rows = parse_csv(CSV)
    assert [r["value"] for r in rows] == [35.0, 100.0, 1.0]
    assert rows[0]["measurement"] == "%" and rows[0]["t"] == T0 - timedelta(hours=1)


@pytest.mark.brd("FR-PLN-3")
async def test_read_entity_request_shape() -> None:
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text=CSV)

    ix = make(httpx.MockTransport(h))
    rows = await ix.read_entity("sensor.living_room_compressor_speed", ["value"])
    assert len(rows) == 3
    r = seen[0]
    assert r.url.path == "/api/v2/query" and r.url.params["org"] == "home"
    assert r.headers["Authorization"] == "Token rtok"
    assert b'"living_room_compressor_speed"' in r.content
    await ix.aclose()


@pytest.mark.brd("NFR-REL-2")
def test_line_protocol() -> None:
    s = line("thermabot_state", {"state": "SHADOW"}, {"score": 91.5, "aux_flag": 0, "n": "a b"}, T0)
    assert s == (
        'thermabot_state,state=SHADOW score=91.5,aux_flag=0i,n="a b" '
        f"{int(T0.timestamp()) * 1_000_000_000}"
    )


@pytest.mark.brd("NFR-REL-2")
async def test_write_buffers_24h_when_down_then_flushes() -> None:
    up = {"ok": False}
    bodies: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        bodies.append(req)
        return httpx.Response(204) if up["ok"] else httpx.Response(503)

    ix = make(httpx.MockTransport(h))
    ix.enqueue(["old v=1i 1"], now=T0)
    ix.enqueue(["new v=2i 2"], now=T0 + timedelta(hours=20))
    assert await ix.flush(now=T0 + timedelta(hours=20)) is False
    assert len(ix.buffer) == 2 and ix.last_error == "HTTPStatusError"
    up["ok"] = True
    assert await ix.flush(now=T0 + timedelta(hours=25)) is True  # 'old' expired (> 24 h)
    last = bodies[-1]
    assert last.content == b"new v=2i 2" and last.url.params["bucket"] == "thermabot"
    assert last.headers["Authorization"] == "Token wtok" and not ix.buffer
    await ix.aclose()


@pytest.mark.brd("NFR-REL-2")
async def test_flush_never_raises_on_connect_error() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    ix = make(httpx.MockTransport(h))
    ix.enqueue(["x v=1i 1"], now=T0)
    assert await ix.flush(now=T0) is False and len(ix.buffer) == 1
    await ix.aclose()


@pytest.mark.brd("NFR-REL-2")
def test_stray_setup_probe_point_ignored() -> None:
    stray = HDR.replace("entity_id", "ok") + ",,0,2026-10-05T17:00:00Z,1,ok,setup_probe,\n"
    rows = [r for r in parse_csv(stray) if r["measurement"].startswith("thermabot_")]
    assert rows == []
