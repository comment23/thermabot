"""v0.6.2 (presence/away-mode fix): presence actually wired into tick() + replan().

Confirms the full fix, not just core units: tick() reads each configured person's life360
tracker from HA, resolves Presence, attaches it to Snapshot, and replan() passes house into
PlanInputs/fallback_plan so EMPTY produces away-band setpoints. Also covers the trigger-replan-
on-presence-change wiring in the tick loop (D§13/loops.trigger_replan).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from tests.house import HOUSE, house_settings, seed_house
from tests.integration.mock_ha import GOOD_TOKEN, MockHA
from tests.integration.test_jobs import NOW, Clock, nws_ok
from thermabot.adapters import db
from thermabot.app.main import Runtime, build, load_env
from thermabot.core.statemachine import Persisted
from thermabot.core.types import HouseState

ALEX = "device_tracker.life360_alex"
SAM = "device_tracker.life360_sam"
JORDAN = "device_tracker.life360_jordan"
ALL_TRACKERS = (ALEX, SAM, JORDAN)


def _seed_away(ha: MockHA, t_iso: str) -> None:
    for eid in ALL_TRACKERS:
        ha._set(eid, "not_home", {}, None)
        ha.states[eid]["last_reported"] = t_iso


def _seed_home(ha: MockHA, t_iso: str) -> None:
    for eid in ALL_TRACKERS:
        ha._set(eid, "home", {}, None)
        ha.states[eid]["last_reported"] = t_iso


def _seed_away_long_unchanged(ha: MockHA, changed_iso: str, reported_iso: str) -> None:
    """A tracker that flipped to not_home at `changed_iso` and hasn't changed VALUE since
    (so last_changed stays old), but HA is still actively polling it at `reported_iso`
    (last_reported/last_updated recent) -- the exact live shape Bob found in production."""
    for eid in ALL_TRACKERS:
        ha._set(eid, "not_home", {}, None)
        ha.states[eid]["last_changed"] = changed_iso
        ha.states[eid]["last_reported"] = reported_iso
        ha.states[eid]["last_updated"] = reported_iso


def rt_of(tmp_path: Path, clk: Clock, ha: MockHA) -> Runtime:
    e = load_env(
        {
            "HEARTBEAT_WEBHOOK_URL": "http://ha/api/webhook/hb-test",
            "HA_URL": "http://ha",
            "HA_TOKEN": GOOD_TOKEN,
            "DATA_DIR": str(tmp_path),
        }
    )
    seed_house(tmp_path)
    return build(
        e,
        clock=clk,
        ha_transport=httpx.ASGITransport(app=ha.app),
        nws_transport=nws_ok(),
    )


async def test_guest_control_prevents_away_plan_until_switched_off(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from thermabot.app.auth import create_user
    from thermabot.app.routes_api import create_app

    ha = MockHA()
    _seed_away(ha, NOW.isoformat())
    rt = rt_of(tmp_path, Clock(NOW), ha)
    await rt.tick(NOW)
    later = NOW + timedelta(minutes=31)
    _seed_away(ha, later.isoformat())
    await rt.tick(later)
    assert rt.ctx.snapshot is not None and rt.ctx.snapshot.presence is not None
    assert rt.ctx.snapshot.presence.house == HouseState.EMPTY
    create_user(rt.conn, "tester", "owner", "correct horse", now=NOW)
    client = TestClient(create_app(rt.ctx))
    csrf = client.post("/login", json={"username": "tester", "password": "correct horse"}).json()[
        "csrf"
    ]
    headers = {"X-CSRF-Token": csrf}
    assert client.post("/api/guest", json={"on": True}, headers=headers).status_code == 200
    await rt.tick(later)
    assert rt.ctx.snapshot.presence.house == HouseState.OCCUPIED
    assert client.post("/api/guest", json={"on": False}, headers=headers).status_code == 200
    await rt.tick(later)
    assert rt.ctx.snapshot.presence.house == HouseState.EMPTY
    await rt.aclose()


@pytest.mark.brd("FR-PRS-1")
@pytest.mark.brd("FR-PLN-7")
async def test_tick_wires_presence_into_snapshot(tmp_path: Path) -> None:
    ha = MockHA()
    _seed_away(ha, NOW.isoformat())
    rt = rt_of(tmp_path, Clock(NOW), ha)
    await rt.tick(NOW)
    snap = rt.ctx.snapshot
    assert snap is not None and snap.presence is not None
    # clock just started: not EMPTY yet (empty_after_min=30)
    assert snap.presence.house == HouseState.OCCUPIED
    await rt.aclose()


@pytest.mark.brd("FR-PRS-1")
@pytest.mark.brd("FR-PLN-7")
async def test_full_tick_replan_cycle_everyone_away_produces_away_band_plan(
    tmp_path: Path,
) -> None:
    """The real end-to-end bug: everyone away > empty_after_min -> a replanned Plan with
    away-band setpoints, not home/sleep bands or a frozen current-setpoint hold."""
    ha = MockHA()
    t0 = NOW
    _seed_away(ha, t0.isoformat())
    clk = Clock(t0)
    rt = rt_of(tmp_path, clk, ha)
    db.write_bot_state(
        rt.conn,
        Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=t0),
        by="test",
        now=t0,
    )
    await rt.tick(t0)
    assert rt.ctx.snapshot is not None and rt.ctx.snapshot.presence is not None
    assert rt.ctx.snapshot.presence.house == HouseState.OCCUPIED  # clock just started

    t1 = t0 + timedelta(minutes=31)
    clk.t = t1
    _seed_away(ha, t1.isoformat())  # HA keeps polling: last_reported refreshes every tick
    await rt.tick(t1)
    assert rt.ctx.snapshot is not None and rt.ctx.snapshot.presence is not None
    assert rt.ctx.snapshot.presence.house == HouseState.EMPTY

    await rt.replan(t1)
    p = rt.ctx.plan
    assert p is not None and p.kind == "fallback"  # no model fit yet in this test
    heat_lo, cool_hi = HOUSE.bands.heat.away[0], HOUSE.bands.cool.away[1]
    assert (p.steps[0].heat_sp, p.steps[0].cool_sp) == (heat_lo, cool_hi)
    assert p.steps[0].reason.startswith("fallback: away band")
    await rt.aclose()


@pytest.mark.brd("FR-PRS-2")
async def test_presence_change_triggers_replan_within_one_tick(tmp_path: Path) -> None:
    """loops.py's trigger_replan docstring says 'presence change' should trigger a replan;
    confirm the tick loop actually calls it on an EMPTY<->OCCUPIED transition."""
    ha = MockHA()
    t0 = NOW
    _seed_away(ha, t0.isoformat())
    clk = Clock(t0)
    rt = rt_of(tmp_path, clk, ha)
    await rt.tick(t0)
    t1 = t0 + timedelta(minutes=31)
    clk.t = t1
    _seed_away(ha, t1.isoformat())  # HA keeps polling: last_reported refreshes every tick
    await rt.tick(t1)
    assert rt.ctx.snapshot is not None and rt.ctx.snapshot.presence is not None
    assert rt.ctx.snapshot.presence.house == HouseState.EMPTY
    rt.loops._replan_due = False  # reset: only the transition itself should set it
    t2 = t1 + timedelta(minutes=1)
    _seed_home(ha, t2.isoformat())  # someone comes home
    clk.t = t2
    await rt.tick(t2)
    assert rt.ctx.snapshot is not None and rt.ctx.snapshot.presence is not None
    assert rt.ctx.snapshot.presence.house == HouseState.OCCUPIED
    assert rt.loops._replan_due is True
    await rt.aclose()


@pytest.mark.brd("FR-PRS-3")
async def test_no_presence_configured_still_defaults_occupied(tmp_path: Path) -> None:
    """Regression: a house with no `presence.people` configured must still default the
    plan/tick path to OCCUPIED, unchanged from before this fix."""
    ha = MockHA()
    e = load_env(
        {
            "HEARTBEAT_WEBHOOK_URL": "http://ha/api/webhook/hb-test",
            "HA_URL": "http://ha",
            "HA_TOKEN": GOOD_TOKEN,
            "DATA_DIR": str(tmp_path),
        }
    )
    no_people = house_settings(presence={"people": {}, "excluded": []})
    seed_house(tmp_path, no_people)
    rt = build(
        e,
        clock=Clock(NOW),
        ha_transport=httpx.ASGITransport(app=ha.app),
        nws_transport=nws_ok(),
    )
    await rt.tick(NOW)
    assert rt.ctx.snapshot is not None and rt.ctx.snapshot.presence is not None
    assert rt.ctx.snapshot.presence.house == HouseState.OCCUPIED
    await rt.aclose()


@pytest.mark.brd("FR-PRS-4")
async def test_long_away_with_fresh_poll_resolves_empty_not_stale(tmp_path: Path) -> None:
    """v0.6.4 regression: Bob's production bug, reproduced against the real tick/read_presence
    pipeline. A tracker flips to not_home once and then sits unchanged for hours (last_changed
    stays old) while HA keeps actively polling it every cycle (last_reported/last_updated
    recent). Before the fix, staleness was computed from last_changed, so by the time
    away_since would clear empty_after_min the reading *also* looked stale under
    life360_stale_min (30 min default) and got discarded -> resolve_person fell through to
    unknown -> HOME -> house stayed OCCUPIED despite everyone confirmed away for hours. Mirrors
    Bob's live evidence: not_home since ~18:05 UTC, last_reported ~23:56 UTC (5h51m gap between
    last_changed and last_reported, life360_stale_min=30)."""
    ha = MockHA()
    t0 = NOW
    changed_iso = t0.isoformat()  # the value flips to not_home here, once, and never again
    _seed_away_long_unchanged(ha, changed_iso, t0.isoformat())
    clk = Clock(t0)
    rt = rt_of(tmp_path, clk, ha)
    await rt.tick(t0)
    assert rt.ctx.snapshot is not None and rt.ctx.snapshot.presence is not None
    assert rt.ctx.snapshot.presence.house == HouseState.OCCUPIED  # clock just started

    t1 = t0 + timedelta(hours=5, minutes=51)  # Bob's exact gap: last_changed stays at t0,
    # last_reported/last_updated are refreshed every poll, so they track close to t1
    clk.t = t1
    _seed_away_long_unchanged(ha, changed_iso, t1.isoformat())
    await rt.tick(t1)
    assert rt.ctx.snapshot is not None and rt.ctx.snapshot.presence is not None
    assert rt.ctx.snapshot.presence.house == HouseState.EMPTY  # correct: away >> empty_after_min
    for name in ("alex", "sam", "jordan"):
        pp = rt.ctx.snapshot.presence.people[name]
        assert pp.source == "life360" and pp.age_min is not None and pp.age_min < 1
    await rt.aclose()


@pytest.mark.brd("FR-PRS-4")
async def test_long_away_with_truly_stale_poll_still_falls_through_to_home(
    tmp_path: Path,
) -> None:
    """Inverse of the regression above: when last_reported really IS old too (the integration
    actually stopped polling, not just a steady unchanged value), the tracker must still be
    treated as stale and fall through to unknown -> HOME. Confirms the fix only changed which
    field drives staleness, not the staleness behavior itself."""
    ha = MockHA()
    t0 = NOW
    _seed_away_long_unchanged(ha, t0.isoformat(), t0.isoformat())
    clk = Clock(t0)
    rt = rt_of(tmp_path, clk, ha)
    await rt.tick(t0)
    assert rt.ctx.snapshot is not None and rt.ctx.snapshot.presence is not None
    assert rt.ctx.snapshot.presence.house == HouseState.OCCUPIED

    t1 = t0 + timedelta(hours=5, minutes=51)
    clk.t = t1
    # last_reported/last_updated NOT refreshed: integration genuinely stopped polling
    await rt.tick(t1)
    assert rt.ctx.snapshot is not None and rt.ctx.snapshot.presence is not None
    assert rt.ctx.snapshot.presence.house == HouseState.OCCUPIED  # correctly falls back to HOME
    for name in ("alex", "sam", "jordan"):
        pp = rt.ctx.snapshot.presence.people[name]
        assert pp.source == "unknown"
    await rt.aclose()
