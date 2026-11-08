import pytest


@pytest.mark.brd("FR-HA-4")
@pytest.mark.parametrize(("mode", "field"), [("heat", "heat_sp"), ("cool", "cool_sp")])
def test_single_mode_uses_temperature_for_active_setpoint(mode: str, field: str) -> None:
    from thermabot.app.main import thermostat

    th = thermostat(
        {
            "state": mode,
            "attributes": {"temperature": 70, "target_temp_low": 68, "target_temp_high": 76},
        }
    )
    assert getattr(th, field) == 70


@pytest.mark.brd("NFR-SAFE-6")
def test_thermostat_last_reported_parsed_from_ha_state() -> None:
    """Regression: parse_last_changed needs 'state'; without it every tick was DEGRADED (ha)."""
    from thermabot.app.main import thermostat

    th = thermostat(
        {
            "state": "heat_cool",
            "attributes": {"target_temp_low": 73, "target_temp_high": 76},
            "last_reported": "2026-10-06T01:46:02.906203+00:00",
            "last_updated": "2026-10-06T01:40:00+00:00",
        }
    )
    assert th.last_reported is not None
    assert th.last_reported.isoformat() == "2026-10-06T01:46:02.906203+00:00"


@pytest.mark.brd("NFR-SAFE-6")
def test_static_thermostat_is_fresh_when_read_now() -> None:
    """Regression (v0.1.5): Nexia only bumps last_reported on change, so a steady house
    sat >10 min "stale" -> DEGRADED (ha). A successful read of an available entity is fresh."""
    from datetime import UTC, datetime, timedelta

    from thermabot.app.main import thermostat
    from thermabot.core.statemachine import Live, Persisted, effective

    now = datetime(2026, 10, 6, 2, 30, tzinfo=UTC)
    stamp = (now - timedelta(minutes=18)).isoformat()  # static since 22:12 ET
    st = {"state": "heat_cool", "attributes": {}, "last_reported": stamp, "last_updated": stamp}
    th = thermostat(st, now)
    assert th.last_reported == now
    age = (now - th.last_reported).total_seconds() / 60
    p = Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=now)
    assert effective(p, Live(ha_mirror="on", tstat_age_min=age)).state == "SHADOW"


@pytest.mark.brd("NFR-SAFE-6")
def test_unavailable_thermostat_not_freshened_and_degrades() -> None:
    from datetime import UTC, datetime, timedelta

    from thermabot.app.main import thermostat
    from thermabot.core.statemachine import Live, Persisted, effective

    now = datetime(2026, 10, 6, 2, 30, tzinfo=UTC)
    stamp = (now - timedelta(minutes=18)).isoformat()
    th = thermostat({"state": "unavailable", "attributes": {}, "last_reported": stamp}, now)
    assert th.last_reported != now  # read time never vouches for an unavailable entity
    p = Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=now)
    live = Live(ha_mirror="on", tstat_unavailable=True, tstat_age_min=None)
    assert effective(p, live).state == "DEGRADED"
