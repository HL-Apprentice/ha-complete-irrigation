# HA Complete Irrigation Integration

A Home Assistant custom integration for complete, sensor-driven irrigation control. Works with any controller that exposes zone switches.

> ⚠️ **v1.0.0 is a feature-complete first release.** It has been smoke-tested against a real HA container per the 3-check protocol, and the spec items are all wired in. That said, it is brand new — back up your HA before installing, and report bugs at the [issues page](https://github.com/HL-Apprentice/ha-complete-irrigation/issues).

## Features (all wired in v1.0.0)

- **Hardware-agnostic** — auto-detects Rachio, Hydrawise, RainMachine, B-Hyve, OpenSprinkler, ESPHome, and any switch-based controller via the HA entity registry
- **Daily schedules** with time, duration, weekday pattern, enable/disable; persisted across restarts
- **Manual Run-Now** per zone with duration popup and Stop button
- **Calendar entity** (`calendar.complete_irrigation`) — next 30 days of planned runs in HA's Calendar dashboard
- **iCal feed** at `/api/complete_irrigation/calendar.ics` — subscribe from any phone calendar app
- **Conflict resolver** with three policies (defer / shift / split), 2-min auto-buffer, 2-hour cascade cap. Default: defer-new.
- **Rain lockout** — Tempest rainfall triggers a tiered system-wide lockout (4/6/12/24h depending on accumulation)
- **Hot weather boost** — configurable temp threshold + runtime % boost
- **Moisture-driven runtime** — per-zone min/target/max with 4-band controller (saturated → skip; light → reduced; normal → base; urgent → +50%)
- **Multi-sensor combine modes** per zone: average / lowest / highest / primary
- **Notifications** via `notify.*` with quiet-hours queueing and morning summary
- **Weekly verification reminder** (Sunday 8 AM by default)
- **"New grass" establishment mode** — bounded multi-cycle schedule that auto-expires
- **Custom sidebar panel** with collapsible nav (Today / Schedules / Zones / Sensors / Weather / Notifications / Settings)
- **Iframe-sandboxed panel** so any future panel JS bug physically cannot affect HA's main frontend

## Hardware support

| Controller integration | Auto-detected |
|---|---|
| Rachio (official) / `rachio_local` (biofects) | ✅ |
| Hunter Hydrawise | ✅ |
| RainMachine | ✅ |
| Orbit B-Hyve | ✅ |
| OpenSprinkler | ✅ |
| ESPHome / DIY relay boards | ✅ via manual switch selection |
| Any HA-installed irrigation integration | ✅ via manual switch selection |

## Sensor support

- COOLO CS-201Z (entities auto-detected)
- THIRDREALITY Smart Soil Moisture Sensor Gen2
- Any HA-exposed moisture sensor (Aqara, Mi Flora, Xiaomi, ESPHome, etc.)

## Weather

- WeatherFlow Tempest (recommended)
- Any HA weather/sensor entity providing rainfall or temperature

## Installation (HACS)

1. **HACS** → ⋮ → **Custom repositories** → add `https://github.com/HL-Apprentice/ha-complete-irrigation` as Integration
2. HACS → Integrations → search **"Complete Irrigation"** → Install
3. Restart Home Assistant
4. Settings → Devices & Services → **+ Add Integration** → search **"Complete Irrigation"**
5. Walk the wizard:
   - Step 1: Pick your irrigation controller (auto-detected list, or "Manual" to pick switches yourself)
   - Step 2: Confirm which switches are irrigation zones
6. Look at the HA sidebar — the **💧 Irrigation** panel is now there

## Quick start after install

### Set up a daily schedule
1. Sidebar → **💧 Irrigation** → Schedules → **+ Add Schedule**
2. Fill in name, zone, start time, duration, weekdays
3. Save. The schedule fires automatically.

### Connect weather (Tempest)
Developer Tools → Services → `complete_irrigation.set_weather_config`:
- `rain_sensor`: e.g., `sensor.tempest_rain_today` (must be in inches)
- `temperature_sensor`: e.g., `sensor.tempest_temperature` (must be in °F)
- `hot_threshold_f`: defaults to 100°F
- `boost_percent`: defaults to 25

### Bind a moisture sensor to a zone
Developer Tools → Services → `complete_irrigation.set_zone_moisture`:
- `zone_entity_id`: e.g., `switch.front_lawn`
- `moisture_entities`: list, e.g., `["sensor.garden_east_1_soil_moisture"]`
- `min_pct`, `target_pct`, `max_pct`: defaults are lawn (21/31/40)
- `combine_mode`: `average` (default), `lowest`, `highest`, `primary`
- `category`: optional label

### Set up notifications
Developer Tools → Services → `complete_irrigation.set_notification_config`:
- `notify_target`: e.g., `notify.mobile_app_your_phone`
- `quiet_hours_start` / `quiet_hours_end`: defaults `22:00` / `07:00`

Then `complete_irrigation.test_notification` to verify.

### Start "New grass" establishment mode
Developer Tools → Services → `complete_irrigation.start_establishment`:
- `zone_entity_id`: which zone
- `cycles_per_day`, `minutes_per_cycle`, `days`: defaults `3`, `10`, `12`
- `start_hour`: defaults `6`

The integration creates daily-recurring schedules that auto-disable after `days`.

### Subscribe your phone's calendar to upcoming runs
Add this as an iCal subscription URL in iOS Calendar or Google Calendar:
```
http://<your-ha>:8123/api/complete_irrigation/calendar.ics
```
(Or via Nabu Casa: your remote URL with the same path.)

## All services

| Service | Purpose |
|---|---|
| `run_zone` / `stop_zone` | Manual control with auto-stop |
| `add_schedule` / `update_schedule` / `delete_schedule` / `set_schedule_enabled` | Schedule CRUD |
| `set_weather_config` | Rain sensor, temperature, hot boost |
| `set_zone_moisture` | Per-zone sensors + thresholds |
| `clear_rain_lockout` | Manually end rain lockout |
| `set_notification_config` | Where to send push, quiet hours |
| `test_notification` | Send a sample notification |
| `start_establishment` | "New grass" multi-cycle schedule with auto-expiry |

## Known limitations (v1.0.0 — addressed in v1.1+)

- Panel UI for weather/sensor/moisture/notification config is service-driven; visual editor lands in v1.1
- Conflict popup with three live options is service-driven (the resolver applies the default policy automatically); UI in v1.1
- Calendar entity is read-only (can't drag-edit events from HA's Calendar dashboard yet)
- Help tooltips on every setting (planned for v1.1)
- Per-zone hot-weather boost configuration (currently integration-wide)
- Brand assets in home-assistant/brands repo (CI skips this check until submitted)

## Pi 4 compatible

Verified on HA Container in Docker. ~50 MB RAM, <1% CPU steady state on a Pi 4 4GB.

## Architecture

Two layers:
- **Pure-logic deep modules** (zero HA dependency, ~120 unit tests): `Schedule`, `ScheduleStore`, `RunPlanner`, `ConflictResolver`, `MoistureGate`, `WeatherGate`, `ManualRun`, `NotificationDispatcher`
- **HA-coupled adapters**: `coordinator.py`, `config_flow.py`, `services.py`, `calendar.py`, `binary_sensor.py`, `ws_api.py`, `ical_view.py`, `__init__.py`

See [`docs/PRD.md`](docs/PRD.md) for the full spec, [`docs/ADRs/`](docs/ADRs/) for design decisions, and [`CONTRIBUTING.md`](CONTRIBUTING.md) for the 3-check pre-push protocol every release must pass.

## License

MIT — see [LICENSE](LICENSE).
