"""Configuration round trips, offline-copy integrity and owner-only recovery boundaries."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.integration.test_api import login, make
from thermabot import __version__
from thermabot.app.routes_api import load_settings


@pytest.mark.parametrize("user", ["ann", "jordan"])
def test_recovery_is_owner_only(tmp_path: Path, user: str) -> None:
    _, _, client = make(tmp_path)
    headers = login(client, user)
    for path in ("/recovery", "/api/recovery", "/api/config/export", "/api/recovery/ha-package"):
        assert client.get(path).status_code == 403
    for path in ("/api/config/preview", "/api/config/import", "/api/backups"):
        assert client.post(path, headers=headers, json={}).status_code == 403


def test_export_and_preview_do_not_change_settings(tmp_path: Path) -> None:
    ctx, _, client = make(tmp_path)
    headers = login(client, "alex")
    before = load_settings(ctx)
    response = client.get("/api/config/export")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    document = response.json()
    assert document["format"] == "thermabot-config"
    assert document["app_version"] == __version__
    assert document["settings"] == before.model_dump(mode="json")
    assert "HA_TOKEN" not in response.text and "SESSION_SECRET" not in response.text
    preview = client.post("/api/config/preview", json={"document": document}, headers=headers)
    assert preview.status_code == 200
    assert preview.json()["changed_sections"] == []
    assert load_settings(ctx) == before
    assert not list(tmp_path.glob("backup/*.db"))
    assert client.post("/api/config/preview", json={"document": document}).status_code == 403
    assert client.post("/api/backups").status_code == 403


@pytest.mark.parametrize("kind", ["version", "unknown", "nested", "missing", "unsafe"])
def test_invalid_import_rejected_without_writes(tmp_path: Path, kind: str) -> None:
    ctx, _, client = make(tmp_path)
    headers = login(client, "alex")
    before = load_settings(ctx)
    document = client.get("/api/config/export").json()
    if kind == "version":
        document["schema_version"] = 999
    elif kind == "unknown":
        document["settings"]["HA_TOKEN"] = "uploaded-secret-must-not-be-echoed"
    elif kind == "nested":
        document["settings"]["thermostat"]["surprise"] = "uploaded-secret-must-not-be-echoed"
    elif kind == "missing":
        del document["settings"]["safety"]
    else:
        document["settings"]["safety"]["floor_f"] = 200
    response = client.post("/api/config/preview", json={"document": document}, headers=headers)
    assert response.status_code == 422
    assert "uploaded-secret-must-not-be-echoed" not in response.text
    assert load_settings(ctx) == before


def test_import_requires_stopped_bot_preview_and_preimport_backup(tmp_path: Path) -> None:
    ctx, _, client = make(tmp_path)
    headers = login(client, "alex")
    old = load_settings(ctx)
    document = client.get("/api/config/export").json()
    document["settings"]["thermostat"]["entity_id"] = "climate.new"
    preview = client.post("/api/config/preview", json={"document": document}, headers=headers)
    assert preview.status_code == 200
    assert preview.json()["changed_sections"] == ["thermostat"]
    assert preview.json()["restart_required"]
    body = {"document": document, "expected_current": preview.json()["expected_current"]}
    assert client.post("/api/config/import", json=body, headers=headers).status_code == 409
    assert client.post("/api/kill", json={"enabled": False}, headers=headers).status_code == 200
    response = client.post("/api/config/import", json=body, headers=headers)
    assert response.status_code == 200 and response.json()["restart_required"]
    assert ctx.eff().state == "DISABLED"
    assert load_settings(ctx).thermostat.entity_id == "climate.new"
    assert ctx.running_entity != "climate.new"
    backup = tmp_path / "backup" / response.json()["backup"]
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        row = conn.execute("SELECT value_json FROM settings WHERE key='settings'").fetchone()
        assert row is None or json.loads(row[0]) == old.model_dump(mode="json")
    assert client.post("/api/config/import", json=body, headers=headers).status_code == 409


def test_backup_failure_prevents_import(tmp_path: Path) -> None:
    ctx, _, client = make(tmp_path)
    headers = login(client, "alex")
    document = client.get("/api/config/export").json()
    document["settings"]["thermostat"]["entity_id"] = "climate.new"
    preview = client.post("/api/config/preview", json={"document": document}, headers=headers)
    client.post("/api/kill", json={"enabled": False}, headers=headers)
    with patch("thermabot.app.recovery.db.backup", side_effect=sqlite3.OperationalError("full")):
        response = client.post(
            "/api/config/import",
            json={"document": document, "expected_current": preview.json()["expected_current"]},
            headers=headers,
        )
    assert response.status_code == 503
    assert load_settings(ctx).thermostat.entity_id != "climate.new"
    assert not list(tmp_path.glob("backup/*.db"))


def test_backup_download_is_a_verified_complete_database(tmp_path: Path) -> None:
    ctx, _, client = make(tmp_path)
    headers = login(client, "alex")
    response = client.post("/api/backups", headers=headers)
    assert response.status_code == 200
    name = response.json()["filename"]
    download = client.get(f"/api/backups/{name}")
    assert download.status_code == 200
    assert download.headers["cache-control"] == "no-store"
    assert download.headers["content-disposition"].startswith("attachment;")
    restored = tmp_path / "copy.db"
    restored.write_bytes(download.content)
    with sqlite3.connect(restored) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    status = client.get("/api/recovery").json()
    assert status["backups"][0]["filename"] == name
    assert not status["heartbeat_configured"]
    assert ctx.eff().state == "SHADOW"
    assert client.get("/api/backups/api.db").status_code == 404
    assert client.get("/api/backups/nonexistent.db").status_code == 404
    login(client, "ann")
    assert client.get(f"/api/backups/{name}").status_code == 403


def test_ha_package_and_version_on_login(tmp_path: Path) -> None:
    _, _, client = make(tmp_path)
    assert f"v{__version__}" in client.get("/login").text
    assert client.get("/api/recovery/ha-package").status_code == 401
    login(client, "alex")
    package = client.get("/api/recovery/ha-package")
    assert package.status_code == 200
    assert "input_boolean.turn_off" in package.text
    assert "climate.set_" not in package.text
    assert "initial: false" in package.text and "CHANGE_ME_" in package.text
