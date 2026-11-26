# Changelog

Application releases and unreleased changes. Prereleases are explicitly labeled; they are not
stable releases and do not satisfy any documented live-soak or owner-review gate. Release
notes are retained here, but historical source tags and release assets are not copied from the
private development repository.

## 0.6.8-rc.12 - 2026-11-26 (prerelease)

The Home Assistant add-on catalog version is `0.6.8-rc.12`, published from `main` only
after CI passes. The in-app version remains `0.6.8`. This candidate does not enable
Control or satisfy the seven-day SHADOW soak and Owner-review gates for stable v0.6.8.

- Populate live room, outdoor and humidity readings before controller guards, using the
  same temperature conversion and freshness rules as history. Refresh Home after samples,
  isolate unavailable/slow sensors, and reject climate entities in auxiliary sensor mappings.
- Keep Settings navigation visible on utility pages, preserve role restrictions, and
  reorganize the header into clear operating controls, System status and Account menus.
- Add a shared Add person / Remove person presence editor in setup and Settings, with
  validation, Home Assistant suggestions and changes applied only on save.
- Add an isolated, SHADOW-only customer UAT harness, a 60-case acceptance plan, fault
  scenarios and desktop/mobile journeys. All 24 automated customer checks pass in the
  corrected-code retest; the full local suites pass 1,307 tests. Real HA installation,
  physical actuation, offline recovery and the live soak remain separate, unverified gates.
- Use a new add-on image version rather than overwrite the published rc.11 images.
  This source/image publication does not create a GitHub release or version tag.

## 0.6.8-rc.11 - 2026-11-25 (prerelease)

The Home Assistant add-on catalog version is `0.6.8-rc.11`. The in-app version remains
`0.6.8`. Updating does not enable Control. Stable v0.6.8 remains blocked on the seven-day
SHADOW soak and Owner review; the testing exception below does not satisfy that release gate.

- Add an Owner-only exception to the seven-day Watch evidence gate for one explicit Control
  start, for reviewed historical data or deliberate testing. Require a reason of 10 to 500
  characters and record it with the actual evidence status atomically before saving Control.
- Keep every other readiness check, both review acknowledgements, typed `ACTIVE` confirmation,
  HA mirror confirmation, device limits, and guarded thermostat write path mandatory.
- Clear the exception selection on reload or state change. Never backdate Watch, rewrite
  historical samples, or persist a standing bypass. Normal updates while already Watching
  retain the saved Watch start and samples; actual interruptions can still invalidate evidence.

## 0.6.8-rc.10 - 2026-11-25 (prerelease)

The Home Assistant add-on catalog version is `0.6.8-rc.10`. The in-app version remains
`0.6.8`. This prerelease does not enable thermostat or equipment control; stable v0.6.8
remains blocked on the seven-day SHADOW soak and Owner review.

- Separate clickable Owner acknowledgements from automatic Control readiness checks.
  Start control still requires all server-side gates and typed confirmation. Add clear
  lifecycle controls, next-blocker guidance and an in-page readiness refresh.
- Add a shared floor/room editor in setup and Settings, with named sensor selection,
  add/remove workflows, per-room comfort importance and a visual home overview.
- Store additional equipment as read-only room inventory, not actuator authorization.
  Preserve saved mappings during discovery failures and protect rooms assigned to accounts.
- Group dashboard rooms by floor and keep newly configured rooms visible while waiting
  for readings. Improve mobile Settings navigation and preserve unused comfort weights.

## 0.6.8-rc.9 - 2026-11-24 (prerelease)

The Home Assistant add-on catalog version is `0.6.8-rc.9`. The in-app version remains
`0.6.8`. This prerelease does not enable thermostat control; stable v0.6.8 remains blocked on
the seven-day SHADOW soak and Owner review.

- Start always enters Watch/SHADOW. Control/ACTIVE requires a separate Owner review of seven
  days of tracked evidence, device readiness, and the independent safety net.
- Stop in Watch makes no thermostat write. Stop in Control attempts the Owner-configured
  comfort band through the guarded write path and persists a warning if the handoff cannot be
  confirmed.
- Restarting from Active returns the bot to Watch and starts a new evidence window.
- Thermostat reconfiguration pauses climate reads and writes until restart. A failed Stop-state
  save fails closed, pauses writes in the running process, attempts to disable the HA mirror,
  and requires retrying Stop before the warning can be dismissed.

## 0.6.8-rc.8 - 2026-11-23 (prerelease)

The Home Assistant add-on catalog version is `0.6.8-rc.8`. The in-app version remains
`0.6.8`. Stable v0.6.8 is still blocked on the seven-day SHADOW soak and owner review.

- The add-on no longer exits at startup when `ha_url` and `ha_token` are blank. s6-overlay
  does not pass `SUPERVISOR_TOKEN` into a `#!/usr/bin/env sh` command; the entrypoint now
  uses `with-contenv` and also reads the s6 environment directory. The image tag is
  `0.6.8-rc.8`.

## 0.6.8-rc.7 - 2026-11-23 (prerelease)

The Home Assistant add-on catalog version is `0.6.8-rc.7`. The in-app version remains
`0.6.8`. Stable v0.6.8 is still blocked on the seven-day SHADOW soak and owner review.
This prerelease does not enable control by itself.

