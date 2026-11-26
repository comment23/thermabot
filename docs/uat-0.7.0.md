# 0.7.0 source UAT execution report

Date: 2026-11-26. Application 0.7.0; add-on candidate 0.7.0-rc.1.
This is **unpublished working-candidate QA**, not acceptance of a published image,
a real Home Assistant installation, or a stable release.

## Decision

**PASS for the executed simulated customer journeys and automated QA.**
**Complete release acceptance is not granted.** The follow-up below completed the
previously partial extended-history/ready-model customer scenarios. External installation,
live soak and Owner sign-off have not run. These exclusions must be reviewed explicitly before a shipping decision;
green automated tests do not waive the remaining gates. Nothing was committed, merged,
pushed, tagged, published or deployed during this UAT task.

## Candidate and isolation

- Baseline HEAD: `b2cb1f4d6a39cd12225bb48428188b6186007b95`, plus the uncommitted 0.7 changes.
- Tested export application-source SHA-256:
  `97b82395d828ab5ef6323fb55564fbfe6f9428a9130507fcff72483e06d5c611`.
- Working application-source SHA-256:
  `6d322d091452924911bbcbe8927f3299ea9cdcd8d4f6f7b348a7582fb047a690`.
- Both fingerprints were unchanged before/after QA. Each covers 120 application,
  package, lock and add-on manifest files, including untracked application source.
  Method: SHA-256 over sorted relative names, NUL, and each file's SHA-256 digest.
- Windows, Python 3.12.12, Playwright 1.64.1, Chromium 156.0.8078.4.
- Real application/controller/scheduler/SQLite; in-process generic HA/weather transports,
  task-owned browser contexts, no production credentials or external browser traffic.
- The export differs from working application bytes only in verified vendor CRLF checkout
  conversion. Documented Git asset bytes were restored **in the export only**; working
  assets, expected hashes and global Git settings were not changed.
- Only the tracked public `.env.example` template was copied. Actual `.env` files,
  credentials and unrelated local tooling were excluded.

## Results and reproducible commands

Run from the verified candidate export. `$py` is the approved Python 3.12 interpreter;
`$evidence` and `$scratch` alias the dedicated paths outside the repository used in the
recorded executions. Raw JUnit and coverage files retain the actual results.

```powershell
& $py -m pytest tests\unit -q --tb=short `
  --cov=thermabot.core --cov-fail-under=90 --cov-report=term:skip-covered `
  --cov-report="xml:$evidence\uat-070-final-coverage.xml" `
  --junitxml="$evidence\uat-070-unit-final.xml" --basetemp="$scratch\uat-070-unit-final"
# PASS: 611 tests; unit-only core coverage 94.76% (required >=90%).

& $py -m pytest tests\integration tests\e2e -q --tb=short `
  --junitxml="$evidence\uat-070-regression.xml" --basetemp="$scratch\uat-070-regression"
# PASS: 748 tests, including raw vendor integrity, access, controller and room regressions.

& $py -m pytest tests\uat tests\integration\test_uat_lab.py -q --tb=short `
  --junitxml="$evidence\uat-070-acceptance.xml" `
  --uat-artifacts="$evidence\uat-070-acceptance-evidence" `
  --basetemp="$scratch\uat-070-acceptance"
# PASS: 33 full-runtime customer/lab cases.
```

The lab barriers also occur in the integration selector; counts are not summed as unique
tests. All commands collected nonzero tests. The existing FastAPI/httpx deprecation
warning was recorded; it did not fail any final suite.

Additional checks: `ruff check .`, `ruff format --check .`, `mypy --strict thermabot`,
`python scripts\check_addon_config.py`, `node --check` on every application JavaScript
file, and `git diff --check`: **PASS**. Strict typing covered 56 application source files.

### Extended customer acceptance follow-up

Executed on 2026-11-26 directly against the Windows working candidate. Only lab tooling,
customer tests and UAT documentation changed; no application remediation was needed by
these checks. The explicitly listed follow-up application/package/lock/add-on scope covers
124 files; before/after SHA-256:
`d09d6c5f045a7286758202409c2bb4843bd5732b15938b524e6bae05431db730`.
Its file scope differs from the original 120-file identity, so the two digests are not
interchangeable. Raw metadata retains the file list and matching before/after identity.

```powershell
& $py -m pytest tests\uat tests\integration\test_uat_lab.py `
  tests\integration\test_room_insights_api.py tests\unit\test_room_insights.py `
  tests\e2e\test_p8_room.py -q --tb=short `
  --junitxml="$evidence\uat-070-extended-final.xml" `
  --uat-artifacts="$evidence\uat-070-extended-final-evidence" `
  --basetemp="$scratch\uat-070-extended-final"
# PASS: 81 tests in 495.29 seconds.
# 29 customer UAT + 9 lab barriers = 38 customer/lab cases; 43 room regressions.
```

