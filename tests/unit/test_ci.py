"""T1.46 local substitute (no push): workflow YAML parses, D§16 jobs exist; qa_report maps rows."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from scripts.qa_report import ROWS, main, parse, report

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.brd("NFR-SEC-5")
def test_ci_workflow_has_all_jobs() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = wf["jobs"]
    want = {"lint", "unit", "integration", "e2e", "security", "gitleaks", "image", "qa-report"}
    assert want <= set(jobs)
    assert set(jobs["image"]["needs"]) == {
        "lint",
        "unit",
        "integration",
        "e2e",
        "security",
        "gitleaks",
    }
    assert "refs/tags/v" in jobs["image"]["if"]
    lint_commands = [step.get("run") for step in jobs["lint"]["steps"]]
    assert "mypy --strict thermabot" in lint_commands
    steps = " ".join(str(s) for s in jobs["unit"]["steps"])
    assert "--cov-fail-under=90" in steps and "--junitxml" in steps
    sec = " ".join(str(s) for s in jobs["security"]["steps"])
    assert (
        "pip-audit" in sec
        and "requirements.runtime.lock" in sec
        and "gitleaks" in sec
        and "trivy" in sec
        and "addon/Dockerfile" in sec
    )
    gl = " ".join(str(s) for s in jobs["gitleaks"]["steps"])
    assert "gitleaks dir ." in gl and "sha256sum -c" in gl and "--exit-code 1" in gl
    on = wf[True]  # YAML 1.1: the `on` key parses as True
    assert "pull_request" in on and "**" in on["push"]["branches"]
    tags = " ".join(str(s) for s in jobs["image"]["steps"])
    # v0.3.0: the dev repo pushes only to thermabot-dev (the plain name is reserved for the
    # public repo at stage 6).
    assert "ghcr.io/comment23/thermabot-dev:" in tags and "ghcr.io/comment23/thermabot:" not in tags
    assert jobs["image"]["permissions"]["packages"] == "write"
    # The plain ghcr.io/comment23/thermabot name stays reserved. Add-on images use a suffix.
    ci_text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "ghcr.io/comment23/thermabot:" not in ci_text
    for job in jobs.values():
        for step in job.get("steps") or []:
            assert "setup-qemu-action" not in str(step.get("uses") or "")


@pytest.mark.brd("NFR-SEC-5")
def test_addon_publish_uses_native_runners_and_config_version() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = wf["jobs"]
    publish = jobs["addon-publish"]
    assert "refs/heads/main" in publish["if"]
    assert "refs/tags/v" in publish["if"]
    assert "addon-config" in publish["needs"]
    rows = {row["arch"]: row for row in publish["strategy"]["matrix"]["include"]}
    assert rows["amd64"]["runner"] == "ubuntu-24.04"
    assert rows["amd64"]["platform"] == "linux/amd64"
    assert rows["aarch64"]["runner"] == "ubuntu-24.04-arm"
    assert rows["aarch64"]["platform"] == "linux/arm64"
    steps = "\n".join(str(step.get("run", "")) for step in publish["steps"])
    assert "--file addon/Dockerfile" in steps
    assert "docker build addon" not in steps
    assert 'check_addon_config.py --arch "${{ matrix.arch }}" --github-output' in steps
    assert "--set-public --require-public" in steps
    assert publish["permissions"]["packages"] == "write"
    resolve = jobs["addon-image-resolve"]
    assert resolve["needs"] == "addon-publish"
    resolve_steps = " ".join(str(step.get("run", "")) for step in resolve["steps"])
    assert "check_addon_config.py --require-public" in resolve_steps
    arm = jobs["addon-aarch64"]
    assert arm["runs-on"] == "ubuntu-24.04-arm"
    assert "pull_request" in arm["if"]
    arm_steps = "\n".join(str(step.get("run", "")) for step in arm["steps"])
    assert "--platform linux/arm64" in arm_steps
    assert "--file addon/Dockerfile" in arm_steps
    assert "BUILD_ARCH=aarch64" in arm_steps


@pytest.mark.brd("NFR-SEC-5")
def test_workflow_actions_are_pinned_to_full_commit_shas() -> None:
    paths = [ROOT / ".github/workflows/ci.yml", ROOT / ".github/workflows/codeql.yml"]
    for path in paths:
        wf = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in wf["jobs"].values():
            for step in job["steps"]:
                action = step.get("uses")
                if action:
                    assert re.fullmatch(r"[^@]+@[0-9a-f]{40}(?:\s+# .+)?", action), (
                        f"{path.relative_to(ROOT)} uses a mutable action reference: {action}"
                    )


@pytest.mark.brd("NFR-SEC-5")
def test_codeql_scans_supported_languages_with_minimal_permissions() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/codeql.yml").read_text(encoding="utf-8"))
    assert set(wf["jobs"]["analyze"]["strategy"]["matrix"]["language"]) == {
        "python",
        "javascript-typescript",
    }
    assert wf["permissions"] == {
        "actions": "read",
        "contents": "read",
        "security-events": "write",
    }


@pytest.mark.brd("NFR-SEC-5")
def test_dependabot_pip_and_actions_weekly() -> None:
    d = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    eco = {u["package-ecosystem"]: u["schedule"]["interval"] for u in d["updates"]}
    assert eco["pip"] == "weekly" and eco["github-actions"] == "weekly"


JUNIT = """<testsuites><testsuite>
<testcase classname="tests.unit.test_guards" name="a"><properties>
<property name="brd" value="NFR-SAFE-2"/></properties></testcase>
<testcase classname="tests.integration.test_kill_soak" name="b"><properties>
<property name="brd" value="FR-KILL-1"/></properties><failure message="x"/></testcase>
<testcase classname="tests.e2e.test_p7_users" name="c"/>
</testsuite></testsuites>"""


@pytest.mark.brd("NFR-SEC-5")
def test_qa_report_lists_every_brd8_row(tmp_path: Path) -> None:
    x = tmp_path / "j.xml"
    x.write_text(JUNIT, encoding="utf-8")
    text, ok = report(parse([x]))
    assert not ok
    for row in ROWS:
        assert f"| {row} |" in text
    assert "| 8.1 Write guards | PASS |" in text
    assert "| 8.1 Kill switch | FAIL |" in text
    assert "| 8.2 P7 Users | PASS |" in text
    assert "| 8.1 Crash | NOT COVERED |" in text
    assert main([str(x), "-o", str(tmp_path / "qa.md")]) == 1
    assert (tmp_path / "qa.md").exists()


@pytest.mark.brd("NFR-SEC-5")
def test_brd_marker_reaches_junit_properties(request: pytest.FixtureRequest) -> None:
    assert ("brd", "NFR-SEC-5") in request.node.user_properties