- Install the add-on from public images, `ghcr.io/comment23/thermabot-addon-amd64` and
  `ghcr.io/comment23/thermabot-addon-aarch64`, tag `0.6.8-rc.7`. Supervisor pulls the
  image for the host and does not build on the device. CI publishes both from native
  runners when `main` is updated, and again when the matching `v0.6.8-rc.7` tag is
  pushed. `docker/setup-qemu-action` is not used.
- Convert Home Assistant °C temperatures to °F inside the app and convert setpoints back
  to the device unit on the single write path. An unknown unit clears the reading, is
  logged, and blocks every climate write, including safety and kill-switch restore.
- Check HVAC mode, setpoint range, step and unit on every planned write and on restore,
  and pass the thermostat's live min/max into the guard. Do not assume `heat_cool` when
  the device does not list it (or `auto`).
- Use one schedule timezone: `TZ`, otherwise Home Assistant's `time_zone`, otherwise UTC.
- Mark the session cookie `Secure` on HTTPS. `X-Forwarded-Proto` is not trusted.
- Report `/healthz` as `starting` (HTTP 200) until the first tick, within a 90s grace.
- Type-check the whole `thermabot` package with `mypy --strict`, including the write path.
- Bump `opentelemetry-api` to 1.45.2 (CVE-2026-126360). FastAPI pulls it in; the
  previous pin failed `pip-audit` on main as well.

## 0.6.8-rc.6 - 2026-11-10 (prerelease)

- Add opt-in weather-aware home and sleep ranges, with indoor-temperature correction and
  no early schedule preconditioning.
- Preserve manual overrides, device capability checks, write limits, and the automatic-mode
  deadband. Review the documented fallback and thermostat limitations before enabling it.
- Let older configuration exports be imported with weather-aware comfort disabled.

## 0.6.8-rc.5 - 2026-11-08 (prerelease)

- Send dashboard temperature and mode overrides through the guarded thermostat write path.
  Show whether each request was confirmed, blocked, failed, or remains unconfirmed.
- Send each manual request at most once and block requests older than three minutes rather
  than executing them after an extended outage. Device capabilities and safety limits can
  adjust or block requested setpoints.
- Recheck the kill switch, Home Assistant enable mirror, run mode, and open overrides before
  planned writes and retries.

## 0.6.8-rc.4 - 2026-10-30 (prerelease)

- Add a cohesive Thermabot visual identity for the web UI and repository.
- Keep chart and weather-preview times in the home's schedule timezone, and calculate daily
  history and weekly-summary boundaries using the local calendar day.
- Refresh cached UI assets when their contents change, including between prereleases.
- Account for 23- and 25-hour daylight-saving days in runtime, SHADOW coverage, and efficiency
  reports, including both occurrences of a repeated hour. Existing stored rows are not
  automatically recalculated.
- Give the start/stop dialog an accessible name and verify keyboard navigation and focus
  restoration on desktop and mobile layouts.
- This candidate does not change the existing run-mode safety safeguards.

## 0.6.8-rc.3 - 2026-10-29 (prerelease)

- Show Home Assistant home coordinates, time zone, and sun position in the Owner setup wizard.
- Suggest fan, cover, supplemental-heat, and door-contact entities without controlling them;
  allow Owners to label configured rooms by floor.
- Add an opt-in, read-only SHADOW assessment for explicitly mapped exterior and garage-door
  contacts, including garage-to-house relationships, sustained-open thresholds, and a
  post-closure recovery delay.
- Keep all new discovery and door assessments read-only. Unknown/stale contacts do not imply
  exposure, and a garage door alone does not imply that the conditioned house is exposed.
- Stable v0.6.8 remains blocked on the documented seven-day SHADOW soak, review of degraded
  intervals and stale rooms, and an explicit owner go/no-go. This prerelease does not enable
  thermostat or device control.

## 0.6.8-rc.2 - 2026-10-29 (prerelease)

- Add browser-based creation of the initial administrator (Owner), with signed, expiring CSRF
  protection, atomic single-account registration, and automatic sign-in.
- Open the Home Assistant setup wizard for an Owner only when no saved configuration exists;
  reuse existing/imported configuration without restarting onboarding.
- Account setup never enables control, changes thermostat settings, or reopens registration
  on an installation with existing accounts.

## 0.6.8-rc.1 - 2026-10-29 (prerelease)

- Added a responsive desktop/mobile dashboard, a Controls area, and a visible application
  version.
- Added Owner-only configuration export/import, validated import preview, verified
  pre-import backups, database downloads, and offline recovery guidance.
- Connected guest mode to occupancy behavior and improved settings refresh and backup
  retention safeguards.
- Made Home Assistant read failures visible immediately, discarded stale readings, and
  added an optional heartbeat-loss package that does not actuate climate settings.
- Added daily read-only SHADOW reporting and per-minute runtime-state diagnostics.
- Stable v0.6.8 remains blocked on the seven-day SHADOW soak, review of degraded intervals
  and stale rooms, and an explicit owner go/no-go. No active thermostat trial is implied.

## 0.6.7 - 2026-10-29 (stable)

- Fail closed during kill-switch restore when the thermostat state cannot be confirmed.
- Show restore failures as errors and keep the bot OFF.
