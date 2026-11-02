## What and why

<!-- What does this change, and why? Link the issue if there is one. -->

## How was it tested?

<!-- Unit/integration/e2e tests added? Ran in SHADOW against a real thermostat? -->

## Checklist

- [ ] `ruff format --check .`, `ruff check .`, `mypy --strict thermabot/core` pass
- [ ] `pytest tests/unit tests/integration` and `pytest tests/e2e` pass
- [ ] New tests carry `@pytest.mark.brd(...)`
- [ ] Only `app/controller.py` calls `_call_service`; no new write path bypasses the guards
- [ ] Writes are still denied when no thermostat is configured; SHADOW stays the default
- [ ] The fallback plan never chooses `off`; `thermabot/core` stays pure
- [ ] Migrations (if any) are forward-only and tested on a copy of a real-shaped DB
- [ ] No secrets, IPs, hostnames, entity ids or personal names added
- [ ] New UI links are relative (ingress-friendly)
