"""HA add-on option -> thermabot env var mapping (stage 6).

Reads /data/options.json (Supervisor's add-on options store, written from config.yaml's
`schema`) plus /data/env (SUPERVISOR_TOKEN, SUPERVISOR_API) and writes a plain `KEY=value`
env file that `run.sh` sources before exec'ing uvicorn. Additive, no app code changes: this
only produces the same env vars thermabot/config.py (Env) already reads.

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

OPTIONS_PATH = "/data/options.json"
OUT_PATH = "/data/env.generated"

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
}


def _quote(v: str) -> str:
    # Values go through a POSIX `source`; keep this simple and safe (single-quote, escape ').
    return "'" + v.replace("'", "'\\''") + "'"


def main() -> int:
    try:
        with open(OPTIONS_PATH, encoding="utf-8") as f:
            options = json.load(f)
    except FileNotFoundError:
        options = {}

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
    sup_token = os.environ.get("SUPERVISOR_TOKEN", "")
    if sup_token and not options.get("ha_url") and not options.get("ha_token"):
        lines.append("export HA_URL='http://supervisor/core'")
        lines.append(f"export HA_TOKEN={_quote(sup_token)}")

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
