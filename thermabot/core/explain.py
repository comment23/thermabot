"""Why a plan step exists, and which safety checks would apply to it.

`guard_chain` walks the same functions as `guards.evaluate`, in the same order, and keeps
every row. The last row is the run mode. Shadow stops there: nothing is sent.
"""

from __future__ import annotations

from datetime import datetime

from thermabot.config import Settings
from thermabot.core.guards import (
    GuardCtx,
    allowlist,
    evaluate,
    hard_limits,
    kill_switch,
    off_guardrails,
    override,
    rate_limit,
    readable,
)
from thermabot.core.schedule import block_at
from thermabot.core.types import (
    Action,
    Forecast,
    GuardCheck,
    Plan,
    PlanStep,
    ReasonFactor,
)

GUARD_LABELS = {
    "readable": "Readable",
    "kill_switch": "Stop switch",
    "override": "Override",
    "allowlist": "Allowlist",
    "hard_limits": "Hard limits",
    "off_guardrails": "Off guardrails",
    "rate_limit": "Rate limit",
    "run_mode": "Run mode",
}


def public_note(note: str) -> str:
    """User-facing guard text. The internal name of the stop control stays out of the UI."""
    return note.replace("kill switch", "stop switch")


def action_for(step: PlanStep, entity_id: str) -> Action:
    """The write this step would ask for. Fan-only changes are not a separate preview."""
    eid = entity_id or "climate.unset"
    if step.hvac_mode == "off":
        return Action(service="set_hvac_mode", entity_id=eid, data={"hvac_mode": "off"})
    data: dict[str, float | str | None] = {"hvac_mode": step.hvac_mode}
    if step.hvac_mode in ("heat_cool", "auto"):
        if step.heat_sp is not None:
            data["target_temp_low"] = step.heat_sp
        if step.cool_sp is not None:
            data["target_temp_high"] = step.cool_sp
    else:
        sp = step.heat_sp if step.hvac_mode == "heat" else step.cool_sp
        if sp is not None:
            data["temperature"] = sp
    return Action(service="set_temperature", entity_id=eid, data=data)


def guard_chain(action: Action, ctx: GuardCtx) -> list[GuardCheck]:
    """Every guard evaluate() would consult, plus run mode. Later rows say they were not reached
    once a guard denies, matching the short-circuit."""
    notes: list[GuardCheck] = []
    stopped = False
    current = action

    def add(guard: str, ok: bool, note: str) -> None:
        nonlocal stopped
        if stopped:
            notes.append(GuardCheck(guard=guard, ok=False, note="Not reached"))
            return
        notes.append(GuardCheck(guard=guard, ok=ok, note=public_note(note) or "Passed"))
        if not ok:
            stopped = True

    ok, note = readable(ctx)
    add("readable", ok, note)
    ok, note = kill_switch(ctx)
    add("kill_switch", ok, note)
    ok, note = override(current, ctx)
    add("override", ok, note)
    ok, note = allowlist(current, ctx)
    add("allowlist", ok, note)
    clamped, clamp_note = hard_limits(current, ctx)
    if clamped is None:
        add("hard_limits", False, clamp_note)
    else:
        add("hard_limits", True, clamp_note)
        current = clamped
    ok, note = off_guardrails(current, ctx)
    add("off_guardrails", ok, note)
    ok, note = rate_limit(current, ctx)
    add("rate_limit", ok, note)
    if stopped:
        add("run_mode", False, "Not reached")
    elif ctx.run_mode != "active":
        add("run_mode", False, "Run mode: stops here, nothing is sent")
    else:
        add("run_mode", True, "Run mode: Active, a write can be sent")
    return notes


def _age(now: datetime, then: datetime | None) -> str:
    if then is None:
        return ""
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


def infer_factors(
    step: PlanStep,
    *,
    block: str,
    forecast: Forecast | None,
    house: str,
    model_label: str,
    now: datetime,
) -> list[ReasonFactor]:
    if step.factors:
        return list(step.factors)
    weather = "No forecast"
    source = "none"
    age = ""
    if forecast is not None:
        weather = forecast.source.replace("_", " ")
        source = forecast.source
        age = _age(now, forecast.fetched_at)
        if forecast.stale:
            weather += " (stale)"
    comfort = step.reason or "Following the schedule"
    return [
        ReasonFactor(tag="schedule", text=f"{block} block", source="schedule", age="this step"),
        ReasonFactor(tag="weather", text=weather, source=source, age=age),
        ReasonFactor(tag="model", text=model_label or "not fitted", source="models", age=""),
        ReasonFactor(tag="presence", text=house or "occupied", source="presence", age="now"),
        ReasonFactor(tag="comfort", text=comfort, source="planner", age="this plan"),
    ]


def coldest_room(step: PlanStep) -> tuple[str, float] | None:
    if not step.pred_rooms:
        return None
    key, temp = min(step.pred_rooms.items(), key=lambda kv: kv[1] if kv[1] is not None else 1e9)
    if temp is None:
        return None
    return key, temp


def attach(
    plan: Plan,
    ctx: GuardCtx,
    *,
    entity_id: str,
    forecast: Forecast | None,
    house: str,
    model_label: str,
) -> Plan:
    """Store tagged factors, the evaluate() Decision, and the full guard chain on each step."""
    schedule = ctx.settings.schedule if isinstance(ctx.settings, Settings) else Settings().schedule
    steps: list[PlanStep] = []
    for step in plan.steps:
        block = block_at(schedule, step.t)
        factors = infer_factors(
            step,
            block=block,
            forecast=forecast,
            house=house,
            model_label=model_label,
            now=ctx.now,
        )
        action = action_for(step, entity_id)
        steps.append(
            step.model_copy(
                update={
                    "factors": factors,
                    "decision": evaluate(action, ctx),
                    "checks": guard_chain(action, ctx),
                }
            )
        )
    return plan.model_copy(update={"steps": steps})
