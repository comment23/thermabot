#!/command/with-contenv sh
# HA add-on entrypoint. Maps Supervisor's add-on options (/data/options.json) to the env
# vars thermabot/config.py already reads, then execs the same app as the plain-Docker image.
# DATA_DIR=/data is HA's add-on data volume.
#
# The hassio base image runs this file as the s6 CMD (entrypoint /init). s6-overlay 3.2
# keeps S6_KEEP_ENV at 0, so a `#!/usr/bin/env sh` script does not see SUPERVISOR_TOKEN
# even though the token is in /run/s6/container_environment. with-contenv copies that
# directory into this process. The base image's own services use the same shebang.
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
