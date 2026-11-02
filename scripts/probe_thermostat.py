"""Thermostat probe (D§7.5a, T1.27/T1.44). READ-ONLY by default.

  python scripts/probe_thermostat.py climate.living_room            # live read-only
  python scripts/probe_thermostat.py --dry-run                      # mock HA, no network
  python scripts/probe_thermostat.py climate.living_room --write-test --i-am-supervised

Env: HA_URL, HA_TOKEN (required live); INFLUX_URL, INFLUX_ORG, INFLUX_READ_TOKEN,
INFLUX_READ_BUCKET (optional coverage check). Optional sensors: --compressor, --blower,
--outdoor. The write test is for a person standing at the thermostat: it only touches the
given climate entity through the adapter allowlist (`check_write`), restores the original
state after every step, and writes probe-results.json (git-ignored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from thermabot.adapters.ha_rest import HAError, HARest  # noqa: E402
from thermabot.adapters.influx import Influx  # noqa: E402

DRY_RUN_ENTITY = "climate.living_room"  # the mock HA thermostat (tests/integration/mock_ha)
# Module state set by main() (or by tests) before any probe function runs.
TSTAT = DRY_RUN_ENTITY
CS: str | None = "sensor.living_room_compressor_speed"
BLOWER: str | None = "binary_sensor.living_room_blower_active"
OUTDOOR: str | None = "sensor.outdoor_temperature"
NON_ADMIN_MSG = "non-admin user cannot call climate services"
SP_KEYS = ("hvac_mode", "temperature", "target_temp_low", "target_temp_high")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def current_setpoint_data(st: dict[str, Any]) -> dict[str, Any]:
    """Re-send exactly the current mode + setpoints (a no-op write)."""
    a = st["attributes"]
    data: dict[str, Any] = {"entity_id": TSTAT, "hvac_mode": st["state"]}
    if st["state"] == "heat_cool":
        data |= {"target_temp_low": a["target_temp_low"], "target_temp_high": a["target_temp_high"]}
    elif a.get("temperature") is not None:
        data["temperature"] = a["temperature"]
    return data


async def print_thermostat(ha: HARest) -> dict[str, Any] | None:
    section("thermostat attributes")
    st = await ha.get_state(TSTAT)
    if st is None:
        print(f"{TSTAT}: NOT FOUND")
        return None
    print(f"state: {st['state']}")
    for k, v in sorted(st["attributes"].items()):
        print(f"  {k}: {v}")
    return st


async def print_ranges(ha: HARest) -> dict[str, Any]:
    section("compressor / blower")
    out: dict[str, Any] = {}
    for eid in (e for e in (CS, BLOWER) if e):
        st = await ha.get_state(eid)
        unit = (st or {}).get("attributes", {}).get("unit_of_measurement")
        print(f"{eid}: {None if st is None else st['state']} unit={unit}")
        out[eid] = {"state": None if st is None else st["state"], "unit": unit}
    return out


async def print_coverage(ix: Influx | None) -> dict[str, Any]:
    section("influx coverage (31 d)")
    out: dict[str, Any] = {}
    if ix is None:
        print("skipped: INFLUX_READ_TOKEN not set")
        return out
    for eid in (e for e in (TSTAT, CS, BLOWER, OUTDOOR) if e):
        try:
            rows = await ix.read_entity(eid, ["value"], start="-31d", every="1h")
        except (httpx.HTTPError, RuntimeError) as e:
            print(f"{eid}: ERROR {type(e).__name__}")
            continue
        vals = [r["value"] for r in rows if isinstance(r["value"], float)]
        rng = f"{min(vals):g}..{max(vals):g}" if vals else "-"
        units = sorted({r["measurement"] for r in rows})
        print(f"{eid}: {len(rows)} hourly points, range {rng}, measurement {units}")
        out[eid] = {
            "points": len(rows),
            "min": min(vals, default=None),
            "max": max(vals, default=None),
            "measurement": units,
        }
    return out


async def wait_for(ha: HARest, pred: Any, timeout_s: float, poll_s: float = 2.0) -> float | None:
    """Seconds until pred(state) is true, None on timeout."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        st = await ha.get_state(TSTAT)
        if st is not None and pred(st):
            return time.monotonic() - t0
        await asyncio.sleep(poll_s)
    return None


