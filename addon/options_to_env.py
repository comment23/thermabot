"""HA add-on option -> thermabot env var mapping (stage 6).

Reads /data/options.json (Supervisor's add-on options store, written from config.yaml's
`schema`) and SUPERVISOR_TOKEN, then writes a plain `KEY=value` env file that `run.sh`
sources before exec'ing uvicorn. Additive, no app code changes: this only produces the
same env vars thermabot/config.py (Env) already reads.

Pattern: standard HA add-on auth. With `homeassistant_api: true` (addon/config.yaml),
Supervisor injects SUPERVISOR_TOKEN and proxies http://supervisor/core/api to HA's own API,
so the user never pastes a long-lived token. HA_URL/HA_TOKEN options are an explicit escape
hatch only (e.g. pointing at a different HA instance); when left blank this script fills them
in from the Supervisor token/proxy instead of asking for one.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

OPTIONS_PATH = "/data/options.json"
OUT_PATH = "/data/env.generated"
# s6-overlay stores the container environment here and does not export it into the
# CMD process unless the script is started with with-contenv. Read the file too, so a
# direct invocation still sees the Supervisor token.
S6_ENV_DIR = Path("/run/s6/container_environment")

# options.json key -> thermabot Env var. Values that are empty strings are omitted entirely,
# so thermabot/config.py's own defaults (e.g. HA_URL's) still apply when nothing is set.
_MAP = {
    "sensor_blower": "THERMABOT_SENSOR_BLOWER",
    "sensor_compressor": "THERMABOT_SENSOR_COMPRESSOR",
    "sensor_outdoor": "THERMABOT_SENSOR_OUTDOOR",
    "influx_url": "INFLUX_URL",
    "influx_org": "INFLUX_ORG",
    "influx_read_token": "INFLUX_READ_TOKEN",
    "influx_write_token": "INFLUX_WRITE_TOKEN",
    "influx_read_bucket": "INFLUX_READ_BUCKET",
    "influx_write_bucket": "INFLUX_WRITE_BUCKET",
    "heartbeat_webhook_url": "HEARTBEAT_WEBHOOK_URL",
    "alert_webhook_url": "ALERT_WEBHOOK_URL",
    "alert_webhook_token": "ALERT_WEBHOOK_TOKEN",
    "bob_token": "BOB_TOKEN",
    "ha_url": "HA_URL",
    "ha_token": "HA_TOKEN",
    "log_level": "LOG_LEVEL",
    "timezone": "TZ",
}


def _quote(v: str) -> str:
    # Values go through a POSIX `source`; keep this simple and safe (single-quote, escape ').
    return "'" + v.replace("'", "'\\''") + "'"


def _s6_value(name: str, s6_dir: Path) -> str:
    try:
        raw = (s6_dir / name).read_text(encoding="utf-8")
    except OSError:
        return ""
    if raw.endswith("\n"):
        raw = raw[:-1]
    return raw


def container_value(name: str, environ: Mapping[str, str], s6_dir: Path = S6_ENV_DIR) -> str:
    """Process environment first, then the s6 container-environment file."""
    value = environ.get(name, "")
    if value:
        return value
    return _s6_value(name, s6_dir)


def render(
    options: Mapping[str, object],
    environ: Mapping[str, str],
    s6_dir: Path = S6_ENV_DIR,
) -> str:
    lines = []
    for opt_key, env_key in _MAP.items():
        v = options.get(opt_key)
        if v in (None, ""):
            continue
        if opt_key == "log_level":
            v = str(v).upper()
        lines.append(f"export {env_key}={_quote(str(v))}")

    # Supervisor token auth: fill HA_URL/HA_TOKEN from the Supervisor proxy unless the user
    # set an explicit override above (ha_url/ha_token options).
    sup_token = container_value("SUPERVISOR_TOKEN", environ, s6_dir)
    if sup_token and not options.get("ha_url") and not options.get("ha_token"):
        lines.append("export HA_URL='http://supervisor/core'")
        lines.append(f"export HA_TOKEN={_quote(sup_token)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        with open(OPTIONS_PATH, encoding="utf-8") as f:
            options = json.load(f)
    except FileNotFoundError:
        options = {}
    if not isinstance(options, dict):
        print("options.json must be an object", file=sys.stderr)
        return 1
    text = render(options, os.environ)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
