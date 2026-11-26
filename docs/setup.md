# First-run accounts, setup and Settings

## Initial administrator (next candidate)

On an installation with no users, opening the app or its login page takes you to
**Create administrator**. Choose a username, a unique password (8 to 128 characters),
and confirm the password. This first account has the **Owner** role, including user
management, configuration, and backups. It is signed in automatically.

Complete this step on a trusted local network, before exposing the app to other people:
anyone who can reach an unclaimed installation can claim its first account. There are no
default credentials. Account creation requires a short-lived, signed browser CSRF token.
The first successful creation closes registration atomically; simultaneous requests cannot
create a second initial Owner. Registration stays closed across restarts. An existing
account of any role also closes registration; this is not a password-reset route.
Manage additional accounts under **More > Users**, or use the existing local
`python -m thermabot create-user NAME --role owner|admin|member` command.

After sign-in, an Owner goes straight to the setup wizard only when no saved household
configuration exists. Existing settings in the database, including imported/restored
configuration, are kept and the wizard is skipped. Legacy effective thermostat configuration
is also reused. An `.env` file or HA connection credentials alone do not count as a completed
household configuration. The wizard remains available manually at **More > Setup wizard**.
Neither account creation nor automatic navigation changes the kill switch, operating mode,
settings, HA helpers, or thermostat.

## Home Assistant setup wizard

A fresh install has no thermostat configured, so the bot shows **SETUP NEEDED** and never reads
or writes a climate entity. An Owner reaches the **setup wizard** automatically after first
sign-in when no configuration exists, or opens it from the dashboard banner (or later from
*Settings > Open the setup wizard*, or *More > Setup wizard*).

1. **Connect**: tests `HA_URL` / `HA_TOKEN` from the environment (`GET /api/config`) and shows the
   Home Assistant version. Secrets are never stored in the database or shown in the UI.
2. **Thermostat**: lists every `climate.*` entity with its name, state, `hvac_modes` and `fan_modes`.
3. **Sensors**: suggested blower, compressor, outdoor-temperature and humidity sensors, plus room
   temperature sensors, each with a confidence. Every field is a searchable override; empty = not used.
4. **Presence**: tick `person.*` / `device_tracker.*` entities and name them. Nobody ticked means
   the house always counts as occupied.
5. **Comfort bands**: low/high per block (home/sleep/away) and season, checked as you go
   (heat high <= cool low; cool high - heat low >= the write deadband, 3 °F by default). Choose °F or °C.
   When weather-aware comfort is enabled in Settings, the wizard also edits its active home
   and sleep ranges; the legacy seasonal ranges still provide away settings.
