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
# An explicit add-on timezone option is exported by options_to_env.py. Otherwise drop
# a base-image TZ so the app follows Home Assistant's time_zone, then UTC.
if ! grep -q '^export TZ=' /data/env.generated; then
  unset TZ
fi

exec /venv/bin/uvicorn thermabot.app.main:app \
    --host 0.0.0.0 --port 8095 --workers 1 --no-access-log
