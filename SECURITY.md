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

**Outbound requests are opt-in and feature-scoped.** Nothing is sent off-box
unless you configure and use one of the three features below. There are no
analytics, telemetry, or background "phone-home" connections of any kind.

**Yard map aerial fetch.** The **"Set up yard map"** action (`set_yard_map`,
admin-only) fetches an aerial backdrop image over HTTPS from Esri World Imagery
(`services.arcgisonline.com`, a fixed host — no API key) — or, since v1.42, from
a custom ArcGIS-compatible export URL you configure — sending only the map
center **latitude/longitude** (defaulting to your Home Assistant location) and a
span. It is opt-in (nothing is sent unless you run that action), times out after
30 s, fails closed (the previous map is kept on any error), and validates the
response is a real image before saving.

**Plant-brain LLM features.** The opt-in plant features — species identify /
research (v1.37) and vision health checks (v1.33) — send plant photos and text
prompts over HTTPS to the OpenAI-compatible endpoint you configure. Since v1.41
that endpoint can be a third-party cloud provider (Anthropic, xAI, Google, or a
custom URL) when `llm_mode` is `external` or `fallback`; in the default `local`
mode requests go only to your own local server, and if you never configure or
use these features nothing is sent at all. The optional external-LLM API key is
stored in the integration's config store on your Home Assistant box, is sent
only as an `Authorization` header to the provider you configured, is never
written to logs, and is redacted from `get_config` output — still, please
redact keys from any logs you share.

**Species-name verification (GBIF).** The **"Verify name"** button
(`verify_species_name`, admin-only, v1.46) sends only the **plant name text you
typed** over HTTPS to GBIF (`api.gbif.org`, a fixed host — no API key) to look up
the accepted scientific name and catch typos. It is opt-in (nothing is sent
unless you press the button), times out after 15 s, and is restricted to the
plant kingdom. No photos, location, or personal data are sent — just the name.

Photos you add are otherwise local under `www/complete_irrigation/` (only the
LLM features above ever transmit them); EXIF GPS is read in your browser only
to place a marker and is stripped from the stored image.

**Weather is not an outbound connection from this integration.** It reads
forecast/rain data from Home Assistant *weather entities* you already have
(Tempest, OpenWeather, the built-in forecast, etc.); those integrations make
their own network calls under HA's control — this one does not contact any
weather service directly.
