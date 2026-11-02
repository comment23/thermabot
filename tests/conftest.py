"""Shared pytest fixtures."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("America/New_York")


@pytest.fixture
def tz() -> ZoneInfo:
    return TZ


def local(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    """Aware datetime in America/New_York."""
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # T1.01: an empty suite ("no tests ran", exit 5) counts as success.
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
        session.exitstatus = pytest.ExitCode.OK


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # D§16: carry @pytest.mark.brd IDs into JUnit XML <properties> for scripts/qa_report.py.
    for item in items:
        for m in item.iter_markers("brd"):
            item.user_properties.append(("brd", str(m.args[0])))
