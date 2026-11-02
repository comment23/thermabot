"""Scheduler loops (D§1.3, D§5, D§13, D§15): plain asyncio, fake-clock injectable.

- tick every 60 s; heartbeat published EVERY tick in EVERY state (DISABLED, kill OFF, even
  when the tick job raises) so the HA watchdog only fires when the process is dead.
- replan hourly at :05 local, or immediately after `trigger_replan()`.
- nightly refit 03:15, metrics rollup 00:05, backup 03:30 (local, America/New_York).
- weekly summary Sunday 18:00 local (DST-safe via core.summary.next_fire); missed while down ->
  sent on start-up only when < 6 h late; never twice for the same slot.
Jobs fire when the clock crosses their local wall time (prev < target <= now), so a restart
never re-runs a missed refit/backup; the next night catches up.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta

from thermabot.adapters import db
from thermabot.core.schedule import TZ
from thermabot.core.summary import next_fire, send_late

log = logging.getLogger(__name__)
TICK_S = 60.0
HEARTBEAT_ENTITY = "sensor.thermabot_heartbeat"
REPLAN_MINUTE = 5
REFIT_AT = time(3, 15)
ROLLUP_AT = time(0, 5)
BACKUP_AT = time(3, 30)
# Upper bound per background job so a hung Influx/NWS call can never stall the heartbeat
# for long (tick/backup/summary keep their existing, unbounded behaviour).
JOB_TIMEOUT_S: dict[str, float] = {"replan": 45.0, "refit": 40.0, "rollup": 40.0, "flush": 10.0}

Job = Callable[[datetime], Awaitable[object]]
Publish = Callable[[str, str, dict[str, object]], Awaitable[None]]


async def _noop(_: datetime) -> None:
    return None


@dataclass
class Jobs:
    tick: Job = _noop
    replan: Job = _noop
    refit: Job = _noop
    rollup: Job = _noop
    backup: Job = _noop
    summary: Job = _noop
    flush: Job = _noop  # Influx write-back, every tick after the control tick


def crossed(prev: datetime | None, now: datetime, at: time) -> bool:
    """True when local wall time `at` lies in (prev, now]. DST-safe (local construction)."""
    if prev is None:
        return False
    lp, ln = prev.astimezone(TZ), now.astimezone(TZ)
    d = lp.date()
    while d <= ln.date():
        target = datetime(d.year, d.month, d.day, at.hour, at.minute, tzinfo=TZ)
        if lp < target <= ln:
            return True
        d += timedelta(days=1)
    return False


@dataclass
class Loops:
    publish: Publish
    state_fn: Callable[[], str]
    jobs: Jobs = field(default_factory=Jobs)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    version: str = "0"
    last_summary_slot: datetime | None = None
    # Persistence hooks (SQLite bot_kv via slot_store); loaded on first tick, saved after send.
    load_slot: Callable[[], datetime | None] | None = None
    save_slot: Callable[[datetime], None] | None = None
    last_tick: datetime | None = None
    _prev: datetime | None = None
    _replan_due: bool = True  # replan on start-up
    _stop: bool = False

    def trigger_replan(self) -> None:
        """Forecast update, presence change, override start/end, nudge, settings save, refit."""
        self._replan_due = True

    def stop(self) -> None:
        self._stop = True

    async def _run(self, name: str, job: Job, now: datetime) -> bool:
        try:
            timeout = JOB_TIMEOUT_S.get(name)
            if timeout is None:
                await job(now)
            else:
                await asyncio.wait_for(job(now), timeout)
            return True
        except Exception:  # noqa: BLE001 - one failing job must never stop the loop
            log.exception("job %s failed", name)
            return False

    def summary_slot(self, now: datetime) -> datetime:
        """Most recent scheduled Sunday 18:00 local at or before now."""
        s = next_fire(now - timedelta(days=7))  # DST week: 7x24h may overshoot by 1 h
        return s if s <= now else next_fire(now - timedelta(days=8))

    async def heartbeat(self, now: datetime) -> None:
        try:
            await self.publish(
                HEARTBEAT_ENTITY,
                now.astimezone(UTC).isoformat(),
                {"state": self.state_fn(), "version": self.version},
            )
        except Exception:  # noqa: BLE001 - HA down; next tick retries
            log.warning("heartbeat publish failed")

    async def run_once(self) -> None:
        """One 60 s iteration. Heartbeat goes out whatever state or job failures."""
        now = self.clock()
        if self._prev is None and self.load_slot is not None and self.last_summary_slot is None:
            try:
                self.last_summary_slot = self.load_slot()
            except Exception:  # noqa: BLE001 - unreadable -> treat as never sent
                log.exception("load summary slot failed")
        try:
            if self._replan_due or (
                self._prev is not None
                and crossed(self._prev, now, time(now.astimezone(TZ).hour, REPLAN_MINUTE))
            ):
                self._replan_due = not await self._run("replan", self.jobs.replan, now)
            await self._run("tick", self.jobs.tick, now)
            self.last_tick = now
            await self._run("flush", self.jobs.flush, now)
            for name, at, job in (
                ("refit", REFIT_AT, self.jobs.refit),
                ("rollup", ROLLUP_AT, self.jobs.rollup),
                ("backup", BACKUP_AT, self.jobs.backup),
            ):
                if crossed(self._prev, now, at):
                    ok = await self._run(name, job, now)
                    if ok and name == "refit":
                        self._replan_due = True
            slot = self.summary_slot(now)
            if slot != self.last_summary_slot and send_late(slot, now):
                if await self._run("summary", self.jobs.summary, now):
                    self.last_summary_slot = slot
                    if self.save_slot is not None:
                        try:
                            self.save_slot(slot)
                        except Exception:  # noqa: BLE001
                            log.exception("save summary slot failed")
        finally:
            await self.heartbeat(now)
            self._prev = now

    async def run(self) -> None:
        while not self._stop:
            await self.run_once()
            await self.sleep(TICK_S)


SUMMARY_SLOT_KEY = "last_summary_slot"


def slot_store(
    conn: sqlite3.Connection,
) -> tuple[Callable[[], datetime | None], Callable[[datetime], None]]:
    """(load, save) for Loops.load_slot/save_slot backed by checksummed SQLite bot_kv."""

    def load() -> datetime | None:
        v = db.read_kv(conn, SUMMARY_SLOT_KEY)
        return None if v is None else datetime.fromisoformat(v)

    def save(slot: datetime) -> None:
        db.write_kv(conn, SUMMARY_SLOT_KEY, slot.astimezone(UTC).isoformat())

    return load, save
