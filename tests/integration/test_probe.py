"""T1.27: probe script against the in-process mock HA (no network)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from tests.integration.mock_ha import GOOD_TOKEN, MockHA
from thermabot.adapters.ha_rest import HARest

ROOT = Path(__file__).resolve().parents[2]


def load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "probe_thermostat", ROOT / "scripts/probe_thermostat.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["probe_thermostat"] = mod
    spec.loader.exec_module(mod)
    return mod


probe = load_probe()


def client(ha: MockHA) -> HARest:
    return HARest(
        "http://ha",
        GOOD_TOKEN,
        transport=httpx.ASGITransport(app=ha.app),
        writable_entity=probe.TSTAT,
    )


@pytest.mark.brd("FR-HA-3")
async def test_dry_run_prints_all_sections_no_writes(capsys: pytest.CaptureFixture[str]) -> None:
    assert await probe.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    for s in ("thermostat attributes", "compressor / blower", "influx coverage", "write test"):
        assert f"== {s}" in out
    assert "service calls made: 0" in out


@pytest.mark.brd("FR-HA-3")
async def test_write_test_requires_supervised() -> None:
    assert await probe.main(["--write-test"]) == 2
    assert await probe.main(["--dry-run", "--write-test", "--i-am-supervised"]) == 2


@pytest.mark.brd("NFR-SEC-4")
async def test_non_admin_403_reported(capsys: pytest.CaptureFixture[str]) -> None:
    ha = MockHA()
    ha.climate_forbidden = True
    c = client(ha)
    res = await probe.write_test(c, timeout_s=0.2, poll_s=0.01)
    assert res["noop_write_status"] == 403
    assert res["noop_write_error"] == probe.NON_ADMIN_MSG
    assert probe.NON_ADMIN_MSG in capsys.readouterr().out
    assert len(ha.calls) == 1  # stopped after the single no-op attempt
    await c.aclose()


@pytest.mark.brd("FR-HA-4")
async def test_write_test_restores_and_only_touches_thermostat() -> None:
    ha = MockHA()
    before = probe.current_setpoint_data(ha.states[probe.TSTAT])
    c = client(ha)
    res = await probe.write_test(c, timeout_s=0.2, poll_s=0.01)
    assert res["noop_write_status"] == 200 and res["deadband_f"] == 1
    assert res["readback_p95_s"] is not None and res["hvac_action_reports_fan"] is False
    assert {x["data"]["entity_id"] for x in ha.calls} == {probe.TSTAT}
    assert probe.current_setpoint_data(ha.states[probe.TSTAT]) == before
    assert ha.states["climate.garage_heater"]["attributes"]["temperature"] == 74
    await c.aclose()
