from __future__ import annotations

from pathlib import Path

import pytest

from scripts.shadow_report import _cookie, render_report


@pytest.mark.brd("FR-MODE-3")
def test_report_lists_soak_metrics_and_stale_rooms() -> None:
    report, status = render_report(
        {
            "state": "SHADOW",
            "run_mode": "shadow",
            "shadow_days": 2.5,
            "rooms": [{"key": "office", "stale": True, "age_min": 65.0}],
        },
        {
            "day_utc": "2026-10-05",
            "sources": {"ha": "ok", "weather": "ok"},
            "writes_today": {"sent": 0, "shadow": 4, "denied": 1, "failed": 0},
            "write_decisions_today": [
                {"outcome": "denied", "guard": "weather", "reason": "stale forecast", "count": 1}
            ],
            "degraded_today": {
                "sampled_minutes": 60,
                "degraded_minutes": 3,
                "untracked_minutes": 2,
                "by_reason": {"weather": 3},
            },
        },
        {"model": {"status": "fitted", "mae": 0.75}},
        assert_no_sent=True,
    )
    assert status == 0
    assert "## Decisions by guard" in report
    assert "| 0 | 4 | 1 | 0 |" in report
    assert "weather | stale forecast | 1" in report
    assert "3 sampled minutes" in report
    assert "office (65.0 min)" in report
    assert "thermostat MAE: 0.750" in report


@pytest.mark.brd("FR-MODE-3")
def test_assert_no_sent_fails_without_hiding_report() -> None:
    report, status = render_report(
        {"state": "SHADOW", "run_mode": "shadow", "shadow_days": 1, "rooms": []},
        {
            "day_utc": "2026-10-05",
            "sources": {"ha": "ok", "weather": "ok"},
            "writes_today": {"sent": 1, "shadow": 0, "denied": 0, "failed": 0},
            "write_decisions_today": [],
            "degraded_today": {
                "sampled_minutes": 1,
                "degraded_minutes": 0,
                "untracked_minutes": 1,
                "by_reason": {},
            },
        },
        {"model": {"status": "none", "mae": None}},
        assert_no_sent=True,
    )
    assert status == 1
    assert "| 1 | 0 | 0 | 0 |" in report


@pytest.mark.brd("FR-MODE-3")
def test_report_rejects_missing_write_counts() -> None:
    with pytest.raises(ValueError, match="sent"):
        render_report(
            {"rooms": []},
            {
                "day_utc": "2026-10-05",
                "sources": {"ha": "ok", "weather": "ok"},
                "writes_today": {"shadow": 0, "denied": 0, "failed": 0},
                "degraded_today": {
                    "sampled_minutes": 0,
                    "degraded_minutes": 0,
                    "untracked_minutes": 0,
                    "by_reason": {},
                },
                "write_decisions_today": [],
            },
            {"model": {}},
        )


def test_cookie_file_rejects_header_injection(tmp_path: Path) -> None:
    path = tmp_path / "cookie.txt"
    path.write_text("tb_session=opaque-token", encoding="utf-8")
    assert _cookie(path) == "tb_session=opaque-token"
    path.write_text("opaque-token\r\nX-Injected: yes", encoding="utf-8")
    with pytest.raises(ValueError, match="cookie"):
        _cookie(path)
