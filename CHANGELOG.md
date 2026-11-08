# Changelog

Application releases and unreleased changes. Prereleases are explicitly labeled; they are not
stable releases and do not satisfy any documented live-soak or owner-review gate. Release
notes are retained here, but historical source tags and release assets are not copied from the
private development repository.

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
