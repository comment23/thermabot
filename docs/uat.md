# Customer UAT harness

For complete release-candidate coverage, execution rules and acceptance gates, use the
[full customer UAT plan](uat-test-plan.md).

The lab runs the **real Thermabot application, controller, scheduler and SQLite history**,
not a dashboard populated with synthetic snapshots. Home Assistant REST, weather and
heartbeat traffic use in-process test transports. No VM or running Home Assistant instance
is required for these journeys.

This is developer tooling; it requires the development lock and the repository's `tests`
directory. It is intentionally absent from the production/add-on images.

## Safety boundaries

- The application gets only generic fixtures from `tests/house.py` and the existing mock HA.
- Every environment field is explicitly supplied. No `.env`, live HA token, notification
  credential or process-environment credential is loaded.
- New databases keep the normal defaults: stopped, kill switch off, run mode SHADOW.
- The lab refuses Control/ACTIVE requests before application execution. Its in-process HA
  transport also refuses every climate service call and records the attempt as a failure.
- Scenario controls require a custom same-origin header. The launcher binds only to loopback;
  there is deliberately no `--host` argument.
- Existing nonempty data directories are refused unless they carry the lab's own marker.
  Never use a production database, backup, data directory or credentials with this harness.
- Browser tests use fresh task-owned contexts and abort external browser requests.

These checks do **not** test physical actuation, a real HA installation, Supervisor/add-on
packaging, the independent watchdog, or the seven-day live soak. Passing UAT does not
authorize Control or a stable release.

## Prerequisites

Use Python **3.12**, the unchanged hashed development lock, and Playwright Chromium, as
described in [CONTRIBUTING](../CONTRIBUTING.md). Run commands from the repository root.

On Windows, after creating the normal development environment:

```powershell
$py = '.venv\Scripts\python.exe'
& $py -m pip install --require-hashes -r requirements.lock
& $py -m playwright install chromium
```

On an isolated Ubuntu 24.04 test VM, create a Python 3.12 virtual environment and install
the same lock and Chromium. Browser OS prerequisites must be provisioned in that disposable
VM, not installed automatically by the harness on a shared host.

## Manual customer session

```powershell
# Configured generic house, stopped. Only this dedicated folder is created/used.
& $py -m scripts.uat --data-dir .uat-demo --port 8096

# Separate first-run journey: no accounts and no saved thermostat.
& $py -m scripts.uat --data-dir .uat-fresh --fresh --port 8096
```

Open `http://127.0.0.1:8096/uat`. The lab panel opens Thermabot in another tab and supplies
scenario controls and live runtime/history evidence.

Configured demo accounts use the existing test password `correct horse`:

| Login | Role | Assigned room |
| --- | --- | --- |
| alex | Owner | None |
| ann | Admin | None |
| jordan | Member | bedroom_1 |
| bea | Member | study |

A fresh lab has **no default account**: create its initial Owner through the normal UI.
Complete setup while stopped. A thermostat selection requires a real process restart;
Ctrl+C, then rerun the same command/data directory. Saved accounts/settings survive.
`--fresh` affects only a new directory; it does not erase an existing lab.
Use a new dedicated directory for another clean session. No automated destructive reset
or database import is offered.

| Scenario | Expected customer behavior |
| --- | --- |
| Healthy | Fresh room readings appear in Home and `/api/status`, and in local history |
| Celsius | Study reports 20 C; internal/history value is 68 F; display honors chosen units |
| Stale room | Study last reported two hours ago; temperature is hidden, marked stale |
| Missing room | Unavailable study does not reuse its previous temperature |
| Away | Trackers report away; real presence grace periods apply, without time acceleration |
| HA outage | Disconnection is visible, live snapshot clears, no thermostat command is sent |
| Healthy after fault | Home refreshes automatically and current readings return |

Devices report on every normal 60-second tick. **Apply scenario and sample now** runs an
immediate real runtime tick. History is stored per minute: repeated actions within the same
minute replace that minute's row rather than creating artificial extra minutes of evidence.
Mock weather forecasts regenerate relative to each fetch, so a multi-day lab session does
not accidentally age out the original fixture dates.

## Automated acceptance and evidence

```powershell
& $py -m pytest tests\uat tests\integration\test_uat_lab.py `
  -q --tb=short --junitxml=junit-uat.xml --uat-artifacts=.uat-results
```

The suite checks customer actions end to end:

- Fresh Owner creation, complete setup, actual application shutdown/restart, and persistence
  on phone and desktop.
- Settings tabs through Backup, Health, Account and Users, including role restrictions.
- Add/remove presence people, draft-before-save, reload, and persisted mappings.
- Full-runtime sensor/history/dashboard agreement, stale/unavailable readings and HA recovery.
- Watch/Stop with zero thermostat writes, Member access denial, and the manual scenario panel.
- Guest occupancy, Shadow override outcome/cancel, pause/cancel and a saved comfort band
  reaching the controller on its next tick.
- Owner-created users, live role changes and session invalidation after deletion.
- Read-only import preview, confirmed stopped import, pre-import backup and integrity of
  the downloaded database copy.
- Read-only Plan, Insights, history, forecast, efficiency, Shadow, model and health screens,
  without external browser requests or console errors.
- Refusal of existing data, isolation from injected environment credentials, and both
  SHADOW-only and transport-level write barriers.

JUnit includes requirement tags and all journey outcomes. The page-fixture journeys also
save screenshots and live JSON evidence in `.uat-results` when requested, on pass or fail.
Onboarding/restart results are in JUnit. Nothing is uploaded.

Check the collected count: this repository's root pytest hook maps an empty suite to exit 0.
Zero collected is **not** a pass. Any unexpected climate request fails UAT even if the lab
blocked it. Inspect failures; do not waive safeguards or switch the lab to ACTIVE.

For the existing broader BRD mapping, optionally run:

```powershell
& $py -m scripts.qa_report junit-uat.xml -o .uat-results\requirements.md
```

Uncovered requirements remain **NOT COVERED**; this customer suite is not the entire QA suite.

## Optional VM handoff

A small disposable Linux VM (2 vCPU, 4 GB RAM, 20 GB disk) is sufficient. Provision a **new**
lab resource, never repurpose the production HA VM. Use isolated networking with no route
to production HA; stage OS/dependencies through an approved offline or restricted mechanism.
Do not relax isolation just to install packages.

Once an authorized operator has staged the approved source and dependencies, run the same
module in the VM with a dedicated data directory. Keep the listener on VM loopback.
Use an approved console or SSH tunnel to reach it; never expose the unauthenticated lab
scenario panel to the LAN/WAN. Do not include host addresses, SSH keys, or credentials in
source, reports or GitHub.

An in-process simulated HA lab validates application customer journeys. A real HA OS/add-on
compatibility environment is a separate test stage and must remain explicitly identified
as unverified until provisioned and exercised.
