from datetime import timedelta

import pytest

from tests.conftest import local
from thermabot.config import ScheduleCfg
from thermabot.core.overrides import (
    Nudge,
    active_nudge_deltas,
    cancel_override,
    create_nudge,
    nudges_today,
    override_active,
    start_override,
)

S = ScheduleCfg()
NOW = local(2026, 10, 5, 14, 0)  # Monday


@pytest.mark.brd("FR-OVR-1")
@pytest.mark.parametrize("d, h", [("1h", 1), ("2h", 2), ("4h", 4)])
def test_override_fixed_durations(d: str, h: int) -> None:
    o = start_override(NOW, S, d)  # type: ignore[arg-type]
    assert o.expires_at == NOW + timedelta(hours=h)
    assert override_active(o, NOW)
    assert not override_active(o, NOW + timedelta(hours=h))


@pytest.mark.brd("FR-OVR-1")
def test_override_next_block_and_until() -> None:
    assert start_override(NOW, S, "next_block").expires_at == local(2026, 10, 5, 22, 0)
    until = local(2026, 10, 5, 18, 0)
    assert start_override(NOW, S, "until", until=until).expires_at == until
    with pytest.raises(ValueError):
        start_override(NOW, S, "until", until=NOW)
    with pytest.raises(ValueError):
        start_override(NOW, S, "until", until=until.replace(tzinfo=None))


@pytest.mark.brd("FR-OVR-3")
def test_external_override_lasts_to_next_block() -> None:
    o = start_override(NOW, S, "1h", source="external")
    assert o.expires_at == local(2026, 10, 5, 22, 0)


@pytest.mark.brd("FR-OVR-2")
def test_cancel() -> None:
    o = cancel_override(start_override(NOW, S, "2h"), NOW + timedelta(minutes=5))
    assert not override_active(o, NOW + timedelta(minutes=6))
    assert not override_active(None, NOW)


def nudge(existing: list[Nudge], t=NOW, **kw):  # type: ignore[no-untyped-def]
    args = dict(user_id=1, role="member", user_room="bedroom_1", delta_f=1, hours=2)
    args.update(kw)
    return create_nudge(existing, t, **args)  # type: ignore[arg-type]


class TestNudge:
    @pytest.mark.brd("FR-CMF-6")
    def test_member_own_room(self) -> None:
        n, _ = nudge([])
        assert n.room == "bedroom_1"
        assert n.expires_at == NOW + timedelta(hours=2)
        assert active_nudge_deltas([n], NOW) == {"bedroom_1": 1}

    @pytest.mark.brd("FR-USR-2")
    def test_member_other_room_denied(self) -> None:
        with pytest.raises(PermissionError):
            nudge([], room="bedroom_2")
        with pytest.raises(PermissionError):
            nudge([], role="viewer")

    @pytest.mark.brd("FR-USR-2")
    def test_owner_any_room(self) -> None:
        n, _ = nudge([], role="owner", user_room=None, room="bedroom_2")
        assert n.room == "bedroom_2"

    @pytest.mark.brd("FR-CMF-6")
    @pytest.mark.parametrize(
        "kw", [{"delta_f": 3}, {"delta_f": 0}, {"delta_f": 1.5}, {"hours": 5}, {"hours": 0}]
    )
    def test_bounds(self, kw: dict[str, float]) -> None:
        with pytest.raises(ValueError):
            nudge([], **kw)

    @pytest.mark.brd("FR-CMF-6")
    def test_three_per_local_day(self) -> None:
        ns: list[Nudge] = []
        for i in range(3):
            n, ns = nudge(ns, t=NOW + timedelta(minutes=i))
            ns.append(n)
        assert nudges_today(ns, 1, NOW) == 3
        with pytest.raises(ValueError):
            nudge(ns, t=NOW + timedelta(minutes=10))
        # another user is unaffected; next local day resets
        nudge(ns, user_id=2, user_room="bedroom_2")
        nudge(ns, t=local(2026, 10, 6, 0, 1))

    @pytest.mark.brd("FR-CMF-6")
    def test_new_nudge_replaces_open_one(self) -> None:
        first, _ = nudge([])
        second, updated = nudge([first], t=NOW + timedelta(minutes=5), delta_f=-2)
        assert updated[0].cancelled_at == NOW + timedelta(minutes=5)
        assert active_nudge_deltas([*updated, second], NOW + timedelta(minutes=6)) == {
            "bedroom_1": -2
        }

    @pytest.mark.brd("FR-CMF-6")
    def test_expiry(self) -> None:
        n, _ = nudge([], hours=1)
        assert active_nudge_deltas([n], NOW + timedelta(hours=1)) == {}
