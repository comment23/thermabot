"""Stand-in for Supervisor and Home Assistant during the add-on startup check.

Listens on port 80. Records each request as ``METHOD path auth_ok=0|1`` without
writing the token. The add-on and the base image's s6 scripts both call this host
as ``supervisor``.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("STUB_TOKEN", "ci-supervisor-token")
LOG_PATH = os.environ.get("STUB_LOG", "")

_ADDON = {
    "name": "thermabot",
    "description": "startup check",
    "version": "0.6.8-rc.8",
    "version_latest": "0.6.8-rc.8",
    "update_available": False,
    "slug": "thermabot",
    "hostname": "thermabot",
}
_INFO = {
    "supervisor": "2026.11.0",
    "homeassistant": "2026.11.0",
    "operating_system": "Home Assistant OS",
    "arch": "amd64",
    "machine": "qemux86-64",
}
_OPTIONS = {
    "log_level": "info",
    "timezone": "",
    "ha_url": "",
    "ha_token": "",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        path = self.path.split("?", 1)[0]
        auth = self.headers.get("Authorization", "")
        ok = auth == f"Bearer {TOKEN}"
        if LOG_PATH:
            with open(LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(f"{self.command} {path} auth_ok={int(ok)}\n")
        body = json.dumps(self._body(path)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self, path: str) -> dict[str, object]:
        if path == "/supervisor/ping":
            return {"result": "ok", "data": {}}
        if path == "/addons/self/options/config":
            return {"result": "ok", "data": _OPTIONS}
        if path == "/addons/self/info":
            return {"result": "ok", "data": _ADDON}
        if path == "/info":
            return {"result": "ok", "data": _INFO}
        if path == "/core/api/config":
            return {"version": "2026.11.0", "time_zone": "UTC", "location_name": "stub"}
        return {"result": "ok", "data": {}}

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", 80), Handler).serve_forever()


if __name__ == "__main__":
    main()
