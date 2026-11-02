"""T1.31: scheduler loops with a fake clock (no real sleeps)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from thermabot.app.loops import HEARTBEAT_ENTITY, TICK_S, Jobs, Loops
from thermabot.core.summary import TZ


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += timedelta(seconds=s)


class Rec:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[datetime] = []
        self.fail = fail

    async def __call__(self, now: datetime) -> None:
        self.calls.append(now)
        if self.fail:
            raise RuntimeError("boom")


def make(start: datetime, state: str = "DISABLED", **jobs: Rec) -> tuple[Loops, Clock, list]:
    clk = Clock(start)
    posts: list[tuple[str, str, dict[str, object]]] = []

    async def publish(eid: str, st: str, attrs: dict[str, object]) -> None:
        posts.append((eid, st, attrs))

    lp = Loops(
        publish=publish, state_fn=lambda: state, jobs=Jobs(**jobs), clock=clk, sleep=clk.sleep
    )
    return lp, clk, posts


async def run_for(lp: Loops, clk: Clock, minutes: int) -> None:
    for _ in range(minutes):
        await lp.run_once()
        await clk.sleep(TICK_S)


T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@pytest.mark.brd("NFR-SAFE-8")
@pytest.mark.parametrize("state", ["DISABLED", "SHADOW", "ACTIVE · OVERRIDE", "SHADOW · DEGRADED"])
async def test_heartbeat_every_tick_every_state(state: str) -> None:
    lp, clk, posts = make(T0, state)
    await run_for(lp, clk, 120)
    hb = [p for p in posts if p[0] == HEARTBEAT_ENTITY]
    assert len(hb) == 120 and all(p[2]["state"] == state for p in hb)


@pytest.mark.brd("NFR-SAFE-8")
async def test_heartbeat_even_when_tick_fails_and_publish_fails() -> None:
    tick = Rec(fail=True)
    lp, clk, posts = make(T0, tick=tick)
    await run_for(lp, clk, 5)
    assert len(posts) == 5 and len(tick.calls) == 5

    async def bad(*_: object) -> None:
        raise ConnectionError

    lp.publish = bad
    await run_for(lp, clk, 2)  # does not raise


@pytest.mark.brd("FR-PLAN-1")
async def test_replan_hourly_at_05_and_on_trigger() -> None:
    rp = Rec()
    lp, clk, _ = make(datetime(2026, 10, 5, 16, 0, tzinfo=UTC), replan=rp)
    await run_for(lp, clk, 180)
    local = [c.astimezone(TZ) for c in rp.calls]
    assert local[0].minute == 0  # start-up replan
    assert [t.minute for t in local[1:]] == [5, 5, 5]
    lp.trigger_replan()
    await run_for(lp, clk, 1)
    assert len(rp.calls) == 5


@pytest.mark.brd("FR-MODEL-2")
async def test_refit_rollup_backup_once_per_night() -> None:
    refit, rollup, backup, rp = Rec(), Rec(), Rec(), Rec()
    start = datetime(2026, 10, 5, 22, 0, tzinfo=TZ)
    lp, clk, _ = make(start, refit=refit, rollup=rollup, backup=backup, replan=rp)
    await run_for(lp, clk, 48 * 60)
    assert [c.astimezone(TZ).strftime("%H:%M") for c in refit.calls] == ["03:15", "03:15"]
    assert [c.astimezone(TZ).strftime("%H:%M") for c in rollup.calls] == ["00:05", "00:05"]
    assert [c.astimezone(TZ).strftime("%H:%M") for c in backup.calls] == ["03:30", "03:30"]
    # refit -> replan on the next tick
    assert any(c.astimezone(TZ).strftime("%H:%M") == "03:16" for c in rp.calls)


@pytest.mark.brd("FR-SUM-1")
@pytest.mark.parametrize(
    "sunday", [datetime(2026, 11, 1), datetime(2027, 3, 14), datetime(2026, 10, 4)]
)
async def test_summary_once_at_1800_et_across_dst(sunday: datetime) -> None:
    summ = Rec()
    start = datetime(sunday.year, sunday.month, sunday.day, tzinfo=TZ) - timedelta(days=1)
    lp, clk, _ = make(start, summary=summ)
    await run_for(lp, clk, 3 * 24 * 60)
    assert [c.astimezone(TZ).strftime("%a %H:%M") for c in summ.calls] == ["Sun 18:00"]


@pytest.mark.brd("FR-SUM-1")
@pytest.mark.parametrize(("late_h", "sent"), [(1, True), (5.9, True), (6.5, False)])
async def test_summary_late_send_rule(late_h: float, sent: bool) -> None:
    summ = Rec()
    start = datetime(2026, 10, 4, 18, 0, tzinfo=TZ) + timedelta(hours=late_h)
    lp, clk, _ = make(start, summary=summ)
    await run_for(lp, clk, 3)
    assert len(summ.calls) == (1 if sent else 0)


@pytest.mark.brd("FR-SUM-1")
async def test_summary_not_resent_after_restart_same_slot() -> None:
    summ = Rec()
    start = datetime(2026, 10, 4, 18, 30, tzinfo=TZ)
    lp, clk, _ = make(start, summary=summ)
    await run_for(lp, clk, 2)
    slot = lp.last_summary_slot
    lp2, clk2, _ = make(clk.t, summary=summ)
    lp2.last_summary_slot = slot
    await run_for(lp2, clk2, 2)
    assert len(summ.calls) == 1


@pytest.mark.brd("FR-SUM-1")
async def test_run_uses_injected_sleep() -> None:
    lp, clk, posts = make(T0)
    n = 0

    async def sl(s: float) -> None:
        nonlocal n
        n += 1
        await clk.sleep(s)
        if n == 3:
            lp.stop()

    lp.sleep = sl
    await lp.run()
    assert len(posts) == 3 and clk.t == T0 + timedelta(minutes=3)


def _persisted(start: datetime, conn: object, summ: Rec) -> tuple[Loops, Clock, list]:
    from thermabot.app.loops import slot_store

    lp, clk, posts = make(start, summary=summ)
    lp.load_slot, lp.save_slot = slot_store(conn)  # type: ignore[arg-type]
    return lp, clk, posts


@pytest.mark.brd("FR-SUM-1")
async def test_summary_slot_persisted_across_restart(tmp_path: object) -> None:
    from thermabot.adapters import db

    path = tmp_path / "t.db"  # type: ignore[operator]
    summ = Rec()
    lp, clk, _ = _persisted(datetime(2026, 10, 4, 17, 58, tzinfo=TZ), db.open_db(path), summ)
    await run_for(lp, clk, 5)
    assert [c.astimezone(TZ).strftime("%a %H:%M") for c in summ.calls] == ["Sun 18:00"]
    # process restart at 18:30: fresh Loops, fresh connection, no in-memory state
    lp2, clk2, _ = _persisted(datetime(2026, 10, 4, 18, 30, tzinfo=TZ), db.open_db(path), summ)
    await run_for(lp2, clk2, 3)
    assert len(summ.calls) == 1


@pytest.mark.brd("FR-SUM-1")
@pytest.mark.parametrize(("restart", "sent"), [((10, 4, 23, 30), True), ((10, 5, 1, 0), False)])
async def test_missed_summary_after_restart_6h_rule(
    tmp_path: object, restart: tuple[int, ...], sent: bool
) -> None:
    """Down over 18:00: start-up send only when < 6 h late (D§12.3); 01:00 Mon is 7 h late."""
    from thermabot.adapters import db

    summ = Rec()
    conn = db.open_db(tmp_path / "t.db")  # type: ignore[operator]
    mo, d, h, m = restart
    lp, clk, _ = _persisted(datetime(2026, mo, d, h, m, tzinfo=TZ), conn, summ)
    await run_for(lp, clk, 3)
    assert len(summ.calls) == (1 if sent else 0)
    lp2, clk2, _ = _persisted(clk.t, db.open_db(tmp_path / "t.db"), summ)  # type: ignore[operator]
    await run_for(lp2, clk2, 3)
    assert len(summ.calls) == (1 if sent else 0)


@pytest.mark.brd("FR-SUM-1")
def test_kv_tamper_reads_none(tmp_path: object) -> None:
    from thermabot.adapters import db

    conn = db.open_db(tmp_path / "t.db")  # type: ignore[operator]
    db.write_kv(conn, "k", "v")
    assert db.read_kv(conn, "k") == "v"
    conn.execute("UPDATE bot_kv SET value='x' WHERE key='k'")
    assert db.read_kv(conn, "k") is None
