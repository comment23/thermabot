"""T1.21: mock HA (D§7.5) speaks REST + WS and counts service calls."""

from __future__ import annotations

import json

import httpx
import pytest
import websockets

from tests.integration.mock_ha import FOREIGN_USER_ID, GOOD_TOKEN, MockHA, MockServer

TSTAT = "climate.living_room"
H = {"Authorization": f"Bearer {GOOD_TOKEN}"}


def client(ha: MockHA) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=ha.app), base_url="http://ha")


@pytest.mark.brd("FR-HA-1")
async def test_rest_states_and_trane_fixtures() -> None:
    ha = MockHA()
    async with client(ha) as c:
        r = await c.get(f"/api/states/{TSTAT}", headers=H)
        a = r.json()["attributes"]
        assert r.status_code == 200 and "heat_cool" in a["hvac_modes"]
        assert {"fan_modes", "preset_modes", "hvac_action"} <= set(a)
        ids = {s["entity_id"] for s in (await c.get("/api/states", headers=H)).json()}
        assert "sensor.living_room_compressor_speed" in ids
        assert "binary_sensor.living_room_blower_active" in ids


@pytest.mark.brd("NFR-SEC-3")
async def test_rest_bad_token_401() -> None:
    async with client(MockHA()) as c:
        r = await c.get("/api/states", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401


@pytest.mark.brd("FR-HA-4")
async def test_service_call_counted_and_applied() -> None:
    ha = MockHA()
    async with client(ha) as c:
        r = await c.post(
            "/api/services/climate/set_temperature",
            headers=H,
            json={
                "entity_id": TSTAT,
                "hvac_mode": "heat_cool",
                "target_temp_low": 67,
                "target_temp_high": 77,
            },
        )
        assert r.status_code == 200
    assert len(ha.calls) == 1
    assert ha.states[TSTAT]["attributes"]["target_temp_low"] == 67


@pytest.mark.brd("FR-HA-4")
async def test_ignore_writes_knob() -> None:
    ha = MockHA()
    ha.ignore_writes = True
    async with client(ha) as c:
        await c.post(
            "/api/services/climate/set_temperature",
            headers=H,
            json={"entity_id": TSTAT, "target_temp_low": 66, "target_temp_high": 76},
        )
    assert len(ha.calls) == 1
    assert ha.states[TSTAT]["attributes"]["target_temp_low"] == 68


@pytest.mark.brd("FR-OVR-3")
async def test_external_change_foreign_context() -> None:
    ha = MockHA()
    await ha.external_change(hvac_mode="heat", temperature=72)
    s = ha.states[TSTAT]
    assert s["state"] == "heat" and s["context"]["user_id"] == FOREIGN_USER_ID
    assert ha.calls == []


@pytest.mark.brd("FR-HA-1")
async def test_ws_protocol_and_drop() -> None:
    with MockServer() as srv:
        async with websockets.connect(srv.ws_url) as ws:
            assert json.loads(await ws.recv())["type"] == "auth_required"
            await ws.send(json.dumps({"type": "auth", "access_token": GOOD_TOKEN}))
            assert json.loads(await ws.recv())["type"] == "auth_ok"
            await ws.send(json.dumps({"id": 1, "type": "get_states"}))
            assert len(json.loads(await ws.recv())["result"]) > 5
            await ws.send(
                json.dumps({"id": 2, "type": "subscribe_events", "event_type": "state_changed"})
            )
            assert json.loads(await ws.recv())["success"] is True
            await srv.run(srv.ha.external_change(temperature=70))
            ev = json.loads(await ws.recv())
            assert ev["type"] == "event" and ev["event"]["data"]["entity_id"] == TSTAT
            await ws.send(json.dumps({"id": 3, "type": "ping"}))
            assert json.loads(await ws.recv())["type"] == "pong"
            await srv.run(srv.ha.drop_ws())
            with pytest.raises(websockets.ConnectionClosed):
                await ws.recv()


@pytest.mark.brd("NFR-SEC-3")
async def test_ws_bad_token() -> None:
    with MockServer() as srv:
        async with websockets.connect(srv.ws_url) as ws:
            await ws.recv()
            await ws.send(json.dumps({"type": "auth", "access_token": "bad"}))
            assert json.loads(await ws.recv())["type"] == "auth_invalid"
