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

## Room insights, temperature table and efficiency indicators

Open a room from Home to see its read-only report. Members can inspect only their assigned
room; Owners and Admins can select any configured room. Existing nudge permissions and
limits are unchanged.

- The **room summary** shows the latest temperature, its difference from the configured
  ideal (73 F by default), last-week ideal-band comfort and the highest-priority action.
  Unknown-age, stale or estimated live readings never produce an ideal-temperature delta.
- **Overview**, **Temperatures**, **Efficiency** and **Tools** are server-rendered tabs.
  Room selection and the history range persist between tabs; navigation and the
  temperature table work without JavaScript. **Adjust room / Nudges** is always accessible
  above the tabs, with active nudges expanded for cancellation.
- The **Temperatures** tab charts interval means with an ideal-reference line and shaded
  tolerance band. Missing/stale intervals stay as gaps, including when only a single
  measured interval is present. The table remains the accessible alternative. Charts use
  the existing local uPlot assets and configured Fahrenheit/Celsius display and time zone.
  Table temperatures are rounded for display only; calculations retain sample precision.
- The **Tools setup checklist** checks actual live-temperature, model-validation and
  forecast gates before a simulation is requested; it does not run simulations or save
  settings. It checks the default four-hour horizon plus four hours recovery; a longer
  request rechecks coverage. Optional humidity/meter inputs do not block simulations.
  Links go to existing settings/model pages only when the viewer has permission.
- Simulation results show **Baseline / Proposed** runtime and ideal-band-comfort bars,
  projected room ranges, estimated costs and differences. They remain explicitly
  SIMULATED, with central-HVAC versus room scope and safety warnings.
- Period labels distinguish the selected history range, fixed last-seven-day diagnostics
  and two completed 14-day weather windows. Scheduled-band comfort is separate from the
  ideal-band metric; central HVAC diagnostics are not attributed to an individual room.

- Choose **24 h**, **7 days**, or **30 days**. Tables use 1-hour, 6-hour, or 24-hour elapsed
  intervals respectively, display the configured local time zone, and retain gaps.
- Insights show the mean, minimum, maximum, paired room-minus-thermostat difference and,
  when both latest intervals have at least five measurements, the change between them.
  Means weight each sampled minute equally, not each interval equally.
- Missing temperatures, unknown/negative sensor ages, readings older than the room's
  configured stale threshold, and recorded stale-room flags are excluded. Sensor samples
  are measured inputs; model estimates are not substituted. Coverage is fresh measured
  minutes divided by all elapsed minutes in the selected period, not just collected rows.
  New samples record thermostat reading age too; older thermostat samples without that
  field are explicitly unverified and excluded from these room metrics.
- Comfort indicators compare measurements with the recorded schedule block's band using
  **current saved settings**, room tolerance and the current forecast season. Without a
  usable forecast, the reference is the shoulder-season band. Historical configuration,
  weather-dependent seasons, humidity tightening and nudges are **not reconstructed**.
  Missing schedule blocks are excluded from comfort comparisons.
- Within-band percentage and outside-band minutes describe only assessed measurements.
  Mean excursion is the average distance outside the reference band, including zeros for
  in-band minutes. It is a temperature difference, including when displayed in Celsius.
- Heating/cooling minutes are central HVAC observations during fresh room measurements.
  They are **not independently measured room runtime, kWh, savings or equipment efficiency**.
  Unknown runtime remains unavailable. The whole-home efficiency page retains its
  weather-normalized curves and explicitly simulated savings.

Reports aggregate the existing samples in SQLite and return at most 30 table intervals.
They do not create database rows, change plans or call Home Assistant services. Assigned
equipment remains passive inventory, not permission to meter or control a device.

## Research-backed next ideas

All ten features below are implemented as read-only evidence/tools, except that schedule
approval explicitly saves a reviewed household schedule and recording an experiment saves
its metadata. None enables Control or adds an actuator write path.

1. Sensor-quality diagnostics for stale data, persistent disagreement and calibration drift.
2. Weather-normalized performance alerts for increasing runtime under comparable conditions.
3. Setback what-if comparisons showing simulated runtime and room comfort trade-offs.
4. Meter-backed energy and cost reporting, separating actual measurements from allocations.
5. Tariff-aware preheat/precool recommendations with comfort and equipment constraints.
6. Heat-pump diagnostics for auxiliary heat and excessive cycling, where signals exist.
7. Humidity-aware room comfort, with sensor-source and freshness labels.
8. Approval-based schedule suggestions when observed occupancy differs from planned occupancy.
9. Before/after experiments for insulation, shading and maintenance, adjusted for weather.
10. Weekly room action reports ranked by evidence, expected benefit and comfort impact.

### Using the research features

- **Ideal reference:** 73 F (22.8 C) with a default 1 F tolerance. Configure
  `insights.ideal_temp_f` and `insights.tolerance_f` in Settings > Advanced. This does not
  silently overwrite existing thermostat comfort bands. The weekly report compares
  measured room temperatures to this reference, separately from the scheduled-band table.
