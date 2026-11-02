"""Owner-only configuration transfer and consistent local database backups."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from thermabot import __version__
from thermabot.adapters import db
from thermabot.app.auth import Principal, require
from thermabot.app.routes_api import Ctx, CtxD, load_settings, settings_put
from thermabot.config import Settings

log = logging.getLogger(__name__)
router = APIRouter()
Owner = Annotated[Principal, Depends(require("edit_safety"))]
NO_CACHE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
BACKUP_NAME = re.compile(
    r"thermabot-(?:(?:manual|pre-import)-[0-9T]+-[a-f0-9]{12}|[0-9]{4}-[0-9]{2}-[0-9]{2})\.db"
)


class ConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["thermabot-config"]
    schema_version: Literal[1]
    app_version: str
    exported_at: datetime
    settings: dict[str, Any]


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: ConfigFile
    expected_current: str | None = None


def fingerprint(settings: Settings) -> str:
    return hashlib.sha256(
        json.dumps(settings.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


def backup_dir(c: Ctx) -> Path:
    filename = c.conn.execute("PRAGMA database_list").fetchone()["file"]
    if not filename:
        raise HTTPException(503, "Backups require a persistent database")
    return Path(filename).parent / "backup"


def create_backup(c: Ctx, kind: str) -> Path:
    directory = backup_dir(c)
    dest = directory / f"thermabot-{kind}-{c.clock():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:12]}.db"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        db.backup(c.conn, dest)
        dest.chmod(0o600)
        with sqlite3.connect(dest) as check:
            if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("backup integrity check failed")
    except (OSError, sqlite3.Error) as exc:
        log.exception("database backup failed")
        if dest.exists():
            dest.unlink()
        raise HTTPException(503, "Backup failed; no configuration was changed") from exc
    return dest


def checked_settings(document: ConfigFile) -> Settings:
    if set(document.settings) != set(Settings.model_fields):
        raise HTTPException(
            422, "Import must contain every settings section, and no unknown sections"
        )
    try:
        return Settings.model_validate(document.settings, extra="forbid")
    except ValidationError as exc:
        # Do not echo uploaded values (including accidentally supplied secrets).
        raise HTTPException(
            422, [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        ) from exc


@router.get("/api/config/export")
async def export_config(c: CtxD, p: Owner, request: Request) -> JSONResponse:
    doc = ConfigFile(
        format="thermabot-config",
        schema_version=1,
        app_version=__version__,
        exported_at=c.clock(),
        settings=load_settings(c).model_dump(mode="json"),
    )
    c.audit(p, "config_export", request)
    return JSONResponse(
        doc.model_dump(mode="json"),
        headers={**NO_CACHE, "Content-Disposition": 'attachment; filename="thermabot-config.json"'},
    )


@router.post("/api/config/preview")
async def preview_import(body: ImportRequest, c: CtxD, _: Owner) -> dict[str, Any]:
    new, old = checked_settings(body.document), load_settings(c)
    changed = [key for key in Settings.model_fields if getattr(new, key) != getattr(old, key)]
    return {
        "changed_sections": changed,
        "changes": {
            key: {
                "before": old.model_dump(mode="json")[key],
                "after": new.model_dump(mode="json")[key],
            }
            for key in changed
        },
        "expected_current": fingerprint(old),
        "restart_required": new.thermostat.entity_id != c.running_entity,
    }


@router.post("/api/config/import")
async def import_config(body: ImportRequest, request: Request, c: CtxD, p: Owner) -> dict[str, Any]:
    new = checked_settings(body.document)
    async with c.kill_lock:
        state = db.read_bot_state(c.conn)
        if state is None or state.kill_switch != "off":
            raise HTTPException(409, "Stop the bot before importing configuration")
        if body.expected_current != fingerprint(load_settings(c)):
            raise HTTPException(409, "Configuration changed; preview the file again")
        path = create_backup(c, "pre-import")
        await settings_put(new.model_dump(mode="json"), request, c, p)
        c.audit(p, "config_import", request, backup=path.name)
    return {
        "ok": True,
        "backup": path.name,
        "restart_required": new.thermostat.entity_id != c.running_entity,
    }


@router.post("/api/backups")
async def backup_now(request: Request, c: CtxD, p: Owner) -> dict[str, str]:
    path = create_backup(c, "manual")
    c.audit(p, "backup_create", request, filename=path.name)
    return {"filename": path.name}


@router.get("/api/backups/{filename}")
async def download_backup(filename: str, request: Request, c: CtxD, p: Owner) -> FileResponse:
    if not BACKUP_NAME.fullmatch(filename):
        raise HTTPException(404, "Backup not found")
    path = backup_dir(c) / filename
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "Backup not found")
    c.audit(p, "backup_download", request, filename=filename)
    return FileResponse(
        path, filename=filename, media_type="application/octet-stream", headers=NO_CACHE
    )


@router.get("/api/recovery")
async def recovery_status(c: CtxD, _: Owner) -> dict[str, Any]:
    directory = backup_dir(c)
    return {
        "ha": c.ha_health,
        "heartbeat_configured": c.heartbeat_configured,
        "state": c.eff().state,
        "backups": [
            {"filename": p.name, "bytes": p.stat().st_size}
            for p in sorted(
                directory.glob("thermabot-*.db"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            if p.is_file() and not p.is_symlink() and BACKUP_NAME.fullmatch(p.name)
        ][:30],
        "automatic_backup": "Nightly at 03:30 America/New_York; seven daily copies retained",
    }


@router.get("/api/recovery/ha-package")
async def ha_package(_: Owner) -> Response:
    return Response(
        (Path(__file__).parent / "ha_survivability.yaml").read_text(encoding="utf-8"),
        media_type="application/yaml",
        headers={
            **NO_CACHE,
            "Content-Disposition": 'attachment; filename="thermabot_survivability.yaml"',
        },
    )
