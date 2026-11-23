"""Home Assistant pulls the published add-on image; it does not build addon/."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from scripts.check_addon_config import (
    IMAGE_TEMPLATE,
    AddonConfigError,
    assert_anonymous_pull,
    main,
    package_name,
    require_root_context_dockerfile,
    set_package_public,
    supervisor_refs,
)

ROOT = Path(__file__).resolve().parents[2]
REF = "ghcr.io/comment23/thermabot-addon-amd64:0.6.8-rc.8"


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _http_error(url: str, status: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, status, "err", hdrs=None, fp=io.BytesIO(body))


@pytest.mark.brd("ADDON-1")
def test_live_manifest_points_supervisor_at_public_images() -> None:
    assert main([]) == 0
    assert supervisor_refs(IMAGE_TEMPLATE, ["amd64", "aarch64"], "0.6.8-rc.8") == [
        "ghcr.io/comment23/thermabot-addon-amd64:0.6.8-rc.8",
        "ghcr.io/comment23/thermabot-addon-aarch64:0.6.8-rc.8",
    ]


@pytest.mark.brd("ADDON-1")
def test_image_template_rejects_a_local_build_or_a_baked_tag() -> None:
    with pytest.raises(AddonConfigError, match="image must be"):
        supervisor_refs("ghcr.io/comment23/other-{arch}", ["amd64"], "0.6.8-rc.8")
    with pytest.raises(AddonConfigError, match="image must be"):
        supervisor_refs(f"{IMAGE_TEMPLATE}:0.6.8-rc.8", ["amd64"], "0.6.8-rc.8")


@pytest.mark.brd("ADDON-1")
def test_dockerfile_must_keep_the_repository_root_context() -> None:
    text = (ROOT / "addon" / "Dockerfile").read_text(encoding="utf-8")
    require_root_context_dockerfile(text)
    with pytest.raises(AddonConfigError, match="repository root"):
        require_root_context_dockerfile("COPY run.sh /run.sh\n")


@pytest.mark.brd("ADDON-1")
def test_anonymous_pull_accepts_only_a_public_manifest() -> None:
    def opener(req: urllib.request.Request, timeout: int = 30) -> _Response:
        del timeout
        if req.full_url.startswith("https://ghcr.io/token"):
            return _Response(200, json.dumps({"token": "anon"}).encode())
        assert req.get_header("Authorization") == "Bearer anon"
        return _Response(200, b"{}")

    assert_anonymous_pull(REF, opener=opener, attempts=1, pause=0)
    assert package_name(REF) == "thermabot-addon-amd64"


@pytest.mark.brd("ADDON-1")
def test_anonymous_pull_reports_the_package_settings_url() -> None:
    def opener(req: urllib.request.Request, timeout: int = 30) -> _Response:
        del timeout
        if req.full_url.startswith("https://ghcr.io/token"):
            return _Response(200, json.dumps({"token": "anon"}).encode())
        raise _http_error(req.full_url, 401, b"denied")

    with pytest.raises(AddonConfigError, match="packages/container/thermabot-addon-amd64/settings"):
        assert_anonymous_pull(REF, opener=opener, attempts=1, pause=0)


@pytest.mark.brd("ADDON-1")
def test_set_package_public_stops_on_the_first_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def opener(req: urllib.request.Request, timeout: int = 30) -> _Response:
        del timeout
        calls.append(req.full_url)
        if len(calls) == 1:
            raise _http_error(req.full_url, 404, b"not yet")
        return _Response(204, b"")

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    set_package_public("thermabot-addon-amd64", "token", opener=opener, attempts=1, pause=0)
    assert len(calls) == 2
    assert calls[0].endswith("/users/comment23/packages/container/thermabot-addon-amd64")
