#!/bin/sh
# Run a built add-on image the way Supervisor does: SUPERVISOR_TOKEN is set, HA_TOKEN
# is not, and ha_url/ha_token options are blank. The process must get past env
# validation and call Home Assistant with that Supervisor token.
set -eu

IMAGE=${1:?image ref}
TOKEN=${ADDON_STUB_TOKEN:-ci-supervisor-token}
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
LOG=$WORK/stub.log
NAME=thermabot-addon-startup-$$
STUB_PID=""

cleanup() {
    if [ -n "$STUB_PID" ]; then
        kill "$STUB_PID" 2>/dev/null || true
        wait "$STUB_PID" 2>/dev/null || true
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/data"
python3 - "$WORK/data/options.json" <<'PY'
import json
import sys

# Same blank advanced options a user leaves empty so the Supervisor token is used.
options = {
    "log_level": "info",
    "timezone": "",
    "sensor_blower": "",
    "sensor_compressor": "",
    "sensor_outdoor": "",
    "influx_url": "",
    "influx_org": "",
    "influx_read_token": "",
    "influx_write_token": "",
    "influx_read_bucket": "",
    "influx_write_bucket": "",
    "heartbeat_webhook_url": "",
    "alert_webhook_url": "",
    "alert_webhook_token": "",
    "bob_token": "",
    "ha_url": "",
    "ha_token": "",
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(options, handle)
PY

STUB_TOKEN=$TOKEN STUB_LOG=$LOG python3 "$ROOT/scripts/addon_supervisor_stub.py" &
STUB_PID=$!
i=0
while [ "$i" -lt 50 ]; do
    if python3 -c 'import socket; s=socket.create_connection(("127.0.0.1", 80), 0.2); s.close()' \
        >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 0.1
done

docker run -d --name "$NAME" \
    --add-host supervisor:host-gateway \
    -e SUPERVISOR_TOKEN="$TOKEN" \
    -v "$WORK/data:/data" \
    "$IMAGE" >/dev/null

ok=0
i=0
while [ "$i" -lt 45 ]; do
    logs=$(docker logs "$NAME" 2>&1 || true)
    if printf '%s\n' "$logs" | grep -q "missing or invalid environment"; then
        printf '%s\n' "$logs"
        echo "add-on exited during env validation" >&2
        exit 1
    fi
    # Uvicorn only starts after env validation. The stub line is the HA connect
    # (GET /core/api/config) made with the Supervisor token, not a pasted HA token.
    if printf '%s\n' "$logs" | grep -q "Uvicorn running" \
        && grep -q "GET /core/api/config auth_ok=1" "$LOG"; then
        ok=1
        break
    fi
    i=$((i + 1))
    sleep 1
done

if [ "$ok" -ne 1 ]; then
    echo "add-on did not reach Home Assistant with the Supervisor token" >&2
    docker logs "$NAME" 2>&1 || true
    echo "stub log:" >&2
    cat "$LOG" >&2 || true
    exit 1
fi

echo "add-on startup accepted SUPERVISOR_TOKEN and called Home Assistant"
