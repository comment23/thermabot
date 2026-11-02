"""Stage 6: validates addon/config.yaml and repository.yaml against the HA add-on spec's
required-keys shape. Used by CI (ci.yml: addon-config job) as a minimal, dependency-free
substitute for the external `frenck/action-app-linter` add-on linter (not added as a new
external Action dependency to keep CI simple; revisit if the external action proves stable
and worth the extra supply-chain surface)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_CONFIG_KEYS = {
    "name",
    "version",
    "slug",
    "description",
    "arch",
    "startup",
    "boot",
}
VALID_ARCHES = {"armhf", "armv7", "aarch64", "amd64", "i386"}


def _fail(msg: str) -> None:
    print(f"addon config invalid: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    cfg_path = ROOT / "addon" / "config.yaml"
    repo_path = ROOT / "repository.yaml"

    if not cfg_path.is_file():
        _fail(f"missing {cfg_path}")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        _fail("addon/config.yaml must be a mapping")

    missing = REQUIRED_CONFIG_KEYS - set(cfg)
    if missing:
        _fail(f"addon/config.yaml missing required keys: {sorted(missing)}")

    if not isinstance(cfg["arch"], list) or not cfg["arch"]:
        _fail("addon/config.yaml: arch must be a non-empty list")
    bad_arch = set(cfg["arch"]) - VALID_ARCHES
    if bad_arch:
        _fail(f"addon/config.yaml: unknown arch entries {sorted(bad_arch)}")

    if cfg["slug"] != "thermabot":
        _fail("addon/config.yaml: slug must be 'thermabot'")

    # Keep the add-on release channel explicit while matching the app's base version.
    sys.path.insert(0, str(ROOT))
    from thermabot import __version__  # noqa: E402

    addon_version = str(cfg["version"])
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", addon_version):
        _fail(f"addon/config.yaml version {addon_version!r} is not valid SemVer")
    if addon_version.split("-", maxsplit=1)[0] != __version__:
        _fail(
            f"addon/config.yaml version {addon_version!r} does not match "
            f"thermabot base version {__version__!r}"
        )

    if cfg.get("ingress") and not cfg.get("ingress_port"):
        _fail("addon/config.yaml: ingress=true needs ingress_port")

    if "schema" in cfg and "options" in cfg:
        missing_opts = set(cfg["schema"]) - set(cfg["options"])
        if missing_opts:
            _fail(f"addon/config.yaml: schema keys with no options default: {sorted(missing_opts)}")

    if not repo_path.is_file():
        _fail(f"missing {repo_path}")
    repo = yaml.safe_load(repo_path.read_text(encoding="utf-8"))
    if not isinstance(repo, dict) or not {"name", "url", "maintainer"} <= set(repo):
        _fail("repository.yaml must have name, url, maintainer")

    dockerfile = ROOT / "addon" / "Dockerfile"
    run_sh = ROOT / "addon" / "run.sh"
    for p in (dockerfile, run_sh):
        if not p.is_file():
            _fail(f"missing {p}")

    print("addon config OK: config.yaml, repository.yaml, Dockerfile, run.sh all present and valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
