"""v0.1.9 SHADOW learning: 1-minute samples, local refit, catch-up, shadow-vs-actual rollup."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests import house as H
from tests.house import HOUSE
from tests.integration.mock_ha import MockHA
from tests.integration.test_jobs import NOW, Clock, rt_of
from thermabot.adapters import db
from thermabot.app import jobs as J
from thermabot.app.main import Runtime
from thermabot.core.schedule import TZ
from thermabot.core.statemachine import Persisted
from thermabot.core.thermal import ThermalParams, step
from thermabot.core.types import ThermostatState

TRUE = ThermalParams(a=0.12, s=0.8, q_h=4.0, q_c0=3.5, q_c1=0.06, sigma=1.0)


def shadow_on(rt: Runtime, ha: MockHA) -> None:
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    db.write_bot_state(
        rt.conn,
        Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=NOW),
        by="t",
        now=NOW,
    )


def doc(
    temp: float, action: str, t_out: float, bot: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "tstat": {
            "temp": temp,
            "heat_sp": 68,
            "cool_sp": 76,
            "hvac_mode": "heat_cool",
            "hvac_action": action,
            "fan_mode": "auto",
            "humidity": 45,
        },
        "blower": 1.0 if action == "heating" else 0.0,
        "cs_raw": 0.0,
        "cs": 0.0,
        "t_out": t_out,
        "t_out_fc": None,
        "rooms": {"study": {"temp": temp, "age_min": 1.0}},
        "house": "OCCUPIED",
        "block": "home",
        "bot": bot,
    }


def fill(conn: Any, start: datetime, minutes: int, bot: dict[str, Any] | None = None) -> None:
    """Synthetic 1-minute samples from TRUE: heat with hysteresis 68-70 °F; temperature is
    held within each 15-min step (exactly what the 15-min resampler reads)."""
    temp, heating = 69.0, False
    t0 = start.astimezone(UTC).replace(second=0, microsecond=0)
    to = hour = 0.0
    for m in range(minutes):
        t = t0 + timedelta(minutes=m)
        if m % 15 == 0:
            if m:
                temp = step(temp, to, hour, "heat" if heating else "idle", TRUE)
            lt = t.astimezone(TZ)
            hour = lt.hour + lt.minute / 60
            to = 45 + 8 * ((hour - 9) / 12 if 3 <= hour <= 21 else 0)
            heating = temp < 68 or (heating and temp < 70)
        act = "heating" if heating else "idle"
        J.save_sample(conn, t, doc(temp, act, to, bot), "SHADOW", None, [])


# ---- sample rows ------------------------------------------------------------------------
@pytest.mark.brd("FR-MET-1")
def test_sample_doc_fields_and_nulls() -> None:
    th = ThermostatState(
        hvac_mode="heat_cool",
        hvac_action="cooling",
        current_temp=75,
        heat_sp=73,
        cool_sp=76,
        fan_mode="auto",
        humidity=50,
    )
    now = NOW
    states: dict[str, dict[str, Any] | None] = {
        H.BLOWER: {"state": "on"},
        H.COMPRESSOR: {"state": "40"},
        H.OUTDOOR: {"state": "unavailable"},
        "sensor.study_temperature": {
            "state": "72.5",
            "last_reported": (now - timedelta(minutes=10)).isoformat(),
        },
    }
    d = J.sample_doc(now, HOUSE, th, states, None, None, None, "OCCUPIED")
    assert d["tstat"]["cool_sp"] == 76 and d["tstat"]["hvac_action"] == "cooling"
    assert d["blower"] == 1.0 and d["cs_raw"] == 40 and d["cs"] == pytest.approx(0.4)
    assert d["t_out"] is None and d["t_out_fc"] is None and d["bot"] is None
    assert d["rooms"]["study"] == {"temp": 72.5, "age_min": 10.0}
    assert d["rooms"]["bedroom_1"] == {"temp": None, "age_min": None}
    assert "thermostat" not in d["rooms"] and d["house"] == "OCCUPIED"
    json.dumps(d)  # serialisable


@pytest.mark.brd("FR-PLN-3")
def test_samples_to_train_steps_and_local_wins(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "t.db", now=NOW)
    start = NOW - timedelta(hours=6)
    fill(conn, start, 6 * 60)
    steps = J.local_steps(conn, start, NOW)
    assert len(steps) == 6 * 4  # last step's t_next = newest sample (step-held, <= 20 min)
    assert all(x.u in ("heat", "idle") for _, x in steps)
    assert steps[1][1].t == steps[0][1].t_next  # consecutive steps chain
    t0, x0 = steps[0]
    other = x0.model_copy(update={"t": 0.0})
    merged = J.merge_steps([(t0, other), (t0 - J.STEP, other)], steps)
    assert merged[0][1] == other and dict(merged)[t0] == x0 and len(merged) == len(steps) + 1


@pytest.mark.brd("FR-PLN-3")
def test_short_history_split_holds_out_newest() -> None:
    from thermabot.core.thermal import Sample

    x = Sample(t=70, t_next=70, t_out=50, hour=1, u="idle")
    steps = [(NOW - timedelta(minutes=15 * k), x) for k in range(200, 0, -1)]
    train, test = J.split(steps, NOW)
    assert len(train) == 160 and len(test) == 40 and test[0][0] > NOW - timedelta(days=1)


# ---- tick writes a samples row in SHADOW ------------------------------------------------
@pytest.mark.brd("FR-MET-1")
async def test_tick_in_shadow_writes_sample_row(tmp_path: Path) -> None:
    ha = MockHA()
    ha.states["climate.living_room"]["attributes"]["target_temp_low"] = 73
    del ha.states["sensor.den_temperature"]  # missing room: row still written (null)
    rt = rt_of(tmp_path, Clock(NOW), ha=ha)
    shadow_on(rt, ha)
    await rt.replan(NOW)
    await rt.tick(NOW)
    await rt.tick(NOW + timedelta(seconds=20))  # same minute: replaced, not duplicated
    rows = rt.conn.execute("SELECT * FROM samples").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["bot_state"] == "SHADOW" and r["score"] is not None
    d = json.loads(r["snapshot_json"])
    assert d["tstat"]["temp"] == 71 and d["tstat"]["heat_sp"] == 73
    assert d["t_out"] == 58 and d["blower"] == 0.0 and d["cs_raw"] == 0.0
    assert d["rooms"]["study"]["temp"] == 70.5 and d["rooms"]["den"]["temp"] is None
    assert d["bot"]["kind"] == "fallback" and (d["bot"]["heat_sp"], d["bot"]["cool_sp"]) == (73, 76)
    assert d["t_out_fc"] is not None  # NWS fixture forecast
    assert rt.ctx.score == r["score"]
    assert [c for c in ha.calls if c["domain"] == "climate"] == []
    await rt.aclose()


@pytest.mark.brd("NFR-REL-2")
async def test_sampler_failure_never_breaks_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = rt_of(tmp_path, Clock(NOW))

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(J, "save_sample", boom)
    await rt.tick(NOW)  # no exception
    assert rt.ctx.last_tick == NOW
    await rt.aclose()


# ---- local refit -> optimised replan (no Influx) ----------------------------------------
@pytest.mark.brd("FR-PLN-3")
async def test_refit_from_local_samples_then_optimised_plan_in_shadow(tmp_path: Path) -> None:
    ha = MockHA()
    rt = rt_of(tmp_path, Clock(NOW), ha=ha)  # no INFLUX_READ_TOKEN
    shadow_on(rt, ha)
    assert not rt.influx.readable
    fill(rt.conn, NOW - timedelta(days=3), 3 * 24 * 60)
    assert await rt.refit(NOW) is True
    params, conf, v = J.active_model(rt.conn)
    assert v == 1 and conf == "ok" and params is not None
    assert abs(params.a - TRUE.a) / TRUE.a < 0.05 and abs(params.q_h - TRUE.q_h) / TRUE.q_h < 0.05
    await rt.replan(NOW)
    assert rt.ctx.plan is not None and rt.ctx.plan.kind == "optimised"
    assert rt.ctx.eff().base == "SHADOW"
    await rt.tick(NOW)
    d = json.loads(
        rt.conn.execute("SELECT snapshot_json FROM samples ORDER BY ts DESC").fetchone()[0]
    )
    assert d["bot"]["kind"] == "optimised"
    assert [c for c in ha.calls if c["domain"] == "climate"] == []  # still dry-run only
    await rt.aclose()


@pytest.mark.brd("FR-PLN-3")
async def test_refit_too_few_local_steps_skipped(tmp_path: Path) -> None:
    rt = rt_of(tmp_path, Clock(NOW))
    fill(rt.conn, NOW - timedelta(hours=10), 10 * 60)  # 39 steps
    assert await rt.refit(NOW) is False and J.active_model(rt.conn)[2] is None
    await rt.aclose()


@pytest.mark.brd("FR-PLN-3")
async def test_no_model_catch_up_refit_after_replan_rate_limited(tmp_path: Path) -> None:
    rt = rt_of(tmp_path, Clock(NOW))
    await rt.replan(NOW)  # too few samples: cheap pre-check, no refit attempted
    assert rt.last_catchup is None and not rt.pending
    fill(rt.conn, NOW - timedelta(days=3), 3 * 24 * 60)
    rt.loops._replan_due = False
    await rt.replan(NOW)
    assert rt.last_catchup == NOW
    for t in list(rt.pending):
        await t
    assert J.active_model(rt.conn)[2] == 1 and rt.loops._replan_due  # adopted -> replan
    rt.conn.execute("DELETE FROM models")
    rt.catch_up(NOW + timedelta(hours=5), None)  # within 6 h: no second attempt
    assert not rt.pending and rt.last_catchup == NOW
    rt.catch_up(NOW + timedelta(hours=6), None)
    assert rt.pending and rt.last_catchup == NOW + timedelta(hours=6)
    for t in list(rt.pending):
        await t
    await rt.aclose()


# ---- rollup: comfort + shadow_daily -----------------------------------------------------
@pytest.mark.brd("FR-MET-1")
async def test_rollup_fills_comfort_and_shadow_daily(tmp_path: Path) -> None:
    now = datetime(2026, 10, 5, 0, 5, tzinfo=TZ)
    rt = rt_of(tmp_path, Clock(now))
    start = datetime(2026, 10, 4, tzinfo=TZ)
    bot = {"mode": "heat_cool", "heat_sp": 66, "cool_sp": 76, "fan": "auto", "kind": "optimised"}
    fill(rt.conn, start, 24 * 60, bot)
    fill_old = datetime(2026, 1, 1, tzinfo=UTC)
    J.save_sample(rt.conn, fill_old, doc(70, "idle", 40), "SHADOW", None, [])  # > 120 d old
    v = J.save_model(rt.conn, TRUE, 0.3, 500, now)
    await rt.rollup(now)
    dm = dict(rt.conn.execute("SELECT * FROM daily_metrics").fetchone())
    assert dm["day"] == "2026-10-04" and dm["comfort_pct"] is not None
    assert dm["violation_min"] is not None and dm["heat_min"] is not None and dm["heat_min"] > 0
    sd = dict(rt.conn.execute("SELECT * FROM shadow_daily").fetchone())
    assert sd["day"] == "2026-10-04" and sd["model_version"] == v
    assert sd["n_samples"] == 1440 and sd["coverage_pct"] == 100
    assert sd["actual_heat_min"] == dm["heat_min"] and sd["actual_comfort_pct"] == dm["comfort_pct"]
    assert sd["bot_heat_min"] is not None and sd["saved_pct"] is not None
    assert sd["saved_pct"] > 0  # bot holds 66 vs the thermostat's 68: less heat
    assert rt.conn.execute("SELECT COUNT(*) FROM samples WHERE ts < '2026-02'").fetchone()[0] == 0
    rep = J.shadow_report(rt.conn, now)
    assert rep["model"]["status"] == "fitted" and rep["days"][0]["day"] == "2026-10-04"
    assert rep["verdict"].startswith("Bot would have run")
    await rt.aclose()


@pytest.mark.brd("FR-MET-1")
async def test_rollup_without_model_stores_null_bot(tmp_path: Path) -> None:
    now = datetime(2026, 10, 5, 0, 5, tzinfo=TZ)
    rt = rt_of(tmp_path, Clock(now))
    fill(rt.conn, datetime(2026, 10, 4, 12, tzinfo=TZ), 60)
    await rt.rollup(now)
    sd = dict(rt.conn.execute("SELECT * FROM shadow_daily").fetchone())
    assert sd["bot_heat_min"] is None and sd["saved_pct"] is None and sd["model_version"] is None
    assert "no thermal model" in sd["note"] and sd["actual_comfort_pct"] is not None
    rep = J.shadow_report(rt.conn, now)
    assert rep["model"]["status"] == "learning" and rep["verdict"].startswith("Not enough data yet")
    assert rep["samples"]["count"] == 60
    await rt.aclose()


@pytest.mark.brd("FR-CFG-1")
async def test_sampler_reads_configured_sensors_only(tmp_path: Path) -> None:
    """v0.3.0: the sampler reads settings.sensors; an empty sensor is not read at all, and a
    configured humidity sensor replaces the thermostat's own humidity."""
    from tests.house import house_dict
    from thermabot.config import Settings

    class Rec(httpx.AsyncBaseTransport):
        def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
            self.inner, self.paths = inner, []  # type: ignore[var-annotated]

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.paths.append(request.url.path)
            return await self.inner.handle_async_request(request)

    ha = MockHA()
    ha.states["sensor.hall_humidity"] = {**ha.states["sensor.study_temperature"]}
    ha.states["sensor.hall_humidity"]["state"] = "55"
    rt = rt_of(tmp_path, Clock(NOW), ha=ha)
    rec = Rec(httpx.ASGITransport(app=ha.app))
    rt.ha._http._transport = rec  # type: ignore[attr-defined]
    s = Settings.model_validate(
        {
            **house_dict(),
            "sensors": {
                "outdoor": "sensor.outdoor_temperature",
                "humidity": "sensor.hall_humidity",
            },
        }
    )
    rt.conn.execute("UPDATE settings SET value_json=? WHERE key='settings'", (s.model_dump_json(),))
    shadow_on(rt, ha)
    await rt.tick(NOW)
    d = json.loads(rt.conn.execute("SELECT snapshot_json FROM samples").fetchone()[0])
    assert d["t_out"] == 58 and d["tstat"]["humidity"] == 55
    assert d["blower"] is None and d["cs_raw"] is None
    assert not [p for p in rec.paths if "blower" in p or "compressor" in p]
    assert [c for c in ha.calls if c["domain"] == "climate"] == []
    await rt.aclose()
