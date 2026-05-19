# HA Complete Irrigation Integration

> ⚠️ **Early development.** Not yet ready for production use. Spec is locked, implementation is in progress.

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
