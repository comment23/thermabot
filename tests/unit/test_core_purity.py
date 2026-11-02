"""T1.19: thermabot/core is pure (D\x151.2). AST scan of every core module."""

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[2] / "thermabot" / "core"
BANNED = (
    "thermabot.adapters",
    "thermabot.app",
    "httpx",
    "sqlite3",
    "websockets",
    "requests",
    "aiohttp",
    "socket",
    "subprocess",
)
FILES = sorted(CORE.rglob("*.py"))


def violations(src: str) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(src)):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                out.append("relative import")
            mods = [node.module or ""]
        for m in mods:
            if any(m == b or m.startswith(b + ".") for b in BANNED):
                out.append(f"import {m}")
            elif m.startswith("thermabot.") and not (
                m == "thermabot.config" or m.startswith("thermabot.core")
            ):
                out.append(f"import {m}")
        if isinstance(node, ast.Attribute):
            base = node.value
            name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
            if node.attr in ("now", "utcnow", "today") and name in ("datetime", "date"):
                out.append(f"{name}.{node.attr}")
            if node.attr in ("environ", "getenv"):
                out.append(f"os.{node.attr}")
            if name == "time" and node.attr in ("time", "monotonic", "perf_counter"):
                out.append(f"time.{node.attr}")
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
            out.append("open()")
    return out


@pytest.mark.brd("NFR-SEC-5")
def test_core_files_found() -> None:
    assert len(FILES) >= 15


@pytest.mark.brd("NFR-SEC-5")
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_core_module_pure(path: Path) -> None:
    assert violations(path.read_text(encoding="utf-8")) == []


@pytest.mark.brd("NFR-SEC-5")
def test_scanner_catches_violations() -> None:
    src = (
        "import os, httpx\nimport sqlite3\nfrom datetime import datetime\n"
        "from thermabot.adapters import ha_rest\nfrom thermabot.app import x\n"
        "from . import y\nimport websockets.client\n"
        "a = datetime.now()\nb = os.environ\nc = os.getenv('X')\nd = open('f')\n"
    )
    v = violations(src)
    for want in (
        "import httpx",
        "import sqlite3",
        "import thermabot.adapters",
        "import thermabot.app",
        "relative import",
        "import websockets.client",
        "datetime.now",
        "os.environ",
        "os.getenv",
        "open()",
    ):
        assert want in v, want
    assert violations("from thermabot.config import Settings\n") == []
