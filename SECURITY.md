# Security Policy

This is a Home Assistant custom integration that controls irrigation
hardware (valves/zones). We take security and privacy seriously and welcome
responsible disclosure.

## Supported versions

Only the latest released version receives security fixes. Please update to
the newest release before reporting, and verify the issue still reproduces.

| Version | Supported |
| ------- | --------- |
| latest  | ✅        |
| older   | ❌        |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately via GitHub's **"Report a vulnerability"** button under the
repository's **Security** tab
(https://github.com/HL-Apprentice/ha-complete-irrigation/security/advisories/new).
This opens a private security advisory visible only to the maintainers.

When reporting, please include:

- a description of the issue and its impact,
- steps to reproduce (or a proof of concept),
- the integration version and your Home Assistant version,
- any relevant logs — **with secrets, tokens, IPs, and entity ids redacted**.

We aim to acknowledge a report within a few days and to ship a fix or
mitigation as quickly as is practical, coordinating disclosure timing with
you.

## Scope

In scope: vulnerabilities in this integration's code — e.g. unsafe handling
of service-call input, websocket commands, stored data (`.storage`), the
custom panel, or the GitHub Actions workflows.

Out of scope: vulnerabilities in Home Assistant core, in third-party
integrations this one talks to (e.g. a vendor's irrigation integration), or
in the user's own network/host configuration.

## Handling of sensitive data

This integration stores its data (schedules, plants, run history, config) in
Home Assistant's local `.storage`. Notification targets and sensor entity ids are
treated as configuration, never logged as secrets. Please keep secrets out of any
logs or screenshots you share.

**One optional outbound request.** The only time the integration contacts an
external service is the **"Set up yard map"** action (`set_yard_map`, admin-only):
it fetches an aerial backdrop image over HTTPS from Esri World Imagery
(`services.arcgisonline.com`, a fixed host — no API key), sending only the map
center **latitude/longitude** (defaulting to your Home Assistant location) and a
span. It is opt-in (nothing is sent unless you run that action), times out after
30 s, fails closed (the previous map is kept on any error), and validates the
response is a real image before saving. No other feature transmits data off-box.
Photos you add stay local under `www/complete_irrigation/`; EXIF GPS is read in
your browser only to place a marker and is stripped from the stored image.
