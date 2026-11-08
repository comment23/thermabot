"""T1.20: SQLite adapter (D§2)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from thermabot.adapters import db
from thermabot.core.statemachine import Persisted, turn_on

T0 = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)


@pytest.mark.brd("FR-KILL-4")
def test_fresh_db_kill_switch_off(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "t.db", now=T0)
    p = db.read_bot_state(conn)
    assert p is not None and p.kill_switch == "off"
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4  # 004_efficiency (v0.5.0)


@pytest.mark.brd("FR-MODE-2")
def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db.open_db(tmp_path / "t.db", now=T0).close()
    conn = db.open_db(tmp_path / "t.db", now=T0)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM bot_state").fetchone()[0] == 1


@pytest.mark.brd("FR-KILL-4")
def test_round_trip(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "t.db", now=T0)
    db.write_bot_state(conn, turn_on(None, "shadow", T0), by="alex", now=T0)
    p = db.read_bot_state(conn)
    assert p == Persisted(kill_switch="on", run_mode="shadow", shadow_started_at=T0)


@pytest.mark.brd("FR-KILL-4")
def test_tampered_checksum_disabled(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "t.db", now=T0)
    conn.execute("UPDATE bot_state SET kill_switch='on'")  # bypasses checksum
    assert db.read_bot_state(conn) is None


@pytest.mark.brd("FR-KILL-4")
def test_missing_row_disabled(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "t.db", now=T0)
    conn.execute("DELETE FROM bot_state")
    assert db.read_bot_state(conn) is None


@pytest.mark.brd("NFR-SAFE-9")
def test_compare_and_set_kv_requires_current_value_and_valid_checksum(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "t.db", now=T0)
    db.write_kv(conn, "command", "pending", now=T0)
    assert db.compare_and_set_kv(conn, "command", "pending", "sending", now=T0)
    assert not db.compare_and_set_kv(conn, "command", "pending", "sending", now=T0)
    conn.execute("UPDATE bot_kv SET value='pending' WHERE key='command'")
    assert not db.compare_and_set_kv(conn, "command", "pending", "sending", now=T0)
    assert db.read_kv(conn, "command") is None


@pytest.mark.brd("FR-KILL-4")
def test_unopenable_file_renamed(tmp_path: Path) -> None:
    f = tmp_path / "t.db"
    f.write_bytes(b"this is not a sqlite database" * 200)
    conn = db.open_db(f, now=T0)
    aside = list(tmp_path.glob("t.db.corrupt-*"))
    assert len(aside) == 1 and aside[0].name == "t.db.corrupt-20261005T180000Z"
    p = db.read_bot_state(conn)
    assert p is not None and p.kill_switch == "off"


@pytest.mark.brd("NFR-REL-2")
def test_vacuum_into_backup(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "t.db", now=T0)
    db.audit(conn, "alex", "kill_on", {"x": 1}, now=T0)
    out = db.backup(conn, tmp_path / "b.db")
    b = sqlite3.connect(out)
    assert b.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
    b.close()
