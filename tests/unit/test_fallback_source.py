"""v0.1.9: fallback_source (hold the thermostat's current setpoints vs band edges).
v0.6.2: house=EMPTY away-band behaviour (presence/away-mode fix)."""

import pytest

from tests.conftest import local
from thermabot.config import PlannerCfg, Settings
from thermabot.core.fallback import fallback_plan, fallback_setpoints, valid_current
from thermabot.core.planner import PlanInputs, plan
from thermabot.core.schedule import block_at
from thermabot.core.types import HouseState

NOW = local(2026, 10, 5, 12, 0)
BANDS = Settings(planner=PlannerCfg(fallback_source="bands"))


@pytest.mark.brd("FR-PLN-7")
def test_default_source_is_thermostat() -> None:
    assert Settings().planner.fallback_source == "thermostat"


@pytest.mark.brd("FR-PLN-7")
def test_thermostat_source_holds_current_73_76() -> None:
    p = fallback_plan(Settings(), NOW, "x", horizon_h=24, current=("heat_cool", 73, 76))
    assert {(s.heat_sp, s.cool_sp) for s in p.steps} == {(73, 76)}
    assert p.steps[0].reason == "fallback: holding current 73-76"
    assert all(s.hvac_mode == "heat_cool" and s.fan_mode == "auto" for s in p.steps)


@pytest.mark.brd("FR-PLN-7")
def test_bands_source_ignores_current() -> None:
    p = fallback_plan(BANDS, NOW, "x", horizon_h=1, current=("heat_cool", 73, 76))
    assert (p.steps[0].heat_sp, p.steps[0].cool_sp) == (68, 76)
    assert p.steps[0].reason.startswith("fallback: home band")


@pytest.mark.brd("FR-PLN-7")
@pytest.mark.parametrize(
    "cur",
    [
        None,
        ("heat_cool", 74, 76),  # deadband 2 < 3
        ("cool", 73, 76),  # not heat_cool
        ("off", 68, 76),  # never 'hold' off
        ("heat_cool", None, 76),
        ("heat_cool", 55, 76),  # below HEAT_SP_MIN
        ("heat_cool", 70, 85),  # above COOL_SP_MAX
    ],
)
def test_invalid_current_falls_back_to_bands(cur: tuple[str, float, float] | None) -> None:
    s = Settings()
    assert valid_current(s, cur) is None
    p = fallback_plan(s, NOW, "x", horizon_h=1, current=cur)
    assert (p.steps[0].heat_sp, p.steps[0].cool_sp) == (68, 76)
    assert all(st.hvac_mode != "off" for st in p.steps)


@pytest.mark.brd("FR-PLN-7")
def test_planner_low_confidence_passes_current_setpoints() -> None:
    p = plan(
        PlanInputs(
            now=NOW,
            t0=74,
            forecast=None,
            current_mode="heat_cool",
            current_sp=(73, 76),
            confidence="low",
        )
    )
    assert p.kind == "fallback" and p.steps[0].heat_sp == 73 and p.steps[0].cool_sp == 76
    assert all(st.hvac_mode != "off" for st in p.steps)


# ---- v0.6.2 presence/away-mode fix: house=EMPTY away-band behaviour --------------------


@pytest.mark.brd("FR-PLN-7")
def test_empty_house_uses_away_band_even_with_thermostat_source() -> None:
    """fallback_source='thermostat' (default) would normally hold the current setpoint, but
    house=EMPTY must always use the away band: holding an occupant-chosen manual setpoint
    while genuinely away defeats the point of away mode."""
    from datetime import timedelta

    s = Settings()
    assert s.planner.fallback_source == "thermostat"
    p = fallback_plan(
        s,
        NOW,
        "x",
        horizon_h=1,
        current=("heat_cool", 73, 76),
        house=HouseState.EMPTY,
        away_until=NOW + timedelta(hours=2),
    )
    assert (p.steps[0].heat_sp, p.steps[0].cool_sp) == (62, 80)  # away band edges
    assert p.steps[0].reason == "fallback: away band 62-80"
    assert all(st.hvac_mode != "off" for st in p.steps)


@pytest.mark.brd("FR-PLN-7")
def test_empty_house_uses_away_band_with_bands_source_too() -> None:
    from datetime import timedelta

    p = fallback_plan(
        BANDS, NOW, "x", horizon_h=1, house=HouseState.EMPTY, away_until=NOW + timedelta(hours=1)
    )
    assert (p.steps[0].heat_sp, p.steps[0].cool_sp) == (62, 80)
    assert p.steps[0].reason.startswith("fallback: away band")


@pytest.mark.brd("FR-PLN-7")
def test_empty_house_no_away_until_derives_next_schedule_boundary() -> None:
    """No bounded away_until given: the away window runs until the next schedule boundary
    (core.schedule.next_boundary), same derivation as planner.build_timeline's away_end."""
    from thermabot.core.schedule import next_boundary

    s = Settings()
    end = next_boundary(s.schedule, NOW)
    p = fallback_plan(s, NOW, "x", horizon_h=24, house=HouseState.EMPTY, away_until=None)
    before = [st for st in p.steps if st.t < end]
    after = [st for st in p.steps if st.t >= end]
    assert before and all((st.heat_sp, st.cool_sp) == (62, 80) for st in before)
    assert after and (after[0].heat_sp, after[0].cool_sp) != (62, 80)


