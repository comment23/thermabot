"""Run a MockHA on a real localhost port (uvicorn in a background thread) for WS tests."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any

import uvicorn

from tests.integration.mock_ha.app import MockHA


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class MockServer:
    """Context manager. `run(coro)` executes a MockHA coroutine on the server's loop."""

    def __init__(self, ha: MockHA | None = None) -> None:
        self.ha = ha or MockHA()
        self.port = _free_port()
        cfg = uvicorn.Config(
            self.ha.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            ws_ping_interval=None,
            lifespan="off",
        )
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.loop: asyncio.AbstractEventLoop | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/websocket"

    def __enter__(self) -> MockServer:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("mock HA did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    async def run(self, coro: Any) -> Any:
        """Await a MockHA coroutine (e.g. drop_ws) on the server thread's loop."""
        loop = self._server_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return await asyncio.wrap_future(fut)

    def _server_loop(self) -> asyncio.AbstractEventLoop:
        if self.loop is None:
            servers = getattr(self._server, "servers", [])
            if not servers:
                raise RuntimeError("server loop unavailable")
            self.loop = servers[0].get_loop()
        return self.loop