async def write(ha: HARest, service: str, data: dict[str, Any]) -> int:
    """Through the adapter allowlist (thermostat only). Returns HTTP status."""
    try:
        return (await ha._post_service("climate", service, data)).status
    except HAError as e:
        return e.status or 0


async def write_test(ha: HARest, timeout_s: float, poll_s: float) -> dict[str, Any]:
    """D§7.5a. Every step restores the original mode/setpoints. Stops on 401/403."""
    res: dict[str, Any] = {}
    orig = await ha.get_state(TSTAT)
    if orig is None:
        return {"error": "thermostat not found"}
    restore = current_setpoint_data(orig)
    modes = orig["attributes"].get("fan_modes") or ["auto", "on"]
    fan0 = orig["attributes"].get("fan_mode") or next(
        (m for m in modes if m.lower() == "auto"), "auto"
    )
    fan_on = next((m for m in modes if m.lower() == "on"), "on")
    section("write test: non-admin no-op service call")
    status = await write(ha, "set_temperature", restore)
    res["noop_write_status"] = status
    if status in (401, 403):
        print(f"HTTP {status}: {NON_ADMIN_MSG}")
        res["noop_write_error"] = NON_ADMIN_MSG
        return res
    print(f"HTTP {status} ({'ok' if status == 200 else 'UNEXPECTED'})")
    if status != 200:
        return res
    try:
        if orig["state"] == "heat_cool":
            lo = float(orig["attributes"]["target_temp_low"])
            hi = float(orig["attributes"]["target_temp_high"])
            section("write test: write-to-confirm lag (x3)")
            lags: list[float] = []
            for _ in range(3):
                want = hi + 1 if hi + 1 <= 80 else hi - 1
                await write(ha, "set_temperature", {**restore, "target_temp_high": want})
                lag = await wait_for(
                    ha,
                    lambda s, w=want: (
                        abs(float(s["attributes"].get("target_temp_high") or 0) - w) <= 0.5
                    ),
                    timeout_s,
                    poll_s,
                )
                print(f"lag: {lag}")
                if lag is not None:
                    lags.append(lag)
                await write(ha, "set_temperature", restore)
                await wait_for(
                    ha, lambda s: s["attributes"].get("target_temp_high") == hi, timeout_s, poll_s
                )
            res["readback_p95_s"] = max(lags) if lags else None
            res["readback_median_s"] = statistics.median(lags) if lags else None
            section("write test: minimum heat_cool deadband")
            accepted = None
            for db in (3, 2, 1):
                st = await write(
                    ha,
                    "set_temperature",
                    {**restore, "target_temp_low": lo, "target_temp_high": lo + db},
                )
                ok = (
                    st == 200
                    and await wait_for(
                        ha,
                        lambda s, d=db: (
                            abs(float(s["attributes"].get("target_temp_high") or 0) - lo - d) <= 0.5
                        ),
                        timeout_s,
                        poll_s,
                    )
                    is not None
                )
                print(f"deadband {db}: HTTP {st}, applied={ok}")
                if not ok:
                    break
                accepted = db
            await write(ha, "set_temperature", restore)
            res["deadband_f"] = accepted
        section("write test: fan circulate-only -> hvac_action")
        await write(ha, "set_fan_mode", {"entity_id": TSTAT, "fan_mode": fan_on})
        seen = await wait_for(
            ha, lambda s: s["attributes"].get("hvac_action") == "fan", min(timeout_s, 180), poll_s
        )
        res["hvac_action_reports_fan"] = seen is not None
        print(f"hvac_action reports fan: {seen is not None}")
        await write(ha, "set_fan_mode", {"entity_id": TSTAT, "fan_mode": fan0})
        section("write test: off -> on")
        await write(ha, "set_hvac_mode", {"entity_id": TSTAT, "hvac_mode": "off"})
        off_lag = await wait_for(ha, lambda s: s["state"] == "off", timeout_s, poll_s)
        await write(ha, "set_temperature", restore)
        on_lag = await wait_for(ha, lambda s: s["state"] == orig["state"], timeout_s, poll_s)
        after = await ha.get_state(TSTAT)
        kept = after is not None and current_setpoint_data(after) == restore
        res["off_on"] = {"off_lag_s": off_lag, "on_lag_s": on_lag, "setpoints_kept": kept}
        print(f"off lag {off_lag}, back-on lag {on_lag}, setpoints restored: {kept}")
    finally:
        await write(ha, "set_temperature", restore)
        await write(ha, "set_fan_mode", {"entity_id": TSTAT, "fan_mode": fan0})
    return res


