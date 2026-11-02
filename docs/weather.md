# Weather and the 48 h forecast plan (v0.4.0)

thermabot plans 48 hours ahead from an hourly outdoor forecast. The forecast comes from a
pluggable **weather provider**:

| Provider | `weather.provider` | Coverage | Key |
|---|---|---|---|
| [Open-Meteo](https://open-meteo.com) | `open_meteo` | worldwide | none (default for new installs) |
| [NWS](https://www.weather.gov/documentation/services-web-api) | `nws` | US only | none (User-Agent only) |

An install configured before v0.4.0 has no `weather.provider` in its settings and keeps
**NWS**, which was the only provider before v0.4.0. Nothing changes unless an Owner/Admin
picks another provider.

## Settings

```json
"weather": {
  "provider": "open_meteo",
  "lat": null,
  "lon": null,
  "units": "auto",
  "stale_h": 3,
  "min_cover_h": 24,
  "bias_correct_above_f": 1
}
```

- `lat` / `lon`: both set (-90..90, -180..180) or both `null`. `null` = Home Assistant's home
  location (`zone.home`), as before v0.4.0.
- `units`: how the Forecast page shows temperatures (`auto` = `display.units`). Providers and the
  planner always work in °F.
- `stale_h`, `min_cover_h`: a forecast older than `stale_h` or covering fewer than `min_cover_h`
  hours ahead is stale. The planner then uses the fallback plan (which never chooses `off`) and
  the bot reports DEGRADED (weather).

Edit them in the setup wizard (step 6, *Weather*) or under *Settings > Weather*:

- **Use HA home location** reads `GET /api/config` from Home Assistant (read only) and fills in
  lat/lon.
- **Test fetch** fetches a forecast with the chosen provider and location and previews the next
  12 hours. It saves nothing and does not change the running bot's forecast.

A provider or location change takes effect at the next replan (hourly, or right after a save).

## Reliability

Each provider has a request timeout, up to 2 retries with backoff (0.5 s, 1 s) on network errors,
timeouts, HTTP 429 and 5xx, and a 30-minute cache. If a refresh fails, the provider serves its last
good forecast flagged `stale: true`. The replan also caps the fetch at 15 s and falls back to the
last forecast. A weather outage never stops the 60 s tick or the heartbeat.

## Forecast page and API

*More > Forecast* (or *48 h forecast & plan* on the dashboard) shows the next 48 hours: outdoor
temperature, the planned setpoint band, the expected indoor temperature and the expected
heat/cool runtime per hour, plus an hourly table. Until the thermal model is fitted, a
**"Fallback plan, model not fitted yet"** banner is shown, and the indoor/runtime estimates use
default model values.

`GET /api/forecast` (any signed-in role):

```json
{
  "provider": "nws", "source": "nws", "fetched_at": "...", "stale": false, "units": "F",
  "model_version": null,
  "projection": {
    "now": "...", "plan_kind": "fallback", "fallback": true, "model_fitted": false,
    "banner": "Fallback plan, model not fitted yet",
    "hours": [
      {"t": "...", "temp_f": 41.0, "humidity": 60, "conditions": "Cloudy", "wind_mph": 8,
       "cloud_pct": null, "mode": "heat_cool", "heat_sp": 68, "cool_sp": 76,
       "indoor_f": 68.0, "heat_min": 41.2, "cool_min": 0.0}
    ]
  }
}
```

The projection is a pure core function (`thermabot/core/projection.py`). It replays the current
plan through the planner's thermostat-control step and the thermal model. It is display only and
never feeds back into the plan.
