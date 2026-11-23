"""Validate the Home Assistant add-on manifest and the image Supervisor pulls.

Supervisor installs an add-on by pulling ``image`` from ``addon/config.yaml``, with the
tag set to ``version``. It does not build ``addon/Dockerfile``. The publish workflow
builds that Dockerfile with the repository root as the context and pushes one image per
architecture. ``--require-public`` fetches those manifests with no credentials, which is
the same anonymous pull Home Assistant does.

This stays a local script so CI does not need an extra add-on linter Action.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

# Supervisor substitutes {arch} and appends :version. Do not put a tag in this string.
IMAGE_TEMPLATE = "ghcr.io/comment23/thermabot-addon-{arch}"
REQUIRED_ARCHES = ("amd64", "aarch64")
OWNER = "comment23"
REGISTRY = "ghcr.io"

REQUIRED_CONFIG_KEYS = {
    "name",
    "version",
    "slug",
    "description",
    "arch",
    "startup",
    "boot",
    "image",
}
VALID_ARCHES = {"armhf", "armv7", "aarch64", "amd64", "i386"}

# Publish context is the repository root. These paths must stay root-relative.
ROOT_CONTEXT_COPIES = (
    "COPY requirements.runtime.lock",
    "COPY thermabot/",
    "COPY addon/run.sh",
    "COPY addon/options_to_env.py",
)

MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
    )
)

Opener = Callable[..., Any]
_SEMVER = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")


class AddonConfigError(Exception):
    """The add-on manifest or its published image is not installable."""


def supervisor_refs(image: str, arches: list[str], version: str) -> list[str]:
    """Image references Supervisor pulls, in ``arches`` order."""
    if image != IMAGE_TEMPLATE:
        raise AddonConfigError(
            f"image must be {IMAGE_TEMPLATE!r} (Supervisor appends :version; {{arch}} is required)"
        )
    name = image.rsplit("/", 1)[-1]
    if ":" in name:
        raise AddonConfigError("image must not include a tag; Supervisor appends the version")
    if "{arch}" not in image:
        raise AddonConfigError("image must contain {arch}")
    refs: list[str] = []
    for arch in arches:
        ref = image.format(arch=arch)
        if "{" in ref or "}" in ref or ":" in ref.rsplit("/", 1)[-1]:
            raise AddonConfigError(f"image template produced an invalid reference: {ref}")
        refs.append(f"{ref}:{version}")
    return refs


def require_root_context_dockerfile(text: str) -> None:
    missing = [line for line in ROOT_CONTEXT_COPIES if line not in text]
    if missing:
        raise AddonConfigError(
            "addon/Dockerfile is published with the repository root as the build context; "
            f"missing {missing}. Supervisor does not build this file."
        )


def validate_repository(root: Path) -> list[str]:
    cfg_path = root / "addon" / "config.yaml"
    repo_path = root / "repository.yaml"
    dockerfile = root / "addon" / "Dockerfile"
    run_sh = root / "addon" / "run.sh"
    options_py = root / "addon" / "options_to_env.py"

    if not cfg_path.is_file():
        raise AddonConfigError(f"missing {cfg_path}")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise AddonConfigError("addon/config.yaml must be a mapping")

    missing = REQUIRED_CONFIG_KEYS - set(cfg)
    if missing:
        raise AddonConfigError(f"addon/config.yaml missing required keys: {sorted(missing)}")

    arches = cfg["arch"]
    if not isinstance(arches, list) or not arches:
        raise AddonConfigError("addon/config.yaml: arch must be a non-empty list")
    bad_arch = set(arches) - VALID_ARCHES
    if bad_arch:
        raise AddonConfigError(f"addon/config.yaml: unknown arch entries {sorted(bad_arch)}")
    if set(arches) != set(REQUIRED_ARCHES):
        raise AddonConfigError(
            f"addon/config.yaml arch must be {list(REQUIRED_ARCHES)} so both published images exist"
        )

    if cfg["slug"] != "thermabot":
        raise AddonConfigError("addon/config.yaml: slug must be 'thermabot'")

    sys.path.insert(0, str(root))
    from thermabot import __version__  # noqa: E402

    addon_version = str(cfg["version"])
    if not _SEMVER.fullmatch(addon_version):
        raise AddonConfigError(f"addon/config.yaml version {addon_version!r} is not valid SemVer")
    if addon_version.split("-", maxsplit=1)[0] != __version__:
        raise AddonConfigError(
            f"addon/config.yaml version {addon_version!r} does not match "
            f"thermabot base version {__version__!r}"
        )

    if cfg.get("ingress") and not cfg.get("ingress_port"):
        raise AddonConfigError("addon/config.yaml: ingress=true needs ingress_port")

    if "schema" in cfg and "options" in cfg:
        missing_opts = set(cfg["schema"]) - set(cfg["options"])
        if missing_opts:
            raise AddonConfigError(
                f"addon/config.yaml: schema keys with no options default: {sorted(missing_opts)}"
            )

    if not repo_path.is_file():
        raise AddonConfigError(f"missing {repo_path}")
    repo = yaml.safe_load(repo_path.read_text(encoding="utf-8"))
    if not isinstance(repo, dict) or not {"name", "url", "maintainer"} <= set(repo):
        raise AddonConfigError("repository.yaml must have name, url, maintainer")

    for path in (dockerfile, run_sh, options_py):
        if not path.is_file():
            raise AddonConfigError(f"missing {path}")
    require_root_context_dockerfile(dockerfile.read_text(encoding="utf-8"))
    return supervisor_refs(str(cfg["image"]), [str(a) for a in arches], addon_version)


def split_ref(ref: str) -> tuple[str, str, str]:
    """Return ``(registry, repository, tag)`` for ``ghcr.io/owner/name:tag``."""
    if "/" not in ref or ":" not in ref.rsplit("/", 1)[-1]:
        raise AddonConfigError(f"not a tagged image reference: {ref}")
    registry, rest = ref.split("/", 1)
    repository, tag = rest.rsplit(":", 1)
    if not registry or not repository or not tag:
        raise AddonConfigError(f"not a tagged image reference: {ref}")
    return registry, repository, tag


def package_name(ref: str) -> str:
    _registry, repository, _tag = split_ref(ref)
    owner, _, name = repository.partition("/")
    if owner != OWNER or not name:
        raise AddonConfigError(f"unexpected image repository: {repository}")
    return name


def _settings_url(package: str) -> str:
    return f"https://github.com/users/{OWNER}/packages/container/{package}/settings"


def _request(opener: Opener, req: urllib.request.Request) -> tuple[int, bytes]:
    try:
        with opener(req, timeout=30) as resp:
            status = int(getattr(resp, "status", getattr(resp, "code", 200)))
            body = resp.read()
            return status, body if isinstance(body, bytes) else bytes(body)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return exc.code, payload if isinstance(payload, bytes) else bytes(payload)


def assert_anonymous_pull(
    ref: str,
    *,
    opener: Opener = urllib.request.urlopen,
    attempts: int = 6,
    pause: float = 5.0,
) -> None:
    """Fail unless an unauthenticated client can read the image manifest."""
    registry, repository, tag = split_ref(ref)
    if registry != REGISTRY:
        raise AddonConfigError(f"unsupported registry in {ref}")
    query = urllib.parse.urlencode({"service": registry, "scope": f"repository:{repository}:pull"})
    token_url = f"https://{registry}/token?{query}"
    manifest_url = (
        f"https://{registry}/v2/{repository}/manifests/{urllib.parse.quote(tag, safe='')}"
    )
    package = package_name(ref)
    last = ""
    for attempt in range(attempts):
        token_status, token_body = _request(
            opener,
            urllib.request.Request(token_url, headers={"User-Agent": "thermabot-addon-publish"}),
        )
        token = ""
        if token_status == 200:
            try:
                token = str(json.loads(token_body.decode("utf-8")).get("token") or "")
            except (UnicodeDecodeError, json.JSONDecodeError):
                token = ""
        manifest_status = 0
        manifest_body = b""
        if token:
            manifest_status, manifest_body = _request(
                opener,
                urllib.request.Request(
                    manifest_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": MANIFEST_ACCEPT,
                        "User-Agent": "thermabot-addon-publish",
                    },
                ),
            )
        if token_status == 200 and token and manifest_status == 200:
            print(f"anonymous pull OK: {ref}")
            return
        settings = _settings_url(package)
        last = (
            f"{ref} is not anonymously pullable "
            f"(token HTTP {token_status}, manifest HTTP {manifest_status}, "
            f"body {manifest_body[:180]!r}). "
            f"The repository owner ({OWNER}) must set the package to Public: {settings}"
        )
        if attempt + 1 < attempts and pause:
            time.sleep(pause)
    raise AddonConfigError(last)


def set_package_public(
    package: str,
    token: str,
    *,
    opener: Opener = urllib.request.urlopen,
    attempts: int = 5,
    pause: float = 3.0,
) -> None:
    """Best-effort visibility flip. Anonymous pull is the real success check."""
    if not token:
        raise AddonConfigError("GITHUB_TOKEN is required to set package visibility")
    body = json.dumps({"visibility": "public"}).encode("utf-8")
    urls = [
        f"https://api.github.com/users/{OWNER}/packages/container/{package}",
        f"https://api.github.com/user/packages/container/{package}",
        f"https://api.github.com/users/{OWNER}/packages/container/{package}/visibility",
        f"https://api.github.com/user/packages/container/{package}/visibility",
    ]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "thermabot-addon-publish",
    }
    last_errors: list[str] = []
    for attempt in range(attempts):
        errors: list[str] = []
        retry = False
        for url in urls:
            status, payload = _request(
                opener, urllib.request.Request(url, data=body, method="PATCH", headers=headers)
            )
            if status in (200, 204):
                print(f"package {package} visibility set to public via {url} ({status})")
                return
            errors.append(f"{url} -> {status} {payload[:180]!r}")
            if status in (404, 500, 502, 503, 504):
                retry = True
        last_errors = errors
        if not retry or attempt + 1 == attempts:
            break
        if pause:
            time.sleep(pause)
    print(f"GITHUB_TOKEN could not change visibility of {package}:", file=sys.stderr)
    for line in last_errors:
        print(line, file=sys.stderr)
    print(
        f"If the anonymous pull fails, {OWNER} must set the package to Public: "
        f"{_settings_url(package)}",
        file=sys.stderr,
    )


def _select_arch(refs: list[str], arch: str) -> list[str]:
    prefix = f"thermabot-addon-{arch}:"
    selected = [ref for ref in refs if ref.rsplit("/", 1)[-1].startswith(prefix)]
    if len(selected) != 1:
        raise AddonConfigError(f"no published image for arch {arch!r}")
    return selected


def _write_github_output(ref: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        raise AddonConfigError("GITHUB_OUTPUT is not set")
    version = ref.rsplit(":", 1)[1]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"version={version}\n")
        handle.write(f"image={ref}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", default="", help="Limit checks to one Home Assistant arch")
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="Append version and image to GITHUB_OUTPUT (requires --arch)",
    )
    parser.add_argument(
        "--set-public",
        action="store_true",
        help="Ask the GitHub API to mark the package public (requires GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--require-public",
        action="store_true",
        help="Fail unless each image manifest is readable without credentials",
    )
    args = parser.parse_args(argv)
    try:
        refs = validate_repository(ROOT)
        if args.arch:
            refs = _select_arch(refs, args.arch)
        if args.github_output:
            if not args.arch:
                raise AddonConfigError("--github-output requires --arch")
            _write_github_output(refs[0])
        if args.set_public:
            token = os.environ.get("GITHUB_TOKEN", "")
            for ref in refs:
                set_package_public(package_name(ref), token)
        if args.require_public:
            for ref in refs:
                assert_anonymous_pull(ref)
    except AddonConfigError as exc:
        print(f"addon config invalid: {exc}", file=sys.stderr)
        return 1
    if not args.github_output:
        print(
            "addon config OK: config.yaml, repository.yaml, Dockerfile, run.sh "
            "all present and valid"
        )
        for ref in refs:
            print(f"supervisor pulls {ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
