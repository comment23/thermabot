"""T1.47: example deploy files parse; secrets are placeholders and deployment is source-based."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from thermabot.config import Env

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
SECRETS = (
    "HA_TOKEN",
    "INFLUX_READ_TOKEN",
    "INFLUX_WRITE_TOKEN",
    "ALERT_WEBHOOK_TOKEN",
    "BOB_TOKEN",
)


def _env_example() -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if ln.strip() and not ln.lstrip().startswith("#"):
            k, _, v = ln.partition("=")
            out[k.strip()] = v.strip()
    return out


@pytest.mark.brd("NFR-REL-2")
def test_compose() -> None:
    svc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["thermabot"]
    assert svc["restart"] == "unless-stopped"
    assert svc["volumes"] == ["../data:/data"]
    assert "8095:8095" in svc["ports"]
    assert svc["image"] == "thermabot:0.6.8-rc.4"
    assert svc["build"]["context"] == ".."
    assert svc["build"]["dockerfile"] == "Dockerfile"
    assert svc["build"]["args"]["VERSION"] == "v0.6.8-rc.4"
    assert svc["env_file"] == "../.env"
    assert not set(svc.get("environment", {})) & set(SECRETS)  # secrets only via .env


def test_addon_config_marks_this_build_as_prerelease() -> None:
    cfg = yaml.safe_load((ROOT / "addon/config.yaml").read_text(encoding="utf-8"))
    from thermabot import __version__

    assert cfg["version"].startswith(f"{__version__}-rc.")


@pytest.mark.brd("D\u00a714.1")
def test_env_example_lists_every_env_var() -> None:
    ex = _env_example()
    assert set(ex) == set(Env.model_fields)
    for s in SECRETS:
        assert ex[s] == "" or ex[s].startswith("replace-with-"), s


@pytest.mark.brd("NFR-SEC-3")
@pytest.mark.parametrize("path", [COMPOSE, ENV_EXAMPLE])
def test_no_secret_values(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}", text)  # JWT (HA long-lived token)
    assert not re.search(r"[A-Za-z0-9_\-]{40,}", text)  # long opaque tokens
    assert "Bearer " not in text
