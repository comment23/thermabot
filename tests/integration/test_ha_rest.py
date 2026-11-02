"""T1.23: HA REST adapter (D§7.2-7.4) incl. adapter-layer write allowlist."""

from __future__ import annotations

import logging

import httpx
import pytest

from tests.integration.mock_ha import GOOD_TOKEN, MockHA
from thermabot.adapters import ha_rest
from thermabot.adapters.ha_rest import HAError, HARest, WriteRefused, _call_service
from thermabot.core.types import Action

TSTAT = "climate.living_room"
FOREIGN = "climate.garage_heater"


def mock_client(ha: MockHA, token: str = GOOD_TOKEN) -> HARest:
    return HARest(
        "http://ha", token, transport=httpx.ASGITransport(app=ha.app), writable_entity=TSTAT
    )


class NoRequest(httpx.AsyncBaseTransport):
    """Fails the test if the adapter makes ANY HTTP request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise AssertionError(f"unexpected HTTP request {request.method} {request.url}")


@pytest.mark.brd("FR-HA-1")
async def test_read_fallback() -> None:
    c = mock_client(MockHA())
    s = await c.get_states([TSTAT, "sensor.nope", "input_boolean.thermabot_enabled"])
    assert set(s) == {TSTAT, "input_boolean.thermabot_enabled"}
    assert await c.get_state("sensor.nope") is None
    await c.aclose()


@pytest.mark.brd("FR-HA-6")
async def test_publish_sensor_prefix_only() -> None:
    ha = MockHA()
    c = mock_client(ha)
    await c.publish_sensor("sensor.thermabot_heartbeat", "2026-10-05T18:00:00Z", {"state": "X"})
    assert ha.states["sensor.thermabot_heartbeat"]["state"].startswith("2026")
    with pytest.raises(WriteRefused):
        await c.publish_sensor("sensor.other", "1")
    assert len(ha.state_posts) == 1 and ha.calls == []
    await c.aclose()


@pytest.mark.brd("FR-HA-7")
async def test_mirror_turn_on_off() -> None:
    ha = MockHA()
    c = mock_client(ha)
    await c.set_mirror(True)
    assert ha.states["input_boolean.thermabot_enabled"]["state"] == "on"
    await c.set_mirror(False)
    assert ha.states["input_boolean.thermabot_enabled"]["state"] == "off"
    await c.aclose()


@pytest.mark.brd("FR-HA-2")
@pytest.mark.parametrize(
    "domain,service,eid",
    [
        ("climate", "set_temperature", FOREIGN),
        ("climate", "set_hvac_mode", FOREIGN),
        ("climate", "turn_off", TSTAT),
        ("input_boolean", "turn_on", "input_boolean.thermabot_watchdog"),
        ("input_boolean", "toggle", "input_boolean.thermabot_enabled"),
        ("input_datetime", "set_datetime", "input_datetime.thermabot_watchdog_last"),
        ("switch", "turn_on", "switch.anything"),
        ("notify", "notify", "notify.mobile"),
    ],
)
async def test_adapter_refuses_before_any_http(domain: str, service: str, eid: str) -> None:
    t = NoRequest()
    c = HARest("http://ha", GOOD_TOKEN, transport=t, writable_entity=TSTAT)
    with pytest.raises(WriteRefused):
        await c._post_service(domain, service, {"entity_id": eid})
    assert t.requests == []
    await c.aclose()


@pytest.mark.brd("FR-HA-2")
async def test_call_service_refuses_foreign_zero_http() -> None:
    t = NoRequest()
    c = HARest("http://ha", GOOD_TOKEN, transport=t, writable_entity=TSTAT)
    act = Action(service="set_temperature", entity_id=FOREIGN, data={"temperature": 70})
    with pytest.raises(WriteRefused):
        await _call_service(c, act)
    with pytest.raises(WriteRefused):  # list entity_id smuggling
        await c._post_service("climate", "set_temperature", {"entity_id": [TSTAT, FOREIGN]})
    assert t.requests == []
    await c.aclose()


@pytest.mark.brd("FR-HA-4")
async def test_call_service_thermostat_ok() -> None:
    ha = MockHA()
    c = mock_client(ha)
    act = Action(
        service="set_temperature",
        entity_id=TSTAT,
        data={"hvac_mode": "heat_cool", "target_temp_low": 67, "target_temp_high": 75},
    )
    r = await _call_service(c, act)
    assert r.status == 200 and len(ha.calls) == 1
    assert ha.states[TSTAT]["attributes"]["target_temp_high"] == 75
    assert ha.states["climate.garage_heater"]["attributes"]["temperature"] == 74
    await c.aclose()


@pytest.mark.brd("NFR-SEC-3")
async def test_bad_token_and_log_redaction(caplog: pytest.LogCaptureFixture) -> None:
    token = "secret-token-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    c = mock_client(MockHA(), token=token)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(HAError) as ei:
        await c.get_state(TSTAT)
    assert ei.value.status == 401
    logging.getLogger("x").warning("header was Authorization: Bearer %s", token)
    assert token not in caplog.text and "Bearer ***" in caplog.text
    assert ha_rest.redact(f"Bearer {token}") == "Bearer ***"
    await c.aclose()
