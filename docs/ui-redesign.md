# Thermabot UI redesign

The interface is a calm status screen. It answers three questions: what mode the bot is in, what it will do next, and why. The stack stays server-rendered: Jinja2, vendored htmx, a small `app.js`, and uPlot for long history charts. There is no SPA framework, no build step, and no request to a host outside this app.

## Status bar

Every signed-in page shows one status bar:

- A mode pill: **Stopped**, **Shadow**, or **Active**. Setup needed uses the same pill in gray.
- A link to the Owner's Watch evidence review, rather than an elapsed-time-only progress claim.
- Home Assistant and weather chips. Color is always paired with the word and a dot.
- **Stop**, one tap that opens a sheet. In Watch the sheet says no thermostat write is made;
  in Control it shows the configured stop band and explains that safety checks may prevent
  the handoff. **Start watching** always starts in Shadow.
- On a phone the bar keeps the mode pill and Stop. Soak and health chips hide.

Control is not a one-tap action. The button on Safety & control stays disabled until the
readiness checklist and Owner review are complete. The server independently enforces the
same checks and typed confirmation.

## Navigation

Four sections: Home, Plan, Insights, Settings. The phone uses the same four as a bottom tab bar. From 600px the tabs become a side rail of icons; from 900px the rail shows labels. A member sees Home and Insights only. Capabilities still hide everything else: a member can open their room and read bands, and cannot open Settings.

Settings tasks:

| Page | What it holds |
| --- | --- |
| Comfort | Bands, display units, season, weather-aware ranges, nudges, overrides |
| Schedule | Week rows for weekday and weekend blocks, band sliders, runtime preview |
| Safety & control | Stopped / Shadow / Active, readiness, tighten-only limits |
| Home Assistant | Thermostat, sensors, weather, link to the setup wizard |
| People & rooms | Rooms, weights, presence |
| Advanced | Planner, metrics, alerts, summary, and the read-only JSON view |
| Backup, Health, Account | Existing recovery, health, and password pages |

## Home

Home is five cards: Right now (hero temperature in display units, HVAC action, comfort-band gauge), Next recommendation (one sentence, three reasons, a Shadow · not sent tag, Why this? and See the plan), Next 24 hours (one SVG time axis plus a table), room tiles, and Quick actions (Pause, Guest, Override). The old controls panel is gone. Members land on their room and the read-only bands.

`GET /partials/{now,next,timeline,rooms,quick}` returns those cards. `app.js` refreshes Right now and Rooms on `tb:snapshot`, and the recommendation and timeline on `tb:plan`, with `htmx.ajax`.

## Why a step

`GET /plan/{i}/why` is a page and, with the `HX-Request` header, a fragment. It shows:

- Why: schedule block, weather source and age, thermal model version and error, presence.
- Predicted effect: comfort score, predicted inside temperature, coldest room, duty versus holding the setpoint.
- Safety checks, in `guards.evaluate` order: readable, stop switch, override, allowlist, hard limits, off guardrails, rate limit, run mode. In Shadow the last row reads "Run mode: stops here, nothing is sent."

Each plan step stores tagged `factors`, the `evaluate()` `Decision`, and the full check list when a replan is saved. The plan chart pads a one-step axis so it no longer stretches across years, and the action line says "Heat to 67°F" instead of "heat heat 67.0".

## Safety

`GET /api/readiness` (Owner) reports tracked seven-day Watch evidence, a fitted model,
Home Assistant health, sensors, device capabilities, the stop band, preliminary safety-net
configuration, and a current plan. Configuration is not proof that the HA safety net works.
Control requires the Owner's review of the history and independent safety net, plus the typed
word `ACTIVE`. The [setup guide](setup.md#watch-control-and-stop) documents the evidence
thresholds and restart behavior. Failed handoffs and Active-to-Watch restarts produce
persistent notices. Limits on this page remain tighten-only; saving shows a diff and asks
for `SAVE`.

## Sheets

`window.confirm` is not used. Deletes, safety saves, and backup restore use a `<dialog>` sheet that traps focus, returns focus, and names the consequence. Deleting a user requires typing that user's name. Restoring a backup requires typing `RESTORE`.

## Units and states

A `temp` Jinja filter and `tb.temp` render every temperature in the display unit. Stored values stay in Fahrenheit.

- No thermostat: gray Setup needed pill and one card.
- No reading yet: a waiting line.
- Home Assistant down: amber chip and the existing banner. Writes stay paused.
- Stale thermostat: values dim and the page says writes are paused.
- Weather down: the weather chip says stale and the plan says it is a fallback.
- Degraded: an amber ring on the mode pill and the reason in words.
- Live updates: a "live" tag. If the event stream drops, the tag says it is reconnecting and status polls every 30 seconds. The live region announces mode changes only.
- Stale room: dashed tile, an amber "Stale" tag, and the value hidden.

## Embedding

Links stay relative to the current page, with `../` on nested routes so Home Assistant ingress still resolves them. `?embed=1` hides the brand name and is remembered for the session. Motion is disabled when `prefers-reduced-motion` is set. Sheets can be dragged down by the handle.

## What did not change

Session CSRF, role checks, the single guarded thermostat write path, and Fahrenheit storage
remain in place. Stop requires no typed confirmation; it always stops the bot even when an
Active handoff cannot be confirmed.
