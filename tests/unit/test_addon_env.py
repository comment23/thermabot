"""Blank add-on options use the Supervisor token, including the s6 environment file."""

from __future__ import annotations

from pathlib import Path

import pytest

from addon.options_to_env import container_value, render

BLANK = {"log_level": "info", "ha_url": "", "ha_token": "", "timezone": ""}


@pytest.mark.brd("ADDON-1")
def test_blank_options_read_the_s6_supervisor_token(tmp_path: Path) -> None:
    s6 = tmp_path / "container_environment"
    s6.mkdir()
    (s6 / "SUPERVISOR_TOKEN").write_text("from-s6", encoding="utf-8")
    text = render(BLANK, {}, s6)
    assert "export HA_URL='http://supervisor/core'" in text
    assert "export HA_TOKEN='from-s6'" in text
    assert "export LOG_LEVEL='INFO'" in text


@pytest.mark.brd("ADDON-1")
def test_process_environment_wins_over_the_s6_file(tmp_path: Path) -> None:
    s6 = tmp_path / "container_environment"
    s6.mkdir()
    (s6 / "SUPERVISOR_TOKEN").write_text("from-s6\n", encoding="utf-8")
    text = render(BLANK, {"SUPERVISOR_TOKEN": "from-process"}, s6)
    assert "export HA_TOKEN='from-process'" in text
    assert "from-s6" not in text


@pytest.mark.brd("ADDON-1")
def test_explicit_ha_options_are_not_replaced_by_the_supervisor_token(tmp_path: Path) -> None:
    text = render(
        {"ha_url": "http://other.example", "ha_token": "user-token"},
        {"SUPERVISOR_TOKEN": "from-process"},
        tmp_path,
    )
    assert "export HA_URL='http://other.example'" in text
    assert "export HA_TOKEN='user-token'" in text
    assert "http://supervisor/core" not in text


@pytest.mark.brd("ADDON-1")
def test_container_value_ignores_a_missing_s6_file(tmp_path: Path) -> None:
    assert container_value("SUPERVISOR_TOKEN", {}, tmp_path) == ""
