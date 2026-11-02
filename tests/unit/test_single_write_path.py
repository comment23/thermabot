"""T1.29: single write path (NFR-SAFE-5, D§6). AST check over production code
(thermabot/ + scripts/): only app/controller.py may import or reference `_call_service`.
The lower-level `_post_service` may be used only by ha_rest itself and the supervised probe."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCOPES = [ROOT / "thermabot", ROOT / "scripts"]
CALL_OK = {"thermabot/adapters/ha_rest.py", "thermabot/app/controller.py"}
POST_OK = {"thermabot/adapters/ha_rest.py", "scripts/probe_thermostat.py"}


def uses(tree: ast.AST, name: str) -> bool:
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and any(a.name == name for a in n.names):
            return True
        if isinstance(n, ast.Name) and n.id == name:
            return True
        if isinstance(n, ast.Attribute) and n.attr == name:
            return True
        if isinstance(n, ast.Constant) and n.value == name:  # getattr(mod, "_call_service")
            return True
    return False


def offenders(name: str, ok: set[str]) -> list[str]:
    out = []
    for scope in SCOPES:
        for f in scope.rglob("*.py"):
            rel = f.relative_to(ROOT).as_posix()
            if rel not in ok and uses(ast.parse(f.read_text(encoding="utf-8")), name):
                out.append(rel)
    return sorted(out)


@pytest.mark.brd("NFR-SAFE-5")
def test_only_controller_uses_call_service() -> None:
    assert offenders("_call_service", CALL_OK) == []


@pytest.mark.brd("NFR-SAFE-5")
def test_controller_actually_imports_it() -> None:
    tree = ast.parse((ROOT / "thermabot/app/controller.py").read_text(encoding="utf-8"))
    assert any(
        isinstance(n, ast.ImportFrom)
        and n.module == "thermabot.adapters.ha_rest"
        and any(a.name == "_call_service" for a in n.names)
        for n in ast.walk(tree)
    )


@pytest.mark.brd("NFR-SAFE-5")
def test_post_service_not_used_elsewhere() -> None:
    assert offenders("_post_service", POST_OK) == []


@pytest.mark.brd("NFR-SAFE-5")
def test_detector_catches_violations() -> None:
    for src in (
        "from thermabot.adapters.ha_rest import _call_service",
        "import thermabot.adapters.ha_rest as h\nh._call_service(1, 2)",
        "getattr(h, '_call_service')",
    ):
        assert uses(ast.parse(src), "_call_service")
    assert not uses(ast.parse("from thermabot.adapters.ha_rest import HARest"), "_call_service")
