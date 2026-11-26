# Full customer UAT plan

## Target and purpose

Plan version 3, updated 2026-11-26 for application **0.7.0** and add-on candidate
**0.7.0-rc.1**. Current execution target is the unpublished working candidate, including
room research tools, room UI and Simple preferences. Record HEAD, dirty-source identity
and application-source SHA-256 before/after; a HEAD commit alone does not identify
uncommitted code. This run does not certify an existing tag or published image.

Historical version-1 target (retain previous evidence under that identity):

- Published prerelease **v0.6.8-rc.11**.
- Peeled Git commit **6abffee7bb3669835d45752bb8f837bd847b2072**.
- Application version **0.6.8**; Home Assistant add-on version **0.6.8-rc.11**.
- The working branch and its uncommitted fixes are **not** the published RC.

Validate a customer's complete installation, setup, daily-use, recovery and failure
experience, not merely whether individual functions pass tests. Keep candidate application
bytes unchanged during the run. Record original regression results separately from the
new full-runtime customer harness; seeded UI snapshots are not proof of HA-to-dashboard
data flow.

This plan includes external installation and live-soak gates even when they cannot be
executed safely in automated UAT. **NOT RUN, BLOCKED and partial evidence are not PASS.**
See the [harness guide](uat.md) and [release gate](v0.6.8-plan.md).

### Maintenance and pre-GitHub QA gate

