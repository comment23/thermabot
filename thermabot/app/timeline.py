"""Server-rendered 24-hour plan timeline. One time axis: prediction, setpoints, runs, blocks."""

from __future__ import annotations

import html
from datetime import datetime, timedelta

from thermabot.app.present import action_sentence
from thermabot.app.units import Units, format_temp
from thermabot.config import Settings
from thermabot.core.schedule import block_at
from thermabot.core.types import PlanStep


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def window(steps: list[PlanStep], now: datetime) -> list[PlanStep]:
    end = now + timedelta(hours=24)
    picked = [s for s in steps if now <= s.t < end]
    if picked:
        return picked
    return [s for s in steps if s.t >= now][: 24 * 4]


def summary(steps: list[PlanStep], units: Units) -> str:
    if not steps:
        return "No plan steps in the next 24 hours."
    changes = _changes(steps)
    bits = [f"{len(steps)} steps"]
    for n, step in enumerate(changes, start=1):
        bits.append(f"{n} {action_sentence(step, units)} at {step.t:%H:%M}")
    return "Next 24 hours: " + "; ".join(bits)


def _changes(steps: list[PlanStep]) -> list[PlanStep]:
    out: list[PlanStep] = []
    prev: tuple[object, ...] | None = None
    for step in steps:
        key = (step.hvac_mode, step.heat_sp, step.cool_sp, step.fan_mode)
        if key != prev:
            out.append(step)
        prev = key
    return out


def svg(steps: list[PlanStep], settings: Settings, units: Units) -> str:
    """A single SVG. Empty when there is nothing to draw."""
    if len(steps) < 1:
        return ""
    width, height = 720, 168
    pad_l, pad_r = 16, 16
    inner_w = width - pad_l - pad_r
    n = len(steps)
    temps = [s.pred_t for s in steps if s.pred_t is not None]
    sps = [s.heat_sp for s in steps if s.heat_sp is not None] + [
        s.cool_sp for s in steps if s.cool_sp is not None
    ]
    vals = temps + sps
    lo = min(vals) if vals else 60
    hi = max(vals) if vals else 80
    if hi - lo < 1:
        lo, hi = lo - 1, hi + 1
    plot_top, plot_h = 36, 96

    def x_at(i: int) -> float:
        if n == 1:
            return pad_l + inner_w / 2
        return pad_l + inner_w * i / (n - 1)

    def y_at(v: float) -> float:
        return plot_top + plot_h * (1 - (v - lo) / (hi - lo))

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" class="timeline-svg" role="img" '
        f'aria-label="{_esc(summary(steps, units))}">'
    ]
    # schedule blocks along the top
    i = 0
    while i < n:
        block = block_at(settings.schedule, steps[i].t)
        j = i + 1
        while j < n and block_at(settings.schedule, steps[j].t) == block:
            j += 1
        x0 = x_at(i)
        x1 = x_at(j - 1) + (inner_w / max(n - 1, 1) if n > 1 else 24)
        parts.append(
            f'<rect class="tl-block tl-{_esc(block)}" x="{x0:.1f}" y="8" '
            f'width="{max(x1 - x0, 8):.1f}" height="16" rx="4">'
            f"<title>{_esc(block)}</title></rect>"
        )
        i = j
    # heating / cooling runs
    i = 0
    while i < n:
        mode = steps[i].hvac_mode
        j = i + 1
        while j < n and steps[j].hvac_mode == mode:
            j += 1
        if mode in ("heat", "cool"):
            x0 = x_at(i)
            x1 = x_at(min(j, n) - 1)
            parts.append(
                f'<rect class="tl-run tl-{mode}" x="{x0:.1f}" y="{plot_top + plot_h + 6}" '
                f'width="{max(x1 - x0, 6):.1f}" height="10" rx="2">'
                f"<title>{_esc(mode)}</title></rect>"
            )
        i = j
    # setpoint step line (heat)
    heat_pts: list[str] = []
    for i, step in enumerate(steps):
        if step.heat_sp is None:
            continue
        heat_pts.append(f"{x_at(i):.1f},{y_at(step.heat_sp):.1f}")
    if heat_pts:
        parts.append(f'<polyline class="tl-heat" points="{" ".join(heat_pts)}" />')
    cool_pts: list[str] = []
    for i, step in enumerate(steps):
        if step.cool_sp is None:
            continue
        cool_pts.append(f"{x_at(i):.1f},{y_at(step.cool_sp):.1f}")
    if cool_pts:
        parts.append(f'<polyline class="tl-cool" points="{" ".join(cool_pts)}" />')
    pred = [
        f"{x_at(i):.1f},{y_at(step.pred_t):.1f}"
        for i, step in enumerate(steps)
        if step.pred_t is not None
    ]
    if pred:
        parts.append(f'<polyline class="tl-pred" points="{" ".join(pred)}" />')
    for n_i, step in enumerate(_changes(steps), start=1):
        idx = steps.index(step)
        y = y_at(step.pred_t if step.pred_t is not None else (step.heat_sp or lo))
        label = action_sentence(step, units)
        parts.append(
            f'<g class="tl-mark"><circle cx="{x_at(idx):.1f}" cy="{y:.1f}" r="8" />'
            f'<text x="{x_at(idx):.1f}" y="{y + 3:.1f}">{n_i}</text>'
            f"<title>{_esc(label)}</title></g>"
        )
    parts.append("</svg>")
    return "".join(parts)


def table_rows(steps: list[PlanStep], units: Units) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for n_i, step in enumerate(_changes(steps), start=1):
        temp = format_temp(step.pred_t, units) if step.pred_t is not None else "—"
        text = f"{action_sentence(step, units)} · {temp}"
        rows.append((str(n_i), step.t.strftime("%H:%M"), text))
    return rows
