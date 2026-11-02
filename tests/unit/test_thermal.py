import random

import pytest

from thermabot.core.thermal import (
    Sample,
    ThermalParams,
    confidence,
    fit,
    holdout_mae,
    in_range,
    simulate,
    solar,
    step,
)

TRUE = ThermalParams(a=0.12, s=0.8, q_h=4.0, q_c0=3.5, q_c1=0.06, sigma=0.8)


def synth(n: int = 600, seed: int = 1) -> list[Sample]:
    rnd = random.Random(seed)
    out: list[Sample] = []
    t = 68.0
    for k in range(n):
        hour = (k * 0.25) % 24
        t_out = rnd.uniform(20, 95)
        u = rnd.choice(["heat", "idle", "cool"])
        sigma = rnd.uniform(0.5, 1.0) if u == "cool" else 1.0
        p = TRUE.model_copy(update={"sigma": sigma})
        t_next = step(t, t_out, hour, u, p)  # type: ignore[arg-type]
        out.append(Sample(t=t, t_next=t_next, t_out=t_out, hour=hour, u=u, sigma=sigma))  # type: ignore[arg-type]
        t = min(max(t_next, 55), 85)
    return out


@pytest.mark.brd("FR-PLN-3")
def test_solar_shape() -> None:
    assert solar(6) == 0 and solar(20) == 0
    assert solar(13) == pytest.approx(1.0)


@pytest.mark.brd("FR-PLN-3")
def test_simulate_length_and_heat_rises() -> None:
    ts = simulate(65, [50] * 4, [2] * 4, ["heat"] * 4, TRUE)
    assert len(ts) == 5 and ts[-1] > ts[0]


@pytest.mark.brd("FR-PLN-3")
def test_fit_recovers_params_within_10pct() -> None:
    r = fit(synth())
    assert r.accepted, r.note
    for k in ("a", "s", "q_h", "q_c0", "q_c1"):
        assert getattr(r.params, k) == pytest.approx(getattr(TRUE, k), rel=0.10), k


@pytest.mark.brd("FR-PLN-3")
def test_fit_idle_only_recovers_a_s_keeps_prev_q_h() -> None:
    """No heat samples yet (e.g. cooling season, no heating cycles logged): a/s (building
    envelope loss, solar gain) still fit from idle rows alone; q_h degrades to prev.q_h
    rather than rejecting the whole model, same pattern as the cooling-side degrade."""
    prev = ThermalParams(a=0.2, s=0.3, q_h=9.9, q_c0=2.0, q_c1=0.01, sigma=0.7)
    rnd = random.Random(2)
    data: list[Sample] = []
    t = 68.0
    for k in range(300):
        hour = (k * 0.25) % 24
        t_out = rnd.uniform(60, 90)
        t_next = step(t, t_out, hour, "idle", TRUE)  # type: ignore[arg-type]
        data.append(Sample(t=t, t_next=t_next, t_out=t_out, hour=hour, u="idle"))
        t = min(max(t_next, 55), 85)
    r = fit(data, prev)
    assert r.accepted, r.note
    assert r.params.a == pytest.approx(TRUE.a, rel=0.10)
    assert r.params.s == pytest.approx(TRUE.s, rel=0.10)
    assert r.params.q_h == prev.q_h  # no heat data: degrades to previous, not refit
    assert r.params.q_c0 == prev.q_c0  # no cool data either: also degrades


@pytest.mark.brd("FR-PLN-3")
def test_out_of_range_fit_rejected_keeps_previous() -> None:
    prev = ThermalParams()
    bad = ThermalParams(a=0.9, s=0.8, q_h=4.0)
    data = []
    t = 68.0
    for k in range(100):
        u = "heat" if k % 2 else "idle"
        t_next = step(t, 40, 3, u, bad)  # type: ignore[arg-type]
        data.append(Sample(t=t, t_next=t_next, t_out=40 + k % 7, hour=3, u=u))  # type: ignore[arg-type]
    r = fit(data, prev)
    assert not r.accepted and r.params == prev
    assert not in_range(bad)


@pytest.mark.brd("FR-PLN-3")
def test_holdout_mae_zero_for_true_model() -> None:
    t = 68.0
    cont = []
    for k in range(64):
        t_next = step(t, 40, (k * 0.25) % 24, "heat" if k % 3 else "idle", TRUE)
        cont.append(
            Sample(
                t=t, t_next=t_next, t_out=40, hour=(k * 0.25) % 24, u="heat" if k % 3 else "idle"
            )
        )
        t = t_next
    assert holdout_mae(cont, TRUE) == pytest.approx(0, abs=1e-9)
    assert holdout_mae(cont, ThermalParams(a=0.3)) > 0.1


@pytest.mark.brd("FR-PLN-3")
def test_confidence() -> None:
    assert confidence(0.8, {"bedroom_1": 1.5}) == "ok"
    assert confidence(1.2, {}) == "low"
    assert confidence(0.5, {"bedroom_2": 2.5}) == "low"
