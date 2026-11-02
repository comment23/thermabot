"""Read-only daily SHADOW report. Example: python scripts/shadow_report.py --url
http://thermabot:8095 --cookie-file thermabot-cookie.txt --assert-no-sent."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _cookie(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if value.startswith("tb_session="):
        value = value.removeprefix("tb_session=")
    if not value or any(c in value for c in "\r\n; "):
        raise ValueError("cookie file must contain only the tb_session value")
    return f"tb_session={value}"


def _fetch(base_url: str, path: str, cookie: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "application/json", "Cookie": cookie},
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{path} returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"{path} request failed: {exc}") from None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} returned invalid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} returned a non-object JSON response")
    return data


def _obj(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"response field {key!r} must be an object")
    return value


def _count(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"response field {key!r} must be a non-negative integer")
    return value


def _cell(value: object) -> str:
    return (
        str(value if value is not None else "—")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def render_report(
    status: dict[str, Any],
    health: dict[str, Any],
    shadow: dict[str, Any],
    assert_no_sent: bool = False,
) -> tuple[str, int]:
    day_utc = health.get("day_utc")
    if not isinstance(day_utc, str):
        raise ValueError("response field 'day_utc' must be a string")
    counts = _obj(health, "writes_today")
    writes = {name: _count(counts, name) for name in ("sent", "shadow", "denied", "failed")}
    degraded = _obj(health, "degraded_today")
    sample_count = _count(degraded, "sampled_minutes")
    degraded_count = _count(degraded, "degraded_minutes")
    untracked_count = _count(degraded, "untracked_minutes")
    reasons = _obj(degraded, "by_reason")
    decisions = health.get("write_decisions_today")
    if not isinstance(decisions, list):
        raise ValueError("response field 'write_decisions_today' must be a list")
    rooms = status.get("rooms")
    if not isinstance(rooms, list):
        raise ValueError("response field 'rooms' must be a list")
    stale: list[str] = []
    for room in rooms:
        if not isinstance(room, dict) or not isinstance(room.get("stale"), bool):
            raise ValueError("each room must include a boolean 'stale' field")
        if room["stale"]:
            key, age = room.get("key"), room.get("age_min")
            stale.append(f"{key} ({age} min)" if age is not None else str(key))
    sources = _obj(health, "sources")
    model = _obj(shadow, "model")
    mae = model.get("mae")
    if mae is not None and (isinstance(mae, bool) or not isinstance(mae, int | float)):
        raise ValueError("shadow model MAE must be numeric or null")
    if not isinstance(reasons, dict) or any(
        type(value) is not int or value < 0 for value in reasons.values()
    ):
        raise ValueError("degraded reasons must map to non-negative integer counts")

    lines = [
        "# Thermabot SHADOW daily report",
        "",
        f"- Generated (UTC): {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- Data date (UTC): {_cell(day_utc)}",
        f"- State / mode: {_cell(status.get('state'))} / {_cell(status.get('run_mode'))}",
        f"- SHADOW days: {_cell(status.get('shadow_days'))}",
        f"- HA / weather: {_cell(sources.get('ha'))} / {_cell(sources.get('weather'))}",
        "",
        "## Today's writes (UTC)",
        "",
        "| Sent | Shadow decisions | Denied guards | Failed |",
        "|---:|---:|---:|---:|",
        f"| {writes['sent']} | {writes['shadow']} | {writes['denied']} | {writes['failed']} |",
        "",
        "## Decisions by guard",
        "",
        "| Outcome | Guard | Reason | Count |",
        "|---|---|---|---:|",
    ]
    if decisions:
        for decision in decisions:
            if not isinstance(decision, dict):
                raise ValueError("write decision rows must be objects")
            count = _count(decision, "count")
            lines.append(
                f"| {_cell(decision.get('outcome'))} | {_cell(decision.get('guard'))} "
                f"| {_cell(decision.get('reason'))} | {count} |"
            )
    else:
        lines.append("| — | — | No write decisions today | 0 |")
    lines += [
        "",
        "## Other checks",
        "",
        f"- DEGRADED: {degraded_count} sampled minutes "
        f"({sample_count} sampled total; {untracked_count} older/untracked)",
        "- DEGRADED reasons: "
        + (
            ", ".join(f"{_cell(name)}: {value}" for name, value in sorted(reasons.items()))
            if reasons
            else "none"
        ),
        "- Stale rooms: " + (", ".join(stale) if stale else "none"),
        f"- Model: {_cell(model.get('status'))}; thermostat MAE: "
        + ("n/a" if mae is None else f"{mae:.3f}"),
        "",
    ]
    return "\n".join(lines), int(assert_no_sent and writes["sent"] != 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Thermabot base URL")
    parser.add_argument(
        "--cookie-file",
        required=True,
        type=Path,
        help="file containing an owner/admin tb_session cookie",
    )
    parser.add_argument("--assert-no-sent", action="store_true")
    parser.add_argument(
        "--output", type=Path, help="write the report to this file instead of stdout"
    )
    args = parser.parse_args(argv)
    parsed = urlsplit(args.url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        parser.error("--url must be an http(s) base URL without credentials, query, or fragment")
    try:
        cookie = _cookie(args.cookie_file)
        base = args.url.rstrip("/")
        status = _fetch(base, "/api/status", cookie)
        health = _fetch(base, "/api/health", cookie)
        shadow = _fetch(base, "/api/shadow", cookie)
        report, result = render_report(status, health, shadow, args.assert_no_sent)
        if args.output:
            args.output.write_text(report, encoding="utf-8")
        else:
            print(report, end="")
        if result:
            print("SHADOW safety check failed: sent writes were recorded.", file=sys.stderr)
        return result
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"shadow report failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
