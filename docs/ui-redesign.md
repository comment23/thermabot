# Thermabot UI redesign

The interface is a calm status screen. It answers three questions: what mode the bot is in, what it will do next, and why. The stack stays server-rendered: Jinja2, vendored htmx, a small `app.js`, and uPlot for long history charts. There is no SPA framework, no build step, and no request to a host outside this app.

## Status bar

Every signed-in page shows one status bar:

- A mode pill: **Stopped**, **Shadow**, or **Active**. Setup needed uses the same pill in gray.
- Brand and mode stay together on the left; actions are grouped on the right.
- **System status** groups Home Assistant, weather, live-update status and the Owner's
  Watch evidence review in one expandable panel, rather than a row of competing pills.
  Its summary signals **Needs attention** or **Reconnecting** without opening the panel.
- **Stop**, one tap that opens a sheet. In Watch the sheet says no thermostat write is made;
  in Control it shows the configured stop band and explains that safety checks may prevent
  the handoff. **Start watching** always starts in Shadow.
- Stop uses the same compact, prominent button on every screen. On a phone the brand,
  mode, Stop and named status/account icons remain available; system details do not disappear.
- The account menu groups Account, Switch theme and Log out. Theme selection still persists.

Home and Safety & control show a dedicated Thermabot controls card. Start watching always
starts SHADOW; Stop explains whether a guarded comfort handoff will be attempted.

Control is not a one-tap action. Automatic readiness checks are separate from the Owner's
clickable acknowledgements. The acknowledgements and typed confirmation remain usable while
readiness is blocked, but Start control stays disabled until both parts are complete.
The page shows the next blocker and an expandable list, and Refresh checks updates readiness
without discarding the review. The server independently enforces all checks and `ACTIVE`.

## Navigation

Four sections: Home, Plan, Insights, Settings. The phone uses the same four as a bottom tab bar. From 600px the tabs become a side rail of icons; from 900px the rail shows labels. A member sees Home and Insights only. Capabilities still hide everything else: a member can open their room and read bands, and cannot open Settings.

Settings tasks:

| Page | What it holds |
| --- | --- |
| Comfort | Bands, display units, season, weather-aware ranges, nudges, overrides |
| Schedule | Week rows for weekday and weekend blocks, band sliders, runtime preview |
| Safety & control | Stopped / Shadow / Active, readiness, tighten-only limits |
| Home Assistant | Thermostat, sensors, weather, link to the setup wizard |
| Home & rooms | Floor overview, room/sensor/equipment editor, comfort importance, presence |
| Advanced | Planner, metrics, alerts, summary, and the read-only JSON view |
| Backup, Health, Account | Existing recovery, health, and password pages |

The Settings navigation stays visible on Backup, Health, Account and Users, with the
current destination highlighted. Links remain relative and capability-gated: administrators
can reach Health, while Backup and user management remain Owner-only. Members' Account page
does not expose the privileged Settings navigation.

## People and presence

Setup and Home & rooms share an **Add person** editor. Presence mappings are configurable
for any household; they are separate from Thermabot login accounts. Each person has a name,
a Home Assistant presence entity and an optional preferred tracker, which takes priority
while fresh. Names added or renamed in the editor become lowercase underscore-separated keys;
unchanged saved names are preserved.

Owners can refresh named `person` and `device_tracker` suggestions. Entity IDs can also be
entered manually in Settings, including saved entities that discovery cannot currently find.
Setup still verifies selected trackers against discovered Home Assistant entities.
Missing names/trackers, duplicate names and excluded people are reported before saving.

Adding, editing and removing people stays local to the form until Save succeeds. Removing
a mapping never deletes a Home Assistant entity or a login account. With no people
configured, the house always counts as occupied. Presence timing and the excluded list remain
editable alongside the people editor. These changes do not start or stop thermostat control.

## Home

Home starts with explicit lifecycle controls, followed by Right now, Next recommendation,
Next 24 hours, floor-grouped room tiles, and Quick actions. Configured rooms with no reading
remain visible as waiting; stale readings remain hidden. Members see their own room and
read-only bands, not lifecycle controls.

