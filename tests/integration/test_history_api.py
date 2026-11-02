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
    page = c.get("/", headers={"Accept-Encoding": "gzip"})
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
