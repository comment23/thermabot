"""Weekly look-ahead summary (D\x1512.3, FR-ALR-2). Pure: builds text from given inputs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from thermabot.core.metrics import Baseline, Savings
from thermabot.core.schedule import TZ

DayKind = Literal["heating", "cooling", "mild"]
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class LastWeek(BaseModel):
    heat_h: float
    cool_h: float
    cool_effort_h: float
    fan_only_h: float
    savings: Savings | None = None  # None -> baseline low confidence
    comfort_pct: float
    violation_min: float
    overrides_by_user: dict[str, int] = Field(default_factory=dict)
    stale_sensor_h: float = 0
    safety_restores: int = 0
    watchdog_events: int = 0


class DayForecast(BaseModel):
    day: date
    high_f: float
    low_f: float


class DayOutlook(BaseModel):
    day: date
    high_f: float
    low_f: float
    kind: DayKind
    expected_h: float
    lower_confidence: bool


def classify(d: DayForecast, heat_below_f: float = 60, cool_above_f: float = 68) -> DayKind:
    mean = (d.high_f + d.low_f) / 2
    if mean < heat_below_f:
        return "heating"
    if mean > cool_above_f:
        return "cooling"
    return "mild"


def outlook(
    days: list[DayForecast], heat: Baseline | None, cool: Baseline | None, base_f: float = 65
) -> list[DayOutlook]:
    out: list[DayOutlook] = []
    for i, d in enumerate(days[:7]):
        mean = (d.high_f + d.low_f) / 2
        hdd, cdd = max(0.0, base_f - mean), max(0.0, mean - base_f)
        mins = 0.0
        if heat is not None:
            mins += max(0.0, heat.beta * hdd + heat.alpha) if hdd > 0 else 0.0
        if cool is not None:
            mins += max(0.0, cool.beta * cdd + cool.alpha) if cdd > 0 else 0.0
        out.append(
            DayOutlook(
                day=d.day,
                high_f=d.high_f,
                low_f=d.low_f,
                kind=classify(d),
                expected_h=round(mins / 60, 1),
                lower_confidence=i >= 3,
            )
        )
    return out


def stretches(days: list[DayForecast]) -> list[str]:
    """>= 2 consecutive days with high >= 88 (hot) or low <= 25 (cold)."""
    out: list[str] = []
    hits: tuple[tuple[str, Callable[[DayForecast], bool]], ...] = (
        ("hot", lambda d: d.high_f >= 88),
        ("cold", lambda d: d.low_f <= 25),
    )
    for name, hit in hits:
        run: list[DayForecast] = []
        for d in [*days, None]:
            if d is not None and hit(d):
                run.append(d)
                continue
            if len(run) >= 2:
                out.append(f"{name} stretch {run[0].day:%a}–{run[-1].day:%a}")
            run = []
    return out


def next_fire(now: datetime, weekday: str = "sun", hhmm: str = "18:00") -> datetime:
    """Next local fire time strictly after now (DST-safe: built in local wall time)."""
    lt = now.astimezone(TZ)
    h, m = (int(x) for x in hhmm.split(":"))
    wd = WEEKDAYS.index(weekday)
    for off in range(8):
        d = lt.date() + timedelta(days=off)
        if d.weekday() == wd:
            cand = datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)
            if cand > lt:
                return cand
    raise AssertionError("unreachable")


def send_late(scheduled: datetime, now: datetime, max_late_h: float = 6) -> bool:
    """Missed 18:00 (container down): send on start-up only when < 6 h late."""
    return scheduled <= now < scheduled + timedelta(hours=max_late_h)


def render(
    last: LastWeek, days: list[DayForecast] | None, heat: Baseline | None, cool: Baseline | None
) -> str:
    """Last-week section always; next-7-days section or 'forecast unavailable'."""
    lines = [
        "Last week",
        f"  heat {last.heat_h:.1f} h, cool {last.cool_h:.1f} h "
        f"(effort {last.cool_effort_h:.1f} full-speed h), fan-only {last.fan_only_h:.1f} h",
    ]
    sv = last.savings
    if sv is None:
        lines.append("  vs baseline: baseline low confidence")
    else:
        flag = "" if sv.counted else f" (not counted: {sv.note})"
        lines.append(f"  vs baseline: saved {sv.saved / 60:.1f} h (±{sv.pi90 / 60:.1f}){flag}")
    lines.append(f"  comfort {last.comfort_pct:.0f}%, violation {last.violation_min:.0f} min")
    ov = ", ".join(f"{u} {n}" for u, n in sorted(last.overrides_by_user.items())) or "none"
    lines.append(f"  overrides: {ov}; stale sensors {last.stale_sensor_h:.0f} h")
    lines.append(f"  safety restores {last.safety_restores}, watchdog {last.watchdog_events}")
    lines.append("Next 7 days")
    if not days:
        lines.append("  forecast unavailable")
        return "\n".join(lines)
    for o in outlook(days, heat, cool):
        tag = " (lower confidence)" if o.lower_confidence else ""
        lines.append(
            f"  {o.day:%a} {o.high_f:.0f}/{o.low_f:.0f}°F {o.kind}, ~{o.expected_h:.1f} h{tag}"
        )
    lines += [f"  {s}" for s in stretches(days)]
    return "\n".join(lines)
