# thermabot add-on changelog

## 0.7.0-rc.1 - 2026-11-26 (prerelease)

- Application version 0.7.0, with room insights, temperature tables, all ten researched
  efficiency tools, room tabs, setup readiness and temperature/simulation visuals.
- Fix room temperature table overflow with Celsius or enlarged text without hiding data.
- Simplify primary Settings to ideal temperature, a comfort/efficiency slider and important
  rooms. Preview and explicit approval are required; technical settings remain in Advanced.
- Retain safety and Control gates, existing schedules/Away settings and passive equipment.
  Migration 005 preserves existing samples and adds home-improvement experiment metadata.
- Include updated customer UAT: 38 customer/lab cases and 43 targeted room regressions
  pass, including validated simulated model/history and offline-copy retention.
- The corrected byte-exact publication candidate passes 85 UAT/room/packaging/layout cases.
- Images publish from `main` and the matching version tag after CI. Wait for successful
  `addon-publish` and anonymous manifest resolution before installing. This prerelease
  does not satisfy live HA/platform, seven-day SHADOW or Owner-review gates.

## 0.6.8-rc.12 - 2026-11-26 (prerelease)

- Restore live Home Assistant sensor temperatures, shared conversion/freshness handling
  and dashboard refresh. Reject climate entities in room/auxiliary sensor mappings.
- Keep Settings tabs visible on utility pages, simplify header controls, and add editable
  presence people in setup and Settings.
- Include the application fixes verified by 24 passing customer checks and 1,307 passing
  local tests. The simulated UAT harness is developer tooling, not part of the add-on image.
- Publish new `0.6.8-rc.12` images from `main` after CI, without overwriting rc.11.
  The in-app version remains `0.6.8`; updating does not enable Control. Stable v0.6.8
  remains blocked on the documented live SHADOW soak and Owner review.

## 0.6.8-rc.11 - 2026-11-25 (prerelease)

- Add a one-start, Owner-only Watch-evidence exception under Safety and control for reviewed
  history or testing, with a required audited reason. All other Control readiness checks,
  review acknowledgements, typed confirmation, and thermostat write safeguards remain enforced.
- Preserve historical data and normal Watch-update continuity. The exception is not retained
  on reload and does not mark the live soak complete or enable Control automatically.
- The in-app version remains `0.6.8`. Stable v0.6.8 remains blocked on the documented live
  SHADOW soak and Owner review.

## 0.6.8-rc.10 - 2026-11-25 (prerelease)

- Add explicit Start watching and Stop thermabot controls, clickable Owner acknowledgements,
  clearer Control readiness blockers, and an in-page readiness refresh.
- Add a shared floor/room editor with named sensor selection, room add/remove workflows,
  and passive equipment assignments. Group dashboard rooms by floor and improve mobile
  Settings navigation.
- The in-app version remains `0.6.8`. All Control gates remain enforced; equipment
  assignments do not authorize device control. Stable v0.6.8 remains blocked on the
  documented live SHADOW soak and Owner review.

## 0.6.8-rc.9 - 2026-11-24 (prerelease)

- Include Watch-first lifecycle controls, the Owner-approved guarded Stop handoff, and
  fail-closed handling for an unsaved Stop state. See the root changelog for application
  behavior. Stable v0.6.8 remains blocked on the documented live SHADOW soak and Owner review.

## 0.6.8-rc.8 - 2026-11-23 (prerelease)

- Start the add-on with s6 `with-contenv`, and read `/run/s6/container_environment`
  when the process environment does not include `SUPERVISOR_TOKEN`. Blank `ha_url` and
  `ha_token` options again use the Supervisor token (`http://supervisor/core`). Previously
  the s6 CMD started with `#!/usr/bin/env sh`, hid that token, and exited with
  `missing or invalid environment: HA_TOKEN`.

## 0.6.8-rc.7 - 2026-11-23 (prerelease)

- Supervisor pulls a prebuilt image instead of building this add-on on the device.
  The image is `ghcr.io/comment23/thermabot-addon-{arch}` tagged `0.6.8-rc.7`, for amd64
  and aarch64. CI publishes those images from `main` and from the matching `v0.6.8-rc.7`
  tag, on native runners. `docker/setup-qemu-action` is not on the Actions allowlist and
  is not used.
- Leave `TZ` unset unless the timezone option is set, so the app follows Home Assistant.
- Application behavior included in this image is listed in the root changelog under
  0.6.8-rc.7. Stable v0.6.8 is still blocked on the documented SHADOW soak.

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
- amd64 only at that release.
- CI validated `addon/config.yaml` and `repository.yaml`. The security job built and
  scanned the add-on image. Supervisor built the image from source on install; 0.6.8-rc.7
  replaced that local build with published images.
