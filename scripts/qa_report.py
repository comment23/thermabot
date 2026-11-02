"""QA report (D§16): JUnit XML (+ brd properties from tests/conftest.py) -> qa-report.md.

Every BRD §8 row is listed with pass/fail/not-covered. Rows map to the requirement IDs the
tests carry in @pytest.mark.brd(...). Usage:
    python scripts/qa_report.py junit-unit.xml junit-integration.xml ... -o qa-report.md
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# BRD §8.1 / §8.2 row -> requirement IDs whose tests evidence it.
ROWS: dict[str, tuple[str, ...]] = {
    "8.1 Kill switch": ("FR-KILL-1", "FR-KILL-2", "FR-KILL-3", "FR-KILL-4", "NFR-SAFE-1"),
    "8.1 Write guards": ("NFR-SAFE-2", "NFR-SAFE-5", "NFR-SAFE-6", "FR-HA-8"),
    "8.1 off guardrails": ("NFR-SAFE-3", "NFR-SAFE-10", "FR-PLN-5"),
    "8.1 HA watchdog": ("NFR-SAFE-8",),
    "8.1 Startup reconcile": ("NFR-SAFE-9",),
    "8.1 Override": ("FR-OVR-1", "FR-OVR-2", "FR-OVR-3"),
    "8.1 Planner": ("FR-PLAN-1", "FR-PLN-1", "FR-PLN-3", "FR-PLN-6", "FR-PLN-7"),
    "8.1 Comfort score": ("FR-CMF-1", "FR-CMF-2", "FR-CMF-3"),
    "8.1 Space-heater detection": ("FR-CMF-5", "FR-CMF-6"),
    "8.1 Member nudge": ("FR-OVR-4", "FR-CMF-4"),
    "8.1 Fan circulation": ("FR-FAN-1", "FR-FAN-2"),
    "8.1 Presence": ("FR-PRS-1", "FR-PRS-2", "FR-PRS-3", "FR-PRS-3a", "FR-PRS-4"),
    "8.1 Weather": ("FR-WX-1", "FR-WX-2", "FR-WX-3"),
    "8.1 HA connection": ("FR-HA-1", "FR-HA-2", "FR-HA-3", "FR-HA-4", "NFR-REL-3"),
    "8.1 Metrics": ("BRD-7.2", "FR-MODEL-2"),
    "8.1 Alerts & weekly summary": ("FR-ALR-1", "FR-ALR-2", "FR-SUM-1"),
    "8.1 Auth/roles": ("FR-USR-1", "FR-USR-2", "NFR-SEC-1", "NFR-SEC-2", "NFR-SEC-4"),
    "8.1 Crash": ("NFR-REL-2",),
    "8.2 P1 Dashboard": ("P1",),
    "8.2 P2 Plan & Timeline": ("P2",),
    "8.2 P3 Settings": ("P3",),
    "8.2 P4 History & Savings": ("P4",),
    "8.2 P5 Model": ("P5",),
    "8.2 P6 Health": ("P6",),
    "8.2 P7 Users": ("P7",),
    "8.2 P8 My Room": ("P8",),
    "8.2 All pages": ("FR-UI-1", "FR-UI-2"),
}
PAGE_FILES = {f"P{i}": f"test_p{i}_" for i in range(1, 9)}


def parse(paths: list[Path]) -> list[tuple[str, set[str], str]]:
    """(node id, brd ids, outcome in pass|fail|skip) per testcase."""
    out = []
    for p in paths:
        for tc in ET.parse(p).getroot().iter("testcase"):
            node = f"{tc.get('classname', '')}::{tc.get('name', '')}"
            ids = {pr.get("value", "") for pr in tc.iter("property") if pr.get("name") == "brd"}
            ids |= {page for page, stem in PAGE_FILES.items() if stem in node}
            outcome = (
                "fail"
                if tc.find("failure") is not None or tc.find("error") is not None
                else "skip"
                if tc.find("skipped") is not None
                else "pass"
            )
            out.append((node, ids, outcome))
    return out


def report(cases: list[tuple[str, set[str], str]]) -> tuple[str, bool]:
    by_id: dict[str, list[str]] = defaultdict(list)
    for _, ids, outcome in cases:
        for i in ids:
            by_id[i].append(outcome)
    lines = [
        "# thermabot QA report (BRD §8)",
        "",
        "| Row | Result | Tests (pass/fail) |",
        "|---|---|---|",
    ]
    ok = True
    for row, ids in ROWS.items():
        outs = [o for i in ids for o in by_id.get(i, [])]
        n_pass, n_fail = outs.count("pass"), outs.count("fail")
        res = "FAIL" if n_fail else "PASS" if n_pass else "NOT COVERED"
        ok = ok and res != "FAIL"
        lines.append(f"| {row} | {res} | {n_pass}/{n_fail} |")
    total = len(cases)
    fails = [n for n, _, o in cases if o == "fail"]
    lines += ["", f"Total testcases: {total}; failed: {len(fails)}"]
    lines += [f"- FAIL {n}" for n in fails]
    return "\n".join(lines) + "\n", ok and not fails


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("junit", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("qa-report.md"))
    a = ap.parse_args(argv)
    text, ok = report(parse([p for p in a.junit if p.exists()]))
    a.out.write_text(text, encoding="utf-8")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
