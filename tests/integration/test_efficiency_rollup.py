"""v0.5.0 efficiency: migration 004, rollup/backfill into efficiency_daily (synthetic samples
only), GET /api/efficiency, the Efficiency page and the dashboard tile."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.integration.mock_ha import GOOD_TOKEN, MockHA
from tests.integration.test_forecast_api import login, make
from tests.integration.test_jobs import Clock, rt_of
from thermabot.adapters import db
from thermabot.app import efficiency as EF
from thermabot.app import jobs as J
from thermabot.core.schedule import TZ
from thermabot.core.shadow import MinuteRow, ShadowDay

NOW = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
FIRST = date(2026, 1, 10)


def doc(t_out: float | None, action: str) -> dict[str, Any]:
    return {
        "tstat": {"temp": 68, "hvac_mode": "heat", "hvac_action": action},
        "blower": 1.0 if action == "heating" else 0.0,
        "cs_raw": 0.0,
        "cs": 0.0,
        "t_out": t_out,
        "t_out_fc": None,
        "rooms": {},
        "house": "OCCUPIED",
        "block": "home",
        "bot": None,
    }


def fill_day(conn: Any, d: date, t_out: float, k: float = 10.0, keep: float = 1.0) -> None:
    """One local day of 1-minute samples: constant outdoor temp, heating k min per HDD, spread
    evenly. `keep` < 1 drops minutes (coverage)."""
    start, end = J.local_day_bounds(d)
    start = start.astimezone(UTC)
    n = int((end.astimezone(UTC) - start).total_seconds() // 60)
    heat = round(k * max(0.0, 65 - t_out))
    every = n / heat if heat else None
    for m in range(int(n * keep)):
        on = every is not None and int(m % every) == 0 and m // every < heat
        J.save_sample(conn, start + timedelta(minutes=m), doc(t_out, "heating" if on else "idle"),
                      "SHADOW", None, [])  # fmt: skip


def counts(conn: Any) -> tuple[int, int, int, int]:
    return tuple(  # type: ignore[return-value]
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("samples", "daily_metrics", "shadow_daily", "efficiency_daily")
    )


@pytest.mark.brd("FR-EFF-5")
def test_migration_004_on_v3_db_forward_only(tmp_path: Path) -> None:
    """A v0.4.0-shaped DB (user_version 3, rows in samples/daily_metrics) gains the table;
    nothing else changes; re-opening is a no-op."""
    import sqlite3

    p = tmp_path / "old.db"
    raw = sqlite3.connect(p)
    for f in sorted(db.MIGRATIONS.glob("00[123]_*.sql")):
        raw.executescript(f.read_text(encoding="utf-8"))
    raw.execute("PRAGMA user_version=3")
    raw.execute("INSERT INTO samples (ts, snapshot_json, bot_state) VALUES ('x', '{}', 'SHADOW')")
    raw.execute("INSERT INTO daily_metrics (day, heat_min) VALUES ('2026-01-01', 5)")
    raw.commit()
    raw.close()
    conn = db.open_db(p, now=NOW)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert counts(conn) == (1, 1, 0, 0)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(efficiency_daily)")]
    assert cols[:6] == ["day", "mean_out_f", "hdd", "cdd", "heat_min", "cool_min"]
    conn.close()
    conn = db.open_db(p, now=NOW)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4 and counts(conn) == (1, 1, 0, 0)


@pytest.mark.brd("FR-EFF-5")
async def test_backfill_idempotent_bounded_never_touches_samples(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "t.db", now=NOW)
    temps = [30, 35, 40, 45, 50, 55]
    for i, t in enumerate(temps):
        fill_day(conn, FIRST + timedelta(days=i), t)
    fill_day(conn, FIRST + timedelta(days=6), 40, keep=0.5)  # low coverage: no point
    fill_day(conn, NOW.astimezone(TZ).date(), 40, keep=0.3)  # today: not complete, skipped
    before = conn.execute("SELECT COUNT(*), SUM(LENGTH(snapshot_json)) FROM samples").fetchone()
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)
        await asyncio.sleep(0)

    res = await EF.backfill(conn, NOW, sleep=sleep)
    span = (NOW.astimezone(TZ).date() - FIRST).days  # Jan 10 .. Jan 19
    assert res["candidates"] == span and res["written"] == 7
    assert res["qualifying_heat"] == 6 and res["qualifying_cool"] == 0
    assert len(sleeps) == span  # a yield after every one-day chunk
    assert res["max_chunk_s"] < 1.0
    after = conn.execute("SELECT COUNT(*), SUM(LENGTH(snapshot_json)) FROM samples").fetchone()
    assert tuple(before) == tuple(after)
    r = conn.execute("SELECT * FROM efficiency_daily WHERE day=?", (FIRST.isoformat(),)).fetchone()
    assert r["mean_out_f"] == 30 and r["hdd"] == 35 and r["cdd"] == 0
    assert r["heat_min"] == pytest.approx(350, abs=1) and r["coverage_pct"] == 100
    assert r["out_hours"] == 24 and r["source"] == "backfill"
    low = conn.execute(
        "SELECT coverage_pct FROM efficiency_daily WHERE day=?",
        ((FIRST + timedelta(days=6)).isoformat(),),
    ).fetchone()[0]
    assert low == 50
    rows = [tuple(x) for x in conn.execute("SELECT * FROM efficiency_daily ORDER BY day")]
    again = await EF.backfill(conn, NOW, sleep=sleep)
    assert again["written"] == 0 and again["candidates"] == span - 7  # empty days only
    assert [tuple(x) for x in conn.execute("SELECT * FROM efficiency_daily ORDER BY day")] == rows


@pytest.mark.brd("FR-EFF-5")
async def test_backfill_empty_db(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "t.db", now=NOW)
    res = await EF.backfill(conn, NOW, pause_s=0)
    assert res["candidates"] == 0 and res["written"] == 0


@pytest.mark.brd("FR-EFF-1")
def test_day_row_nulls_without_outdoor() -> None:
    t = datetime(2026, 1, 10, 5, 0, tzinfo=UTC)
    rows = EF.light_rows([(t, doc(None, "heating")), (t + timedelta(minutes=1), "bad")])  # type: ignore[list-item]
    r = EF.day_row(rows, 1440)
    assert r["mean_out_f"] is None and r["hdd"] is None and r["heat_min"] == 1
    assert r["n_samples"] == 1 and r["out_hours"] == 0


@pytest.mark.brd("FR-EFF-1")
def test_day_row_counts_both_fall_back_hours() -> None:
    rows = [
        MinuteRow(t=datetime(2026, 11, 1, hour, 0, tzinfo=UTC), t_out=temp)
        for hour, temp in ((5, 40), (6, 60), (7, 60))
    ]
    result = EF.day_row(rows, 1500)
    assert result["out_hours"] == 3
    assert result["mean_out_f"] == pytest.approx(53.333)


@pytest.mark.brd("FR-EFF-5")
async def test_rollup_writes_efficiency_row(tmp_path: Path) -> None:
    clk = Clock(datetime(2026, 1, 11, 5, 5, tzinfo=UTC))  # 00:05 local
    rt = rt_of(tmp_path, clk)
    fill_day(rt.conn, FIRST, 35)
    n0 = rt.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    await rt.rollup(clk.t)
    r = rt.conn.execute("SELECT * FROM efficiency_daily").fetchall()
    assert len(r) == 1 and r[0]["day"] == FIRST.isoformat() and r[0]["source"] == "rollup"
    assert r[0]["hdd"] == 30 and r[0]["heat_min"] == pytest.approx(300, abs=1)
    dm = rt.conn.execute("SELECT heat_min, hdd FROM daily_metrics").fetchone()
    assert dm["heat_min"] == r[0]["heat_min"]  # same runtime source as daily_metrics
    assert rt.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == n0
    await rt.rollup(clk.t)  # re-run: replaced, not duplicated
    assert rt.conn.execute("SELECT COUNT(*) FROM efficiency_daily").fetchone()[0] == 1
    await rt.aclose()


@pytest.mark.brd("FR-EFF-5")
@pytest.mark.parametrize(
    ("day", "minutes"),
    [(date(2026, 11, 1), 1500), (date(2027, 3, 14), 1380)],
)
async def test_rollup_uses_elapsed_day_and_counts_repeated_hour(
    tmp_path: Path, day: date, minutes: int
) -> None:
    start, end = J.local_day_bounds(day)
    clk = Clock(end.astimezone(UTC) + timedelta(minutes=5))
    rt = rt_of(tmp_path, clk)
    for minute in range(minutes):
        J.save_sample(
            rt.conn,
            start.astimezone(UTC) + timedelta(minutes=minute),
            doc(40, "idle"),
            "SHADOW",
            None,
            [],
        )
    await rt.rollup(clk.t)
    shadow = rt.conn.execute("SELECT n_samples, coverage_pct FROM shadow_daily").fetchone()
    efficiency = rt.conn.execute("SELECT * FROM efficiency_daily").fetchone()
    assert (shadow["n_samples"], shadow["coverage_pct"]) == (minutes, 100)
    assert efficiency["n_samples"] == minutes and efficiency["coverage_pct"] == 100
    assert efficiency["out_hours"] == minutes // 60
    assert efficiency["hdd"] == 25
    await rt.aclose()


@pytest.mark.brd("FR-EFF-5")
def test_app_start_runs_backfill_no_climate_calls(tmp_path: Path) -> None:
    from tests.house import seed_house
    from thermabot.app.main import create_app, load_env

    data = tmp_path / "data"
    seed_house(data)
    conn = db.open_db(data / "thermabot.db", now=NOW)
    yday = datetime.now(UTC).astimezone(TZ).date() - timedelta(days=1)
    fill_day(conn, yday, 40, keep=0.2)
    conn.close()
    ha = MockHA()
    env = {"HA_URL": "http://ha", "HA_TOKEN": GOOD_TOKEN, "DATA_DIR": str(data)}
    app = create_app(load_env(env), ha_transport=httpx.ASGITransport(app=ha.app))
    import time

    with TestClient(app):
        rt = app.state.runtime
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if rt.conn.execute("SELECT COUNT(*) FROM efficiency_daily").fetchone()[0]:
                break
            time.sleep(0.05)
        assert rt.conn.execute("SELECT day FROM efficiency_daily").fetchone()[0] == yday.isoformat()
    assert [x for x in ha.calls if x["domain"] == "climate"] == []


def seed_api(conn: Any, n_heat: int) -> None:
    now = datetime.now(UTC)
    for i in range(n_heat):
        day = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        t = 30 + 2 * i
        EF.save_day(conn, day, {"mean_out_f": t, "hdd": 65 - t, "cdd": 0, "heat_min": 9 * (65 - t),
                                "cool_min": 0, "n_samples": 1440, "out_hours": 24,
                                "coverage_pct": 100, "base_f": 65}, now, "backfill")  # fmt: skip
        J.save_shadow_day(conn, day, ShadowDay(actual_heat_min=200, actual_cool_min=0,
                                               saved_min=10 * (i % 3) - 5), 1, now)  # fmt: skip


@pytest.mark.brd("FR-EFF-2")
@pytest.mark.parametrize("user", ["alex", "ann", "jordan"])
def test_api_efficiency_any_role_empty(tmp_path: Path, user: str) -> None:
    c, ctx, ha = make(tmp_path)
    assert c.get("/api/efficiency").status_code == 401
    login(c, user)
    d = c.get("/api/efficiency").json()
    assert d["heat"]["empty"] == "needs >= 5 qualifying days (0/5)"
    assert d["heat"]["progress"] == "0/5" and d["cool"]["fit"] is None
    assert d["tile"]["text"] == "collecting data 0/5"
    assert d["savings"]["simulated"] is True and d["savings"]["days"] == []
    assert "base 65" in d["method"]
    assert ha.calls == []


@pytest.mark.brd("FR-EFF-2")
def test_api_efficiency_with_data_and_pages(tmp_path: Path) -> None:
    c, ctx, ha = make(tmp_path)
    seed_api(ctx.conn, 9)
    w0 = ctx.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0]
    login(c, "jordan")
    d = c.get("/api/efficiency").json()
    h = d["heat"]
    assert h["n"] == 9 and h["ready"] and h["empty"] is None
    assert h["fit"]["kind"] == "linear" and h["fit"]["r2"] == pytest.approx(1.0)
    assert len(h["fit"]["band"]) == 25 and h["points"][0]["y"] == pytest.approx(9)
    assert h["index"]["latest"] == 100.0 and d["tile"]["text"] == "index 100 (heat)"
    s = d["savings"]
    assert len(s["days"]) == 9 and s["n_estimated"] == 9
    assert s["days"][-1]["cum_saved_min"] == s["total_saved_min"]
    page = c.get("/efficiency")
    assert page.status_code == 200
    # v0.6.0: uPlot draws on a canvas; the page carries the chart data + a fallback table
    for tid in ("eff-chart-heat", "savings-chart", "simulated-label", "eff-table-heat"):
        assert f'data-testid="{tid}"' in page.text, tid
    assert 'id="eff-data-heat"' in page.text and '"band": [{' in page.text
    assert page.text.count('data-testid="eff-point"') == 9
    assert 'data-testid="eff-empty-cool"' in page.text
    assert 'href="/' not in page.text and 'src="/' not in page.text
    dash = c.get("/")
    assert "Efficiency: index 100 (heat)" in dash.text
    assert ctx.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == w0
    assert ha.calls == []
    st = db.read_bot_state(ctx.conn)
    assert st is not None and st.run_mode == "shadow"
    assert json.loads(json.dumps(d)) == d


@pytest.mark.brd("FR-EFF-2")
def test_efficiency_page_empty_and_ingress(tmp_path: Path) -> None:
    c, _, _ = make(tmp_path)
    login(c, "jordan")
    r = c.get("/efficiency", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
    assert r.status_code == 200
    assert 'data-testid="eff-empty-heat"' in r.text and 'data-testid="savings-empty"' in r.text
    dash = c.get("/")
    assert "Efficiency: collecting data 0/5" in dash.text
