import pytest

from tests.conftest import local
from thermabot.config import Settings
from thermabot.core.fallback import fallback_plan, fallback_setpoints

NOW = local(2026, 10, 5, 12, 0)  # Monday


@pytest.mark.brd("FR-PLN-7")
def test_heat_cool_at_band_edges() -> None:
    s = Settings()
    assert fallback_setpoints(s, "home") == (68, 76)
    assert fallback_setpoints(s, "sleep") == (66, 74)
    p = fallback_plan(s, NOW, "low confidence")
    assert p.kind == "fallback" and p.reason_banner == "low confidence"
    assert all(st.hvac_mode == "heat_cool" and st.fan_mode == "auto" for st in p.steps)


@pytest.mark.brd("FR-PLN-7")
def test_band_edges_follow_schedule() -> None:
    p = fallback_plan(Settings(), NOW, "x", horizon_h=24)
    assert len(p.steps) == 96
    by_t = {st.t: st for st in p.steps}
    assert (by_t[local(2026, 10, 5, 21, 45)].heat_sp, by_t[local(2026, 10, 5, 21, 45)].cool_sp) == (
        68,
        76,
    )
    assert (by_t[local(2026, 10, 5, 22, 0)].heat_sp, by_t[local(2026, 10, 5, 22, 0)].cool_sp) == (
        66,
        74,
    )
    assert by_t[local(2026, 10, 5, 22, 0)].reason.startswith("fallback: sleep")
    assert by_t[local(2026, 10, 5, 22, 15)].reason == ""


@pytest.mark.brd("FR-PLN-7")
def test_never_off_and_deadband() -> None:
    s = Settings()
    p = fallback_plan(s, NOW, "forecast missing")
    for st in p.steps:
        assert st.hvac_mode != "off"
        assert st.heat_sp is not None and st.cool_sp is not None
        assert st.cool_sp - st.heat_sp >= s.writes.deadband_f
