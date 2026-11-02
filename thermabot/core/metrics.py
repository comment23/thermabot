"""Runtime metrics, degree-days, baseline OLS, savings (D\x158.3-8.4). Pure; numpy only."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from pydantic import BaseModel

Z90 = 1.6448536269514722  # one-sided 95% normal quantile -> two-sided 90% interval


class MinuteSample(BaseModel):
    hvac_action: str | None = None  # heating/cooling/fan/idle/off
    blower_active: bool = False
    cs: float | None = None  # compressor speed, already normalised to 0-1


class DayRuntime(BaseModel):
    heat_min: float = 0
    cool_min: float = 0
    cool_effort: float = 0
    heat_effort: float = 0
    fan_only_min: float = 0
    blower_min: float = 0
    minutes: int = 0


def normalise_cs(raw: float | None, max_value: float = 100.0) -> float | None:
    """Raw compressor speed (e.g. % or stage) -> 0-1 fraction, clipped."""
    if raw is None or max_value <= 0:
        return None
    return min(1.0, max(0.0, raw / max_value))


def runtime(samples: Sequence[MinuteSample]) -> DayRuntime:
    """One 1-minute sample per minute (D\x158.3 table)."""
    r = DayRuntime(minutes=len(samples))
    for s in samples:
        heating = s.hvac_action == "heating"
        if heating:
            r.heat_min += 1
        if s.cs is not None:
            if s.cs > 0:
                r.cool_min += 1
                r.cool_effort += s.cs
        elif s.hvac_action == "cooling":
            r.cool_min += 1
            r.cool_effort += 1.0  # no speed signal: count as full speed (conservative)
        if s.blower_active:
            r.blower_min += 1
            if not heating and not (s.cs or 0) > 0 and s.hvac_action != "cooling":
                r.fan_only_min += 1
    r.heat_effort = r.heat_min
    return r


def degree_days(daily_mean_f: float, base_f: float = 65.0) -> tuple[float, float]:
    """(HDD, CDD) from a daily mean outdoor temperature."""
    return max(0.0, base_f - daily_mean_f), max(0.0, daily_mean_f - base_f)


class Baseline(BaseModel):
    beta: float
    alpha: float
    n: int
    resid_sd: float
    x_mean: float
    sxx: float
    r2: float


def fit_baseline(x: Sequence[float], y: Sequence[float]) -> Baseline:
    """OLS y = beta*x + alpha (e.g. heat_min/day on HDD). Needs >= 3 days."""
    n = len(x)
    if n < 3 or n != len(y):
        raise ValueError("baseline needs >= 3 paired days")
    xa, ya = np.asarray(x, float), np.asarray(y, float)
    a = np.column_stack([xa, np.ones(n)])
    (beta, alpha), *_ = np.linalg.lstsq(a, ya, rcond=None)
    resid = ya - (beta * xa + alpha)
    ss_res = float(resid @ resid)
    ss_tot = float(((ya - ya.mean()) ** 2).sum())
    return Baseline(
        beta=float(beta),
        alpha=float(alpha),
        n=n,
        resid_sd=math.sqrt(ss_res / (n - 2)) if n > 2 else 0.0,
        x_mean=float(xa.mean()),
        sxx=float(((xa - xa.mean()) ** 2).sum()),
        r2=1 - ss_res / ss_tot if ss_tot > 0 else 0.0,
    )


def t90(df: int) -> float:
    """Two-sided 90% Student-t quantile (Cornish-Fisher expansion; < 0.5% error, df >= 3)."""
    z = Z90
    g1 = (z**3 + z) / 4
    g2 = (5 * z**5 + 16 * z**3 + 3 * z) / 96
    g3 = (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / 384
    return z + g1 / df + g2 / df**2 + g3 / df**3


class Savings(BaseModel):
    baseline: float
    actual: float
    saved: float
    pi90: float  # +/- half-width of the 90% prediction interval
    pct: float | None
    counted: bool = True
    note: str = ""


def savings(b: Baseline, x: Sequence[float], actual: Sequence[float]) -> Savings:
    """Savings = sum(baseline prediction) - sum(actual) over the period's days. Interval:
    t(n-2) x sqrt(sum of per-day prediction variances) (independent-day residuals)."""
    if len(x) != len(actual):
        raise ValueError("x and actual differ in length")
    pred = sum(b.beta * xi + b.alpha for xi in x)
    act = float(sum(actual))
    var = 0.0
    for xi in x:
        lev = 1 / b.n + ((xi - b.x_mean) ** 2 / b.sxx if b.sxx > 0 else 0.0)
        var += b.resid_sd**2 * (1 + lev)
    half = t90(b.n - 2) * math.sqrt(var) if b.n > 2 else math.inf
    return Savings(
        baseline=pred,
        actual=act,
        saved=pred - act,
        pi90=half,
        pct=(pred - act) / pred * 100 if pred > 0 else None,
    )


def best_base(
    daily_mean_f: Sequence[float],
    y: Sequence[float],
    heating: bool = True,
    lo: int = 55,
    hi: int = 68,
) -> tuple[float, float]:
    """Grid search over 55-68°F for the degree-day base maximizing baseline R². (base, r2)."""
    best = (65.0, -math.inf)
    for base in range(lo, hi + 1):
        x = [degree_days(m, base)[0 if heating else 1] for m in daily_mean_f]
        try:
            r2 = fit_baseline(x, y).r2
        except ValueError:
            continue
        if r2 > best[1]:
            best = (float(base), r2)
    return best


def usable_days(hours_of_data: Sequence[float], min_hours: float = 20) -> list[bool]:
    return [h >= min_hours for h in hours_of_data]


def comfort_guard(
    s: Savings,
    comfort_pct: float,
    baseline_comfort_pct: float,
    violation_min_per_day: float,
    baseline_violation_min_per_day: float,
) -> Savings:
    """Savings 'not counted' when comfort% fell > 2 points or violation minutes/day rose."""
    if comfort_pct < baseline_comfort_pct - 2:
        return s.model_copy(update={"counted": False, "note": "comfort fell > 2 points"})
    if violation_min_per_day > baseline_violation_min_per_day:
        return s.model_copy(update={"counted": False, "note": "violation minutes increased"})
    return s


def heating_baseline_confident(hdd: Sequence[float], min_days: int = 15) -> bool:
    """P4 shows 'low confidence' until >= 15 days with HDD >= 5 exist."""
    return sum(1 for h in hdd if h >= 5) >= min_days
