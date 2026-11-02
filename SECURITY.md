# Security policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Please **do not** open a public issue. Report privately through GitHub:
**Security > Report a vulnerability** on this repository (private vulnerability reporting).
Include the version, how to reproduce, and the impact. Expect an acknowledgement within a
few days.

## Scope and design notes

thermabot drives a physical HVAC system, so these count as security issues:

- any way to make the bot write to Home Assistant while the kill switch is off, while it is
  in SHADOW, or while no thermostat is configured;
- any way to write a climate entity other than the configured one, or a service outside
  the allowlist (`set_hvac_mode`, `set_temperature`, `set_fan_mode`, `set_preset_mode`);
- bypassing the safety floors/ceilings, the hard limits or the rate limits;
- authentication or session bypass, CSRF, privilege escalation between roles;
- leaking `HA_TOKEN` or other secrets (logs, API responses, error pages).

Hardening expectations for deployments:

- give the bot a **non-admin** Home Assistant user and long-lived token;
- keep the web UI on your LAN (no port forward); put it behind HTTPS if exposed;
- keep secrets in `.env` (never committed) or your orchestrator's secret store;
- install the independent HA watchdog (`deploy/ha/`).
- Claim the initial Owner account on a trusted local network before allowing other people
  to reach an unclaimed installation. First-run registration is available only with no
  existing users and no completed browser setup; it is not an account-recovery backdoor.
