#!/usr/bin/env sh
# HA add-on entrypoint (stage 6). Maps Supervisor's add-on options (/data/options.json) to
# the env vars thermabot/config.py already reads, then execs the SAME app as the plain-Docker
# image (uvicorn thermabot.app.main:app). DATA_DIR=/data is HA's add-on data volume, same role
# as the ./data bind mount in deploy/docker-compose.yml.
set -e

python3 /app/options_to_env.py
# shellcheck disable=SC1091
. /data/env.generated

export DATA_DIR=/data
export PORT=8095
export TZ="${TZ:-America/New_York}"

exec /venv/bin/uvicorn thermabot.app.main:app \
    --host 0.0.0.0 --port 8095 --workers 1 --no-access-log
