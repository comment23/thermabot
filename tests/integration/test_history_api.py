"""v0.6.0 GET /api/history?range=24h|7d|30d: dashboard chart series from `samples`, read-only,
server-side downsampled to <= 500 points, any signed-in role; gzip + cache-busted statics.
Synthetic samples only (generic test house)."""

from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import pytest

import thermabot
from tests.integration.test_api import BOB, NOW, login, make
from thermabot.app import history as H
from thermabot.app import jobs as J
from thermabot.app import routes_ui as UI


def _doc(i: int) -> dict[str, object]:
    action = "heating" if i % 60 < 15 else ("fan" if i % 60 < 20 else "idle")
    return {
        "tstat": {"temp": 68 + (i % 10) / 10, "heat_sp": 68, "cool_sp": 76, "hvac_action": action},
        "t_out": 40.0 if i % 2 else None,
        "t_out_fc": 41.0,
        "blower": 1.0 if action != "idle" else 0.0,
    }


def _seed(conn: object, minutes: int, gap: tuple[int, int] = (0, 0)) -> None:
    for m in range(minutes):
        if gap[0] <= m < gap[1]:
            continue
        J.save_sample(conn, NOW - timedelta(minutes=m), _doc(m), "SHADOW", 90.0, [])  # type: ignore[arg-type]


