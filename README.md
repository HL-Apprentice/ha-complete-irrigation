# HA Complete Irrigation Integration

> ⚠️ **EARLY DEVELOPMENT — DO NOT INSTALL ON PRODUCTION HOME ASSISTANT**
>
> Versions 0.x are pre-release and have shipped bugs that broke unrelated
> parts of Home Assistant (frontend icons, etc.) — recovery required
> deleting `custom_components/complete_irrigation/` from disk and
> restarting HA. v0.2 sandboxes the panel in an iframe to prevent
> repeats, but more bugs are likely while we build out the remaining
> slices.
>
> **Strongly recommended before installing:**
> 1. Take a Home Assistant snapshot (Settings → System → Backups → Create)
> 2. Install on a test HA instance, not your main one
> 3. Wait for v1.0 if you can't afford downtime on your main HA

A Home Assistant custom integration that gives you complete, sensor-driven control over irrigation — works with any controller that exposes zone switches.

## Features (planned)

- **Hardware-agnostic** — auto-detects Rachio, Hydrawise, RainMachine, B-Hyve, OpenSprinkler, ESPHome, and any switch-based controller via the HA entity registry
- **Zone-by-zone scheduling** with interval and time-of-day rules
- **Moisture-sensor-driven runtime adjustment** — sensor is the primary input when present
- **Plant categories** (Lawn, Vegetable, Bushes, Citrus, Trees, Custom) with min/target/max moisture targets
- **Tempest weather integration** — rain lockout, hot weather boost, wind defer
- **Conflict resolution** with visual previews (three strategies + cancel)
- **Local HA calendar** with two-way edit
- **"New grass" establishment mode** — bounded schedule for new plantings
- **Custom panel UI** in the HA sidebar
- **Push notifications** via the HA Companion app + Nabu Casa
- **Help tooltips** on every adjustable setting

## Controller support

| Controller | Status |
|---|---|
| Rachio (official + biofects/rachio_local) | ✅ via entity registry |
| Hunter Hydrawise | ✅ |
| RainMachine | ✅ |
| Orbit B-Hyve | ✅ |
| OpenSprinkler | ✅ |
| ESPHome / DIY relay boards | ✅ |
| Any HA switch-based controller | ✅ |

## Moisture sensor support

- COOLO CS-201Z (auto-mapped)
- THIRDREALITY Smart Soil Moisture Gen2 (auto-mapped)
- Any HA-exposed moisture sensor (Aqara, Mi Flora, ESPHome, etc.)

## Weather source

- WeatherFlow Tempest (recommended — full feature set)
- HA's default weather entity (basic rain/temp only)

## Pi 4 compatible

Designed for Pi 4 (4 GB+) — runs natively inside Home Assistant. No external services, no Docker, no cloud dependencies (except optionally Nabu Casa + HA Companion app for push notifications).

## Installation

Via HACS — instructions added when initial release is published.

## Development

- Spec lives in [`docs/PRD.md`](docs/PRD.md)
- Vertical slices / work items in [`docs/ISSUES.md`](docs/ISSUES.md)
- Architecture decisions in [`docs/ADRs/`](docs/ADRs/)

## License

MIT — see [LICENSE](LICENSE).
