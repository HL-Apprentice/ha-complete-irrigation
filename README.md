# HA Complete Irrigation Integration

A Home Assistant custom integration for complete, sensor-driven irrigation control. Works with any controller that exposes zone switches.

> **Latest: v1.7.0.** Smoke-tested against a real HA container per the 3-check protocol every release. Back up your HA before installing; report bugs at the [issues page](https://github.com/HL-Apprentice/ha-complete-irrigation/issues).

## What it does

- **Hardware-agnostic** — auto-detects Rachio, Hydrawise, RainMachine, B-Hyve, OpenSprinkler, ESPHome relays, and any switch-based controller via HA's entity registry.
- **Schedules** with weekday OR "every N days" recurrence, hours+minutes duration up to 8h, enable/disable. Persisted across restarts.
- **Manual Run-Now** per zone with duration popup and Stop button. Tile shows "Running — 4:52 left of 10 min" — hydrates after page reload via WS so external runs (Developer Tools, automations) also show the countdown.
- **Calendar entity** (`calendar.complete_irrigation`) + **iCal feed** at `/api/complete_irrigation/calendar.ics` for phone calendar subscription.
- **Conflict resolver** with three policies (defer / shift earlier / split difference) — picker in the Settings tab. 2-min auto-buffer + 2-hour cascade cap.
- **Rain lockout** — Tempest (or any rainfall sensor) triggers tiered system-wide lockout. **Multi-rain** support: bind several rain sensors; first is primary for lockout, all show on the banner.
- **Hot weather boost** — temp threshold + runtime % boost.
- **Moisture-driven runtime** — per-zone min/target/max with 4-band controller (saturated → skip; light → reduced; normal → base; urgent → +50%).
- **Multi-sensor combine modes** per zone: average / lowest / highest / primary. Each sensor's reading + the combined value display side-by-side.
- **Per-zone temperature + humidity sensors** (display-only) — chips on the Zones tab show live averaged values across multiple sensors.
- **Notifications** via `notify.*` with master toggle, quiet hours window, morning summary, and **daily low-moisture alert** (fires once at quiet-hours-end if any zone sensor is below its min%).
- **"New grass" establishment mode** — start from any zone row with a 🌱 button. Bounded multi-cycle schedule that auto-expires.
- **Custom sidebar panel** with Today / Schedules / Zones / Sensors / Weather / Notifications / Settings.
- **Light + Dark themes** — Auto follows HA/OS preference; ☀️/🌙 toggle on Today cycles Light → Dark → Auto. All text WCAG AA contrast verified.
- **User-arrangeable weather banner** — ⚙️ gear opens a modal to show/hide and reorder cells (condition, temp, humidity, wind, UV, sunrise/sunset, etc.). Layout persists in browser.
- **Modern 3-day forecast** — uses `weather.get_forecasts` service (HA 2024+ API) instead of the deprecated forecast attribute.
- **Iframe-sandboxed panel** so any future panel JS bug physically cannot affect HA's main frontend.

## Hardware support

| Controller integration | Auto-detected |
|---|---|
| Rachio (official) / `rachio_local` | ✅ |
| Hunter Hydrawise | ✅ |
| RainMachine | ✅ |
| Orbit B-Hyve | ✅ |
| OpenSprinkler | ✅ |
| ESPHome / DIY relay boards | ✅ via manual switch selection |
| Any HA-installed irrigation integration | ✅ via manual switch selection |

## Sensor support

- COOLO CS-201Z (auto-detected)
- THIRDREALITY Smart Soil Moisture Sensor Gen2
- Any HA-exposed moisture sensor (Aqara, Mi Flora, Xiaomi, ESPHome, etc.)
- WeatherFlow Tempest (or any HA weather/sensor entity)

## Installation (HACS)

1. **HACS** → ⋮ → **Custom repositories** → add `https://github.com/HL-Apprentice/ha-complete-irrigation` as Integration
2. HACS → Integrations → search **"Complete Irrigation"** → Install
3. Restart Home Assistant
4. Settings → Devices & Services → **+ Add Integration** → search **"Complete Irrigation"**
5. Walk the wizard:
   - Step 1: Pick your irrigation controller (auto-detected list, or "Manual" to pick switches yourself)
   - Step 2: Confirm which switches are irrigation zones
6. The **💧 Irrigation** panel appears in your HA sidebar.

> **Upgrading?** The panel JS URL is version-stamped (`?v=1.7.0`), so browsers will auto-fetch the new file after a HACS upgrade — no hard refresh needed.

## Quick start

Almost everything is configurable from the panel — no Developer Tools required.

### Schedules (Schedules tab)
**+ Add Schedule** → name, zone, start time, hours + minutes duration, recurrence (weekdays OR every-N-days), enable. Save fires it on the configured cadence.

### Moisture sensors per zone (Sensors tab)
Click **Configure** on a zone card to bind moisture sensors. If you pick more than one, the modal requires a combine mode (Average / Lowest / Highest / Primary — no silent default). The card shows each sensor's live reading and the combined value used for irrigation decisions; min-violations show red.

### Climate (temp + humidity) sensors per zone (Sensors tab)
Same Configure modal has an optional **Climate sensors** section. Bind one or more temperature and humidity sensors — they show as live chips in the Zones tab (averaged when multiple).

### Weather + rain lockout (Weather tab)
Multi-pick rainfall sensors (first checked = primary for the lockout calc; all bound sensors show on the Today banner). Pick a temperature sensor; set hot-weather threshold and boost %. The Today banner gets a customizable ⚙️ gear to show/hide and reorder cells.

### Notifications (Notifications tab)
Set notify target, master enable, quiet hours window, and toggle the daily low-moisture summary. **Send test** button verifies routing.

### "New grass" establishment mode (Zones tab)
Click **🌱 New Grass** on the zone you reseeded. Modal sets cycles per day, minutes per cycle, total days, start hour. The integration creates an auto-expiring schedule.

### Schedule conflict policy (Settings tab)
When two schedules' run windows overlap, pick one of: defer new (safest, default), shift existing earlier, or split the difference.

### Calendar feed (Settings tab → Copy)
Subscribe to `/api/complete_irrigation/calendar.ics` from your phone calendar app for the next 30 days of planned runs.

## All services

Most users won't need these — the panel covers it. Available for advanced automations.

| Service | Purpose |
|---|---|
| `run_zone` / `stop_zone` | Manual control with auto-stop |
| `add_schedule` / `update_schedule` / `delete_schedule` / `set_schedule_enabled` | Schedule CRUD |
| `set_weather_config` | Rain sensor(s), temperature, hot boost |
| `set_zone_moisture` | Per-zone moisture/temp/humidity sensors + thresholds |
| `clear_rain_lockout` | Manually end rain lockout |
| `set_notification_config` | Where to send push, quiet hours, low-moisture toggle |
| `test_notification` | Send a sample notification |
| `start_establishment` | "New grass" multi-cycle schedule with auto-expiry |
| `set_conflict_policy` | Global conflict resolution policy |

## Pi 4 compatible

Verified on HA Container in Docker. ~50 MB RAM, <1% CPU steady state on a Pi 4 4GB.

## Architecture

Two layers:
- **Pure-logic deep modules** (zero HA dependency, ~140 unit tests): `Schedule`, `ScheduleStore`, `RunPlanner`, `ConflictResolver`, `MoistureGate`, `WeatherGate`, `ManualRun`, `NotificationDispatcher`, `compute_low_moisture_offenders`
- **HA-coupled adapters**: `coordinator.py`, `config_flow.py`, `services.py`, `calendar.py`, `binary_sensor.py`, `ws_api.py`, `ical_view.py`, `__init__.py`

Every release runs:
1. `scripts/check.sh` — ruff lint + format + pytest + manifest sanity
2. `scripts/smoke-test.sh` — disposable HA in Docker, verifies no integration errors
3. Puppeteer click-tests in `dev/ui-test/` — actually exercise the panel UI in a real browser
4. `scripts/release.sh` — tags, pushes, creates the GitHub Release (HACS reads Releases, not tags)

See [`docs/PRD.md`](docs/PRD.md) for the original v1.0 spec, [`docs/ADRs/`](docs/ADRs/) for design decisions, and [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full pre-push protocol.

## License

MIT — see [LICENSE](LICENSE).
