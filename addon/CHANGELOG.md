# thermabot add-on changelog

## 0.6.8-rc.6 - 2026-11-10 (prerelease)

- Add optional weather-aware home and sleep comfort ranges, with indoor-temperature
  correction and no early preconditioning. Existing configurations remain unchanged.
- Preserve thermostat write safeguards and support imports of older configuration exports.

## 0.6.8-rc.5 - 2026-11-08 (prerelease)

- Apply dashboard temperature and mode overrides once through the guarded thermostat path,
  and show the request outcome. Requests that have waited over three minutes are not sent.
- Recheck write authorization before planned writes and retries.

## 0.6.8 (earlier release candidates through v0.6.8-rc.4)

- Refresh the Home Assistant base image and remove build-only tools from the add-on runtime.
- Refresh the add-on icon and logo to match the Thermabot web interface.
- Align chart, weather-preview, daily-history and summary dates with the schedule timezone.
- Refresh cached web assets when their contents change, even without an application version bump.
- Correct daily runtime, SHADOW coverage and efficiency calculations on daylight-saving days.
- Announce the start/stop dialog title to assistive technology and support keyboard navigation.
- Add read-only Home Assistant home/sun context, room-floor labels, and optional mapped
  exterior-door and garage SHADOW assessment. No new device or HVAC control is enabled.
- Offer first-run administrator creation in the browser. The initial account is an Owner;
  account creation never enables control. Existing accounts keep registration closed.
- Run the setup wizard after first sign-in only when no saved configuration exists; keep
  existing/imported configuration without forcing another wizard.
- Redesign the desktop and mobile shell, and show the actual version on every screen.
- Replace the header Stop button with a dashboard Controls area: start/stop, guarded run-mode
  selection, temporary plan pause, override access, guest mode, and recovery shortcuts.
- Wire guest mode into occupancy so a saved guest toggle actually prevents an away plan.
- Add Owner-only configuration export, validated preview/import while stopped, pre-import
  backups, consistent database downloads and offline restore guidance.
- Refresh saved non-entity settings together on the next tick; preserve thermostat binding
  until restart. Nightly retention does not remove manual or pre-import backups.
- Mark HA read failures degraded immediately, discard stale readings and record the outage.
- Provide an optional HA heartbeat-loss package that disables the enable mirror without
  writing thermostat settings. It is not installed or enabled automatically.
- Include the read-only daily SHADOW report and per-minute runtime-state diagnostics in the
  add-on version.
- The seven-day SHADOW soak, review, and owner go/no-go remain pending. No active thermostat
  trial or completed live soak is implied by this release candidate.

## 0.6.7

- Fail closed during kill-switch restore when the thermostat's state cannot be confirmed.
- Show restore failures as errors and keep the bot OFF.

## 0.6.1

- First Home Assistant add-on release, alongside the existing plain-Docker install
  (`deploy/docker-compose.yml`). Same app, same requirements lock
  (`requirements.runtime.lock`), packaged on a hassio-addons base image with ingress and
  Supervisor-token auth (no long-lived HA token to create).
- Options map 1:1 to the env vars thermabot already reads (`thermabot/config.py: Env`):
  optional HVAC sensors, InfluxDB, heartbeat/alert webhooks, monitor token, log level, and an
  advanced HA URL/token override for pointing at a different Home Assistant instance.
- Thermostat entity, rooms, bands, schedule and presence are still configured in the
  in-app setup wizard / Settings page (unchanged; already ingress-friendly since v0.3.0).
- amd64 only; other architectures are not currently supported.
- CI validates `addon/config.yaml` and `repository.yaml` on every push/PR
  (`.github/workflows/ci.yml`: `addon-config` job). The security job also builds and scans
  the add-on image; Supervisor builds the image from source on install.
