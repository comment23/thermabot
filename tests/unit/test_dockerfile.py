"""T1.45 local substitute (no Docker daemon here): static checks on Dockerfile/.dockerignore."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DF = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _instr(name: str) -> list[str]:
    return [ln.split(None, 1)[1] for ln in DF.splitlines() if ln.split(None, 1)[:1] == [name]]


@pytest.mark.brd("NFR-SEC-3")
def test_multi_stage_non_root_99_100() -> None:
    assert len(_instr("FROM")) >= 2
    assert _instr("USER")[-1].strip() == "99:100"


@pytest.mark.brd("NFR-REL-1")
def test_healthcheck_hits_healthz_without_curl() -> None:
    (hc,) = _instr("HEALTHCHECK")
    assert "--start-period=60s" in hc and "/healthz" in hc and "curl" not in DF


@pytest.mark.brd("NFR-REL-1")
def test_single_worker_uvicorn_entry_point() -> None:
    cmd = json.loads(_instr("CMD")[-1])
    assert cmd[:2] == ["uvicorn", "thermabot.app.main:app"]
    assert cmd[cmd.index("--workers") + 1] == "1" and "8095" in cmd


@pytest.mark.brd("NFR-SEC-5")
def test_hash_pinned_install_and_lock_has_hashes() -> None:
    assert "--require-hashes" in DF
    lock = (ROOT / "requirements.runtime.lock").read_text(encoding="utf-8")
    assert "--hash=sha256:" in lock and not re.search(r"^(pytest|playwright|ruff)==", lock, re.M)


@pytest.mark.brd("NFR-SEC-3")
def test_dockerignore_excludes_secrets_and_tests() -> None:
    ign = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert {".env", ".git", ".venv", "tests", "*.db"} <= set(ign)