Every program/UI/API/schema change must update the affected UAT cases, scenario tooling and
customer journeys before shipping. Run current-source UAT as part of QA **before push,
merge or publication to GitHub**; stale acceptance or unresolved P0/P1 failures is a no-go.
Local working-candidate acceptance and later immutable-tag/image provenance are different
stages. Record the change scope and all exclusions; a new version number does not waive
the live-soak or Owner-review gates. See [CONTRIBUTING](../CONTRIBUTING.md#before-you-push).

## Environment and safety

1. For published-candidate validation, freeze the tag into a dedicated test directory using
   `git -c core.autocrlf=false -c core.eol=lf archive`; verify the remote tag's peeled
   commit and every extracted Git blob hash. Plain Windows `git archive` can honor
   `core.autocrlf` and alter vendor bytes. Never change global Git settings to work around it.
   For pre-publication QA, fingerprint all candidate application/configuration/manifest
   files, including untracked source, and verify the digest before/after. Do not treat
   working source as the older RC. Product fixes invalidate affected acceptance: preserve
   the failure, compute the new source identity and retest dependent cases.
2. Use Python 3.12 and the matching hashed development lock. Record OS, interpreter,
   browser, source/archive hashes, commands, exits and collection counts.
3. Execute on Windows loopback and the disposable isolated Linux UAT VM. VM dependency
   installation is offline; retain its lack of a route to production HA.
4. Use only generic fixtures from [the test house](../tests/house.py) and
   [mock HA](../tests/integration/mock_ha/app.py). Never import a real household database,
   `.env`, token, account, entity mapping, host address or unredacted log.
5. Customer/browser sessions remain stopped or Watch/SHADOW. Never start a real or
   standalone ACTIVE service, weaken guards, backdate Watch, or actuate HVAC.
   Existing controller/API regression fixtures may exercise synthetic control decisions
   solely against mock transports; that is not a customer control trial.
6. Use fresh task-owned browser contexts. No personal profiles, external browser requests,
   cookie exports, public uploads, shared-process kills or production changes.
7. Keep new harness files separate from the frozen RC. Overlay only test tooling and its
   test helpers into a second snapshot, never application, templates, static assets,
   migrations, configuration defaults, adapters or core.
8. For UAT-67-71 use the qualified stopped-only `room-history` / `room-model` replays.
   Validate the actual daily rollup coverage/counts and model training/holdout results
   before accepting positive customer outcomes. Replayed minutes and an injected
   retention clock are explicitly simulated and never satisfy UAT-58's real soak.

### Personas and matrix

- Owner: generic `alex`; Admin: `ann`; Member: `jordan`/`bea`; newly added account: `sam`.
  Use the existing generic test password, never customer credentials.
- Fresh installation, configured installation, saved/imported legacy configuration, and
  isolated backup copy.
- Phone 375/390 px, tablet 768 px, desktop 1440 px; light/dark, keyboard-only and touch
  emulation. Record which matrix combinations actually ran.
- Fahrenheit/Celsius; occupied, guest, away/grace, unknown presence; fresh, stale,
  unavailable and future/missing-timestamp sensors.
- Healthy HA, rejected authorization, disconnection/reconnection, missing entities,
  failed/slow reads and weather outage.
- Chromium emulation is not a physical Safari/iOS, Firefox or real-device certification.

## Evidence suites

Each case below links to one or more evidence groups. Run complete existing suites, then
run customer journeys with the real runtime/controller/scheduler and no hand-seeded live
snapshots or replacement controllers.

| Code | Evidence |
| --- | --- |
| A | Frozen tag/hash, versions, [add-on manifest](../addon/config.yaml), published image digests and exact-commit CI; Ruff, strict mypy, JS syntax and manifest validator |
| S | [Setup UI](../tests/e2e/test_setup_wizard.py), [first-run UI](../tests/e2e/test_first_run_ui.py), [first-run API](../tests/integration/test_first_run.py), [setup-needed](../tests/integration/test_setup_needed.py), [setup API](../tests/integration/test_setup_api.py) |
| R | [Auth](../tests/unit/test_auth.py), [permissions](../tests/unit/test_permissions.py), [users UI](../tests/e2e/test_p7_users.py), [password UI](../tests/e2e/test_p7_password.py), [password API](../tests/integration/test_api_password.py) |
| C | [Settings UI](../tests/e2e/test_p3_settings.py), [Simple preferences UI](../tests/e2e/test_simple_settings.py), [preference API](../tests/integration/test_preferences_api.py), [preference policy](../tests/unit/test_preferences.py), [configuration](../tests/unit/test_config.py), [schedule](../tests/unit/test_schedule.py), [seasonal runtime](../tests/integration/test_seasonal_runtime.py) |
| L | [Controls UI](../tests/e2e/test_controls_recovery.py), [kill API](../tests/integration/test_kill_api.py), [Watch continuity](../tests/integration/test_kill_soak.py), [state machine](../tests/unit/test_statemachine.py) |
| P | [Presence policy](../tests/unit/test_presence.py), [runtime presence](../tests/integration/test_presence_wiring.py) |
| T | [Runtime jobs](../tests/integration/test_jobs.py), [thermostat parsing](../tests/unit/test_thermostat_parse.py), [comfort freshness](../tests/unit/test_comfort_stale.py), [HA REST](../tests/integration/test_ha_rest.py) |
| F | [Planner](../tests/unit/test_planner.py), [fallback](../tests/unit/test_fallback.py), [thermal model](../tests/unit/test_thermal.py), [learning](../tests/integration/test_shadow_learning.py), [Plan UI](../tests/e2e/test_p2_plan.py) |
| W | [NWS](../tests/integration/test_nws.py), [Open-Meteo](../tests/integration/test_open_meteo.py), [forecast API](../tests/integration/test_forecast_api.py), [forecast UI](../tests/e2e/test_forecast_page.py) |
| O | [Controller](../tests/integration/test_controller.py), [override policy](../tests/unit/test_overrides.py), [dashboard UI](../tests/e2e/test_p1_dashboard.py), [member room](../tests/e2e/test_p8_room.py) |
| H | [Health UI](../tests/e2e/test_p6_health.py), [history API](../tests/integration/test_history_api.py), [history UI](../tests/e2e/test_p4_history.py), [Shadow UI](../tests/e2e/test_shadow_page.py), [efficiency](../tests/integration/test_efficiency_rollup.py), [heartbeat](../tests/integration/test_heartbeat.py) |
| B | [Recovery](../tests/integration/test_recovery.py), [database](../tests/integration/test_db.py), [controls/recovery UI](../tests/e2e/test_controls_recovery.py), [crash](../tests/integration/test_crash.py) |
| V | [Layout](../tests/e2e/test_layout.py), [mobile](../tests/e2e/test_mobile.py), [visual/accessibility/budgets](../tests/e2e/test_ui_v060.py) |
| K | [Guards](../tests/unit/test_guards.py), [safety](../tests/unit/test_safety.py), [single write path](../tests/unit/test_single_write_path.py), [core purity](../tests/unit/test_core_purity.py) |
| U | [Full-runtime customer journeys](../tests/uat/test_customer_journeys.py), [customer operations](../tests/uat/test_customer_operations.py), [0.7 customer journeys](../tests/uat/test_customer_rooms_preferences.py), [lab boundaries](../tests/integration/test_uat_lab.py) |
| I | [Room insights policy](../tests/unit/test_room_insights.py), [room API](../tests/integration/test_room_insights_api.py), [room UI](../tests/e2e/test_p8_room.py), [full-runtime room journeys](../tests/uat/test_customer_rooms_preferences.py) |
| X | Explicitly authorized external/manual evidence, not fabricated by mocks or CI |

## Test cases

Priority: **P0** safety/access/data integrity; **P1** essential customer workflow;
**P2** usability/secondary behavior. For each case capture the tested persona/matrix,
steps reached, expected/actual outcome and evidence. A multi-step case is not a full PASS
when a prerequisite failed or only policy-level regression evidence exists.

### Installation, first run and saved state

| ID | Priority | Customer action / injection | Expected result | Evidence |
| --- | --- | --- | --- | --- |
| UAT-01 | P0 | Identify unpublished source or pin published tag; compare source digest, app/add-on versions and image refs | Exact candidate identified; source changes and publication status explicit; no older-tag certification | A |
| UAT-02 | P1 | Fresh directory; open Home; create initial Owner | Normal registration, signed-in setup; stopped/SHADOW defaults unchanged | S, U |
| UAT-03 | P0 | Invalid password confirmation, duplicate/concurrent initial registration | Visible errors; exactly one initial Owner; no control enabled | S, R |
| UAT-04 | P0 | Open an existing configured/imported install | No re-registration, overwritten accounts, forced wizard or replaced configuration | S, B |
| UAT-05 | P0 | Before selecting thermostat, run ticks and view setup | No individual climate read/write; setup-needed guidance remains visible | S, K, U |
| UAT-06 | P1 | Complete setup, save, stop process, restart same lab and sign in | Accounts, room/presence mappings and units persist; real readings appear; no climate write | S, T, U |
| UAT-07 | P1 | Read image metadata and simulate Supervisor's blank advanced options | Correct public amd64/aarch64 tags; Supervisor token reaches stub HA without pasted HA token | A, X |
| UAT-08 | P1 | Install exact add-on on isolated real HA OS/Supervisor; open ingress | Actual catalog/install/start/ingress flow works; record image digest and HA version | X |

### HA, home/rooms and presence

| ID | Priority | Customer action / injection | Expected result | Evidence |
| --- | --- | --- | --- | --- |
| UAT-09 | P1 | HA connection test and named entity discovery; inject auth/availability failure | Validated choices; explicit failure; saved mappings retained, never silently replaced | S, T |
| UAT-10 | P1 | Select supported thermostat, HVAC sensors and optional humidity | Only confirmed mappings saved; unsupported/missing selections explained | S, C, T |
| UAT-11 | P1 | Add floor/room, change sensor and comfort importance; save/reload | Home grouping and weights match saved configuration; new mappings take effect next tick | C, T, U |
| UAT-12 | P0 | Remove a room assigned to an account; remove another room | Assigned room protected; approved removal cleans dependent weights atomically | C, R |
| UAT-13 | P0 | Assign fan/cover/heater/door inventory to a room | Passive inventory only; no new actuator authorization or climate/service request | S, K |
| UAT-14 | P1 | In Settings add a presence person, save/reload, remove/save/reload | Arbitrary household supported; additions/removals remain drafts until save | P, C, U |
| UAT-15 | P1 | Blank/duplicate/excluded names, missing tracker, manual undiscovered tracker | Clear validation; no partial save; valid manual Settings mappings remain usable | C, P |
| UAT-16 | P1 | Fresh preferred tracker versus stale alternate; empty people; unknown/away | Configured precedence, conservative unknown handling and normal grace periods | P, U |
| UAT-17 | P1 | Enable Guest, inject away trackers, reload; then disable Guest | Real runtime occupancy stays occupied while Guest is on; saved flag and later recovery agree | P, U |
| UAT-18 | P2 | Sun/location missing, stale opening contact, brief/sustained door/garage cases | Read-only assessment distinguishes cases; no presumed shutdown or door actuation | S, K |

### Temperatures, comfort, schedule and weather

| ID | Priority | Customer action / injection | Expected result | Evidence |
| --- | --- | --- | --- | --- |
| UAT-19 | P1 | Known fresh room temperatures; run a real tick, inspect Home/My Room/status/history | Same normalized readings reach controller snapshot, APIs, pages and history, without reload | T, U |
| UAT-20 | P1 | Sensor Celsius/Fahrenheit and chosen display units | Correct conversion; thermostat, room, outdoor, history and UI values agree | T, C, U |
| UAT-21 | P0 | Old `last_changed`, fresh report/update; stale/missing/future timestamps | Unchanged-but-reported data remains fresh; stale/unknown/future data never becomes fresh | T, K, U |
| UAT-22 | P1 | One unavailable/unknown-unit/failed/slow sensor while others are healthy | No reuse of old temperature; visible unavailability; healthy readings preserved; failures logged | T, U |
| UAT-23 | P1 | Recover sensor/HA; exercise SSE and polling fallback | Live cards update automatically; stale/disconnected values clear and recover correctly | T, V, U |
| UAT-24 | P1 | Valid comfort edit as Admin; save/reload and run next tick | Persisted values reach actual controller settings; unrelated settings remain unchanged | C, U |
| UAT-25 | P0 | Overlapping/inverted bands, loosened safety values, invalid times/weights | Explicit errors; tighten-only limits and cross-field invariants enforced server-side | C, K |
| UAT-26 | P1 | Weekday/weekend schedules, timezone and both DST transitions | Correct block boundaries; 23/25-hour local days and repeated hour handled correctly | C, H |
| UAT-27 | P1 | Test NWS/Open-Meteo; change units/location; simulate outage/partial coverage | Clear source/freshness; safe documented fallback; no assumed full forecast | W, F |
| UAT-28 | P1 | Weather-aware comfort enabled/disabled; hot/cold/shoulder/away conditions | Documented bands/modes, deadband and no early preconditioning; unsupported devices blocked | C, W, K |
| UAT-29 | P2 | Keep lab running over multiple days and fetch forecast again | Mock dates regenerate; no accidental stale fixture or manufactured extra Watch minutes | W, U |

### Lifecycle, readiness and temporary actions

| ID | Priority | Customer action / injection | Expected result | Evidence |
| --- | --- | --- | --- | --- |
| UAT-30 | P0 | Start Watching, apply healthy tick, Stop, reload | Correct state/mirror; Stop persists; zero thermostat writes throughout customer session | L, K, U |
| UAT-31 | P0 | Complete Owner review/typed confirmation while readiness is blocked; refresh | Acknowledgements usable; blockers explicit; Control still unavailable; review not discarded | L |
| UAT-32 | P0 | Inspect rc.11 one-start Watch exception, reason length, reload and non-Watch blockers | Only evidence gate waivable; no standing bypass/history rewrite; all other checks mandatory | L, K |
| UAT-33 | P0 | Synthetic interrupted Watch/update/restart/Active-restart policy fixtures | Continuity preserved only when valid; restart-to-Shadow and fresh review enforced | L, B |
| UAT-34 | P0 | Synthetic failed Stop persistence/handoff/network response; observe other sessions | Fail closed, visible warning, blocked start/write path, guarded retry; no false success | L, K, B |
| UAT-35 | P1 | Request target override in Watch, run actual tick, reload, cancel | Request status reports blocked in Shadow; no thermostat write; cancellation returns to Watch | O, U |
| UAT-36 | P0 | Synthetic expired request, retries, device limits and manual wall-panel change | At-most-once/three-minute semantics; limits and manual override precedence preserved | O, K |
| UAT-37 | P1 | Pause plan, reload, tick and cancel; member nudge within assigned room | Pause persists/cancels, nudge capability boundaries respected; safety not disabled | O, R, U |
| UAT-38 | P0 | Unknown thermostat unit, unsupported mode/range/step, wrong entity/service | Guard and adapter layers reject; fallback never selects `off`; core remains pure | K, T, F |

### Daily operation, access and recovery

| ID | Priority | Customer action / injection | Expected result | Evidence |
| --- | --- | --- | --- | --- |
| UAT-39 | P1 | Review Plan/why-step, timeline, model without enough learning data | Honest fallback/evidence, understandable next action; no fabricated fitted model or savings | F, U |
| UAT-40 | P1 | History ranges, sampling, runtime/efficiency, UTC Shadow counts and local dates | Correct aggregates/units/freshness; missing data not fake zero; sampling stays per minute | H, F |
| UAT-41 | P1 | Model refit/adoption failure, backlog flush, HA/websocket outage and recovery | Previous safe state retained; failures explicit; sampling/heartbeat continue as designed | F, T, H |
| UAT-42 | P0 | Owner/Admin/Member pages and crafted API mutations, CSRF/lockout/service token | Backend capabilities enforced, no session/token leak; no role-by-UI-only authorization | R, L |
| UAT-43 | P1 | Owner creates Member, member signs in, Owner changes role then deletes account | Live sessions see changed permissions; deleted account cannot continue; audit retained | R, U |
| UAT-44 | P0 | Demote/delete last Owner; change password; disable account | Last Owner protected; password/session/lockout behavior matches policy | R |
| UAT-45 | P0 | Export config; preview modified import while stopped | No environment credentials; preview read-only; meaningful diff/fingerprint | B, U |
| UAT-46 | P0 | Confirm import; inject bad fingerprint/unknown/unsafe settings or backup failure | Atomic valid apply only, verified pre-import copy, failure blocks import; bot stays stopped | B, K, U |
| UAT-47 | P0 | Download manual backup; inspect isolated copy; nightly retention | SQLite integrity and expected contents pass; manual/pre-import copies not pruned | B, U |
| UAT-48 | P0 | Restore a generic backup offline into a new empty lab directory | Exact documented drill, preserved data, no live replacement route or auto-enable | B, X |
| UAT-49 | P1 | Health startup grace, heartbeat, Shadow CLI, alerts/summary delivery failures | Accurate schema/status, no silent success; report `--assert-no-sent` rejects sent evidence | H, L |
| UAT-50 | P0 | Real optional watchdog/heartbeat-loss and HA outage drill | Independent approved safety net verified; no climate writes or automatic resume | X |

### Usability, packaging and final gates

| ID | Priority | Customer action / injection | Expected result | Evidence |
| --- | --- | --- | --- | --- |
| UAT-51 | P1 | Settings -> Backup, Health, Account, Users -> another Settings task | Settings tab bar remains visible and active destination highlighted, subject to role | V, U |
| UAT-52 | P1 | Phone/tablet/desktop shell, status/actions, themes and account menu | No horizontal overflow; clear Stop/mode; navigation remains usable across supported widths | V |
| UAT-53 | P1 | Keyboard dialogs/focus, accessible names, touch targets and light/dark contrast | Existing checks pass: normal targets >=44 px (0.5 px tolerance), documented compact-header exception; WCAG AA computed contrast | V |
| UAT-54 | P2 | Cold-cache page loads and history chart downsampling | <=307,200 bytes per page excluding uPlot; charts <=500 plotted points; no console errors | V |
| UAT-55 | P1 | Relative links under ingress prefix, session cookies, back/forward/reload | No root-relative escape; correct secure-cookie handling; no lost draft without user action | V, R |
| UAT-56 | P1 | Exact published container image boot; amd64 and aarch64 provenance | Matching digest/version/startup; CI evidence separated from locally executed packaging tests | A, X |
| UAT-57 | P2 | Physical iOS/Safari and Firefox, screen reader, real touch/network conditions | Human/device results recorded; Chromium emulation alone cannot close this case | X |
| UAT-58 | P0 | Seven consecutive real SHADOW days, all degraded/stale intervals and UTC write counts | Zero sent thermostat writes, trusted continuous evidence and explained material intervals | X |
| UAT-59 | P0 | Owner reviews soak, independent safety net and unresolved defects | Explicit dated go/no-go; no implicit approval from green automated tests or rc.11 exception | X |
| UAT-60 | P1 | Reconcile all cases, defects, evidence and skipped/blocked stages | Scoped decision, reproducible results and retest list; no claim of a stable/live release | A, U, X |

### 0.7 room insights and simpler configuration

| ID | Priority | Customer action / injection | Expected result | Evidence |
| --- | --- | --- | --- | --- |
| UAT-61 | P1 | Owner/Admin opens Simple settings in F/C, phone/tablet/desktop, keyboard and light/dark | Saved ideal starts at 73 F; custom configuration stays untouched; three accessible priority positions and important-room choices | C, U |
| UAT-62 | P0 | Preview Comfort/Balanced/Efficiency, cancel, edit draft, re-preview and apply; tick/reload | Reviewed ideal +/-1/2/3 F bands; automatic deadband widening disclosed; equal Home/Sleep influence reaches real controller; Away/schedule/safety/mode preserved | C, U |
| UAT-63 | P0 | Another tab changes settings after preview; tamper draft/fingerprint; unsafe reference or fallback | Stale/changed/unsafe apply rejected explicitly; another review required; no partial save or loosened safety | C, K, U |
| UAT-64 | P1 | Open Advanced; edit numeric comfort/units and reload/tick | Legacy technical controls remain usable in Advanced; untouched 22.8 C preserves exactly 73 F; edited C follows 0.5 F rounding | C, U |
| UAT-65 | P0 | Map humidity/dedicated meter and passive equipment; run measured room scenario | Fresh source-identified pairs/deltas reach history/report; dedicated versus central scope explicit; no actuator authorization | I, T, K, U |
| UAT-66 | P1 | Summary -> Temperatures -> Efficiency -> Tools; change room and 24h/7d/30d | Current/ideal delta, coverage, period labels and rounded interval means agree; chart gaps stay null; table accessible; nudges reachable on every tab | I, V, U |
| UAT-67 | P1 | Quality/humidity/weekly actions with sparse/fresh/stale data; two completed weather windows and cycling/aux states | Insufficient evidence explicit; humidity 30-60%; no invented savings or auxiliary inference; qualifying disagreements/weather/cycling produce only evidence-backed actions | I, T, U |
| UAT-68 | P0 | Setback tool without model/readings/forecast, then with genuinely validated simulated model and sufficient forecast | Missing inputs rejected; ready request returns labeled baseline/proposed/recovery results, no setting changes or climate writes | I, F, W, U |
| UAT-69 | P1 | Tariff tool with missing/flat/distinct rates and rated-power assumptions | Constraints explicit; ready comparison labeled simulated; estimates never replace measured kWh or become automatic actuator policy | I, U |
| UAT-70 | P0 | No schedule evidence, then five qualifying Away days; cancel/approve, repeat with stale fingerprint | No fabricated suggestion; approval changes only shared schedule, preserves control/safety; stale approval rejected; Members cannot approve | I, C, R, U |
| UAT-71 | P1 | Record improvement, reload/restart; compare qualifying windows; change settings | Experiment persists in migration 005; sparse comparisons stay unavailable; completed comparisons retained; confounding settings change disclosed | I, B, U |
| UAT-72 | P0 | Member changes room query/crafts room or Simple API mutation | Assigned-room scope and Owner/Admin mutation boundaries enforced by server, not only hidden buttons | I, R, U |
| UAT-73 | P0 | Reconcile program changes with playbook, evidence and shipping checklist | Updated, current-candidate UAT is included in QA before any GitHub shipping; no stale or partial evidence reported as complete acceptance | A, U |

## Execution protocol

1. Execute the candidate unit suite with **unit-only core coverage >=90%**, integration
   suite, all existing E2E tests and static/manifest checks. Do not boost the unit coverage
   number by combining integration tests into that coverage run.
2. Run the updated full-runtime customer suite against unchanged candidate application
   code. For published tags use the separate tooling overlay; for working candidates
   identify the run as **unpublished 0.7 source UAT**. Verify source hashes before/after.
   A test-selector-only correction may be retested without calling it a product fix.
3. Capture JUnit requirement tags, command exits and nonzero collection; screenshots and
   JSON evidence for customer journeys. Keep original failures. Retry a genuinely
   random-port/browser infrastructure failure at most once with the exact selector and
   report both attempts; do not retry real product failures into a passing result.
4. Distinguish `FAIL` (observed mismatch), `BLOCKED` (prerequisite failed), `NOT RUN`
   (unavailable/unauthorized stage), `PARTIAL` (regression/mocked evidence only), and `PASS`
   (the defined step/criterion was actually satisfied).
5. Deduplicate cascading failures into root defect records. Record steps not reached,
   customer impact, severity, RC version, generic reproduction, evidence and retest IDs.
6. Do not fix or edit the RC during execution, hide assertions, exclude vendor integrity,
   weaken thresholds, waive safety checks, start deployments, publish releases, or open
   GitHub issues without a separate request.

### Exit decision

- **Customer UAT go:** executed critical workflows pass, no unresolved P0/P1 customer
  defects, collection/evidence trusted, and remaining external limitations explicitly
  accepted for the stated test scope.
- **No-go:** any unresolved P0/P1 workflow defect, untrusted evidence or failed mandatory
  automated safety/coverage check. Good seeded regression results cannot override a
  failing full-runtime customer journey.
- **Stable release remains blocked** until UAT-58/59 have real evidence. This plan/run does
  not authorize physical actuation or publication.

Store the execution report and raw evidence separately, identifying the exact RC, platform,
case dispositions, command outcomes, original failures, root defects, unrun gates and
required retests. Re-run affected and dependent cases against a **new immutable RC** after
fixes; do not certify unpublished working-tree changes as rc.11.