6. **Weather** (v0.4.0): provider (Open-Meteo, global and keyless, the default; or NWS, US only),
   latitude/longitude (empty = Home Assistant's home location), a **Use HA home location** button
   (reads `GET /api/config`) and a **Test fetch** preview of the next 12 hours. See [weather.md](weather.md).
7. **Review and save**: writes the `settings` row (`updated_by` = you). It never changes the run
   mode or the kill switch. **A new thermostat takes effect after a restart**; a banner says so
   until then. Stop Control before changing or removing the thermostat. While the saved
   thermostat differs from the running one, direct climate reads and writes are paused; restart
   to use the new thermostat. Restoring the saved thermostat to the running one also clears the
   mismatch. Any thermostat change restarts the seven-day Watch evidence window.

The wizard only reads from Home Assistant (`GET /api/config`, `GET /api/states`, cached 15 s,
5 s timeout). It never calls a Home Assistant service.

## Temperature overrides

The dashboard can pause the plan or request a temporary temperature and/or HVAC mode. Temperature
requests use °F and require an ACTIVE bot, the enabled HA mirror, a fresh thermostat reading, and
the normal safety and write-rate guards. Home Assistant must report the thermostat's setpoint
range, increment, and a known Fahrenheit or Celsius unit.

In heat/cool mode the target is the center of a range at least as wide as the configured deadband.
The range is rounded outward to the device's increment, and safety limits may adjust it further.
The status shows the actual confirmed setpoints or why the request was blocked. Some device
increments and safety limits may be incompatible, in which case no setpoint is sent.

An override is sent once, not repeatedly maintained: later changes at the thermostat are left
alone. Failed or interrupted requests are not retried automatically; if the status is failed,
sending, or unconfirmed, check the thermostat before requesting another change. A legacy request
without a command status is treated as unconfirmed rather than replayed after an update.

Requests more than three minutes old are blocked rather than sent. If Home Assistant is offline,
the status can remain pending until control resumes and checks the request's age. API requests
using an `until` time must include a timezone, and a pause cannot be combined with a target or mode.

The planned schedule remains paused while an override is open, including when its command is
blocked or fails. Cancel it to resume planning. Cancellation cannot recall a command already
sent to Home Assistant or undo a thermostat change.

## Watch, control, and stop

An Owner starts the bot in **Watch (SHADOW)**. Watch records recommendations but never writes
the thermostat. In **Safety & control**, the Owner can configure and enable a stop comfort
band, independent of the schedule. It is disabled by default.
If the HA enable mirror cannot be confirmed during a Watch start, the request remains enabled
but the bot may still be stopped; Watch can begin when the mirror becomes available. Check status.

Starting **Control (ACTIVE)** is a separate Owner action. The server requires:

- A running, healthy Watch and a current thermostat reading and plan.
- Seven elapsed days of tracked Watch evidence: at least 9,576 of 10,080 minute samples
  (95%), no gaps longer than three minutes, a current sample, and no untracked or non-SHADOW
  samples. Sent and unconfirmed thermostat writes invalidate the window.
- A valid fitted model, configured HVAC sensors, known device capabilities, and an enabled
  stop band compatible with the thermostat and safety limits.
- Preliminary safety-net configuration and the Owner's explicit review of the seven-day
  history, degraded intervals, stale rooms, and the independent Home Assistant safety net.
  A configured heartbeat or helper alone does **not** prove that the safety net works.
- Any previous stop warning must be checked and acknowledged before Control can start.

The Owner confirms the review and types `ACTIVE`. The displayed plan is a current
recommendation, not a guarantee of the first command. A missing day, long outage, stop,
control interval, untracked data, or potentially sent write requires a new seven-day Watch
window by default. A normal update/restart while already Watching preserves the saved start
time and samples; the update itself does not reset Watch. A long update outage can still
fail the sampling/currentness requirements.

For reviewed historical data or deliberate testing, an Owner can expand **Use existing Watch
history or test without waiting seven days** under **Safety and control**. Select the Watch-only
exception and give a reason of 10 to 500 characters. This waives only the seven-day evidence
gate for that one Control start. It never rewrites history, backdates Watch, or marks the live
soak complete. All other readiness checks, both review acknowledgements, and the typed `ACTIVE`
confirmation remain mandatory. The server checks readiness again after confirming the HA
mirror. The reason and actual evidence status are audited atomically before Control is saved;
an audit failure leaves Watch unchanged. Reloading clears the selection, and a later start
requires a new explicit exception. API clients supply the optional `watch_override_reason`
on the existing `POST api/kill` Control-start request.

If the HA enable mirror cannot be confirmed on, Control is not enabled. These automated
checks do not substitute for the live-soak review or release gate.

Stopping Watch makes no thermostat write, even if the thermostat is off. Stopping Control
attempts the Owner's configured band through the normal safety guards and read-back. The
thermostat must support a dual-setpoint `heat_cool` or `auto` mode; Celsius setpoints are
aligned to its reported increment. Overrides, rate limits, unsupported capabilities, or
communication failures can block or interrupt the handoff. A failed or unconfirmed handoff
still stops the bot, but may have changed the thermostat: check its actual settings. The
warning remains visible until an Owner or Admin acknowledges it after checking.
A Settings or setup save waits for an in-progress start or stop to finish, so a handoff uses
one committed configuration. An Owner can still update the stop comfort band during Control.
If the HA enable mirror cannot be confirmed off, the bot remains stopped locally and shows
the same persistent warning. Reload Safety & control after acknowledgement to refresh readiness.
If Stop cannot be saved, thermostat writes are paused in the running process, the bot attempts
to turn the HA mirror off, and an error asks you to check the thermostat and retry Stop. The
warning cannot be acknowledged until Stop is saved; restarting does not resume Active control.

The legacy API's `restore: "leave"` option cannot bypass an Active handoff. A stopped bot
stays stopped across restart; Watch resumes as Watch; Active returns to Watch and requires
a new seven-day evidence window and Owner approval. A restart notice remains until
acknowledged. Neither starting Watch nor restarting authorizes thermostat control.

## Daily SHADOW report

The read-only soak report uses an Owner/Admin session cookie to read `/api/status`,
`/api/health`, and `/api/shadow`. Store the `tb_session` cookie value (or the full
`tb_session=<value>` pair) in a local file that is not committed:

```text
python scripts/shadow_report.py --url http://thermabot:8095 --cookie-file thermabot-cookie.txt --assert-no-sent
```

The report includes today's write decisions grouped by outcome/guard/reason, sampled
DEGRADED minutes and reasons, stale rooms, and thermostat model MAE. Historical samples
without state metadata are counted as untracked. `--assert-no-sent` exits nonzero if any
thermostat writes were sent; request or response errors also fail explicitly.
The report is an aid to the seven-day SHADOW review, not evidence that a live soak has passed;
see the [v0.6.8 release gate](v0.6.8-plan.md).

## Units

°C is display only. Settings are stored and computed in °F. In °C mode the UI shows 0.1 °C and
converts edits back to °F, rounded to 0.5 °F; untouched values are sent back unchanged, so a
save with no edits changes nothing.

## Settings page

### Simple settings

The primary Comfort page starts with three choices: your ideal temperature (73 F by default),
a three-position **Comfort / Balanced / Efficiency** slider, and important rooms for Home
(daytime) and Sleep (nighttime). Confirm the thermostat and selected temperature-sensor
assignments before previewing. The sensor/equipment wizard still requires an Owner.

- Comfort recommends ideal +/- 1 F, Balanced +/- 2 F, and Efficiency +/- 3 F. These are
  Home/Sleep reference ranges, not hard temperature guarantees or estimated savings.
  Existing room tolerances, scoring and safety guards remain in force.
- **Preview recommendation** changes no settings. It shows old/new reference ranges,
  selected room influence, and any widening required by the thermostat's automatic-mode
  deadband. Missing mappings and conflicts with saved safety limits are rejected explicitly.
- **Apply reviewed preferences** enables weather-aware shared Home/Sleep bands, updates the
  ideal analysis reference/tolerance and gives selected rooms equal influence. The existing
  Away ranges/weights, schedule, thermostat, sensors, safety, write limits and operating mode
  are preserved. Nonselected rooms keep their measurements and safety checks.
- Custom configurations remain untouched until application; the starting Balanced suggestion
  is not automatically saved. Later custom edits are shown as Custom, not silently reset.
  A changed settings fingerprint or edited preview requires a new review.
- Applying preferences does not start Active control. If already Active, future guarded
  decisions use the new ranges. The existing planner/fallback handles weather, occupancy and
  model confidence; schedule suggestions remain approval-based. Tariff tools do not become
  an automatic actuator policy.
- Optional humidity, metering, tariff and technical tuning stay in Home & rooms / Advanced.
  Simple preferences use authenticated, CSRF-protected
  `POST /api/settings/preferences/preview` and `POST /api/settings/preferences/apply`
  (Owner/Admin). Celsius input follows the existing conversion and untouched-value rules.

### Advanced settings

Custom bands, display units, scoring, nudges, overrides, fan/model tuning, insights/tariffs,
metrics and reporting are available in **Advanced**. Forms are generated from the `Settings`
model. A save goes through `PUT /api/settings`
and the same pydantic validation, and errors are shown next to the field. The `thermostat`,
`safety`, `off`, and `stop_handoff` sections need an Owner. Owners also get a read-only JSON view
under *Advanced*.

**Weather-aware comfort** is opt-in through a reviewed Simple recommendation or Advanced.
Enable it and set both the home and sleep
ranges to use those ranges in every season. Review the [weather-aware behavior and safety
limitations](weather.md#weather-aware-comfort) before enabling it. An older configuration export
without this section can still be imported; the new policy will be disabled in the imported
configuration.

## API (Owner only)

| Method | Path | What |
|---|---|---|
| GET | `api/setup/ha-test` | `{ok, version, location_name, url, ms, error}` |
| GET | `api/setup/entities?domain=climate` | entities of one domain (climate, sensor, binary_sensor, person, device_tracker) |
| GET | `api/setup/discover[?thermostat=climate.x]` | climate list, ranked suggestions, all candidate entities |
| GET | `api/setup/ha-location` | HA home location `{ok, lat, lon, location_name, error}` (Owner + Admin, v0.4.0) |
| POST | `api/setup/weather-test` | `{provider, lat, lon}` -> `{ok, points, hours, preview[], ms, error}`; saves nothing (Owner + Admin, v0.4.0) |
| POST | `api/setup/save` | full Settings JSON -> `{settings, restart_required, running_entity}` |

## Reverse proxy / Home Assistant ingress

All page links, assets and API calls are relative, so thermabot works under a path prefix. When
the `X-Ingress-Path` header is present (HA Supervisor ingress), it becomes the ASGI `root_path`.
`X-Remote-User-*` headers are **not** trusted yet; sign-in is still the bot's own session.
The add-on (v1.0.0) will map them to roles.

## Screenshots

`docs/screenshots/` is generated from the generic test house only:
`THERMABOT_SCREENSHOTS=1 python -m pytest tests/e2e/test_setup_wizard.py tests/e2e/test_forecast_page.py`.
