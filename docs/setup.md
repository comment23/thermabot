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
6. **Weather** (v0.4.0): provider (Open-Meteo, global and keyless, the default; or NWS, US only),
   latitude/longitude (empty = Home Assistant's home location), a **Use HA home location** button
   (reads `GET /api/config`) and a **Test fetch** preview of the next 12 hours. See [weather.md](weather.md).
7. **Review and save**: writes the `settings` row (`updated_by` = you). It never changes the run
   mode or the kill switch. **A new thermostat takes effect after a restart**; a banner says so
   until then.

The wizard only reads from Home Assistant (`GET /api/config`, `GET /api/states`, cached 15 s,
5 s timeout). It never calls a Home Assistant service.

## Turning the bot off

When disabling the bot, **Restore to band** only writes after Home Assistant confirms the
thermostat is currently `off`. If that state cannot be read, restore reports `failed` and
no climate command is sent. The bot still turns OFF, and the audit log records the restore
failure. Check the thermostat manually if a restore was needed.

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

One form per section, generated from the `Settings` model. A save goes through `PUT /api/settings`
and the same pydantic validation, and errors are shown next to the field. The `thermostat`,
`safety` and `off` sections need an Owner. Owners also get a read-only JSON view under *Advanced*.

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
