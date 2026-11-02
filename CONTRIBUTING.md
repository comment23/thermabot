# Contributing to thermabot

Thanks for helping. thermabot controls a real HVAC system, so safety rules come before
features. Please read this before opening a pull request.

## Ground rules (non-negotiable)

- **One write path.** Only `thermabot/app/controller.py` may call `_call_service`
  (enforced by `tests/unit/test_single_write_path.py`). Every write goes through the core
  guard chain *and* the adapter allowlist (`check_write`).
- **No thermostat, no writes.** If `settings.thermostat.entity_id` is empty the bot is in
  SETUP NEEDED and must never read or write a climate entity. Any new write path needs a
  test that proves it denies while unconfigured.
- **SHADOW is the default.** A fresh install starts with the kill switch off and run mode
  `shadow`. Never change that default.
- **The fallback plan never chooses `off`.**
- **`thermabot/core` stays pure**: no I/O, clocks, env, network or DB
  (`tests/unit/test_core_purity.py`).
- **Safety settings are tighten-only** (`safety`, `off`, `writes` in `thermabot/config.py`).
- **Migrations are forward-only** and must be tested against a copy of a real-shaped DB.
- **No personal data in the repo.** No IPs, hostnames, entity ids, names or tokens. Tests use
  generic fixtures (`tests/house.py`: `climate.living_room`, people `alex`/`sam`). CI runs
  gitleaks on every push and PR.

## Development setup

```bash
python -m venv .venv && . .venv/bin/activate     # Python 3.12
pip install --require-hashes -r requirements.lock
playwright install chromium                       # for the e2e tests
```

## Before you push

```bash
ruff format --check .
ruff check .
mypy --strict thermabot/core
python -m pytest tests/unit tests/integration -q
python -m pytest tests/e2e -q
```

Tag new tests with the requirement they cover: `@pytest.mark.brd("FR-...")`.

## Pull requests

- Keep each PR focused. Explain *why*, and how you tested it (ideally in SHADOW against a
  real thermostat).
- Fill in the PR template checklist.
- Dependencies: update `requirements.in` and regenerate the hashed lock files.

## Releases and changelog

- Update the root `CHANGELOG.md` with the exact version, publication date, user-visible
  changes, and any remaining release gates. Label prereleases as prereleases; do not imply
  that automated tests substitute for a live soak or owner review.
- Update the README's release-status section in the same change so it points to the latest
  stable and prerelease versions. Mirror add-on-specific notes in `addon/CHANGELOG.md` and
  keep `addon/config.yaml` aligned with the add-on version.
- Include matching notes in the GitHub release. Do not publish a stable release while a
  documented gate is pending; if a publication happens before its docs update, follow up
  immediately with the changelog and README correction.

## Reporting bugs and security issues

Use the issue templates for bugs and features. **Do not** open public issues for security
problems; see [SECURITY.md](SECURITY.md).
