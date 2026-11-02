from datetime import timedelta

import pytest

from tests.conftest import local
from tests.house import HOUSE
from thermabot.core.guards import GuardCtx, evaluate
from thermabot.core.safety import (
    OffWindow,
    check,
    crossing,
    cumulative_off_h,
    forced_on_until,
    needs_reevaluation,
    off_allowed_now,
    off_cap_reached,
    open_off_window,
)
from thermabot.core.thermal import ThermalParams, step
from thermabot.core.types import (
    HouseState,
    Presence,
    RoomReading,
    Snapshot,
    ThermostatState,
)

NOW = local(2026, 10, 5, 12, 0)
S = HOUSE


def snap(
    temp: float,
    mode: str = "off",
    lo: float | None = None,
    hi: float | None = None,
    rooms: dict[str, RoomReading] | None = None,
    house: HouseState | None = None,
) -> Snapshot:
    th = ThermostatState(
        hvac_mode=mode, current_temp=temp, heat_sp=lo, cool_sp=hi, last_reported=NOW
    )
    pres = Presence(house=house, since=NOW) if house else None
    return Snapshot(t=NOW, thermostat=th, rooms=rooms or {}, presence=pres)


class TestRestore:
    @pytest.mark.brd("NFR-SAFE-3")
    def test_floor_and_ceiling(self) -> None:
        r = check(snap(62), S, "home", NOW)
        assert r is not None and r.kind == "heat" and r.action.safety_restore
        assert r.action.data == {
            "hvac_mode": "heat_cool",
            "target_temp_low": 68,
            "target_temp_high": 76,
        }
        assert r.hold_until == NOW + timedelta(minutes=60)
        r = check(snap(80), S, "sleep", NOW)
        assert r is not None and r.kind == "cool" and r.action.data["target_temp_high"] == 74
        assert check(snap(62.1), S, "home", NOW) is None
        assert check(snap(79.9), S, "home", NOW) is None

    @pytest.mark.brd("NFR-SAFE-3")
    def test_rooms_only_when_occupied_and_fresh(self) -> None:
        cold = {"bedroom_1": RoomReading(key="bedroom_1", temp=60)}
        assert crossing(snap(66, rooms=cold), S) is not None
        assert crossing(snap(66, rooms=cold, house=HouseState.EMPTY), S) is None
        stale = {"x": RoomReading(key="x", temp=59, stale=True)}
        assert crossing(snap(66, rooms=stale), S) is None
        hot = {"x": RoomReading(key="x", temp=82)}
        assert crossing(snap(75, rooms=hot), S) == ("cool", "x 82°F >= 82")

    @pytest.mark.brd("NFR-SAFE-3")
    def test_no_restore_when_already_conditioning(self) -> None:
        assert check(snap(61, mode="heat_cool", lo=68, hi=76), S, "home", NOW) is None
        assert check(snap(61, mode="heat_cool", lo=58, hi=76), S, "home", NOW) is not None
        assert check(snap(81, mode="cool", hi=76), S, "home", NOW) is None
        assert check(snap(81, mode="heat", lo=68), S, "home", NOW) is not None

    @pytest.mark.brd("NFR-SAFE-3")
    def test_restore_passes_guards_despite_override(self) -> None:
        r = check(snap(61), S, "home", NOW)
        assert r is not None
        c = GuardCtx(
            kill_switch="on",
            ha_mirror="on",
            run_mode="active",
            override_open=True,
            snapshot=snap(61),
            now=NOW,
            settings=S,
        )
        assert evaluate(r.action, c).allowed
        assert not evaluate(r.action, c.model_copy(update={"kill_switch": "off"})).allowed


def _sim_until_restore(t0: float, t_out: float, hour0: float) -> tuple[int, float]:
    """Drift with HVAC off in 15-min steps; return (steps, temp) at first restore."""
    p = ThermalParams(a=0.15, s=1.0)
    t = t0
    for k in range(200):
        if check(snap(t), S, "sleep", NOW) is not None:
            return k, t
        t = step(t, t_out, (hour0 + k * 0.25) % 24, "idle", p)
    raise AssertionError("never restored")


class TestSimulations:
    @pytest.mark.brd("NFR-SAFE-3")
    def test_cold_night_restores_immediately(self) -> None:
        k, t = _sim_until_restore(66, 20, 23)
        assert k > 0 and 61 < t <= 62  # restored on the first step at/below 62

    @pytest.mark.brd("NFR-SAFE-3")
    def test_hot_afternoon_restores_immediately(self) -> None:
        k, t = _sim_until_restore(76, 98, 12)
        assert k > 0 and 80 <= t < 81


class TestOffWindows:
    @pytest.mark.brd("NFR-SAFE-1")
    def test_reevaluate_at_8h(self) -> None:
        w = open_off_window(NOW, S)
        assert w.reevaluate_at == NOW + timedelta(hours=8)
        assert not needs_reevaluation(w, NOW + timedelta(hours=7, minutes=59))
        assert needs_reevaluation(w, NOW + timedelta(hours=8))
        assert not needs_reevaluation(None, NOW)

    @pytest.mark.brd("NFR-SAFE-2")
    def test_24h_cap_then_forced_hour(self) -> None:
        ws = [
            OffWindow(
                start=NOW + timedelta(hours=8 * i),
                reevaluate_at=NOW + timedelta(hours=8 * i + 8),
                end=NOW + timedelta(hours=8 * i + 8),
            )
            for i in range(3)
        ]
        end = NOW + timedelta(hours=24)
        assert cumulative_off_h(ws, end) == 24 and off_cap_reached(ws, end)
        assert forced_on_until(ws, end + timedelta(minutes=30)) == end + timedelta(hours=1)
        assert not off_allowed_now(ws, None, end + timedelta(minutes=59))
        assert off_allowed_now(ws, None, end + timedelta(hours=1))
        assert cumulative_off_h(ws, end + timedelta(hours=2)) == 0  # run reset by the on hour

    @pytest.mark.brd("NFR-SAFE-2")
    def test_open_window_hits_cap(self) -> None:
        ws = [OffWindow(start=NOW, reevaluate_at=NOW + timedelta(hours=8))]
        assert not off_cap_reached(ws, NOW + timedelta(hours=23))
        assert off_cap_reached(ws, NOW + timedelta(hours=24))

    @pytest.mark.brd("NFR-SAFE-3")
    def test_post_restore_hold_blocks_off(self) -> None:
        assert not off_allowed_now([], NOW + timedelta(minutes=59), NOW)
        assert off_allowed_now([], NOW, NOW)