@pytest.mark.brd("FR-UI-1")
@pytest.mark.parametrize("rng", ["24h", "7d", "30d"])
def test_history_ranges_downsampled(tmp_path: Path, rng: str) -> None:
    ctx, _, c = make(tmp_path)
    _seed(ctx.conn, 2 * 24 * 60, gap=(120, 180))  # 2 days with a 1 h hole
    login(c, "jordan")  # any role
    r = c.get(f"/api/history?range={rng}")
    assert r.status_code == 200
    d = r.json()
    n = len(d["t"])
    assert 0 < n <= H.MAX_POINTS
    for k in ("indoor", "outdoor", "heat_sp", "cool_sp", "heat", "cool", "fan", "n"):
        assert len(d[k]) == n, k
    assert d["range"] == rng and d["units"] == "F"
    assert all(b - a == d["step_s"] for a, b in zip(d["t"], d["t"][1:], strict=False))
    assert d["t"][-1] <= NOW.timestamp() < d["t"][-1] + d["step_s"]
    # the window is n * step seconds ending just after NOW's minute: n * step / 60 minutes
    assert d["count"] == min(2 * 24 * 60, n * d["step_s"] // 60) - 60
    assert max(x for x in d["heat_sp"] if x is not None) == 68

    def top(k: str) -> float:
        return max(x for x in d[k] if x is not None)

    assert 0 < top("heat") <= 1 and top("cool") == 0 and 0 < top("fan") <= 1
    assert all(x is None or 40 <= x <= 41 for x in d["outdoor"])  # t_out, else forecast
    assert None in d["indoor"]  # gaps stay null, never zero


@pytest.mark.brd("FR-UI-1")
def test_history_gap_and_empty(tmp_path: Path) -> None:
    ctx, _, c = make(tmp_path)
    login(c, "alex")
    d = c.get("/api/history?range=24h").json()
    assert d["count"] == 0 and all(v is None for v in d["indoor"]) and max(d["n"]) == 0
    _seed(ctx.conn, 24 * 60, gap=(120, 180))
    d = c.get("/api/history?range=24h").json()
    step = d["step_s"]
    hole = [i for i, n in enumerate(d["n"]) if n == 0]
    assert len(hole) >= 60 * 60 // step - 2  # the hour without samples is a gap
    assert all(d["indoor"][i] is None and d["heat"][i] is None for i in hole)


@pytest.mark.brd("NFR-SEC-1")
def test_history_auth_validation_read_only(tmp_path: Path) -> None:
    ctx, _, c = make(tmp_path)
    _seed(ctx.conn, 30)
    assert c.get("/api/history?range=24h").status_code == 401
    bob = {"Authorization": f"Bearer {BOB}"}
    assert c.get("/api/history?range=24h", headers=bob).status_code == 403  # not a BOB path
    login(c, "jordan")
    assert c.get("/api/history?range=1y").status_code == 422
    assert c.get("/api/history?days=7").json()["days"] == []  # legacy shape unchanged
    before = ctx.conn.total_changes
    for rng in H.RANGES:
        assert c.get(f"/api/history?range={rng}").status_code == 200
    assert ctx.conn.total_changes == before  # no writes of any kind
    st = ctx.conn.execute("SELECT kill_switch, run_mode FROM bot_state").fetchone()
    assert (st["kill_switch"], st["run_mode"]) == ("on", "shadow")


@pytest.mark.brd("FR-UI-1")
def test_grid_bounds() -> None:
    for span in H.RANGES.values():
        step, n = H.grid(span)
        assert step % 60 == 0 and n <= H.MAX_POINTS and step * n >= span.total_seconds()


@pytest.mark.brd("NFR-PERF-1")
def test_gzip_and_versioned_static(tmp_path: Path) -> None:
    _, _, c = make(tmp_path)
    login(c, "alex")
    page = c.get("/history", headers={"Accept-Encoding": "gzip"})
    assert page.headers.get("content-encoding") == "gzip"
    v = thermabot.__version__
    static = Path(__file__).resolve().parents[2] / "thermabot" / "app" / "static"
    css_hash = sha256((static / "app.css").read_bytes()).hexdigest()[:16]
    chart_hash = sha256((static / "charts.js").read_bytes()).hexdigest()[:16]
    css = f"static/app.css?v={v}-{css_hash}"
    charts = f"static/charts.js?v={v}-{chart_hash}"
    assert f'href="{css}"' in page.text
    assert f'src="{charts}"' in page.text
    r = c.get(f"/{css}", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert r.headers.get("content-encoding") == "gzip"
    assert "immutable" not in c.get("/static/app.css").headers.get("cache-control", "")


@pytest.mark.brd("NFR-PERF-1")
def test_asset_cache_key_changes_for_same_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    content = static / "sample.js"
    monkeypatch.setattr(UI, "HERE", tmp_path)
    try:
        content.write_text("before", encoding="utf-8")
        UI.asset.cache_clear()
        before = UI.asset("sample.js")
        content.write_text("after", encoding="utf-8")
        UI.asset.cache_clear()  # A new application process loads the changed asset.
        after = UI.asset("sample.js")
        assert before.startswith(f"static/sample.js?v={thermabot.__version__}-")
        assert after.startswith(f"static/sample.js?v={thermabot.__version__}-")
        assert before != after
    finally:
        UI.asset.cache_clear()


def _room_doc(temp: object, age: object = 0, block: str = "home") -> dict[str, object]:
    return {
        "tstat": {"temp": 70, "age_min": 0, "hvac_action": "heating" if temp == 70 else "cooling"},
        "t_out_fc": 40,
        "block": block,
        "rooms": {
            "bedroom_1": {"temp": temp, "age_min": age},
            "study": {"temp": 99, "age_min": 0},
        },
    }


@pytest.mark.brd("FR-UI-1")
def test_room_report_weights_minutes_and_excludes_untrusted_readings(tmp_path: Path) -> None:
    ctx, _, _ = make(tmp_path)
    for m in range(1, 7):
        J.save_sample(ctx.conn, NOW - timedelta(minutes=m), _room_doc(70), "SHADOW", 90, [])
    for m in range(61, 64):
        J.save_sample(ctx.conn, NOW - timedelta(minutes=m), _room_doc(64), "SHADOW", 90, [])
    for m, temp, age, stale in (
        (10, 99, None, []),
        (11, 99, 91, []),
        (12, 99, 0, ["bedroom_1"]),
        (13, None, 0, []),
        (14, True, 0, []),
    ):
        J.save_sample(
            ctx.conn, NOW - timedelta(minutes=m), _room_doc(temp, age), "SHADOW", 90, stale
        )
    before = ctx.conn.total_changes
    r = H.room_report(ctx.conn, NOW, "24h", "bedroom_1", ctx.settings, "shoulder")
    assert ctx.conn.total_changes == before
    assert (r["measured"], r["stale"], r["missing"]) == (9, 3, 2)
    assert r["mean"] == 68 and (r["low"], r["high"]) == (64, 70)
    assert r["comfort_pct"] == pytest.approx(100 * 6 / 9)
    assert r["excursion"] == pytest.approx(2 / 3) and r["outside_min"] == 3
    assert r["offset"] == -2 and (r["heat_min"], r["cool_min"]) == (6, 3)
    assert r["coverage_pct"] == pytest.approx(100 * 9 / 1440)
    assert r["change"] is None  # too few measurements in the previous interval
    assert r["rows"][0]["temp"] is None  # no fill-forward across gaps


@pytest.mark.brd("FR-UI-1")
@pytest.mark.parametrize(("rng", "intervals"), [("24h", 24), ("7d", 28), ("30d", 30)])
def test_room_report_range_bounds_and_thermostat(tmp_path: Path, rng: str, intervals: int) -> None:
    ctx, _, _ = make(tmp_path)
    for t, doc, stale in (
        (NOW, _room_doc(70), []),
        (NOW + timedelta(minutes=1), _room_doc(99), []),
        (NOW - H.RANGES[rng], _room_doc(99), []),
        (NOW - timedelta(minutes=1), _room_doc(99), ["thermostat"]),
    ):
        J.save_sample(ctx.conn, t, doc, "SHADOW", 90, stale)
    r = H.room_report(ctx.conn, NOW, rng, "thermostat", ctx.settings, "heat")
    assert len(r["rows"]) == intervals and r["measured"] == 1 and r["stale"] == 1
    assert r["mean"] == 70 and r["comfort_pct"] == 100 and r["offset"] == 0
    assert r["rows"][-1]["end"].timestamp() == NOW.timestamp() + 60


@pytest.mark.brd("FR-UI-1")
def test_room_report_reference_blocks_and_unknown_runtime(tmp_path: Path) -> None:
    ctx, _, _ = make(tmp_path)
    for m, block in enumerate(("home", "sleep", "unknown")):
        doc = _room_doc(65, block=block)
        doc["tstat"] = {"temp": None, "hvac_action": None}
        J.save_sample(ctx.conn, NOW - timedelta(minutes=m), doc, "SHADOW", None, [])
    r = H.room_report(ctx.conn, NOW, "24h", "bedroom_1", ctx.settings, "shoulder")
    assert r["measured"] == 3 and r["assessed"] == 2
    assert r["comfort_pct"] == 50 and r["outside_min"] == 1
    assert r["offset"] is None and r["heat_min"] is None and r["cool_min"] is None


@pytest.mark.brd("FR-UI-1")
def test_legacy_thermostat_samples_do_not_claim_verified_freshness(tmp_path: Path) -> None:
    ctx, _, _ = make(tmp_path)
    doc = _room_doc(70)
    doc["tstat"] = {"temp": 70, "hvac_action": "heating"}
    J.save_sample(ctx.conn, NOW, doc, "SHADOW", 90, [])
    r = H.room_report(ctx.conn, NOW, "24h", "thermostat", ctx.settings, "heat")
    assert r["measured"] == 0 and r["stale"] == 1
    assert r["mean"] is None and r["comfort_pct"] is None and r["heat_min"] is None


@pytest.mark.brd("NFR-SEC-1")
def test_room_reports_preserve_room_access_and_are_read_only(tmp_path: Path) -> None:
    ctx, _, client = make(tmp_path)
    ctx.conn.execute("UPDATE users SET room='bedroom_1' WHERE username='jordan'")
    J.save_sample(ctx.conn, NOW, _room_doc(70), "SHADOW", 90, [])
    assert client.get("/room", follow_redirects=False).status_code == 303
    login(client, "jordan")
    before = ctx.conn.total_changes
    for rng in H.RANGES:
        response = client.get(f"/room?range={rng}")
        assert response.status_code == 200 and 'data-testid="room-mean">70°F' in response.text
        assert 'href="room?room=study' not in response.text and "99°F" not in response.text
    assert ctx.conn.total_changes == before
    assert client.get("/room?room=study&range=7d").status_code == 403
    assert client.get("/room?range=invalid").status_code == 422
    login(client, "ann")
    assert client.get("/room?room=missing").status_code == 404
    assert client.get("/room?room=study&range=7d").status_code == 200
    assert "No room is assigned" in client.get("/room").text
