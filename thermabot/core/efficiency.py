"""v0.5.0 efficiency curve and savings over time. Pure; numpy only.

Per day: runtime per degree-day (heat_min / HDD, cool_min / CDD, base 65 °F, degree-days
integrated over hourly mean outdoor temperatures) against the day's mean outdoor
temperature. A robust (Huber IRLS) linear fit, or quadratic when it clearly fits better,
with a 95% confidence band for the fitted curve. An efficiency index tracks the rolling
7-day median of runtime per degree-day against the first 7 qualifying days (100 = same,
lower = less runtime per degree-day). See docs/efficiency.md.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

Season = Literal["heat", "cool"]

BASE_F = 65.0  # degree-day base
MIN_DD = 2.0  # a qualifying day has more than this many degree-days ...
MIN_COVERAGE_PCT = 80.0  # ... and at least this share of its minutes sampled
MIN_DAYS = 5  # qualifying days before a curve is fitted
INDEX_DAYS = 7  # baseline window (first N qualifying days) and rolling window (days)
HUBER_K = 1.345
QUAD_MIN_N = 10  # a quadratic is only considered with this many points ...
QUAD_GAIN = 0.02  # ... and only kept when it raises adjusted R² by this much
BAND_STEPS = 25
MAX_IRLS = 50

# Two-sided 95% Student-t quantiles for df = 1..30.
_T95 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip
_Z975 = 1.959963984540054


def t95(df: int) -> float:
    """Two-sided 95% Student-t quantile (table to df 30, Cornish-Fisher beyond)."""
    if df < 1:
        return math.inf
    if df <= len(_T95):
        return _T95[df - 1]
    z = _Z975
    g1 = (z**3 + z) / 4
    g2 = (5 * z**5 + 16 * z**3 + 3 * z) / 96
    g3 = (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / 384
    return z + g1 / df + g2 / df**2 + g3 / df**3


def _ok(*v: float | None) -> bool:
    return all(x is not None and math.isfinite(x) for x in v)


def hourly_degree_days(
    hourly_f: Sequence[float | None], base_f: float = BASE_F
) -> tuple[float, float, float] | None:
    """(mean °F, HDD, CDD) from a day's hourly mean outdoor temperatures. Degree-days are
    integrated hour by hour (mean of max(0, base - T)), so a day that swings across the base
    counts both heating and cooling degree-days. None when there is no usable hour."""
    xs = [float(t) for t in hourly_f if t is not None and math.isfinite(t)]
    if not xs:
        return None
    n = len(xs)
    return (
        sum(xs) / n,
        sum(max(0.0, base_f - t) for t in xs) / n,
        sum(max(0.0, t - base_f) for t in xs) / n,
    )


class EffDay(BaseModel):
    """One `efficiency_daily` row."""

    day: str  # local date, ISO
    mean_out_f: float | None = None
    hdd: float | None = None
    cdd: float | None = None
    heat_min: float | None = None
    cool_min: float | None = None
    coverage_pct: float = 0.0


class Point(BaseModel):
    day: str
    x: float  # mean outdoor °F
    dd: float  # HDD or CDD
    runtime_min: float
    y: float  # runtime minutes per degree-day


def points(
    days: Sequence[EffDay],
    season: Season,
    min_dd: float = MIN_DD,
    min_coverage_pct: float = MIN_COVERAGE_PCT,
) -> list[Point]:
    """Qualifying days for a season, oldest first: degree-days > min_dd, coverage >=
    min_coverage_pct, all inputs finite. Duplicate days keep the last row."""
    by_day = {d.day: d for d in days}
    out: list[Point] = []
    for day in sorted(by_day):
        d = by_day[day]
        dd = d.hdd if season == "heat" else d.cdd
        rt = d.heat_min if season == "heat" else d.cool_min
        if not _ok(dd, rt, d.mean_out_f, d.coverage_pct):
            continue
        assert dd is not None and rt is not None and d.mean_out_f is not None
        if dd <= min_dd or d.coverage_pct < min_coverage_pct or rt < 0:
            continue
        out.append(Point(day=day, x=d.mean_out_f, dd=dd, runtime_min=rt, y=rt / dd))
    return out


class BandPoint(BaseModel):
    x: float
    y: float
    lo: float
    hi: float


class Fit(BaseModel):
    kind: Literal["linear", "quadratic"]
    coef: list[float]  # y = coef[0] + coef[1]*x (+ coef[2]*x²), x in °F
    n: int
    df: int
    r2: float
    resid_sd: float
    x_min: float
    x_max: float
    downweighted: int  # points the robust fit gave less than half weight
    band: list[BandPoint]


def predict(f: Fit, x: float) -> float:
    return float(sum(c * x**i for i, c in enumerate(f.coef)))


class _Res(BaseModel):
    beta: list[float]
    w: list[float]
    r2: float
    adj_r2: float
    s2: float
    cov: list[list[float]]


def _design(x: np.ndarray, deg: int, x0: float) -> np.ndarray:
    t = x - x0
    return np.column_stack([t**k for k in range(deg + 1)])


def _irls(x: np.ndarray, y: np.ndarray, deg: int, x0: float) -> _Res:
    """Huber M-estimate by iteratively reweighted least squares (MAD scale)."""
    a = _design(x, deg, x0)
    n, p = a.shape
    w = np.ones(n)
    floor = 1e-9 * (1.0 + float(np.mean(np.abs(y))))
    beta = np.zeros(p)
    for _ in range(MAX_IRLS):
        sw = np.sqrt(w)
        beta = np.linalg.lstsq(a * sw[:, None], y * sw, rcond=None)[0]
        r = y - a @ beta
        s = max(1.4826 * float(np.median(np.abs(r - np.median(r)))), floor)
        u = np.abs(r) / (HUBER_K * s)
        w_new = np.where(u <= 1.0, 1.0, 1.0 / np.maximum(u, 1e-12))
        done = float(np.max(np.abs(w_new - w))) < 1e-8
        w = w_new
        if done:
            break
    sw = np.sqrt(w)
    beta = np.linalg.lstsq(a * sw[:, None], y * sw, rcond=None)[0]
    r = y - a @ beta
    ss_res = float(r @ r)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    if ss_tot > 0:
        r2 = 1.0 - ss_res / ss_tot
    else:
        r2 = 1.0 if ss_res <= 1e-12 else 0.0
    dof = n - p
    adj = 1.0 - (1.0 - r2) * (n - 1) / dof if dof > 0 else r2
    s2 = float((w * r * r).sum()) / dof if dof > 0 else 0.0
    cov = s2 * np.linalg.pinv(a.T @ (a * w[:, None]))
    return _Res(
        beta=[float(b) for b in beta],
        w=[float(v) for v in w],
        r2=r2,
        adj_r2=adj,
        s2=s2,
        cov=[[float(v) for v in row] for row in cov],
    )


def _raw_coef(beta: Sequence[float], x0: float) -> list[float]:
    """Coefficients in t = x - x0 -> coefficients in x."""
    c = list(beta) + [0.0] * (3 - len(beta))
    raw = [c[0] - c[1] * x0 + c[2] * x0 * x0, c[1] - 2 * c[2] * x0, c[2]]
    return raw[: len(beta)]


def fit_curve(pts: Sequence[Point]) -> Fit | None:
    """Robust fit of runtime per degree-day vs mean outdoor °F. None with < 3 finite points
    or when every point has the same outdoor temperature (no slope to fit)."""
    xy = [(p.x, p.y) for p in pts if _ok(p.x, p.y)]
    if len(xy) < 3:
        return None
    x = np.array([v[0] for v in xy], float)
    y = np.array([v[1] for v in xy], float)
    if float(np.ptp(x)) < 1e-9:
        return None
    x0 = float(x.mean())
    res, deg = _irls(x, y, 1, x0), 1
    if len(xy) >= QUAD_MIN_N:
        q = _irls(x, y, 2, x0)
        if q.adj_r2 >= res.adj_r2 + QUAD_GAIN:
            res, deg = q, 2
    n, p = len(xy), deg + 1
    cov = np.array(res.cov)
    beta = np.array(res.beta)
    grid = np.linspace(float(x.min()), float(x.max()), BAND_STEPS)
    a0 = _design(grid, deg, x0)
    yhat = a0 @ beta
    se = np.sqrt(np.maximum(np.einsum("ij,jk,ik->i", a0, cov, a0), 0.0))
    half = t95(n - p) * se
    band = [
        BandPoint(x=round(float(g), 3), y=float(v), lo=float(v - h), hi=float(v + h))
        for g, v, h in zip(grid, yhat, half, strict=True)
    ]
    return Fit(
        kind="quadratic" if deg == 2 else "linear",
        coef=_raw_coef(res.beta, x0),
        n=n,
        df=n - p,
        r2=round(res.r2, 4),
        resid_sd=round(math.sqrt(res.s2), 4),
        x_min=float(x.min()),
        x_max=float(x.max()),
        downweighted=sum(1 for v in res.w if v < 0.5),
        band=band,
    )


class IndexPoint(BaseModel):
    day: str
    value: float


class Index(BaseModel):
    baseline: float  # median runtime per degree-day over the first INDEX_DAYS qualifying days
    latest: float
    series: list[IndexPoint]


def efficiency_index(pts: Sequence[Point]) -> Index | None:
    """100 x rolling median (qualifying days in the last INDEX_DAYS calendar days) / the
    median of the first INDEX_DAYS qualifying days. None until INDEX_DAYS qualifying days
    exist or when the baseline is 0 (no runtime)."""
    ps = sorted((p for p in pts if _ok(p.y)), key=lambda p: p.day)
    if len(ps) < INDEX_DAYS:
        return None
    base = statistics.median(p.y for p in ps[:INDEX_DAYS])
    if base <= 0:
        return None
    series: list[IndexPoint] = []
    for i in range(INDEX_DAYS - 1, len(ps)):
        d = date.fromisoformat(ps[i].day)
        lo = (d - timedelta(days=INDEX_DAYS)).isoformat()
        win = [p.y for p in ps[: i + 1] if p.day > lo]
        series.append(
            IndexPoint(day=ps[i].day, value=round(100 * statistics.median(win) / base, 1))
        )
    return Index(baseline=round(base, 3), latest=series[-1].value, series=series)


class SeasonCurve(BaseModel):
    season: Season
    n: int
    need: int = MIN_DAYS
    progress: str
    ready: bool
    points: list[Point]
    fit: Fit | None = None
    index: Index | None = None
    empty: str | None = None  # empty-state text when there is no curve


def season_curve(days: Sequence[EffDay], season: Season) -> SeasonCurve:
    pts = points(days, season)
    n = len(pts)
    ready = n >= MIN_DAYS
    fit = fit_curve(pts) if ready else None
    empty = None
    if not ready:
        empty = f"needs >= {MIN_DAYS} qualifying days ({n}/{MIN_DAYS})"
    elif fit is None:
        empty = "no curve: every qualifying day had the same outdoor temperature"
    return SeasonCurve(
        season=season,
        n=n,
        progress=f"{min(n, MIN_DAYS)}/{MIN_DAYS}",
        ready=ready,
        points=pts,
        fit=fit,
        index=efficiency_index(pts),
        empty=empty,
    )


# ---- savings over time (shadow_daily) ----------------------------------------------------
class ShadowIn(BaseModel):
    day: str
    actual_min: float | None = None  # measured heat + cool runtime
    saved_min: float | None = None  # simulated (bot vs actual setpoints)


class SavingsDay(BaseModel):
    day: str
    actual_min: float | None
    bot_min: float | None
    saved_min: float | None
    saved_pct: float | None
    cum_saved_min: float
    cum_pct: float | None


class SavingsSeries(BaseModel):
    simulated: bool = True
    label: str = (
        "Simulated: actual runtime is measured, the bot's runtime is a thermal-model"
        " simulation of its dry-run plan (SHADOW), not a measurement."
    )
    days: list[SavingsDay] = Field(default_factory=list)
    n_estimated: int = 0
    total_saved_min: float = 0.0
    total_pct: float | None = None


def savings_series(rows: Sequence[ShadowIn]) -> SavingsSeries:
    """Per-day and cumulative saved runtime, oldest first. Days without a bot estimate are
    listed with None and leave the cumulative values unchanged."""
    out = SavingsSeries()
    cum_saved = cum_actual = 0.0
    for r in sorted({x.day: x for x in rows}.values(), key=lambda x: x.day):
        a = r.actual_min if _ok(r.actual_min) else None
        s = r.saved_min if _ok(r.saved_min) else None
        pct = bot = None
        if a is not None and s is not None:
            cum_saved += s
            cum_actual += a
            out.n_estimated += 1
            bot = a - s
            pct = 100 * s / a if a > 0 else None
        out.days.append(
            SavingsDay(
                day=r.day,
                actual_min=a,
                bot_min=bot,
                saved_min=s if a is not None else None,
                saved_pct=None if pct is None else round(pct, 1),
                cum_saved_min=round(cum_saved, 1),
                cum_pct=round(100 * cum_saved / cum_actual, 1) if cum_actual > 0 else None,
            )
        )
    out.total_saved_min = round(cum_saved, 1)
    out.total_pct = round(100 * cum_saved / cum_actual, 1) if cum_actual > 0 else None
    return out


class Tile(BaseModel):
    text: str
    season: Season | None = None
    index: float | None = None


def tile(heat: SeasonCurve, cool: SeasonCurve) -> Tile:
    """Dashboard tile: the latest index of the season with the most recent qualifying day;
    otherwise curve-ready or 'collecting data k/5'."""
    seasons = sorted(
        (c for c in (heat, cool) if c.points), key=lambda c: c.points[-1].day, reverse=True
    )
    for c in seasons:
        if c.index is not None:
            return Tile(
                text=f"index {c.index.latest:.0f} ({c.season})",
                season=c.season,
                index=c.index.latest,
            )
    for c in seasons:
        if c.ready:
            return Tile(
                text=f"curve ready ({c.n} {c.season} days), index from day {INDEX_DAYS}",
                season=c.season,
            )
    k = max(heat.n, cool.n)
    return Tile(text=f"collecting data {min(k, MIN_DAYS)}/{MIN_DAYS}")


class Efficiency(BaseModel):
    base_f: float = BASE_F
    min_dd: float = MIN_DD
    min_coverage_pct: float = MIN_COVERAGE_PCT
    min_days: int = MIN_DAYS
    days: int
    heat: SeasonCurve
    cool: SeasonCurve
    savings: SavingsSeries
    tile: Tile


def summarize(days: Sequence[EffDay], shadow: Sequence[ShadowIn]) -> Efficiency:
    heat, cool = season_curve(days, "heat"), season_curve(days, "cool")
    return Efficiency(
        days=len({d.day for d in days}),
        heat=heat,
        cool=cool,
        savings=savings_series(shadow),
        tile=tile(heat, cool),
    )
