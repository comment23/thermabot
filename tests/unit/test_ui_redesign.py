"""Display units, plan sentences, and the guard chain shown in the why drawer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from thermabot.app.present import action_sentence
from thermabot.app.units import format_temp
from thermabot.config import Settings
from thermabot.core.explain import guard_chain
from thermabot.core.guards import GuardCtx
from thermabot.core.types import Action, PlanStep, Snapshot, ThermostatState

STATIC = Path(__file__).parents[2] / "thermabot" / "app" / "static"
TEMPLATES = Path(__file__).parents[2] / "thermabot" / "app" / "templates"


def test_format_temp_uses_display_units() -> None:
    assert format_temp(69.5, "F") == "69.5°F"
    assert format_temp(68, "F") == "68°F"
    assert format_temp(68, "C") == "20.0°C"
    assert format_temp(2, "F", delta=True) == "+2°F"
    assert format_temp(None, "F") == "—"


def test_action_sentence_does_not_repeat_the_mode() -> None:
    step = PlanStep(t=datetime(2026, 11, 1, tzinfo=UTC), hvac_mode="heat", heat_sp=67)
    text = action_sentence(step, "F")
    assert text == "Heat to 67°F"
    assert "heat heat" not in text.lower()


def test_guard_chain_matches_evaluate_order_and_stops_in_shadow() -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    settings = Settings.model_validate({"thermostat": {"entity_id": "climate.hall"}})
    snap = Snapshot(
        t=now,
        thermostat=ThermostatState(
            hvac_mode="heat",
            current_temp=70,
            heat_sp=68,
            last_reported=now,
            hvac_modes=["heat", "off"],
        ),
    )
    ctx = GuardCtx(
        kill_switch="on",
        ha_mirror="on",
        run_mode="shadow",
        snapshot=snap,
        settings=settings,
        now=now,
    )
    notes = guard_chain(
        Action(
            service="set_temperature",
            entity_id="climate.hall",
            data={"hvac_mode": "heat", "temperature": 67},
        ),
        ctx,
    )
    assert [n.guard for n in notes] == [
        "readable",
        "kill_switch",
        "override",
        "allowlist",
        "hard_limits",
        "off_guardrails",
        "rate_limit",
        "run_mode",
    ]
    assert all(n.ok for n in notes[:-1])
    assert notes[-1].note == "Run mode: stops here, nothing is sent"


def test_first_party_assets_stay_small_and_local() -> None:
    css = (STATIC / "app.css").stat().st_size
    js = (STATIC / "app.js").stat().st_size
    assert css < 80_000, css
    assert js < 40_000, js
    for folder in (TEMPLATES, STATIC):
        for path in folder.rglob("*"):
            if path.suffix not in {".html", ".js", ".css"} or "vendor" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            assert "http://" not in text and "https://" not in text, path