- **Sensors:** Settings > Home & rooms lets you select each room's optional humidity sensor
  and explicitly dedicated energy meter. Settings > Home Assistant exposes central HVAC
  `sensors.energy`, `sensors.power`, and `sensors.auxiliary_heat` mappings. Only configured
  mappings are read; passive equipment inventory does not infer a meter or grant control.
  Energy accepts kWh, Wh or MWh; power accepts W or kW; humidity accepts percent. Unsupported
  units are logged and discarded. Sensor age/source accompanies the stored measurements.
- **Sensor quality:** last-week coverage and persistent paired disagreement (at least 120
  fresh pairs, a mean offset of at least 2 F, and at least 80% consistently beyond 2 F).
  This prompts a placement/airflow/calibration check, not a claim of proven sensor drift.
- **Humidity:** joint temperature/RH comfort requires fresh paired measurements. The
  reference RH range is 30-60%, with no invented feels-like temperature or health diagnosis.
- **Energy:** uses cumulative meter deltas, or integrates measured power when no cumulative
  meter readings exist. Only adjacent intervals up to two minutes with the same meter
  source qualify; gaps, resets and source changes are excluded and counted. Coverage is
  shown, not extrapolated. Central HVAC energy is never allocated to rooms without meters.
- **Tariffs:** configure both `insights.off_peak_rate` and `insights.peak_rate`, currency,
  local peak start/end hours, and optionally `rated_power_kw` for simulated costs. The
  two-tier peak window cannot cross midnight. Meter-backed costs estimate covered energy
  at configured local-hour rates; they do not include fixed charges or guarantee a bill.
- **What-if tools:** compare a setback or tariff shift with the 73 F reference baseline.
  They require fresh room/thermostat measurements, a complete fresh forecast, a validated
  model with acceptable thermostat MAE, and a model fitted within 30 days. Each comparison
  includes four recovery hours. Room projections explicitly assume a constant current
  room/thermostat offset; there is no independent room thermal model. Simulated costs
  require an explicitly configured rated-power assumption. Comfort loss or predicted
  safety breaches prevent a recommendation. The tools never apply setpoints.
- **Weather alerts:** compare the preceding and latest 14-day windows of central runtime
  per degree-day. Each needs five qualifying days within 5 F of baseline median outdoor
  temperature; a rise of at least 20% prompts review. This is a weather-adjusted runtime
  proxy, not causal energy savings.
- **Heat-pump/HVAC diagnostics:** detect observed starts and completed short runs only
  across contiguous, known equipment-state samples. Three runs shorter than three minutes
  prompt review. Actual auxiliary heat requires its own fresh mapped signal; temperature
  spikes do not stand in for auxiliary-heat measurements. Compressor speed is not kWh.
- **Schedule suggestions:** at least five days, each with 48 or more samples in a complete
  scheduled Home hour, must consistently show at least 80% Away. Owner/Admin approval
  changes only that weekday/weekend hour, preserves other boundaries and restores the
  prior block afterward. Stale settings fingerprints or lost evidence reject approval.
- **Experiments:** record an improvement and its change date (today or within the last
  14 days). Migration 005 adds `room_experiments`; existing samples are untouched. The
  report compares 14-day weather-normalized windows before and after the change, waits
  for adequate evidence, and flags later settings changes as a confounder. Completed
  comparisons remain readable beyond the room's 30-day sample window while their daily
  rollups remain available.
- **Weekly actions:** the room page automatically summarizes the last seven days and
  ranks sensor, comfort, humidity, equipment and performance findings with their evidence.
  These are on-demand local reports, not outbound notifications or guaranteed savings.

Authenticated endpoints are `GET /api/rooms/{room}/insights`,
`POST /api/rooms/{room}/what-if`, `POST /api/rooms/{room}/schedule-approval`, and
`POST /api/rooms/{room}/experiments`. Members can read/simulate only their assigned room.
Saving a schedule or experiment requires Owner/Admin permissions; browser POSTs require
CSRF. New reports use at most 30 days of raw samples and do not silently replace missing
inputs with assumed measurements.

Sources reviewed November 25, 2026:

- [ecobee eco+](https://www.ecobee.com/en-us/eco-plus/): humidity adjustments, schedule
  assistance, occupancy and time-of-use optimization.
- [tado Heat Pump Optimizer X](https://www.tado.com/en/products/heat-pumps): room-guided
  operation and equipment-integrated energy information.
- [ENERGY STAR smart thermostat guidance](https://www.energystar.gov/products/energy_star_home_upgrade/smart_thermostats):
  compatibility and real-world savings evidence rather than assumed savings.

Vendor savings claims are not predictions for a Thermabot household. New actuator behavior
requires separate authorization, SHADOW evaluation, equipment safeguards and Owner approval.
