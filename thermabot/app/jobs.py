"""Background jobs wired into Loops (D§1.3, D§4, D§8): replan, refit, rollup, Influx flush.

Every job is isolated: Loops logs and swallows exceptions and bounds each job's run time
(loops.JOB_TIMEOUT_S), so none of them can stop the loop or hold the heartbeat for long.
Live control never depends on Influx or the forecast: failures degrade to the fallback plan
(replan), keep the previous model (refit), skip the day (rollup) or keep buffering (flush).
This module holds the I/O-side helpers; the jobs themselves are Runtime methods (main.py).
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from thermabot.adapters.influx import Influx, line
from thermabot.config import PlannerCfg, Settings
from thermabot.core import openings as opening_policy
from thermabot.core import planner
from thermabot.core.comfort import room_age_min
from thermabot.core.metrics import MinuteSample, normalise_cs
from thermabot.core.safety import OffWindow
from thermabot.core.schedule import TZ, block_at
from thermabot.core.shadow import MinuteRow, Setpoints
from thermabot.core.thermal import Sample, ThermalParams, U, confidence, holdout_mae
from thermabot.core.types import Forecast, Plan, PlanStep
from thermabot.core.weather import forecast_at

log = logging.getLogger(__name__)

STEP = timedelta(minutes=15)
REFIT_DAYS = 28  # local `samples` (preferred) merged with Influx history
HOLDOUT_DAYS = 7
MIN_TRAIN_STEPS = 96  # one day of 15-min steps before a fit is attempted
SHORT_HOLDOUT_MIN = 16  # short history: hold out the last 20% (>= one 4 h MAE window)
LOCAL_MAX_AGE = timedelta(minutes=3)  # 1-min samples: a longer gap breaks the run
SAMPLE_RETENTION = timedelta(days=120)  # D§2: local 1-min samples; Influx keeps the rest
PLAN_RETENTION = timedelta(days=14)
MAX_AGE = timedelta(minutes=20)  # Influx data is 5-min resolution with up to 5-min lag

Value = float | str
Series = list[tuple[datetime, Value]]


# ---- Influx series helpers -------------------------------------------------------------
def rfc3339(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def fetch(
    influx: Influx,
    entity_id: str,
    fields: list[str],
    start: datetime,
    stop: datetime,
    every: str = "5m",
) -> dict[str, Series]:
    if not entity_id:  # optional sensor not configured (v0.3.0): nothing to read
        return {f: [] for f in fields}
    rows = await influx.read_entity(entity_id, fields, rfc3339(start), every, rfc3339(stop))
    out: dict[str, Series] = {f: [] for f in fields}
    for r in rows:
        if r["field"] in out:
            out[r["field"]].append((r["t"], r["value"]))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


def value_at(s: Series, t: datetime, max_age: timedelta = MAX_AGE) -> Value | None:
    """Last value at or before t, if not older than max_age."""
    i = bisect.bisect_right(s, t, key=lambda x: x[0])
    if i == 0:
        return None
    ts, v = s[i - 1]
    return v if t - ts <= max_age else None


def num(v: Value | None) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except ValueError:
        return {"on": 1.0, "off": 0.0, "true": 1.0, "false": 0.0}.get(str(v).lower())
    return f if math.isfinite(f) else None


def action_u(a: Value | None) -> U:
    m: dict[str, U] = {"heating": "heat", "cooling": "cool", "fan": "fan"}
    return m.get(str(a), "idle")


# ---- refit (D§4.1) -------------------------------------------------------------------
def build_samples(
    temp: Series,
    action: Series,
    outdoor: Series,
    cs: Series,
    start: datetime,
    end: datetime,
    max_age: timedelta = MAX_AGE,
) -> list[tuple[datetime, Sample]]:
    """15-min steps on a grid aligned to :00/:15/:30/:45. Steps missing any input are
    skipped (a gap breaks the run; `holdout` never spans one)."""
    t = start.astimezone(UTC).replace(second=0, microsecond=0)
    t -= timedelta(minutes=t.minute % 15)
    out: list[tuple[datetime, Sample]] = []
    while t + STEP <= end:
        t0, t1 = num(value_at(temp, t, max_age)), num(value_at(temp, t + STEP, max_age))
        to = num(value_at(outdoor, t, max_age))
        if t0 is not None and t1 is not None and to is not None:
            u = action_u(value_at(action, t, max_age))
            c = normalise_cs(num(value_at(cs, t, max_age)))
            lt = t.astimezone(TZ)
            sample = Sample(
                t=t0,
                t_next=t1,
                t_out=to,
                hour=lt.hour + lt.minute / 60,
                u=u,
                sigma=c if u == "cool" and c else 1.0,
            )
            out.append((t, sample))
        t += STEP
    return out


def override_spans(conn: sqlite3.Connection, start: datetime) -> list[tuple[datetime, datetime]]:
    rows = conn.execute(
        "SELECT started_at, COALESCE(ended_at, expires_at) e FROM overrides WHERE e >= ?",
        (start.astimezone(UTC).isoformat(),),
    ).fetchall()
    return [(datetime.fromisoformat(r[0]), datetime.fromisoformat(r[1])) for r in rows]


def outside(t: datetime, spans: Sequence[tuple[datetime, datetime]]) -> bool:
    """Step [t, t+15 min) does not overlap any override span (D§4.1 exclusion)."""
    return not any(a < t + STEP and t < b for a, b in spans)


def holdout(steps: Sequence[tuple[datetime, Sample]], p: ThermalParams, horizon: int = 16) -> float:
    """4 h-ahead MAE over contiguous runs only (a data gap never spans a window)."""
    runs: list[list[Sample]] = []
    prev: datetime | None = None
    for t, x in steps:
        if prev is None or t - prev != STEP:
            runs.append([])
        runs[-1].append(x)
        prev = t
    total, n = 0.0, 0
    for run in runs:
        k = len(run) - horizon + 1
        if k > 0:
            total += holdout_mae(run, p, horizon) * k
            n += k
    return total / n if n else math.inf


def _finite(x: float) -> float | None:
    return round(x, 4) if math.isfinite(x) else None


def active_model(
    conn: sqlite3.Connection, cfg: PlannerCfg | None = None
) -> tuple[ThermalParams | None, Literal["ok", "low"], int | None]:
    """(params, confidence, version) of the active model; no model -> (None, 'low', None)."""
    r = conn.execute(
        "SELECT version, params_json, mae_json FROM models WHERE active=1"
        " ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if r is None:
        return None, "low", None
    params = ThermalParams.model_validate_json(r["params_json"])
    mae = json.loads(r["mae_json"]).get("thermostat")
    conf = confidence(float(mae), {}, cfg) if isinstance(mae, int | float) else "low"
    return params, conf, int(r["version"])


def save_model(
    conn: sqlite3.Connection, params: ThermalParams, mae: float, n: int, now: datetime
) -> int:
    """New `models` row that becomes the only active one (atomic swap)."""
    v = int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM models").fetchone()[0]) + 1
    mae_json: dict[str, float | None] = {"thermostat": _finite(mae)}
    conn.execute("BEGIN")
    try:
        conn.execute("UPDATE models SET active=0 WHERE active=1")
        conn.execute(
            "INSERT INTO models (version, fitted_at, params_json, mae_json, n_samples, active)"
            " VALUES (?,?,?,?,?,1)",
            (v, now.astimezone(UTC).isoformat(), params.model_dump_json(), json.dumps(mae_json), n),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return v


# ---- replan (D§4.2) ------------------------------------------------------------------
def save_plan(
    conn: sqlite3.Connection, p: Plan, trigger: str, version: int | None, source: str | None
) -> None:
    scores = [s.score for s in p.steps if s.score is not None]
    conn.execute(
        "INSERT INTO plans (created_at, trigger, kind, model_version, forecast_source,"
        " steps_json, predicted_runtime_min, min_score) VALUES (?,?,?,?,?,?,?,?)",
        (
            p.created_at.astimezone(UTC).isoformat(),
            trigger,
            "optimized" if p.kind == "optimised" else "fallback",
            version,
            source,
            json.dumps([s.model_dump(mode="json") for s in p.steps]),
            planner.runtime_min(p),
            min(scores) if scores else None,
        ),
    )
    cut = (p.created_at - PLAN_RETENTION).astimezone(UTC).isoformat()
    conn.execute("DELETE FROM plans WHERE created_at < ?", (cut,))


def plan_lines(p: Plan) -> list[str]:
    out: list[str] = []
    for s in p.steps:
        f: dict[str, float | int | str | bool] = {"mode": s.hvac_mode, "fan": s.fan_mode}
        for k, v in (
            ("heat_sp", s.heat_sp),
            ("cool_sp", s.cool_sp),
            ("pred_t", s.pred_t),
            ("pred_u", s.pred_u),
            ("score", s.score),
        ):
            if v is not None:
                f[k] = float(v)
        out.append(line("thermabot_plan", {"kind": p.kind}, f, s.t))
    return out


def current_step(p: Plan | None, now: datetime) -> PlanStep | None:
    if p is None:
        return None
    due = [s for s in p.steps if s.t <= now]
    return due[-1] if due else None


def off_run(p: Plan, step: PlanStep) -> list[PlanStep]:
    """Contiguous `off` steps of p starting at `step`."""
    try:
        i = p.steps.index(step)
    except ValueError:
        return []
    out: list[PlanStep] = []
    for s in p.steps[i:]:
        if s.hvac_mode != "off":
            break
        out.append(s)
    return out


def off_guard_extra(
    p: Plan | None, step: PlanStep | None, fc: Forecast | None, target: float, used_h: float
) -> dict[str, Any]:
    """GuardCtx off-window inputs (NFR-SAFE-1/2) for Controller.tick(guard_extra=...).
    Only an optimised plan whose every step of the off window scores >= target, with a
    forecast covering the whole window, proves `off`; otherwise the guard keeps denying."""
    out: dict[str, Any] = {"off_used_h": used_h}
    if p is None or step is None or step.hvac_mode != "off" or p.kind != "optimised":
        return out
    win = off_run(p, step)
    out["off_score_proven"] = bool(win) and all(
        s.score is not None and s.score >= target for s in win
    )
    temps = [] if fc is None else [forecast_at(fc, s.t) for s in win]
    out["off_forecast_f"] = [t for t in temps if t is not None] if None not in temps else []
    return out


def load_off_windows(conn: sqlite3.Connection) -> list[OffWindow]:
    rows = conn.execute(
        "SELECT started_at, reevaluate_at, ended_at FROM off_windows ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return [
        OffWindow(
            start=datetime.fromisoformat(r[0]),
            reevaluate_at=datetime.fromisoformat(r[1]),
            end=None if r[2] is None else datetime.fromisoformat(r[2]),
        )
        for r in rows
    ]


def track_off_window(
    conn: sqlite3.Connection, hvac_mode: str | None, now: datetime, reevaluate_h: float
) -> None:
    """Open an `off_windows` row when the thermostat is `off` after a bot `off` write
    (D§4.2: rows appear when the first off write actually happens); close it when the
    system leaves `off`."""
    iso = now.astimezone(UTC).isoformat()
    open_row = conn.execute("SELECT id FROM off_windows WHERE ended_at IS NULL").fetchone()
    if hvac_mode == "off" and open_row is None:
        last = conn.execute(
            "SELECT data_json FROM writes WHERE outcome='sent' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last is not None and json.loads(last[0]).get("hvac_mode") == "off":
            conn.execute(
                "INSERT INTO off_windows (started_at, planned_end, reevaluate_at) VALUES (?,?,?)",
                (iso, iso, (now + timedelta(hours=reevaluate_h)).astimezone(UTC).isoformat()),
            )
    elif hvac_mode not in (None, "off") and open_row is not None:
        conn.execute(
            "UPDATE off_windows SET ended_at=?, end_reason='mode' WHERE id=?", (iso, open_row[0])
        )


# ---- rollup (D§8.3) ------------------------------------------------------------------
def local_day_bounds(d: date) -> tuple[datetime, datetime]:
    """[00:00, next 00:00) local, as aware datetimes (23/25 h on DST days)."""
    nxt = d + timedelta(days=1)
    return (
        datetime(d.year, d.month, d.day, tzinfo=TZ),
        datetime(nxt.year, nxt.month, nxt.day, tzinfo=TZ),
    )


def elapsed_minutes(start: datetime, end: datetime) -> int:
    """Use elapsed UTC time so DST days can be 23 or 25 hours."""
    return int((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() // 60)


def minute_samples(
    action: Series,
    blower: Series,
    cs: Series,
    start: datetime,
    end: datetime,
    cs_max: float = 100.0,
    max_age: timedelta = timedelta(minutes=10),
) -> list[MinuteSample]:
    """One sample per minute in [start, end) from 5-min Influx series (step-held). Minutes
    with no fresh signal at all are skipped, so `minutes` counts covered minutes."""
    out: list[MinuteSample] = []
    n = elapsed_minutes(start, end)
    t0 = start.astimezone(UTC)
    for m in range(n):
        t = t0 + timedelta(minutes=m)
        a, b, c = (value_at(x, t, max_age) for x in (action, blower, cs))
        if a is None and b is None and c is None:
            continue
        out.append(
            MinuteSample(
                hvac_action=None if a is None else str(a),
                blower_active=(num(b) or 0.0) > 0.5,
                cs=normalise_cs(num(c), cs_max),
            )
        )
    return out


def mean(vals: Sequence[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


# ---- local 1-minute samples (v0.1.9, FR-MET / D8) -------------------------------------
def extra_entities(s: Settings) -> list[str]:
    """Configured blower/compressor/outdoor(/humidity) ids in the legacy order; empty ones
    are skipped (not configured = not read)."""
    x = s.sensors
    return [e for e in (x.blower, x.compressor, x.outdoor, x.humidity) if e]


def _stamp(st: dict[str, Any], key: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(st.get(key)))
    except ValueError:
        return None
    return t if t.tzinfo else None


def _state_num(st: dict[str, Any] | None) -> float | None:
    return None if st is None else num(st.get("state"))


def sample_doc(
    now: datetime,
    s: Settings,
    th: Any,
    states: dict[str, dict[str, Any] | None],
    plan: Plan | None,
    step: PlanStep | None,
    forecast: Forecast | None,
    house: str | None,
) -> dict[str, Any]:
    """snapshot_json for one `samples` row. `th` is a ThermostatState (or None); `states` maps
    entity_id -> HA state dict (None = unread). Missing values stay None."""
    sn = s.sensors
    blower = states.get(sn.blower) if sn.blower else None
    cs_raw = _state_num(states.get(sn.compressor)) if sn.compressor else None
    humidity = None if th is None else th.humidity
    if sn.humidity:
        humidity = _state_num(states.get(sn.humidity))
    rooms: dict[str, dict[str, float | None]] = {}
    for key, cfg in s.rooms.items():
        if key == "thermostat":
            continue
        st = states.get(cfg.temp)
        if st is None:
            rooms[key] = {"temp": None, "age_min": None}
            continue
        age = room_age_min(now, _stamp(st, "last_reported"), _stamp(st, "last_updated"))
        rooms[key] = {"temp": _state_num(st), "age_min": None if age is None else round(age, 1)}
    opening_states = {eid: states.get(eid) for eid in s.openings.sensors}
    return {
        "tstat": None
        if th is None
        else {
            "temp": th.current_temp,
            "heat_sp": th.heat_sp,
            "cool_sp": th.cool_sp,
            "hvac_mode": th.hvac_mode,
            "hvac_action": th.hvac_action,
            "fan_mode": th.fan_mode,
            "humidity": humidity,
        },
        "blower": None if blower is None else num(blower.get("state")),
        "cs_raw": cs_raw,
        "cs": normalise_cs(cs_raw),
        "t_out": _state_num(states.get(sn.outdoor)) if sn.outdoor else None,
        "t_out_fc": None if forecast is None else forecast_at(forecast, now),
        "rooms": rooms,
        "opening_assessment": [
            finding.model_dump(mode="json")
            for finding in opening_policy.assess(s.openings, opening_states, now)
        ],
        "house": house,
        "block": block_at(s.schedule, now),
        "bot": None
        if step is None or plan is None
        else {
            "mode": step.hvac_mode,
            "heat_sp": step.heat_sp,
            "cool_sp": step.cool_sp,
            "fan": step.fan_mode,
            "kind": plan.kind,
        },
    }


def minute_key(now: datetime) -> str:
    return now.astimezone(UTC).replace(second=0, microsecond=0).isoformat()


def save_sample(
    conn: sqlite3.Connection,
    now: datetime,
    doc: dict[str, Any],
    bot_state: str,
    score: float | None,
    stale_rooms: list[str],
    effective_state: str | None = None,
    degraded_reason: str | None = None,
) -> None:
    """One row per minute (ts = minute, UTC). A re-run in the same minute replaces it."""
    snapshot = doc
    if effective_state is not None:
        snapshot = {
            **doc,
            "_thermabot": {"state": effective_state, "degraded_reason": degraded_reason},
        }
    conn.execute(
        "INSERT OR REPLACE INTO samples (ts, snapshot_json, bot_state, score, aux_heat_flag,"
        " stale_rooms) VALUES (?,?,?,?,?,?)",
        (
            minute_key(now),
            json.dumps(snapshot, default=str),
            bot_state,
            None if score is None else round(score, 2),
            None,
            ",".join(stale_rooms) or None,
        ),
    )


def prune_samples(conn: sqlite3.Connection, now: datetime) -> int:
    cut = (now - SAMPLE_RETENTION).astimezone(UTC).isoformat()
    return int(conn.execute("DELETE FROM samples WHERE ts < ?", (cut,)).rowcount)


def load_samples(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> list[tuple[datetime, dict[str, Any]]]:
    rows = conn.execute(
        "SELECT ts, snapshot_json FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts",
        (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
    ).fetchall()
    out: list[tuple[datetime, dict[str, Any]]] = []
    for r in rows:
        try:
            out.append((datetime.fromisoformat(r[0]), json.loads(r[1])))
        except (ValueError, TypeError):
            continue
    return out


def _g(d: Any, *path: str) -> Any:
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def sample_series(rows: Sequence[tuple[datetime, dict[str, Any]]]) -> dict[str, Series]:
    """Local samples as the same series shape the Influx path uses (for build_samples)."""
    out: dict[str, Series] = {"temp": [], "action": [], "outdoor": [], "cs": []}
    for t, d in rows:
        for k, v in (
            ("temp", _g(d, "tstat", "temp")),
            ("action", _g(d, "tstat", "hvac_action")),
            ("outdoor", d.get("t_out") if d.get("t_out") is not None else d.get("t_out_fc")),
            ("cs", d.get("cs_raw")),
        ):
            if v is not None:
                out[k].append((t, v))
    return out


def local_steps(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> list[tuple[datetime, Sample]]:
    """15-min thermal Samples from the local samples table (no Influx needed)."""
    s = sample_series(load_samples(conn, start, end))
    return build_samples(s["temp"], s["action"], s["outdoor"], s["cs"], start, end, LOCAL_MAX_AGE)


def merge_steps(
    influx: Sequence[tuple[datetime, Sample]], local: Sequence[tuple[datetime, Sample]]
) -> list[tuple[datetime, Sample]]:
    """Union on the 15-min grid; where both have a step, the local one wins."""
    by_t = {t: x for t, x in influx}
    by_t.update({t: x for t, x in local})
    return sorted(by_t.items(), key=lambda kv: kv[0])


def split(
    steps: Sequence[tuple[datetime, Sample]], now: datetime
) -> tuple[list[Sample], list[tuple[datetime, Sample]]]:
    """(train, holdout). Normal: holdout = the last HOLDOUT_DAYS. Short history (SHADOW's
    first days, train below MIN_TRAIN_STEPS): hold out the newest 20% (>= SHORT_HOLDOUT_MIN
    steps, one 4 h MAE window) so a model can be judged on unseen data after ~30 h."""
    cut = now - timedelta(days=HOLDOUT_DAYS)
    train = [x for t, x in steps if t < cut]
    if len(train) >= MIN_TRAIN_STEPS:
        return train, [(t, x) for t, x in steps if t >= cut]
    n_test = max(SHORT_HOLDOUT_MIN, len(steps) // 5)
    k = max(0, len(steps) - n_test)
    return [x for _, x in steps[:k]], list(steps[k:])


def minute_rows(rows: Sequence[tuple[datetime, dict[str, Any]]], s: Settings) -> list[MinuteRow]:
    """Decode samples into core.shadow rows (block = 'away' while the house is EMPTY)."""
    out: list[MinuteRow] = []
    for t, d in rows:
        empty = d.get("house") == "EMPTY"
        rooms = {
            k: float(v["temp"])
            for k, v in (d.get("rooms") or {}).items()
            if isinstance(v, dict) and v.get("temp") is not None
        }
        ages = {
            k: v.get("age_min") for k, v in (d.get("rooms") or {}).items() if isinstance(v, dict)
        }
        bot = d.get("bot")
        out.append(
            MinuteRow(
                t=t,
                block="away" if empty else block_at(s.schedule, t),
                occupied=not empty,
                t_in=num(_g(d, "tstat", "temp")),
                t_out=num(d.get("t_out") if d.get("t_out") is not None else d.get("t_out_fc")),
                rooms=rooms,
                ages=ages,
                hvac_action=_g(d, "tstat", "hvac_action"),
                blower=(num(d.get("blower")) or 0.0) > 0.5,
                cs=num(d.get("cs")),
                actual=Setpoints(
                    mode=_g(d, "tstat", "hvac_mode"),
                    heat_sp=num(_g(d, "tstat", "heat_sp")),
                    cool_sp=num(_g(d, "tstat", "cool_sp")),
                ),
                bot=None
                if not isinstance(bot, dict)
                else Setpoints(
                    mode=bot.get("mode"),
                    heat_sp=num(bot.get("heat_sp")),
                    cool_sp=num(bot.get("cool_sp")),
                ),
            )
        )
    return out


SHADOW_COLS = (
    "actual_heat_min",
    "actual_cool_min",
    "actual_cool_effort",
    "actual_fan_only_min",
    "actual_comfort_pct",
    "actual_violation_min",
    "sim_actual_runtime_min",
    "bot_heat_min",
    "bot_cool_min",
    "bot_cool_effort",
    "bot_comfort_pct",
    "bot_violation_min",
    "saved_min",
    "saved_pct",
    "energy_saved_pct",
    "n_samples",
    "bot_steps",
    "coverage_pct",
    "note",
)


def save_shadow_day(
    conn: sqlite3.Connection, day: str, sd: Any, version: int | None, now: datetime
) -> None:
    row: dict[str, Any] = {"day": day, **{k: getattr(sd, k) for k in SHADOW_COLS}}
    row["model_version"] = version
    row["created_at"] = now.astimezone(UTC).isoformat()
    cols = ", ".join(row)
    conn.execute(
        f"INSERT OR REPLACE INTO shadow_daily ({cols}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )


def shadow_report(conn: sqlite3.Connection, now: datetime, days: int = 30) -> dict[str, Any]:
    """Everything /shadow and GET /api/shadow show. Read-only."""
    from thermabot.core.shadow import ShadowDay, totals, verdict

    r = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM samples").fetchone()
    n, first, last = int(r[0]), r[1], r[2]
    m = conn.execute(
        "SELECT version, fitted_at, mae_json, n_samples FROM models WHERE active=1"
        " ORDER BY version DESC LIMIT 1"
    ).fetchone()
    steps = len(local_steps(conn, now - timedelta(days=REFIT_DAYS), now))
    model: dict[str, Any]
    if m is None:
        model = {
            "status": "learning" if steps else "none",
            "steps": steps,
            "need": MIN_TRAIN_STEPS,
        }
    else:
        model = {
            "status": "fitted",
            "version": int(m["version"]),
            "fitted_at": m["fitted_at"],
            "mae": json.loads(m["mae_json"]).get("thermostat"),
            "n_train": m["n_samples"],
            "steps": steps,
            "need": MIN_TRAIN_STEPS,
        }
    since = (now.astimezone(TZ) - timedelta(days=days)).date().isoformat()
    rows = [
        dict(x)
        for x in conn.execute(
            "SELECT * FROM shadow_daily WHERE day >= ? ORDER BY day DESC", (since,)
        )
    ]
    week = (now.astimezone(TZ) - timedelta(days=7)).date().isoformat()
    last7 = [
        ShadowDay.model_validate({k: x[k] for k in SHADOW_COLS if x[k] is not None})
        for x in rows
        if x["day"] >= week
    ]
    tot = totals(last7)
    return {
        "samples": {"count": n, "first": first, "last": last},
        "model": model,
        "days": rows,
        "week": tot.model_dump(),
        "verdict": verdict(tot, m is not None, steps, MIN_TRAIN_STEPS),
        "method": (
            "Actual = measured from 1-minute samples. Bot = thermal-model simulation of the"
            " bot's dry-run setpoints, restarted from the measured temperature every 15 min,"
            " with measured outdoor temperature. Runtime saved % compares the same model run"
            " on the bot's vs the thermostat's setpoints. Energy is a runtime proxy (cooling"
            " weighted by compressor speed); no kWh rate is configured."
        ),
    }


# ---- Influx write-back (D§8.2) ---------------------------------------------------------
@dataclass
class Background:
    """Influx write-back state. Built once in main.build()."""

    conn: sqlite3.Connection
    influx: Influx
    state_fn: Callable[[], str]
    score_fn: Callable[[], float | None] = lambda: None
    last_write_id: int | None = None  # writes rows already sent to Influx

    def enqueue(self, lines: list[str], now: datetime) -> None:
        """Buffer points for the write bucket; no-op without a write token."""
        if self.influx.writable:
            self.influx.enqueue(lines, now=now)

    def _write_lines(self) -> list[str]:
        if self.last_write_id is None:  # start-up: don't replay history
            r = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM writes").fetchone()
            self.last_write_id = int(r[0])
        rows = self.conn.execute(
            "SELECT id, ts, service, data_json, reason, outcome, guard FROM writes"
            " WHERE id > ? ORDER BY id LIMIT 500",
            (self.last_write_id,),
        ).fetchall()
        out: list[str] = []
        for r in rows:
            self.last_write_id = int(r["id"])
            out.append(
                line(
                    "thermabot_write",
                    {"service": r["service"], "outcome": r["outcome"]},
                    {"data": r["data_json"], "reason": r["reason"], "guard": r["guard"] or ""},
                    datetime.fromisoformat(r["ts"]),
                )
            )
        return out

    async def flush(self, now: datetime) -> None:
        """Every tick: enqueue thermabot_state + new thermabot_write rows, then POST the
        buffer. Influx.flush never raises and keeps a bounded, drop-oldest buffer."""
        if not self.influx.writable:
            return
        fields: dict[str, float | int | str | bool] = {"state": self.state_fn()}
        score = self.score_fn()
        if score is not None:
            fields["score"] = float(score)
        self.enqueue([line("thermabot_state", {}, fields, now), *self._write_lines()], now)
        await self.influx.flush(now=now)