@pytest.mark.brd("FR-PLN-7")
def test_occupied_house_unaffected_by_away_args() -> None:
    """house defaults to OCCUPIED: passing away_until with OCCUPIED house must not matter
    (regression: away must never leak in when the house isn't actually EMPTY)."""
    from datetime import timedelta

    p = fallback_plan(
        Settings(),
        NOW,
        "x",
        horizon_h=1,
        current=("heat_cool", 73, 76),
        house=HouseState.OCCUPIED,
        away_until=NOW + timedelta(hours=2),
    )
    assert (p.steps[0].heat_sp, p.steps[0].cool_sp) == (73, 76)  # still holds current


# ---- v0.6.5 stale away-band residue fix ------------------------------------------------


@pytest.mark.brd("FR-PLN-7")
def test_occupied_with_stale_away_residue_falls_through_to_schedule_band() -> None:
    """Reproduces the confirmed production bug: house transitions EMPTY -> OCCUPIED but the
    thermostat is still physically sitting at the away band edges (the bot's own prior
    write). `fallback_source='thermostat'` must NOT hold that stale 62-80 forever -- it must
    fall through to the current schedule block's band instead."""
    s = Settings()
    away_heat, away_cool = fallback_setpoints(s, "away")
    block = block_at(s.schedule, NOW)
    exp_heat, exp_cool = fallback_setpoints(s, block)
    p = fallback_plan(
        s,
        NOW,
        "x",
        horizon_h=1,
        current=("heat_cool", away_heat, away_cool),
        house=HouseState.OCCUPIED,
    )
    assert (p.steps[0].heat_sp, p.steps[0].cool_sp) == (exp_heat, exp_cool)
    assert (p.steps[0].heat_sp, p.steps[0].cool_sp) != (away_heat, away_cool)
    assert p.steps[0].reason == (
        f"fallback: stale away-band residue cleared, {block} band {exp_heat:g}-{exp_cool:g}"
    )


@pytest.mark.brd("FR-PLN-7")
def test_occupied_genuine_manual_setpoint_still_held_not_away_band() -> None:
    """Regression guard: a genuine household manual setpoint that differs from the away
    band must still be held exactly as before -- this fix must not over-fire."""
    s = Settings()
    away_heat, away_cool = fallback_setpoints(s, "away")
    manual = (65.0, 78.0)
    assert manual != (away_heat, away_cool)
    p = fallback_plan(
        s,
        NOW,
        "x",
        horizon_h=1,
        current=("heat_cool", *manual),
        house=HouseState.OCCUPIED,
    )
    assert (p.steps[0].heat_sp, p.steps[0].cool_sp) == manual
    assert p.steps[0].reason == f"fallback: holding current {manual[0]:g}-{manual[1]:g}"


@pytest.mark.brd("FR-PLN-7")
def test_bands_source_with_away_band_current_unaffected_by_fix() -> None:
    """fallback_source='bands' already ignores `current` entirely (hold is always None);
    confirm this configuration was never affected by the bug or by the fix."""
    s = BANDS
    away_heat, away_cool = fallback_setpoints(s, "away")
    block = block_at(s.schedule, NOW)
    exp_heat, exp_cool = fallback_setpoints(s, block)
    p = fallback_plan(
        s,
        NOW,
        "x",
        horizon_h=1,
        current=("heat_cool", away_heat, away_cool),
        house=HouseState.OCCUPIED,
    )
    assert (p.steps[0].heat_sp, p.steps[0].cool_sp) == (exp_heat, exp_cool)
    assert p.steps[0].reason.startswith("fallback: ")
    assert "stale away-band residue" not in p.steps[0].reason


@pytest.mark.brd("FR-PLN-7")
def test_empty_house_with_away_band_current_unaffected_by_fix() -> None:
    """house=EMPTY must be completely untouched by this fix: the away branch already takes
    priority over hold-current, same as before v0.6.5."""
    from datetime import timedelta

    s = Settings()
    away_heat, away_cool = fallback_setpoints(s, "away")
    p = fallback_plan(
        s,
        NOW,
        "x",
        horizon_h=1,
        current=("heat_cool", away_heat, away_cool),
        house=HouseState.EMPTY,
        away_until=NOW + timedelta(hours=1),
    )
    assert (p.steps[0].heat_sp, p.steps[0].cool_sp) == (away_heat, away_cool)
    assert p.steps[0].reason == f"fallback: away band {away_heat:g}-{away_cool:g}"


@pytest.mark.brd("FR-PLN-7")
def test_occupied_near_miss_away_band_still_treated_as_residue() -> None:
    """Float round-trip through HA/SQLite storage can leave `current` a tiny epsilon off
    the exact away band edges -- that must still be treated as stale residue, not a
    distinct genuine manual setpoint. Tolerance chosen: 0.05°F (comfortably covers typical
    float round-trip noise, far below any plausible deliberate manual offset)."""
    s = Settings()
    away_heat, away_cool = fallback_setpoints(s, "away")
    block = block_at(s.schedule, NOW)
    exp_heat, exp_cool = fallback_setpoints(s, block)
    p = fallback_plan(
        s,
        NOW,
        "x",
        horizon_h=1,
        current=("heat_cool", away_heat + 0.01, away_cool - 0.01),
        house=HouseState.OCCUPIED,
    )
    assert (p.steps[0].heat_sp, p.steps[0].cool_sp) == (exp_heat, exp_cool)
    assert "stale away-band residue" in p.steps[0].reason