These are reruns plus new cases, not 81 additional unique tests to add to the original
suite counts. All customer UAT and the newly extended lab barriers ran, not just the two
new journeys. Original 611-unit / 748-integration-E2E results remain recorded above.
Follow-up `ruff check .`, `ruff format --check .` (192 files), `mypy --strict thermabot`
(56 source files), `python scripts\check_addon_config.py` and `git diff --check`: **PASS**.

- `room-history`: 14,400 simulated minute reports through actual HA reads, controller and
  sampler, plus a current tick. Real rollups produced ten completed weekdays, each with
  1,440 samples, 100% coverage and 20 heating degree-days; five days had 96 heating minutes,
  five had 120. Both weather windows qualified: **25.0% central runtime/DD increase**.
  Persistent 73 F room / 70 F thermostat disagreement, paired 65% RH, completed short
  cycles and explicit auxiliary-heat observations produced the intended evidence-backed
  review actions. This is a deliberately constructed diagnostic fixture, not a fitted
  house model or measured savings.
- `room-model`: 4,320 simulated physical thermal reports plus a current tick. The customer
  used the normal Model refit action: adopted version 1, **230 training steps**, thermostat
  holdout **MAE 0.0087 F**. Flat tariffs stayed unavailable for shifting; distinct tariffs
  and rated-power assumptions enabled actual API/UI setback and tariff comparisons.
  Baseline/proposed runtime, cost deltas, displayed numbers and four-hour recovery labels
  agreed. No settings or climate writes resulted from comparisons.
- Ten qualifying observed Away weekdays yielded the 10:00 proposal. Cancel preserved
  settings; approval changed only the shared schedule and reached the real controller.
  Another independently monitored browser context's old approval failed with HTTP 409;
  a Member saw the proposal but could not approve it or craft an authorized mutation.
- A dated improvement showed the completed 25.0% comparison and later disclosed the
  schedule change as a confounder. The customer downloaded a verified generic backup,
  shut down the original application and booted its copy in a new marked lab. With an
  explicitly simulated clock 21 days later, comparison, settings and accounts remained
  available. This is an offline generic-copy/retention drill, not a production restore.

All **29 final runtime JSON snapshots** were audited: SHADOW, zero climate service calls
and zero blocked accidental climate requests. Phone weather/cycling and desktop validated
model comparison screenshots were inspected. Replay and restored sessions remained stopped.
Neither these minute counts nor the injected retention clock are live Watch/soak evidence.

## Customer acceptance evidence

| Playbook scope | Actual execution and disposition |
| --- | --- |
| UAT-02/05/06, 14/17, 19-24, 30/35/37, 43/45-47, 51 | Existing full-runtime first-run/restart, presence, HA faults/recovery, Watch/Stop, overrides, users, import/backup and utility-navigation journeys passed. |
| UAT-61/62/64 | Owner phone 390 px/F/light: Comfort 72-74 F; Admin desktop 1440 px/C/dark: Balanced 71-75 F; Owner tablet 768 px/F/light: Efficiency 70-76 F. Keyboard, preview, cancel, edit invalidation, apply, reload and actual next-tick controller adoption passed. Untouched 22.8 C preserved 73 F. Safety, Away, schedule, presence and stopped/SHADOW state were retained. |
| UAT-63 | Another tab's real Advanced save invalidated the preview; stale application was rejected and the intervening setting retained. Unsafe bands/deadband and edited-C behavior also passed targeted API/UI regressions. |
| UAT-65/66 | Customer mapped humidity/meter and passive equipment. Three explicitly simulated stopped runtime minutes reached history and UI: 73 F, paired 65% RH, 10.0 -> 10.2 kWh / 0.200 kWh delta. Summary, chart gaps/table, actual 24h/7d/30d selection, weekly/scheduled labels and dedicated-versus-central scope passed. |
| UAT-67 | **PASS (simulated customer evidence):** sparse-data outcomes plus qualified weather windows, persistent disagreement, paired humidity, short cycling and explicit auxiliary states/actions reached the real customer API/UI. |
| UAT-68/69 | **PASS (simulated customer evidence):** missing prerequisites and flat tariffs stayed unavailable; genuine refit/holdout and distinct tariffs enabled read-only baseline/proposed/runtime/cost/recovery comparisons. |
| UAT-70 | **PASS (simulated customer evidence):** insufficient history produced no suggestion; ten qualifying Away weekdays produced a proposal. Cancel, explicit approval, real controller adoption, stale rejection and Member denial passed. |
| UAT-71 | **PASS (simulated customer evidence):** recording/reload/restart, qualifying before/after comparison, settings-change disclosure and retention after offline-copy restore with a 21-day simulated clock passed. |
| UAT-72/73 | Member's foreign-room UI/API access and mutations denied; current playbook, journeys and pre-GitHub QA gate updated. |

