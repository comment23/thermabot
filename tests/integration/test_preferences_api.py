from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from tests.house import house_settings
from tests.integration.test_api import NOW, login, make
from thermabot.adapters import db
from thermabot.app.room_insights import fingerprint
from thermabot.config import Settings
from thermabot.core.statemachine import Persisted


def choices(settings: Settings) -> dict[str, object]:
    return {
        "ideal_f": 73,
        "priority": "comfort",
        "home_rooms": ["study"],
        "sleep_rooms": ["bedroom_1"],
        "confirmed_mappings": True,
        "fingerprint": fingerprint(settings),
    }


@pytest.mark.brd("FR-CFG-1")
@pytest.mark.parametrize("mode", ["shadow", "active"])
def test_preview_and_reviewed_apply_preserve_mode_and_guarded_configuration(
    tmp_path: Path,
    mode: Literal["shadow", "active"],
) -> None:
    ctx, _, client = make(tmp_path, settings=house_settings())
    db.write_bot_state(ctx.conn, Persisted(kill_switch="on", run_mode=mode), by="test", now=NOW)
    headers = login(client, "ann")
    body = choices(ctx.settings)
    before = ctx.settings.model_dump()
    state = db.read_bot_state(ctx.conn)
    replans: list[int] = []
    ctx.replan = lambda: replans.append(1)
    changes = ctx.conn.total_changes
    preview = client.post("/api/settings/preferences/preview", json=body, headers=headers).json()
    assert preview["home"] == [72, 74] and preview["automatic"] == [72, 75]
    assert preview["changed"] and ctx.conn.total_changes == changes
    assert ctx.settings.model_dump() == before and replans == []
    applied = client.post(
        "/api/settings/preferences/apply",
        headers=headers,
        json={
            **body,
            "preview_fingerprint": preview["preview_fingerprint"],
        },
    )
    assert applied.status_code == 200
    assert ctx.settings.seasonal_control.enabled and ctx.settings.seasonal_control.home == (72, 74)
    assert ctx.settings.weights["home"] == {"study": 1}
    assert ctx.settings.weights["sleep"] == {"bedroom_1": 1}
    for section in (
        "safety",
        "writes",
        "off",
        "thermostat",
        "rooms",
        "schedule",
        "bands",
        "sensors",
    ):
        assert ctx.settings.model_dump()[section] == before[section]
    assert ctx.settings.weights["away"] == before["weights"]["away"]
    assert db.read_bot_state(ctx.conn) == state and replans == [1]
    assert ctx.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0
    changes = ctx.conn.total_changes
    matching = client.post(
        "/api/settings/preferences/preview", headers=headers, json=choices(ctx.settings)
    ).json()
    assert not matching["changed"] and ctx.conn.total_changes == changes
    assert (
        client.post(
            "/api/settings/preferences/apply",
            headers=headers,
            json={
                **choices(ctx.settings),
                "preview_fingerprint": matching["preview_fingerprint"],
            },
        ).status_code
        == 200
    )
    assert ctx.conn.total_changes == changes and replans == [1]


@pytest.mark.brd("NFR-SEC-1")
@pytest.mark.parametrize("action", ["preview", "apply"])
def test_preferences_require_permissions_csrf_and_confirmed_sensor_assignments(
    tmp_path: Path,
    action: str,
) -> None:
    ctx, _, client = make(tmp_path, settings=house_settings())
    body = choices(ctx.settings)
    endpoint = f"/api/settings/preferences/{action}"
    if action == "apply":
        body["preview_fingerprint"] = "0" * 64
    assert client.post(endpoint, json=body).status_code == 401
    member = login(client, "jordan")
    assert client.post(endpoint, json=body, headers=member).status_code == 403
    admin = login(client, "ann")
    assert client.post(endpoint, json=body).status_code == 403
    before = ctx.settings.model_dump()
    for altered in (
        {**body, "confirmed_mappings": False},
        {**body, "ideal_f": 79},
        {**body, "home_rooms": []},
        {**body, "safety": {"floor_tstat_f": 55}},
        {**body, "home_rooms": ["unknown"]},
    ):
        assert client.post(endpoint, json=altered, headers=admin).status_code == 422
    assert ctx.settings.model_dump() == before
    assert ctx.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0


@pytest.mark.brd("FR-CFG-1")
def test_stale_settings_or_altered_preferences_require_another_preview(tmp_path: Path) -> None:
    ctx, _, client = make(tmp_path, settings=house_settings())
    headers = login(client, "ann")
    body = choices(ctx.settings)
    result = client.post("/api/settings/preferences/preview", json=body, headers=headers).json()
    apply = {**body, "preview_fingerprint": result["preview_fingerprint"]}
    assert (
        client.post(
            "/api/settings/preferences/apply",
            headers=headers,
            json={**apply, "priority": "efficiency"},
        ).status_code
        == 409
    )
    changed = ctx.settings.model_dump()
    changed["insights"]["currency"] = "CAD"
    assert client.put("/api/settings", json=changed, headers=headers).status_code == 200
    assert (
        client.post("/api/settings/preferences/apply", json=apply, headers=headers).status_code
        == 409
    )
    assert (
        client.post("/api/settings/preferences/preview", json=body, headers=headers).status_code
        == 409
    )
    assert not ctx.settings.seasonal_control.enabled
