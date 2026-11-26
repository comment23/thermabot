"""Safety boundaries of the opt-in lab, not changes to production defaults."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from scripts.uat import SimulatedHA, create_lab
from thermabot.adapters.ha_rest import HAError


@pytest.mark.brd("NFR-SAFE-1")
async def test_uat_refuses_existing_data_and_ignores_live_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "thermabot.db").write_bytes(b"must not open this existing database")
    with pytest.raises(ValueError, match="existing data refused"):
        create_lab(tmp_path)
    monkeypatch.setenv("HA_URL", "http://must-not-connect.test")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://must-not-connect.test")
    monkeypatch.setenv("INFLUX_READ_TOKEN", "must-not-load-this-value")
    monkeypatch.setenv("BOB_TOKEN", "must-not-load-this-value")
    lab = create_lab(tmp_path / "isolated")
    try:
        assert lab.runtime.env.HA_URL == "http://ha.test"
        assert lab.runtime.env.ALERT_WEBHOOK_URL is None
        assert lab.runtime.env.INFLUX_READ_TOKEN is None
        assert lab.runtime.env.BOB_TOKEN is None
        await lab.tick(datetime.now(UTC))
        assert lab.evidence()["snapshot"]["rooms"]["study"]["temp"] == 70.5
    finally:
        await lab.runtime.aclose()


@pytest.mark.brd("NFR-SAFE-1")
async def test_uat_blocks_active_before_it_reaches_application(tmp_path: Path) -> None:
    lab = create_lab(tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=lab.app), base_url="http://uat.test"
        ) as client:
            response = await client.post("/api/kill", json={"enabled": True, "mode": "active"})
            assert response.status_code == 403 and "SHADOW only" in response.text
            assert lab.evidence()["run_mode"] == "shadow"
            assert lab.evidence()["kill_switch"] == "off"
            response = await client.post("/uat/api/scenario", json={"name": "ha-outage"})
            assert response.status_code == 403
            assert lab.transport.scenario == "healthy"
            response = await client.post(
                "/uat/api/scenario", headers={"X-Thermabot-UAT": "1"}, json={"name": "invalid"}
            )
            assert response.status_code == 422
    finally:
        await lab.runtime.aclose()


@pytest.mark.brd("NFR-SAFE-1")
async def test_uat_transport_refuses_network_and_climate_writes() -> None:
    transport = SimulatedHA()
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://ha.test") as client:
            response = await client.post(
                "/api/services/climate/set_temperature", json={"entity_id": "climate.living_room"}
            )
            assert response.status_code == 403
            assert transport.ha.calls == []
            assert transport.blocked_writes == ["/api/services/climate/set_temperature"]
            with pytest.raises(RuntimeError, match="outside simulated"):
                await client.get("http://must-not-connect.test/api/states")
    finally:
        await transport.aclose()


@pytest.mark.brd("FR-CFG-1")
async def test_fresh_uat_remains_unconfigured_and_reads_no_climate(tmp_path: Path) -> None:
    lab = create_lab(tmp_path, fresh=True)
    try:
        await lab.tick(datetime.now(UTC))
        assert lab.evidence()["climate_reads"] == []
        assert lab.evidence()["run_mode"] == "shadow"
        assert lab.evidence()["kill_switch"] == "off"
        assert lab.runtime.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        with pytest.raises(HAError):
            await lab.runtime.ha.get_state("climate.living_room")
        assert lab.evidence()["climate_reads"] == []
    finally:
        await lab.runtime.aclose()


@pytest.mark.brd("FR-WX-1")
async def test_uat_weather_fixtures_remain_current_during_multiday_sessions(tmp_path: Path) -> None:
    lab = create_lab(tmp_path)
    future = (datetime.now(UTC) + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    try:
        assert lab.runtime.nws_transport is not None
        with patch("scripts.uat.datetime") as wall_clock:
            wall_clock.now.return_value = future
            async with httpx.AsyncClient(transport=lab.runtime.nws_transport) as client:
                response = await client.get(
                    "https://api.weather.gov/gridpoints/test/forecast/hourly"
                )
            periods = response.json()["properties"]["periods"]
            assert datetime.fromisoformat(periods[0]["startTime"]) == future
    finally:
        await lab.runtime.aclose()