def fake_influx() -> Influx:
    """--dry-run: canned annotated CSV in the HA InfluxDB integration's schema."""
    csv = (
        "#datatype,string,long,dateTime:RFC3339,double,string,string,string\n"
        ",result,table,_time,_value,_field,_measurement,entity_id\n"
        ",,0,2026-10-01T00:00:00Z,0,value,%,x\n,,0,2026-10-01T01:00:00Z,100,value,%,x\n"
    )
    t = httpx.MockTransport(lambda r: httpx.Response(200, text=csv))
    return Influx("http://influx", "home", "ha_hvac", "thermabot", "dry", None, transport=t)


async def main(argv: list[str] | None = None) -> int:
    global TSTAT, CS, BLOWER, OUTDOOR
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("entity", nargs="?", help="climate entity to probe, e.g. climate.living_room")
    p.add_argument("--compressor", default=CS, help="compressor speed sensor ('' = skip)")
    p.add_argument("--blower", default=BLOWER, help="blower binary_sensor ('' = skip)")
    p.add_argument("--outdoor", default=OUTDOOR, help="outdoor temperature sensor ('' = skip)")
    p.add_argument("--dry-run", action="store_true", help="in-process mock HA, no network")
    p.add_argument("--write-test", action="store_true")
    p.add_argument("--i-am-supervised", action="store_true")
    p.add_argument("--out", default="probe-results.json")
    p.add_argument("--timeout-s", type=float, default=180)
    a = p.parse_args(argv)
    if a.write_test and not a.i_am_supervised:
        print(
            "--write-test requires --i-am-supervised (someone at the thermostat)", file=sys.stderr
        )
        return 2
    if a.dry_run and a.write_test:
        print("--dry-run makes no writes; drop --write-test", file=sys.stderr)
        return 2
    entity = a.entity or (DRY_RUN_ENTITY if a.dry_run else None)
    if not entity or not entity.startswith("climate."):
        print("give the climate entity to probe, e.g. climate.living_room", file=sys.stderr)
        return 2
    TSTAT, CS, BLOWER, OUTDOOR = entity, a.compressor or None, a.blower or None, a.outdoor or None
    ix: Influx | None
    if a.dry_run:
        from tests.integration.mock_ha import GOOD_TOKEN, MockHA

        mock = MockHA()
        ha = HARest(
            "http://mock-ha",
            GOOD_TOKEN,
            transport=httpx.ASGITransport(app=mock.app),
            writable_entity=TSTAT,
        )
        ix = fake_influx()
        print("DRY RUN: in-process mock HA and fake Influx (no network, no writes)")
    else:
        token = os.environ.get("HA_TOKEN")
        if not token:
            print("HA_TOKEN not set", file=sys.stderr)
            return 2
        url = os.environ.get("HA_URL")
        if not url:
            print("HA_URL not set", file=sys.stderr)
            return 2
        ha = HARest(url, token, writable_entity=TSTAT)
        rt = os.environ.get("INFLUX_READ_TOKEN")
        ix = (
            Influx(
                os.environ.get("INFLUX_URL", "http://influxdb:8086"),
                os.environ.get("INFLUX_ORG", "home"),
                os.environ.get("INFLUX_READ_BUCKET", "ha_hvac"),
                "thermabot",
                rt,
                None,
            )
            if rt
            else None
        )
    try:
        st = await print_thermostat(ha)
        ranges = await print_ranges(ha)
        cov = await print_coverage(ix)
        if not a.write_test:
            section("write test")
            print("skipped (read-only; use --write-test --i-am-supervised)")
            if a.dry_run:
                mock_calls = len(mock.calls)
                print(f"service calls made: {mock_calls}")
                return 0 if mock_calls == 0 else 1
            return 0
        res = await write_test(ha, a.timeout_s, poll_s=2.0)
        res["cs_unit"] = ranges.get(CS, {}).get("unit") if CS else None
        res["thermostat"] = None if st is None else st["attributes"]
        res["influx_coverage"] = cov
        Path(a.out).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {a.out}")
        return 0 if "noop_write_error" not in res else 3
    finally:
        await ha.aclose()
        if ix is not None:
            await ix.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
