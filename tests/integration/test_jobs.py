"""Replan / refit / rollup jobs (D§4.1, D§4.2, D§8.3) against MockHA + mocked NWS/Influx."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tests.house import house_settings, seed_house
from tests.integration.mock_ha import GOOD_TOKEN, MockHA
from thermabot.adapters import db
from thermabot.app import jobs as J
from thermabot.app.main import Runtime, build, load_env
from thermabot.app.routes_api import _kv_put
from thermabot.config import OpeningPolicyCfg
from thermabot.core import planner
from thermabot.core.schedule import TZ
from thermabot.core.thermal import ThermalParams, step
from thermabot.core.types import Forecast, ForecastPoint, Plan, PlanStep

NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)  # NWS fixture fetch time
NWS_FIX = Path(__file__).parent / "fixtures" / "nws"
HDR = (
    "#datatype,string,long,dateTime:RFC3339,double,string,string,string\n"
    "#group,false,false,false,false,true,true,true\n#default,_result,,,,,,\n"
    ",result,table,_time,_value,_field,_measurement,entity_id\n"
)
Data = dict[tuple[str, str], list[tuple[datetime, object]]]


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def nws_ok(start: datetime | None = None) -> httpx.MockTransport:
    """Static fixtures (2026-10-05 fetch time) by default. `start`: e2e/live-clock callers
    shift every period's startTime/endTime by the same offset so the fixture never goes stale
    relative to a real wall-clock Ctx (same pattern om_transport already uses for open_meteo).
    """
    offset = None if start is None else start - NOW

    def _shift(iso: str) -> str:
        if offset is None:
            return iso
        return (datetime.fromisoformat(iso) + offset).isoformat()

    def h(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        name = "points" if p.startswith("/points/") else "hourly"
        body = json.loads((NWS_FIX / f"{name}.json").read_text())
        if offset is not None and name == "hourly":
            for period in body["properties"]["periods"]:
                period["startTime"] = _shift(period["startTime"])
                period["endTime"] = _shift(period["endTime"])
        return httpx.Response(200, json=body)

    return httpx.MockTransport(h)


def nws_down() -> httpx.MockTransport:
    return httpx.MockTransport(lambda r: httpx.Response(503))


def influx_mock(data: Data, log: list[str] | None = None) -> httpx.MockTransport:
    """Answers Flux queries from `data[(entity_id, field)]`, honouring range start/stop."""

    def h(req: httpx.Request) -> httpx.Response:
        q = req.content.decode()
        if log is not None:
            log.append(q)
        if req.url.path.endswith("/write"):
            return httpx.Response(204)
        ent = re.search(r'r\.entity_id == "([^"]+)"', q).group(1)  # type: ignore[union-attr]
        fields = re.findall(r'r\._field == "([^"]+)"', q)
        m = re.search(r"range\(start: (\S+?), stop: (\S+?)\)", q)
        lo = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))  # type: ignore[union-attr]
        hi = datetime.fromisoformat(m.group(2).replace("Z", "+00:00"))  # type: ignore[union-attr]
        body = HDR
        for f in fields:
            for t, v in data.get((ent, f), []):
                if lo <= t < hi:
                    ts = t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    body += f",,0,{ts},{v},{f},u,{ent}\n"
        return httpx.Response(200, text=body)

    return httpx.MockTransport(h)


def rt_of(
    tmp: Path,
    clk: Clock,
    ha: MockHA | None = None,
    nws: httpx.MockTransport | None = None,
    influx: httpx.MockTransport | None = None,
    **env: str,
) -> Runtime:
    e = load_env(
        {
            "HEARTBEAT_WEBHOOK_URL": "http://ha/api/webhook/hb-test",
            "HA_URL": "http://ha",
            "HA_TOKEN": GOOD_TOKEN,
            "DATA_DIR": str(tmp),
            **env,
        }
    )
    seed_house(tmp)  # a configured install (v0.2.0 defaults are 'setup needed')
    return build(
        e,
        clock=clk,
        ha_transport=httpx.ASGITransport(app=(ha or MockHA()).app),
        nws_transport=nws or nws_ok(),
        influx_transport=influx,
    )


def no_off(p: Plan) -> bool:
    return all(s.hvac_mode != "off" for s in p.steps)


async def test_opening_assessment_is_sampled_and_persisted(tmp_path: Path) -> None:
    ha = MockHA()
    ha.states["binary_sensor.front_door"] = {
        "state": "on",
        "attributes": {"device_class": "door"},
        "last_changed": (NOW - timedelta(minutes=12)).isoformat(),
        "last_updated": NOW.isoformat(),
        "last_reported": NOW.isoformat(),
    }
    rt = rt_of(tmp_path, Clock(NOW), ha=ha)
    policy = OpeningPolicyCfg(
        enabled=True,
        min_open_min=10,
        sensors={
            "binary_sensor.front_door": {
                "role": "exterior",
                "affected_rooms": ["bedroom_1"],
            }
        },
    )
    settings = rt.ctx.settings.model_copy(update={"openings": policy})
    _kv_put(rt.ctx, "settings", settings.model_dump(mode="json"), "test")

    await rt.sample(NOW, None, None, None)

    stored = rt.conn.execute(
        "SELECT snapshot_json FROM samples ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert stored is not None
    finding = json.loads(stored["snapshot_json"])["opening_assessment"][0]
    assert finding["status"] == "sustained"
    assert finding["entity_ids"] == ["binary_sensor.front_door"]
    assert rt.ctx.opening_assessment[0].status == "sustained"
    await rt.aclose()


# ---- replan --------------------------------------------------------------------------
@pytest.mark.brd("FR-PLN-7")
async def test_replan_without_model_uses_fallback_and_persists(tmp_path: Path) -> None:
    rt = rt_of(tmp_path, Clock(NOW))
    await rt.replan(NOW)
    p = rt.ctx.plan
    assert p is not None and p.kind == "fallback" and no_off(p)
    assert "confidence low" in p.reason_banner
    assert rt.ctx.weather_health == "ok" and not rt.weather_stale
    r = rt.conn.execute("SELECT kind, forecast_source, trigger FROM plans").fetchone()
    assert tuple(r) == ("fallback", "nws", "replan")
    await rt.aclose()


@pytest.mark.brd("FR-PLN-1")
async def test_replan_with_ok_model_is_optimised(tmp_path: Path) -> None:
    rt = rt_of(tmp_path, Clock(NOW))
    v = J.save_model(rt.conn, ThermalParams(a=0.1, s=0.5), 0.4, 500, NOW)
    await rt.replan(NOW)
    p = rt.ctx.plan
    assert p is not None and p.kind == "optimised" and p.steps[0].t == NOW
    assert rt.conn.execute("SELECT model_version FROM plans").fetchone()[0] == v
    await rt.aclose()


@pytest.mark.brd("FR-WX-2")
@pytest.mark.brd("FR-PLN-7")
async def test_replan_forecast_down_fallback_never_off(tmp_path: Path) -> None:
    rt = rt_of(tmp_path, Clock(NOW), nws=nws_down())
    J.save_model(rt.conn, ThermalParams(), 0.4, 500, NOW)  # model ok: only weather fails
    await rt.replan(NOW)
    p = rt.ctx.plan
    assert p is not None and p.kind == "fallback" and no_off(p)
    assert "forecast" in p.reason_banner
    assert rt.weather_stale and rt.ctx.weather_health == "stale"
    await rt.aclose()


@pytest.mark.brd("FR-PLN-7")
async def test_replan_planner_exception_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_: object) -> Plan:
        raise ValueError("bad")

    monkeypatch.setattr(planner, "plan", boom)
    rt = rt_of(tmp_path, Clock(NOW))
    await rt.replan(NOW)
    p = rt.ctx.plan
    assert p is not None and p.kind == "fallback" and no_off(p)
    assert "ValueError" in p.reason_banner
    await rt.aclose()


@pytest.mark.brd("FR-PLN-7")
async def test_replan_no_thermostat_temp_fallback(tmp_path: Path) -> None:
    ha = MockHA()
    ha.states["climate.living_room"]["attributes"]["current_temperature"] = None
    rt = rt_of(tmp_path, Clock(NOW), ha=ha)
    await rt.replan(NOW)
    assert rt.ctx.plan is not None and rt.ctx.plan.kind == "fallback" and no_off(rt.ctx.plan)
    await rt.aclose()


@pytest.mark.brd("FR-PLN-1")
async def test_loop_startup_replan_feeds_tick_and_api(tmp_path: Path) -> None:
    """First loop iteration replans, then the tick follows the plan (shadow write)."""
    ha = MockHA()
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    ha.states["climate.living_room"]["last_reported"] = NOW.isoformat()
    ha.states["climate.living_room"]["attributes"]["target_temp_low"] = 62
    rt = rt_of(tmp_path, Clock(NOW), ha=ha)
    from thermabot.config import PlannerCfg
    from thermabot.core.statemachine import Persisted

    # band-edge fallback (68/76) differs from the thermostat's 62/76 -> a dry-run write. With
    # the v0.1.9 default ('thermostat') the fallback would hold 62/76 and write nothing.
    rt.ctx.settings = house_settings(planner=PlannerCfg(fallback_source="bands"))
    rt.conn.execute(
        "UPDATE settings SET value_json=? WHERE key='settings'",
        (rt.ctx.settings.model_dump_json(),),
    )
    db.write_bot_state(
        rt.conn,
        Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=NOW),
        by="t",
        now=NOW,
    )
    await rt.loops.run_once()
    assert rt.ctx.plan is not None
    rows = rt.conn.execute("SELECT outcome FROM writes").fetchall()
    assert rows and {r[0] for r in rows} <= {"shadow", "denied"}
    assert [x for x in ha.calls if x["domain"] == "climate"] == []
    await rt.aclose()


@pytest.mark.brd("NFR-SAFE-1")
def test_off_guard_extra_only_proves_optimised_scored_window() -> None:
    t = [NOW + timedelta(minutes=15 * k) for k in range(4)]
    fc = Forecast(
        source="nws",
        fetched_at=NOW,
        points=[ForecastPoint(t=NOW + timedelta(hours=h), temp_f=60) for h in range(3)],
    )
    steps = [PlanStep(t=x, hvac_mode="off", score=90) for x in t[:3]] + [
        PlanStep(t=t[3], hvac_mode="heat_cool", heat_sp=68, cool_sp=76, score=95)
    ]
    p = Plan(kind="optimised", created_at=NOW, steps=steps)
    ex = J.off_guard_extra(p, steps[0], fc, 80, 2.0)
    assert ex == {"off_used_h": 2.0, "off_score_proven": True, "off_forecast_f": [60, 60, 60]}
    steps[1] = steps[1].model_copy(update={"score": 70})
    p2 = Plan(kind="optimised", created_at=NOW, steps=steps)
    assert J.off_guard_extra(p2, steps[0], fc, 80, 0)["off_score_proven"] is False
    fb = Plan(kind="fallback", created_at=NOW, steps=steps)
    assert J.off_guard_extra(fb, steps[0], fc, 80, 0) == {"off_used_h": 0}
    assert J.off_guard_extra(p, steps[0], None, 80, 0)["off_forecast_f"] == []


@pytest.mark.brd("NFR-SAFE-2")
async def test_off_window_tracked_after_bot_off_write(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "t.db", now=NOW)
    conn.execute(
        "INSERT INTO writes (ts, service, entity_id, data_json, reason, outcome)"
        " VALUES (?,?,?,?,?,?)",
        (NOW.isoformat(), "set_hvac_mode", "x", '{"hvac_mode": "off"}', "plan", "sent"),
    )
    J.track_off_window(conn, "off", NOW, 8)
    J.track_off_window(conn, "off", NOW + timedelta(minutes=1), 8)
    ws = J.load_off_windows(conn)
    assert len(ws) == 1 and ws[0].end is None
    assert ws[0].reevaluate_at == NOW + timedelta(hours=8)
    J.track_off_window(conn, "heat_cool", NOW + timedelta(hours=2), 8)
    assert J.load_off_windows(conn)[0].end == NOW + timedelta(hours=2)


# ---- refit ---------------------------------------------------------------------------
TRUE = ThermalParams(a=0.12, s=0.8, q_h=4.0, q_c0=3.5, q_c1=0.06, sigma=1.0)


def history(
    end: datetime, days: int, p: ThermalParams, t_out: Callable[[float], float] = lambda h: 45
) -> Data:
    """15-min synthetic history: heat with hysteresis 68-70 °F, outdoor by local hour."""
    start = end - timedelta(days=days)
    t, temp, heating = start, 69.0, False
    d: Data = {
        (k, f): []
        for k, f in (
            ("living_room", "current_temperature"),
            ("living_room", "hvac_action_str"),
            ("outdoor_temperature", "value"),
            ("living_room_compressor_speed", "value"),
        )
    }
    while t <= end:
        lt = t.astimezone(TZ)
        hour = lt.hour + lt.minute / 60
        to = t_out(hour)
        heating = temp < 68 or (heating and temp < 70)
        d[("living_room", "current_temperature")].append((t, round(temp, 6)))
        d[("living_room", "hvac_action_str")].append((t, "heating" if heating else "idle"))
        d[("outdoor_temperature", "value")].append((t, to))
        d[("living_room_compressor_speed", "value")].append((t, 0))
        temp = step(temp, to, hour, "heat" if heating else "idle", p)
        t += timedelta(minutes=15)
    return d


def outdoor(h: float) -> float:
    return 45 + 8 * math.sin(math.pi * (h - 9) / 12)


@pytest.mark.brd("FR-PLN-3")
async def test_refit_adopts_in_range_model_and_triggers_replan(tmp_path: Path) -> None:
    now = datetime(2026, 10, 5, 7, 15, tzinfo=UTC)  # 03:15 local
    q: list[str] = []
    rt = rt_of(
        tmp_path,
        Clock(now),
        influx=influx_mock(history(now, 28, TRUE, outdoor), q),
        INFLUX_READ_TOKEN="r",
    )
    old = J.save_model(rt.conn, ThermalParams(), 0.9, 10, now - timedelta(days=1))
    rt.loops._replan_due = False
    assert rt.ctx.refit == rt.refit_now  # routes_api hook wired to the same job
    await rt.loops.jobs.refit(now)  # the Loops 03:15 hook spawns it in the background
    for t in list(rt.pending):
        await t
    params, conf, v = J.active_model(rt.conn)
    assert v == old + 1 and conf == "ok" and params is not None
    assert abs(params.a - TRUE.a) / TRUE.a < 0.05 and abs(params.q_h - TRUE.q_h) / TRUE.q_h < 0.05
    rows = rt.conn.execute(
        "SELECT version, active, mae_json FROM models ORDER BY version"
    ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(old, 0), (old + 1, 1)]
    assert json.loads(rows[-1][2])["thermostat"] < 0.05
    assert rt.loops._replan_due
    assert all('r.entity_id == "' in x for x in q) and 'bucket:"ha_hvac"' in q[0]
    await rt.aclose()


@pytest.mark.brd("FR-PLN-3")
async def test_refit_out_of_range_keeps_previous(tmp_path: Path) -> None:
    now = datetime(2026, 10, 5, 7, 15, tzinfo=UTC)
    bad = ThermalParams(a=0.9, s=0.8, q_h=4.0)  # a out of physical range
    rt = rt_of(
        tmp_path,
        Clock(now),
        influx=influx_mock(history(now, 28, bad, outdoor)),
        INFLUX_READ_TOKEN="r",
    )
    old = J.save_model(rt.conn, ThermalParams(), 0.9, 10, now - timedelta(days=1))
    assert await rt.refit(now) is False
    _, _, v = J.active_model(rt.conn)
    assert v == old and rt.conn.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 1
    await rt.aclose()


@pytest.mark.brd("FR-PLN-3")
async def test_refit_without_token_or_data_or_influx_down(tmp_path: Path) -> None:
    now = datetime(2026, 10, 5, 7, 15, tzinfo=UTC)
    rt = rt_of(tmp_path / "a", Clock(now))  # no read token
    assert await rt.refit(now) is False
    await rt.aclose()
    rt = rt_of(tmp_path / "b", Clock(now), influx=influx_mock({}), INFLUX_READ_TOKEN="r")
    assert await rt.refit(now) is False  # no data -> skipped
    await rt.aclose()
    down = httpx.MockTransport(lambda r: httpx.Response(503))
    rt = rt_of(tmp_path / "c", Clock(now), influx=down, INFLUX_READ_TOKEN="r")
    rt.loops._replan_due = False
    await rt.refit_now(now)  # hook swallows + logs; no model, no replan
    assert J.active_model(rt.conn)[2] is None and not rt.loops._replan_due
    await rt.aclose()


@pytest.mark.brd("FR-PLN-3")
async def test_refit_excludes_override_steps(tmp_path: Path) -> None:
    now = datetime(2026, 10, 5, 7, 15, tzinfo=UTC)
    rt = rt_of(
        tmp_path,
        Clock(now),
        influx=influx_mock(history(now, 28, TRUE, outdoor)),
        INFLUX_READ_TOKEN="r",
    )
    s0 = now - timedelta(days=20)
    rt.conn.execute(
        "INSERT INTO overrides (kind, source, payload_json, started_at, expires_at, ended_at)"
        " VALUES ('ui_pause','t','{}',?,?,?)",
        (
            s0.isoformat(),
            (s0 + timedelta(days=2)).isoformat(),
            (s0 + timedelta(days=1)).isoformat(),
        ),
    )
    assert await rt.refit(now) is True
    n = rt.conn.execute("SELECT n_samples FROM models").fetchone()[0]
    assert n == (28 - 7 - 1) * 96  # 20 training days minus the 1-day override
    await rt.aclose()


# ---- rollup --------------------------------------------------------------------------
def day_data(d: date, heat: tuple[int, int], cs_pct: float = 0) -> Data:
    start = datetime(d.year, d.month, d.day, tzinfo=TZ).astimezone(UTC)
    nxt = d + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=TZ).astimezone(UTC)  # 23/25 h on DST
    out: Data = {
        ("living_room", "hvac_action_str"): [],
        ("living_room_blower_active", "value"): [],
        ("living_room_compressor_speed", "value"): [],
        ("outdoor_temperature", "value"): [],
    }
    t = start - timedelta(minutes=10)
    while t < end:
        h = t.astimezone(TZ).hour if t >= start else -1
        on = heat[0] <= h < heat[1]
        out[("living_room", "hvac_action_str")].append((t, "heating" if on else "idle"))
        out[("living_room_blower_active", "value")].append((t, 1 if on else 0))
        out[("living_room_compressor_speed", "value")].append((t, cs_pct))
        out[("outdoor_temperature", "value")].append((t, 55))
        t += timedelta(minutes=5)
    return out


@pytest.mark.brd("FR-MET-1")
@pytest.mark.parametrize(
    ("day", "minutes"),
    [(date(2026, 11, 1), 1500), (date(2027, 3, 14), 1380), (date(2026, 10, 4), 1440)],
)
def test_minute_samples_cover_exact_local_day(day: date, minutes: int) -> None:
    data = day_data(day, (0, 24))
    start, end = J.local_day_bounds(day)
    samples = J.minute_samples(
        data[("living_room", "hvac_action_str")],
        data[("living_room_blower_active", "value")],
        data[("living_room_compressor_speed", "value")],
        start,
        end,
    )
    assert len(samples) == minutes
    assert sum(sample.hvac_action == "heating" for sample in samples) == minutes


@pytest.mark.brd("FR-MET-1")
async def test_rollup_writes_yesterday_daily_metrics(tmp_path: Path) -> None:
    now = datetime(2026, 10, 5, 0, 5, tzinfo=TZ)
    rt = rt_of(
        tmp_path,
        Clock(now),
        influx=influx_mock(day_data(date(2026, 10, 4), (6, 8))),
        INFLUX_READ_TOKEN="r",
    )
    await rt.loops.jobs.rollup(now)
    for t in list(rt.pending):
        await t
    r = dict(rt.conn.execute("SELECT * FROM daily_metrics").fetchone())
    assert r["day"] == "2026-10-04" and r["heat_min"] == 120 and r["heat_effort"] == 120
    assert r["cool_min"] == 0 and r["fan_only_min"] == 0
    assert r["hdd"] == 10 and r["cdd"] == 0 and r["bot_state"] == "DISABLED"
    assert r["comfort_pct"] is None
    # history API / summary read it
    from thermabot.app.main import last_week

    assert last_week(rt.conn, now).heat_h == 2.0
    await rt.aclose()


@pytest.mark.brd("FR-MET-1")
async def test_rollup_dst_day_and_rerun_idempotent(tmp_path: Path) -> None:
    now = datetime(2026, 11, 2, 0, 5, tzinfo=TZ)  # yesterday = 25-hour DST day
    rt = rt_of(
        tmp_path,
        Clock(now),
        influx=influx_mock(day_data(date(2026, 11, 1), (1, 2))),  # 01:xx happens twice
        INFLUX_READ_TOKEN="r",
    )
    await rt.rollup(now)
    await rt.rollup(now)
    rows = rt.conn.execute("SELECT day, heat_min FROM daily_metrics").fetchall()
    assert [tuple(r) for r in rows] == [("2026-11-01", 120)]
    await rt.aclose()


@pytest.mark.brd("FR-MET-1")
async def test_rollup_without_influx_writes_null_metrics(tmp_path: Path) -> None:
    now = datetime(2026, 10, 5, 0, 5, tzinfo=TZ)
    rt = rt_of(tmp_path, Clock(now))
    await rt.rollup(now)
    r = dict(rt.conn.execute("SELECT * FROM daily_metrics").fetchone())
    assert r["day"] == "2026-10-04" and r["heat_min"] is None and r["overrides"] == 0
    await rt.aclose()


@pytest.mark.brd("NFR-REL-2")
async def test_failing_background_jobs_never_stop_tick_or_heartbeat(tmp_path: Path) -> None:
    """Influx down at 00:05/03:15 and NWS down: every tick and heartbeat still runs."""
    clk = Clock(datetime(2026, 10, 5, 0, 0, tzinfo=TZ))
    ha = MockHA()
    down = httpx.MockTransport(lambda r: httpx.Response(503))
    rt = rt_of(tmp_path, clk, ha=ha, nws=down, influx=down, INFLUX_READ_TOKEN="r")
    for _ in range(4 * 60):
        await rt.loops.run_once()
        clk.t += timedelta(minutes=1)
    for t in list(rt.pending):
        await t
    beats = ha.webhook_posts
    assert len(beats) == 240 and rt.ctx.plan is not None and rt.ctx.plan.kind == "fallback"
    assert rt.conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == 0
    await rt.aclose()


@pytest.mark.brd("NFR-SAFE-6")
async def test_tick_static_thermostat_not_degraded(tmp_path: Path) -> None:
    """HA last_reported 30 min old (idle house, no change) must not read as stale."""
    from thermabot.core.statemachine import Persisted, effective

    ha = MockHA()
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    ha.states["climate.living_room"]["last_reported"] = (NOW - timedelta(minutes=30)).isoformat()
    rt = rt_of(tmp_path, Clock(NOW), ha=ha)
    await rt.tick(NOW)
    assert rt.ctx.live is not None and rt.ctx.live.tstat_age_min == 0
    p = Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=NOW)
    assert effective(p, rt.ctx.live).state == "SHADOW"
    await rt.aclose()


@pytest.mark.brd("NFR-REL-2")
async def test_ha_outage_clears_stale_state_and_recovers_without_writes(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, patch

    from thermabot.core.statemachine import Persisted

    ha = MockHA()
    ha.states["input_boolean.thermabot_enabled"]["state"] = "on"
    rt = rt_of(tmp_path, Clock(NOW), ha=ha)
    db.write_bot_state(
        rt.conn,
        Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=NOW),
        by="test",
        now=NOW,
    )
    await rt.tick(NOW)
    assert rt.ctx.snapshot is not None
    with (
        patch.object(rt.ha, "get_state", AsyncMock(side_effect=httpx.ConnectError("HA down"))),
        patch.object(rt.controller, "tick", AsyncMock()) as control,
    ):
        with pytest.raises(httpx.ConnectError):
            await rt.tick(NOW + timedelta(minutes=1))
        control.assert_not_awaited()
    assert rt.ctx.snapshot is None and rt.ctx.ha_health == "down"
    assert rt.ctx.eff().state == "DEGRADED"
    assert not rt.ctx.eff().plan_writes and not rt.ctx.eff().restore_writes
    sample = rt.conn.execute(
        "SELECT snapshot_json FROM samples ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert sample is not None
    assert json.loads(sample[0])["_thermabot"] == {"state": "DEGRADED", "degraded_reason": "ha"}
    await rt.tick(NOW + timedelta(minutes=2))
    assert rt.ctx.ha_health == "ok" and rt.ctx.snapshot is not None
    assert rt.ctx.eff().state == "SHADOW"
    await rt.aclose()


async def test_nightly_retention_preserves_manual_and_preimport_backups(tmp_path: Path) -> None:
    from thermabot.app.recovery import create_backup

    rt = rt_of(tmp_path, Clock(NOW))
    manual = create_backup(rt.ctx, "manual")
    before_import = create_backup(rt.ctx, "pre-import")
    for day in range(10):
        await rt.backup(NOW + timedelta(days=day))
    assert manual.exists() and before_import.exists()
    assert len(list((tmp_path / "backup").glob("thermabot-2026-*.db"))) == 7
    await rt.aclose()


async def test_saved_settings_refresh_guards_but_keep_bound_entity(tmp_path: Path) -> None:
    from thermabot.app.routes_api import _kv_put

    rt = rt_of(tmp_path, Clock(NOW))
    doc = rt.ctx.settings.model_dump(mode="json")
    original_entity = rt.controller.entity
    doc["thermostat"]["entity_id"] = "climate.pending_restart"
    doc["presence"]["empty_after_min"] = 45
    _kv_put(rt.ctx, "settings", doc, "test")
    await rt.tick(NOW)
    assert rt.controller.settings.presence.empty_after_min == 45
    assert rt.controller.entity == original_entity
    assert rt.ha.writable_entity == original_entity
    await rt.aclose()


async def test_offline_backup_restore_boots_with_mirror_off_without_writes(tmp_path: Path) -> None:
    import shutil

    from thermabot.app.recovery import create_backup
    from thermabot.core.statemachine import Persisted

    source = tmp_path / "source"
    source.mkdir()
    original = rt_of(source, Clock(NOW))
    db.write_bot_state(
        original.conn,
        Persisted(kill_switch="on", run_mode="active", shadow_started_at=NOW - timedelta(days=8)),
        by="test",
        now=NOW,
    )
    saved_settings = original.ctx.settings
    backup = create_backup(original.ctx, "manual")
    env = original.env
    await original.aclose()
    restored_dir = tmp_path / "restored"
    restored_dir.mkdir()
    shutil.copyfile(backup, restored_dir / "thermabot.db")
    ha = MockHA()
    ha.states["input_boolean.thermabot_enabled"]["state"] = "off"
    restored = build(
        env.model_copy(update={"DATA_DIR": str(restored_dir)}),
        clock=Clock(NOW),
        ha_transport=httpx.ASGITransport(app=ha.app),
        nws_transport=nws_ok(),
    )
    await restored.tick(NOW)
    assert restored.ctx.settings == saved_settings
    assert restored.ctx.eff().state == "DISABLED"
    assert not [call for call in ha.calls if call["domain"] == "climate"]
    await restored.aclose()
