"""T1.33: JSON API + SSE (D§11.2) with Pydantic responses; /healthz 503 when tick stale."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.house import HOUSE
from thermabot.adapters import db
from thermabot.app import jobs as J
from thermabot.app.auth import CSRF_HEADER, Auth, create_user
from thermabot.app.routes_api import Ctx, create_app
from thermabot.app.sse import Broker, stream
from thermabot.core.statemachine import Live, Persisted
from thermabot.core.types import Plan, PlanStep, Snapshot, ThermostatState

NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)
PW = "correct horse"
BOB = "bob-test-token-not-a-secret-0123456789"


class Clock:
    def __init__(self) -> None:
        self.t = NOW

    def __call__(self) -> datetime:
        return self.t


def make(tmp: Path, **kw: Any) -> tuple[Ctx, Clock, TestClient]:
    clk = Clock()
    conn = db.open_db(tmp / "api.db", now=NOW)
    db.write_bot_state(
        conn,
        Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=NOW - timedelta(days=3)),
        by="test",
        now=NOW,
    )
    for name, role, room in (
        ("alex", "owner", None),
        ("ann", "admin", None),
        ("jordan", "member", "jordan"),
    ):
        create_user(conn, name, role, PW, room=room, now=NOW)  # type: ignore[arg-type]
    kw.setdefault("settings", HOUSE)
    ctx = Ctx(
        conn=conn,
        auth=Auth(conn, bob_token=BOB, clock=clk),
        clock=clk,
        live=Live(ha_mirror="on", tstat_age_min=0),
        last_tick=NOW,
        ha_health="ok",
        **kw,
    )
    return ctx, clk, TestClient(create_app(ctx))


def login(c: TestClient, user: str) -> dict[str, str]:
    r = c.post("/login", json={"username": user, "password": PW})
    assert r.status_code == 200
    return {CSRF_HEADER: r.json()["csrf"]}


@pytest.mark.brd("NFR-REL-3")
def test_healthz_ok_then_503_when_stale(tmp_path: Path) -> None:
    _, clk, c = make(tmp_path)
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["state"] == "SHADOW" and r.json()["ok"] is True
    clk.t += timedelta(minutes=3, seconds=1)
    r = c.get("/healthz")
    assert r.status_code == 503 and r.json()["ok"] is False


@pytest.mark.brd("FR-MODE-1")
def test_status_shape_and_bob(tmp_path: Path) -> None:
    ctx, _, c = make(tmp_path)
    ctx.snapshot = Snapshot(t=NOW, thermostat=ThermostatState(hvac_mode="heat", current_temp=69))
    assert c.get("/api/status").status_code == 401
    b = c.get("/api/status", headers={"Authorization": f"Bearer {BOB}"}).json()
    assert b["state"] == "SHADOW" and b["kill_switch"] == "on" and b["shadow_days"] == 3.0
    assert b["snapshot"]["hvac_mode"] == "heat"
    login(c, "jordan")
    assert c.get("/api/status").json()["run_mode"] == "shadow"


@pytest.mark.brd("FR-PLN-1")
def test_plan_and_history(tmp_path: Path) -> None:
    ctx, _, c = make(tmp_path)
    login(c, "jordan")
    assert c.get("/api/plan").status_code == 404
    ctx.plan = Plan(
        kind="fallback",
        created_at=NOW,
        steps=[
            PlanStep(t=NOW + timedelta(minutes=15), hvac_mode="heat", heat_sp=68, reason="warm up")
        ],
    )
    j = c.get("/api/plan").json()
    assert j["kind"] == "fallback" and j["steps"][0]["reason"] == "warm up"
    assert c.get("/api/status").json()["next_change"] == "warm up"
    ctx.conn.execute("INSERT INTO daily_metrics (day, heat_min) VALUES ('2026-10-04', 120)")
    h = c.get("/api/history?days=7").json()
    assert h["days"][0]["heat_min"] == 120 and h["savings"]["counted"] is False
    assert c.get("/api/history?days=0").status_code == 422


@pytest.mark.brd("FR-UI-1")
@pytest.mark.parametrize(
    ("now", "oldest"),
    [
        (datetime(2026, 10, 30, 2, 0, tzinfo=UTC), "2026-10-22"),
        (datetime(2026, 11, 2, 3, 30, tzinfo=UTC), "2026-10-25"),
        (datetime(2027, 3, 15, 3, 30, tzinfo=UTC), "2027-03-07"),
    ],
)
def test_daily_history_uses_local_calendar_day(tmp_path: Path, now: datetime, oldest: str) -> None:
    from thermabot.app.main import last_week

    ctx, clk, c = make(tmp_path)
    clk.t = now
    boundary = date.fromisoformat(oldest)
    for day, heat_min in (
        (boundary - timedelta(days=1), 60),
        (boundary, 120),
        (boundary + timedelta(days=1), 180),
    ):
        ctx.conn.execute(
            "INSERT INTO daily_metrics (day, heat_min) VALUES (?, ?)", (day.isoformat(), heat_min)
        )
    login(c, "jordan")

    data = c.get("/api/history?days=7").json()
    assert [row["day"] for row in data["days"]] == [
        oldest,
        (boundary + timedelta(days=1)).isoformat(),
    ]
    page = c.get("/history?days=7")
    assert page.status_code == 200 and f"<td>{oldest}</td>" in page.text
    assert f"<td>{(boundary - timedelta(days=1)).isoformat()}</td>" not in page.text
    assert last_week(ctx.conn, now).heat_h == 5


@pytest.mark.brd("FR-OVR-2")
def test_override_start_cancel_audited(tmp_path: Path) -> None:
    ctx, _, c = make(tmp_path)
    replans: list[int] = []
    ctx.replan = lambda: replans.append(1)
    h = login(c, "ann")
    r = c.post("/api/override", json={"target_f": 70, "duration": "1h"}, headers=h)
    assert r.status_code == 200 and r.json()["kind"] == "ui_target"
    assert c.get("/api/status").json()["state"] == "OVERRIDE"
    assert c.post("/api/override", json={"duration": "1h"}, headers=h).status_code == 422
    assert c.delete("/api/override", headers=h).status_code == 200
    assert c.get("/api/status").json()["state"] == "SHADOW"
    acts = [r[0] for r in ctx.conn.execute("SELECT action FROM audit WHERE actor='ann'")]
    assert "override_start" in acts and "override_cancel" in acts and len(replans) == 2


@pytest.mark.brd("FR-CMF-6")
def test_member_nudge_own_room_limit_3(tmp_path: Path) -> None:
    _, _, c = make(tmp_path)
    h = login(c, "jordan")
    for _ in range(3):
        assert c.post("/api/nudge", json={"delta_f": 1, "hours": 1}, headers=h).status_code == 200
    assert c.post("/api/nudge", json={"delta_f": 1, "hours": 1}, headers=h).status_code == 422
    r = c.post("/api/nudge", json={"room": "loft", "delta_f": 1, "hours": 1}, headers=h)
    assert r.status_code == 403


@pytest.mark.brd("FR-CMF-5")
def test_settings_validation_and_safety_cap(tmp_path: Path) -> None:
    _, _, c = make(tmp_path)
    h = login(c, "ann")
    s = c.get("/api/settings").json()
    s["bands"]["heat"]["home"] = [72, 70]
    r = c.put("/api/settings", json=s, headers=h)
    assert r.status_code == 422 and "bands" in str(r.json())
    s = c.get("/api/settings").json()
    s["safety"]["floor_tstat_f"] = 63
    assert c.put("/api/settings", json=s, headers=h).status_code == 403  # admin: no safety
    h = login(c, "alex")
    assert c.put("/api/settings", json=s, headers=h).status_code == 200
    assert c.get("/api/settings").json()["safety"]["floor_tstat_f"] == 63


@pytest.mark.brd("FR-USR-1")
def test_users_crud_and_last_owner(tmp_path: Path) -> None:
    _, _, c = make(tmp_path)
    h = login(c, "alex")
    r = c.post(
        "/api/users", json={"username": "bobby", "role": "member", "password": PW}, headers=h
    )
    assert r.status_code == 201
    uid = r.json()["id"]
    assert (
        c.patch(f"/api/users/{uid}", json={"room": "jordan"}, headers=h).json()["room"] == "jordan"
    )
    assert c.delete(f"/api/users/{uid}", headers=h).status_code == 200
    me = next(u for u in c.get("/api/users").json() if u["username"] == "alex")
    assert c.delete(f"/api/users/{me['id']}", headers=h).status_code == 409
    assert c.patch(f"/api/users/{me['id']}", json={"role": "admin"}, headers=h).status_code == 409


@pytest.mark.brd("FR-UI-2")
def test_health_audit_model_routes(tmp_path: Path) -> None:
    ctx, _, c = make(tmp_path)
    sent: list[str] = []

    async def alert(msg: str) -> None:
        sent.append(msg)

    ctx.send_alert = alert
    h = login(c, "alex")
    assert c.get("/api/health").json()["writes_today"]["sent"] == 0
    assert isinstance(c.get("/api/audit?limit=5").json(), list)
    assert c.get("/api/model").json()["model"] is None
    assert c.post("/api/alerts/test", headers=h).status_code == 200 and sent
    assert c.post("/api/model/refit", headers=h).status_code == 200
    assert c.post("/api/fan-experiment", json={"override_id": 99}, headers=h).status_code == 404
    assert c.post("/api/guest", json={"on": True}, headers=h).status_code == 200


@pytest.mark.brd("FR-MODE-3")
def test_health_reports_daily_shadow_soak_metrics(tmp_path: Path) -> None:
    ctx, _, c = make(tmp_path)
    login(c, "alex")
    next_day = (NOW + timedelta(days=1)).isoformat()
    for outcome, guard, reason, ts in (
        ("shadow", "shadow", "run mode SHADOW", NOW.isoformat()),
        ("shadow", "shadow", "run mode SHADOW", NOW.isoformat()),
        ("denied", "weather", "weather guard", NOW.isoformat()),
        ("sent", None, "next day write", next_day),
    ):
        ctx.conn.execute(
            "INSERT INTO writes (ts, service, entity_id, data_json, reason, outcome, guard)"
            " VALUES (?,?,?,?,?,?,?)",
            (ts, "climate.set_hvac_mode", "climate.test", "{}", reason, outcome, guard),
        )
    J.save_sample(
        ctx.conn,
        NOW,
        {},
        "SHADOW",
        None,
        [],
        effective_state="DEGRADED",
        degraded_reason="weather",
    )
    J.save_sample(
        ctx.conn, NOW + timedelta(minutes=1), {}, "SHADOW", None, [], effective_state="SHADOW"
    )
    ctx.conn.execute(
        "INSERT INTO samples (ts, snapshot_json, bot_state) VALUES (?,?,?)",
        ((NOW + timedelta(minutes=2)).isoformat(), "{}", "SHADOW"),
    )
    J.save_sample(
        ctx.conn,
        NOW + timedelta(days=1),
        {},
        "SHADOW",
        None,
        [],
        effective_state="DEGRADED",
        degraded_reason="ha",
    )

    health = c.get("/api/health").json()
    assert health["day_utc"] == "2026-10-05"
    assert health["writes_today"] == {"sent": 0, "shadow": 2, "denied": 1, "failed": 0}
    assert health["write_decisions_today"] == [
        {"outcome": "denied", "guard": "weather", "reason": "weather guard", "count": 1},
        {"outcome": "shadow", "guard": "shadow", "reason": "run mode SHADOW", "count": 2},
    ]
    assert health["degraded_today"] == {
        "sampled_minutes": 3,
        "degraded_minutes": 1,
        "untracked_minutes": 1,
        "by_reason": {"weather": 1},
    }


@pytest.mark.brd("FR-UI-2")
async def test_sse_stream_first_frame_and_publish() -> None:
    b = Broker()
    gone = False

    async def disc() -> bool:
        return gone

    g = stream(b, "event: state\ndata: {}\n\n", disc, keepalive_s=0.01)
    assert (await anext(g)).startswith("event: state")
    b.publish("alert", {"key": "write_failed"})
    assert '"write_failed"' in await anext(g)
    assert await anext(g) == ": ka\n\n"
    gone = True
    with pytest.raises(StopAsyncIteration):
        await anext(g)
    assert b.subscribers == 0


@pytest.mark.brd("FR-UI-2")
def test_events_requires_view(tmp_path: Path) -> None:
    _, _, c = make(tmp_path)
    assert c.get("/events").status_code == 401