`GET /partials/{now,next,timeline,rooms,quick}` returns those cards. `app.js` refreshes Right now and Rooms on `tb:snapshot`, and the recommendation and timeline on `tb:plan`, with `htmx.ajax`.

Each control tick reads configured Home Assistant temperature sensors once, before the
controller evaluates room safety limits. The same readings feed the live snapshot and
historical sample. Celsius sensors are converted to Fahrenheit internally; room freshness
uses the newer of `last_reported` and `last_updated`, not the last temperature change.
Missing, stale or future timestamps never make a room reading fresh. A failed or timed-out
sensor is logged and shown as unavailable without discarding healthy rooms. Snapshot events
refresh the Home readings automatically; the polling fallback refreshes the same cards.
Room and auxiliary sensor mappings never authorize climate reads. Climate IDs in those
mappings are ignored and logged; only the configured thermostat's read path may fetch it.

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

## Floors, rooms and equipment

Setup and Home & rooms use the same editor rather than a flat generated field list.
Floor cards show their rooms; selecting a room moves focus to its editor. Add floor creates
a draft group, persisted through a room's `floor` label when settings are saved. An empty
draft floor is not a separate stored object or a Home Assistant registry change.

Add room requires a unique name and a temperature sensor. Sensor selectors show discovered
friendly names and retain saved-but-undiscovered mappings. Room keys remain stable for
accounts and door mappings. Comfort importance, tolerance, staleness and auxiliary-heat
observation are grouped under each room rather than scattered across separate maps.
Inclusion checkboxes are reserved for discovered suggestions; configured rooms use the
explicit Remove room action. Empty importance blocks remain empty rather than silently
assigning influence to the thermostat.
Removing a room also removes its weights; users or door-contact mappings must be reassigned
first. The server rejects deleting an account's assigned room in both setup and settings.

`rooms.<key>.devices` stores additional Home Assistant entity IDs for the local inventory.
Fans, covers, supplemental heaters and contacts can be associated with rooms, but this
metadata is not an allowlist, a new read/write path, or permission to actuate equipment.
Door assessment still requires its separate explicit role/relationship configuration.

Home Assistant settings offer named thermostat/sensor suggestions without automatically
selecting or saving them. Discovery remains Owner-only; existing settings retain their
role checks. All edits are drafts until the existing configuration save succeeds.

## Audit findings addressed

- Review boxes were disabled whenever any automatic readiness check failed, making them look
  broken. Reviews are now interactive while the Control gate remains intact.
- Three equal-width mode cards hid a long readiness checklist in a narrow column. Lifecycle
  actions, next blocker, full checks and Owner approval now have separate, readable sections.
- Settings could only edit existing room-map leaves, not add or remove rooms. The shared
  editor adds complete room workflows and cleans dependent weights.
- The setup Add room handler had an outdated argument list, producing an unchecked row and
  a boolean floor value. The duplicate row implementation has been replaced.
- Floor labels were repeated on a flat tile list rather than visualized as groups.
- Raw entity fields and unassignable equipment lists lacked a useful connection to rooms.
  Named suggestions and explicit read-only inventory assignments now make that relationship visible.

The design follows Home Assistant's [floor/area grouping](https://www.home-assistant.io/docs/organizing/)
and [GOV.UK checkbox guidance](https://design-system.service.gov.uk/components/checkboxes/):
clear hierarchy, named actions, grouped acknowledgements, and a large clickable label.
It keeps Jinja, native controls and local assets; no new frontend framework is required.

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
- Live updates: a "live" label in System status. If the event stream drops, System status
  signals reconnection and status polls every 30 seconds. The live region announces mode changes only.
- Stale room: dashed tile, an amber "Stale" tag, and the value hidden.

## Embedding

Links stay relative to the current page, with `../` on nested routes so Home Assistant ingress still resolves them. `?embed=1` hides the brand name and is remembered for the session. Motion is disabled when `prefers-reduced-motion` is set. Sheets can be dragged down by the handle.

## What did not change

Session CSRF, role checks, the single guarded thermostat write path, and Fahrenheit storage
remain in place. Stop requires no typed confirmation; it always stops the bot even when an
Active handoff cannot be confirmed.
