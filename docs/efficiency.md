# Efficiency curve and savings over time (v0.5.0)

The Efficiency page (More > Efficiency, and the dashboard tile) shows how much your system
runs for a given amount of weather, and how that changes over time. `GET /api/efficiency`
returns the same data as JSON to any signed-in role. Everything here is read-only: it never
feeds the planner or the controller and never writes to Home Assistant.

## Degree-days (base 65 °F)

A degree-day measures how much heating or cooling the weather asked for:

- **HDD** (heating degree-days) for a day = the mean over its hours of `max(0, 65 - T)`.
- **CDD** (cooling degree-days) = the mean over its hours of `max(0, T - 65)`.

`T` is the hourly mean outdoor temperature, taken from the 1-minute samples (the
configured outdoor sensor, or the forecast temperature when the sensor has no value).
Integrating hour by hour, not using the daily mean, means a day that is cold at night and
warm in the afternoon counts both heating and cooling degree-days. 65 °F is the usual US
base (`metrics.hdd_base_f`). A daylight-saving transition day has 23 or 25 elapsed hours;
both occurrences of a repeated hour are included.

## Runtime per degree-day

For each completed local day, the bot stores (table `efficiency_daily`) the mean outdoor
temperature, HDD, CDD, heating minutes, cooling minutes (counted the same way as the daily
metrics: `hvac_action`, plus compressor speed when configured) and the sample coverage.

A day **qualifies** for a season when:

- it has more than **2** degree-days of that kind (HDD for heat, CDD for cool), and
- at least **80%** of its minutes were sampled.

Its point is `y = runtime minutes / degree-days`, plotted against `x = mean outdoor °F`.

## The curve

With **5 or more** qualifying days, the page fits a curve to the points per season:

- a **robust linear fit** (Huber M-estimator via iteratively reweighted least squares), so a
  single odd day (a party, an open window, a sensor glitch) is down-weighted instead of
  pulling the line;
- a **quadratic** is used instead only with 10+ points and when it clearly fits better
  (adjusted R² at least 0.02 higher);
- the shaded **95% band** is the confidence band of the fitted curve (Student-t, n - p
  degrees of freedom). It shows how well the *average* behaviour is pinned down, not where a
  single future day will land;
- **n** and **R²** are shown under the chart.

Before 5 qualifying days the page shows "needs >= 5 qualifying days (k/5)".

### What it means

A lower curve means fewer runtime minutes for the same weather. Heating curves usually
rise as it gets colder (heat-pump capacity drops, losses grow faster); a cooling curve
often rises with heat and sun. Comparing the curve over months shows whether the house
plus HVAC is getting more or less efficient (filters, insulation, setpoint strategy).

## Efficiency index

Once a season has **7** qualifying days, the index is

`100 x median(y over qualifying days in the last 7 calendar days) / median(y of the first 7 qualifying days)`.

100 = the same runtime per degree-day as at the start; 90 = 10% less. The dashboard tile
shows the latest index of the most recent season, or "collecting data k/5".

## Savings over time (SIMULATED in SHADOW)

From `shadow_daily` (written at 00:05 for the previous day): per-day and cumulative saved
runtime minutes and %. **In SHADOW these numbers are simulated**: the actual runtime is
measured, but the bot's runtime is a thermal-model simulation of its dry-run plan. The page
labels them SIMULATED. Days without a fitted model have no bot estimate and do not count.

## Limits

- Runtime minutes are a proxy for energy, not kWh. Variable-speed systems and auxiliary
  heat are only partly captured.
- Degree-days ignore sun, wind, humidity, internal gains (cooking, people) and setpoint
  changes. A changed schedule moves the curve as much as a better furnace would.
- Few points, or points over a narrow temperature range, make the band wide. Do not read
  the curve outside the plotted temperature range.
- Shoulder-season days with 2 or fewer degree-days are excluded on purpose: dividing by a
  tiny degree-day count blows up the ratio.
- The base 65 °F is a convention; the real balance point of a house is often lower.

## Data and storage

- Migration `004_efficiency` adds the `efficiency_daily` table (forward-only; nothing else
  changes). The nightly 00:05 rollup writes yesterday's row.
- On start-up a one-time **backfill** fills completed days that have samples but no row
  (within the 120-day sample retention), one day per chunk with a pause in between. It is
  idempotent and never modifies `samples`. The log line is
  `efficiency backfill done: N days written of M candidates; qualifying heat H, cool C; ...`.
- Existing stored rows are not automatically recalculated when the daylight-saving
  calculation changes.
