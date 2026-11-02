"""First-order thermal model (D\x154.1, FR-PLN-3). Pure: numpy only, no I/O."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Literal

import numpy as np
from pydantic import BaseModel

from thermabot.config import PlannerCfg

U = Literal["heat", "cool", "fan", "idle"]
DT_H = 0.25  # 15-minute step
COOL_REF_F = 75.0

# Physical ranges (D\x154.1). A fit outside these is rejected; the previous model stays.
RANGES: dict[str, tuple[float, float]] = {
    "a": (0.01, 0.5),
    "s": (0.0, 5.0),
    "q_h": (0.5, 10.0),
    "q_c0": (0.5, 10.0),
    "q_c1": (-0.5, 0.5),
}


class ThermalParams(BaseModel):
    a: float = 0.1
    s: float = 0.5
    q_h: float = 3.0
    q_c0: float = 3.0
    q_c1: float = 0.05
    sigma: float = 1.0  # mean compressor speed fraction while cooling (D\x158.3)


class Sample(BaseModel):
    """One 15-minute step: T at k, T at k+1, outdoor, local hour, equipment state."""

    t: float
    t_next: float
    t_out: float
    hour: float
    u: U
    sigma: float = 1.0


class FitResult(BaseModel):
    params: ThermalParams
    accepted: bool
    note: str = ""


def solar(hour: float) -> float:
    """S(h) = max(0, sin(pi*(h-7)/12)) for 7 <= h <= 19, else 0."""
    if 7 <= hour <= 19:
        return max(0.0, math.sin(math.pi * (hour - 7) / 12))
    return 0.0


def q_u(u: U, t_out: float, p: ThermalParams, sigma: float | None = None) -> float:
    if u == "heat":
        return p.q_h
    if u == "cool":
        sg = p.sigma if sigma is None else sigma
        return -(p.q_c0 + p.q_c1 * (t_out - COOL_REF_F)) * sg
    return 0.0


def step(t: float, t_out: float, hour: float, u: U, p: ThermalParams) -> float:
    return t + DT_H * (p.a * (t_out - t) + p.s * solar(hour) + q_u(u, t_out, p))


def simulate(
    t0: float, t_out: Sequence[float], hours: Sequence[float], us: Sequence[U], p: ThermalParams
) -> list[float]:
    """Returns T[0..n] (n+1 values) for n steps."""
    out = [t0]
    for to, h, u in zip(t_out, hours, us, strict=True):
        out.append(step(out[-1], to, h, u, p))
    return out


def in_range(p: ThermalParams) -> bool:
    return all(lo <= getattr(p, k) <= hi for k, (lo, hi) in RANGES.items())


def fit(samples: Sequence[Sample], prev: ThermalParams | None = None) -> FitResult:
    """OLS (numpy lstsq). Heat+idle rows: dT/dt = a(To-T) + sS + q_h*[heat] -> a, s, q_h.
    If no `heat` rows exist yet (e.g. no heating season data), `a`/`s` (building envelope
    loss, solar gain -- properties of the house, not the heating equipment) still fit from
    `idle` rows alone via a reduced 2-column regression; q_h degrades to `prev.q_h`, same
    pattern already used below for the cooling-side params when `cool` data is sparse.
    Cool rows on the residual: -(q_c0 + q_c1(To-75))*sigma -> q_c0, q_c1.
    Out-of-range parameters reject the whole fit; the previous model stays."""
    prev = prev or ThermalParams()
    hi_rows = [x for x in samples if x.u in ("heat", "idle")]
    cool = [x for x in samples if x.u == "cool"]
    if len(hi_rows) < 3:
        return FitResult(params=prev, accepted=False, note="not enough heat/idle rows")
    has_heat = any(x.u == "heat" for x in hi_rows)
    if has_heat:
        x1 = np.array(
            [[x.t_out - x.t, solar(x.hour), 1.0 if x.u == "heat" else 0.0] for x in hi_rows]
        )
        y1 = np.array([(x.t_next - x.t) / DT_H for x in hi_rows])
        (a, s, q_h), *_ = np.linalg.lstsq(x1, y1, rcond=None)
    else:
        x1 = np.array([[x.t_out - x.t, solar(x.hour)] for x in hi_rows])
        y1 = np.array([(x.t_next - x.t) / DT_H for x in hi_rows])
        (a, s), *_ = np.linalg.lstsq(x1, y1, rcond=None)
        q_h = prev.q_h
    q_c0, q_c1, sigma = prev.q_c0, prev.q_c1, prev.sigma
    if len(cool) >= 2:
        x2 = np.array([[x.sigma, x.sigma * (x.t_out - COOL_REF_F)] for x in cool])
        y2 = np.array(
            [-((x.t_next - x.t) / DT_H - a * (x.t_out - x.t) - s * solar(x.hour)) for x in cool]
        )
        (q_c0, q_c1), *_ = np.linalg.lstsq(x2, y2, rcond=None)
        sigma = float(np.mean([x.sigma for x in cool]))
    p = ThermalParams(
        a=float(a), s=float(s), q_h=float(q_h), q_c0=float(q_c0), q_c1=float(q_c1), sigma=sigma
    )
    if not in_range(p):
        return FitResult(params=prev, accepted=False, note=f"out of range: {p.model_dump()}")
    return FitResult(params=p, accepted=True)


def mae(pred: Sequence[float], actual: Sequence[float]) -> float:
    errs = [abs(p - a) for p, a in zip(pred, actual, strict=True)]
    return sum(errs) / len(errs) if errs else math.inf


def holdout_mae(samples: Sequence[Sample], p: ThermalParams, horizon_steps: int = 16) -> float:
    """4-hour-ahead rolling simulation MAE (D\x154.1) over consecutive samples."""
    errs: list[float] = []
    for i in range(0, max(0, len(samples) - horizon_steps + 1)):
        win = samples[i : i + horizon_steps]
        t = win[0].t
        for x in win:
            t = step(t, x.t_out, x.hour, x.u, p)
        errs.append(abs(t - win[-1].t_next))
    return sum(errs) / len(errs) if errs else math.inf


def confidence(
    tstat_mae: float, room_mae: Mapping[str, float], cfg: PlannerCfg | None = None
) -> Literal["ok", "low"]:
    """ok when thermostat MAE <= 1.0 and every upstairs room MAE <= 2.0 (configurable)."""
    cfg = cfg or PlannerCfg()
    if tstat_mae <= cfg.mae_ok_tstat_f and all(v <= cfg.mae_ok_room_f for v in room_mae.values()):
        return "ok"
    return "low"


def room_offset(t: float, t_out: float, d: float, e: float) -> float:
    """T_r = T + d + e*(To - T) (D\x154.1 upstairs offset model)."""
    return t + d + e * (t_out - t)
