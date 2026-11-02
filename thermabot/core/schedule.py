"""Weekly schedule -> active block at time t, next boundary, season (D§5.2, D§8.5, FR-CMF-2).

Pure: `now` is passed in. Wall-clock evaluation in America/New_York, so DST days have
23 or 25 hours but blocks still start at their local times.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from thermabot.config import BlockName, BlockStart, ScheduleCfg, SeasonCfg
from thermabot.core.types import Forecast, Season

TZ = ZoneInfo("America/New_York")


def _day_list(cfg: ScheduleCfg, d: datetime) -> list[BlockStart]:
    return cfg.weekend if d.weekday() >= 5 else cfg.weekday


def _start(b: BlockStart) -> time:
    h, m = b.start.split(":")
    return time(int(h), int(m))


def _require_aware(t: datetime) -> datetime:
    if t.tzinfo is None:
        raise ValueError("naive datetime; core requires aware times")
    return t.astimezone(TZ)


def block_at(cfg: ScheduleCfg, t: datetime) -> BlockName:
    """Scheduled block (home/sleep) in effect at t. Before the day's first start, the
    previous day's last block still applies (e.g. Fri 22:00 sleep runs to Sat 08:00)."""
    lt = _require_aware(t)
    today = _day_list(cfg, lt)
    current = [b for b in today if _start(b) <= lt.time()]
    if current:
        return current[-1].block
    return _day_list(cfg, lt - timedelta(days=1))[-1].block


def next_boundary(cfg: ScheduleCfg, t: datetime) -> datetime:
    """First block start strictly after t, as an aware local datetime."""
    lt = _require_aware(t)
    for offset in range(8):
        day = (lt + timedelta(days=offset)).date()
        dl = _day_list(cfg, datetime(day.year, day.month, day.day, tzinfo=TZ))
        for b in dl:
            cand = datetime.combine(day, _start(b), tzinfo=TZ)
            if cand > lt:
                return cand
    raise AssertionError("schedule has no blocks")  # unreachable: validator requires blocks


def season_for(cfg: SeasonCfg, forecast_temps_f: Sequence[float]) -> Season:
    """heat if 24 h mean < heat_below_f, cool if > cool_above_f, else shoulder."""
    if not forecast_temps_f:
        raise ValueError("no forecast temperatures")
    mean = sum(forecast_temps_f) / len(forecast_temps_f)
    if mean < cfg.heat_below_f:
        return "heat"
    if mean > cfg.cool_above_f:
        return "cool"
    return "shoulder"


def season_from_forecast(cfg: SeasonCfg, fc: Forecast, now: datetime) -> Season:
    """Season from the forecast points in [now, now + 24 h)."""
    end = now + timedelta(hours=24)
    temps = [p.temp_f for p in fc.points if now <= p.t < end]
    return season_for(cfg, temps)
