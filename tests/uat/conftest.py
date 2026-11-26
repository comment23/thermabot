"""Fresh browser profiles and the same full-runtime lab used for manual UAT."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Browser, ConsoleMessage, Page, Route

from scripts.uat import Lab, create_lab
from tests.e2e.conftest import Srv
from tests.e2e.conftest import browser as browser  # noqa: F401


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--uat-artifacts", type=Path, help="Save local screenshots and runtime evidence"
    )


@contextmanager
def running_lab(lab: Lab) -> Iterator[Srv]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(lab.app, log_level="warning", timeout_graceful_shutdown=1)
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            for _ in range(250):
                if server.started or not thread.is_alive():
                    break
                time.sleep(0.02)
            if not server.started:
                pytest.fail("Full-runtime UAT server did not start")
            yield Srv(ctx=lab.runtime.ctx, url=f"http://127.0.0.1:{port}")
        finally:
            server.should_exit = True
            thread.join(10)
            if thread.is_alive():
                pytest.fail("UAT server did not stop; its thread is still running")


@contextmanager
def customer_page(browser: Browser, srv: Srv, width: int = 390) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5000)
    page.on("pageerror", lambda error: srv.errors.append(f"pageerror: {error}"))

    def console_error(message: ConsoleMessage) -> None:
        if message.type == "error":
            srv.errors.append(f"console: {message.text}")

    page.on("console", console_error)

    def local_only(route: Route) -> None:
        if route.request.url.startswith((srv.url + "/", "data:", "about:")):
            route.continue_()
        else:
            srv.errors.append("UAT blocked an external browser request")
            route.abort()

    context.route("**/*", local_only)
    try:
        yield page
    finally:
        context.close()


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return create_lab(tmp_path / "lab")


@pytest.fixture
def srv(lab: Lab) -> Iterator[Srv]:
    with running_lab(lab) as server:
        yield server


def record_evidence(page: Page, lab: Lab, request: pytest.FixtureRequest, name: str) -> None:
    output: Path | None = request.config.getoption("--uat-artifacts")
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_-]", "_", name)
        page.screenshot(path=str(output / f"{name}.png"), full_page=True)
        (output / f"{name}.json").write_text(json.dumps(lab.evidence(), indent=2), encoding="utf-8")


@pytest.fixture
def page(browser: Browser, srv: Srv, lab: Lab, request: pytest.FixtureRequest) -> Iterator[Page]:
    with customer_page(browser, srv) as page:
        yield page
        record_evidence(page, lab, request, request.node.name)
        evidence = lab.evidence()
        assert evidence["run_mode"] == "shadow"
        assert evidence["climate_service_calls"] == []
        assert evidence["blocked_climate_requests"] == []
        assert srv.errors == [], srv.errors
