"""SQLite adapter (D§2): WAL, numbered migrations in `user_version`, checksummed `bot_state`.

Missing or tampered `bot_state` reads as None, which core treats as DISABLED (FR-KILL-4).
An unopenable file is renamed `<name>.corrupt-<ts>` and a fresh DB (kill switch off) is created.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from thermabot.core.statemachine import Persisted

log = logging.getLogger(__name__)
MIGRATIONS = Path(__file__).parent / "migrations"
_STATE_COLS = (
    "kill_switch",
    "run_mode",
    "shadow_started_at",
    "disabled_since",
    "preset_cleared",
    "updated_at",
    "updated_by",
)


def _iso(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(UTC).isoformat()


def _now() -> datetime:
    return datetime.now(UTC)


def checksum(row: dict[str, object]) -> str:
    body = json.dumps({k: row.get(k) for k in _STATE_COLS}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("quick_check failed")
    except sqlite3.DatabaseError:
        conn.close()
        raise
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for f in sorted(MIGRATIONS.glob("*.sql")):
        n = int(f.name.split("_", 1)[0])
        if n <= ver:
            continue
        sql = f.read_text(encoding="utf-8")
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version={n};\nCOMMIT;")
        ver = n
    return ver


def open_db(path: str | Path, now: datetime | None = None) -> sqlite3.Connection:
    """Open (or create) the DB. Corrupt file -> renamed aside, fresh DB with kill switch off."""
    p = Path(path)
    try:
        conn = _connect(p)
    except sqlite3.DatabaseError as e:
        stamp = (now or _now()).strftime("%Y%m%dT%H%M%SZ")
        dest = p.with_name(f"{p.name}.corrupt-{stamp}")
        log.error("db unopenable (%s); renaming to %s", e, dest.name)
        for suffix in ("", "-wal", "-shm"):
            side = Path(f"{p}{suffix}")
            if side.exists():
                side.rename(Path(f"{dest}{suffix}"))
        conn = _connect(p)
    migrate(conn)
    if conn.execute("SELECT COUNT(*) FROM bot_state").fetchone()[0] == 0:
        t = now or _now()
        write_bot_state(
            conn,
            Persisted(kill_switch="off", run_mode="shadow", disabled_since=t),
            by="init",
            now=t,
        )
    return conn


def read_bot_state(conn: sqlite3.Connection) -> Persisted | None:
    """Re-read on every guard evaluation. None = missing/corrupt -> DISABLED."""
    try:
        r = conn.execute("SELECT * FROM bot_state WHERE id=1").fetchone()
    except sqlite3.DatabaseError as e:
        log.error("bot_state unreadable: %s", e)
        return None
    if r is None:
        return None
    row = {k: r[k] for k in r.keys()}  # noqa: SIM118
    if row["checksum"] != checksum(row):
        log.error("bot_state checksum mismatch -> DISABLED")
        return None
    return Persisted(
        kill_switch=row["kill_switch"],
        run_mode=row["run_mode"],
        shadow_started_at=row["shadow_started_at"],
        disabled_since=row["disabled_since"],
        preset_cleared=bool(row["preset_cleared"]),
    )


def write_bot_state(
    conn: sqlite3.Connection, p: Persisted, by: str, now: datetime | None = None
) -> None:
    row: dict[str, object] = {
        "kill_switch": p.kill_switch,
        "run_mode": p.run_mode,
        "shadow_started_at": _iso(p.shadow_started_at),
        "disabled_since": _iso(p.disabled_since),
        "preset_cleared": int(p.preset_cleared),
        "updated_at": _iso(now or _now()),
        "updated_by": by,
    }
    row["checksum"] = checksum(row)
    cols = ", ".join(row)
    conn.execute(
        f"INSERT OR REPLACE INTO bot_state (id, {cols}) VALUES (1, {', '.join('?' * len(row))})",
        tuple(row.values()),
    )


def _kv_sum(key: str, value: str) -> str:
    return hashlib.sha256(json.dumps([key, value]).encode()).hexdigest()


def read_kv(conn: sqlite3.Connection, key: str) -> str | None:
    """Checksummed key/value read; missing or tampered -> None."""
    r = conn.execute("SELECT value, checksum FROM bot_kv WHERE key=?", (key,)).fetchone()
    if r is None:
        return None
    if r["checksum"] != _kv_sum(key, r["value"]):
        log.error("bot_kv %s checksum mismatch", key)
        return None
    return str(r["value"])


def write_kv(conn: sqlite3.Connection, key: str, value: str, now: datetime | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO bot_kv (key, value, updated_at, checksum) VALUES (?,?,?,?)",
        (key, value, _iso(now or _now()), _kv_sum(key, value)),
    )


def audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    detail: dict[str, object],
    now: datetime | None = None,
    ip: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit (ts, actor, ip, action, detail_json) VALUES (?,?,?,?,?)",
        (_iso(now or _now()), actor, ip, action, json.dumps(detail, default=str)),
    )


def backup(conn: sqlite3.Connection, dest: str | Path) -> Path:
    """Online consistent copy via VACUUM INTO (dest must not exist)."""
    d = Path(dest)
    conn.execute("VACUUM INTO ?", (str(d),))
    return d
