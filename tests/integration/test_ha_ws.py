"""T1.22: HA WebSocket client (D§7.1) against the mock HA."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable

import pytest

from tests.integration.mock_ha import GOOD_TOKEN, MockServer
from thermabot.adapters.ha_ws import AUTH_FAIL_BACKOFF_S, HAWebSocket, backoff_s, in_read_set

TSTAT = "climate.living_room"


class FastSleep:
    """Records requested delays, sleeps only a few ms."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, s: float) -> None:
        self.delays.append(s)
        await asyncio.sleep(0.02)


async def until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.02)


@pytest.mark.brd("FR-HA-1")
async def test_seed_subscribe_event_and_forecast() -> None:
    with MockServer() as srv:
        c = HAWebSocket(srv.ws_url, GOOD_TOKEN, thermostat_entity=TSTAT)
        task = asyncio.create_task(c.run())
        await until(lambda: c.health == "ok")
        assert TSTAT in c.cache and "climate.garage_heater" not in c.cache
        assert "sensor.study_temperature" not in c.cache  # not in read set without config
        await srv.run(srv.ha.external_change(temperature=73))
        await until(lambda: c.cache[TSTAT]["attributes"]["temperature"] == 73)
        fc = await c.get_forecasts("hourly")
        assert len(fc) == 48 and "temperature" in fc[0]
        c.stop()
        await asyncio.wait_for(task, 5)


@pytest.mark.brd("NFR-REL-1")
async def test_disconnect_reconnects_and_reseeds() -> None:
    sleep = FastSleep()
    with MockServer() as srv:
        c = HAWebSocket(srv.ws_url, GOOD_TOKEN, sleep=sleep)
        task = asyncio.create_task(c.run())
        await until(lambda: c.health == "ok")
        assert (c.connects, c.seeds) == (1, 1)
        await srv.run(srv.ha.drop_ws())
        await until(lambda: c.connects == 2 and c.health == "ok")
        assert c.seeds == 2
        assert sleep.delays and 1.0 <= sleep.delays[0] <= 1.1
        c.stop()
        await asyncio.wait_for(task, 5)


@pytest.mark.brd("NFR-REL-1")
async def test_missed_pong_reconnects() -> None:
    sleep = FastSleep()
    with MockServer() as srv:
        c = HAWebSocket(srv.ws_url, GOOD_TOKEN, sleep=sleep, ping_s=0.05, pong_timeout_s=0.1)
        task = asyncio.create_task(c.run())
        await until(lambda: c.health == "ok")
        srv.ha.latency_s = 0.5  # pong arrives too late
        await until(lambda: c.connects >= 2, timeout=15)
        srv.ha.latency_s = 0
        c.stop()
        await asyncio.wait_for(task, 5)


@pytest.mark.brd("NFR-SEC-3")
async def test_invalid_token_auth_failed_no_tight_loop() -> None:
    sleep = FastSleep()
    with MockServer() as srv:
        c = HAWebSocket(srv.ws_url, "wrong-token", sleep=sleep)
        task = asyncio.create_task(c.run())
        await until(lambda: len(sleep.delays) >= 2)
        c.stop()
        await asyncio.wait_for(task, 5)
        assert c.health == "auth_failed"
        assert all(d == AUTH_FAIL_BACKOFF_S for d in sleep.delays)
        assert srv.ha.ws_auth_attempts <= len(sleep.delays) + 1


@pytest.mark.brd("NFR-REL-1")
def test_backoff_schedule() -> None:
    rng = random.Random(1)
    for i, base in enumerate([1, 2, 4, 8, 16, 32, 60, 60]):
        assert base <= backoff_s(i, rng) <= base * 1.1


@pytest.mark.brd("FR-HA-1")
def test_read_set() -> None:
    assert in_read_set(TSTAT, thermostat=TSTAT) and in_read_set("person.alex")
    assert not in_read_set(TSTAT)  # v0.2.0: no thermostat configured -> no climate read
    assert in_read_set("input_boolean.thermabot_enabled")
    assert not in_read_set("climate.garage_heater")
    assert in_read_set("sensor.study_temperature", frozenset({"sensor.study_temperature"}))