All 24 page-fixture JSON evidence snapshots were audited: SHADOW, zero climate service
calls and zero blocked accidental climate requests. Screenshots were saved and phone
room-efficiency and desktop dark/Celsius views inspected. Restart journeys additionally
assert their live runtime evidence and appear in JUnit.

## Original failures and corrections

Original JUnit and failure logs were retained; final images were saved separately.
Failures were explained and corrected, not blindly retried to green:

1. Legacy UAT tried numeric bands on Simple settings. Updated its destination to Advanced;
   the saved band still had to reach the real controller.
2. New assertions initially looked for missing-model errors in the wrong element and
   assumed a Member's foreign-room query redirected. Actual behavior was an explicit
   unavailable status and HTTP 403. Assertions now test those intended outcomes.
3. Tariff readiness reports missing distinct tariffs before missing model. Its acceptance
   expectation now checks that prerequisite explicitly.
4. Two old unit assertions assumed only four sensor fields. They now enforce the intended
   invariant: every fresh/legacy default sensor assignment, including new fields, is empty.
5. Windows CRLF caused the original vendor preflight to fail. All five Git blobs matched
   documented hashes; byte-preserving export QA passed without weakening integrity.
6. The first export omitted the public `.env.example` along with credentials. Inspected and
   copied only that template; the complete final unit suite then passed.

No production/application implementation was changed to hide these test/tooling mismatches.

Follow-up first attempt: **1 failed, 1 passed**. The positive history journey reached
rollup/diagnostics/approval but expected an HTTP 409 console event in the main page's
collector even though the request came from an unmonitored second page. It now uses the
standard independently monitored customer context and verifies the visible stale error.
The ready-model journey already passed. Original follow-up JUnit and screenshots were
retained separately; the complete final run above passed. The diagnostic fixture's attempted
automatic refit was correctly rejected as physically invalid; only the separate physical
model fixture's real fit was adopted.

## Remaining gates

- Published tag/image provenance, amd64/aarch64 boot, isolated HA OS/Supervisor/ingress,
  physical browsers/devices/screen reader, Linux VM and independent watchdog drills:
  **NOT RUN** in this Windows simulated session. Generic offline-copy restore passed
  above; a real installation/off-device recovery drill remains unrun.
- Seven consecutive real SHADOW days and explicit Owner review: **NOT RUN**.
  Stable/live acceptance remains blocked.
- Review exclusions and obtain the relevant release decision before shipping. Keep UAT
  synchronized with every later program change and rerun affected/dependent journeys.

Read-only environment checks found no configured HA/Supervisor connection, no supplied
isolated live target or current-candidate soak evidence, and no running Docker Desktop
Linux engine. Existing WSL Python 3.12.3 lacks the application/pytest/Playwright dependencies
and is not the provisioned isolated UAT VM. No dependencies were installed, no host service
configuration was changed and no production access or safety-net changes were attempted. Completing
the external gates needs the approved test environment, actual elapsed SHADOW days and
a dated human Owner decision; none can be inferred from local green tests.

The maintenance gate is recorded in [CONTRIBUTING](../CONTRIBUTING.md#before-you-push),
the PR checklist and the [current playbook](uat-test-plan.md).
