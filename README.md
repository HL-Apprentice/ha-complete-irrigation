<p align="center">
  <img src="https://raw.githubusercontent.com/HL-Apprentice/ha-complete-irrigation/main/assets/icon.png" alt="Complete Irrigation" width="160" height="160" />
</p>

# HA Complete Irrigation Integration

A Home Assistant custom integration for complete, sensor-driven irrigation control. Works with any controller that exposes zone switches.

> **Smoke-tested against a real HA container per the [3-check protocol](CONTRIBUTING.md) every release.** Back up your HA before installing; report bugs at the [issues page](https://github.com/HL-Apprentice/ha-complete-irrigation/issues). Security issues: see [SECURITY.md](SECURITY.md). See [CHANGELOG.md](CHANGELOG.md) for what's new.

<img width="1160" height="1284" alt="Today tab: weather banner, per-zone Run Now tiles, and the today/tomorrow run timeline" src="https://raw.githubusercontent.com/HL-Apprentice/ha-complete-irrigation/main/assets/screenshots/panel-1.png" />

<details>
<summary>More screenshots</summary>

<img width="1168" height="437" alt="Schedules tab: schedule list with recurrence, duration, and enable/edit/delete controls" src="https://raw.githubusercontent.com/HL-Apprentice/ha-complete-irrigation/main/assets/screenshots/panel-2.png" />
<img width="1163" height="498" alt="Zones tab: per-zone week-at-a-glance run dots with New Planting and Hide from Today buttons" src="https://raw.githubusercontent.com/HL-Apprentice/ha-complete-irrigation/main/assets/screenshots/panel-3.png" />
<img width="1168" height="459" alt="History tab: run history filtered by zone, schedule, status, and date range" src="https://raw.githubusercontent.com/HL-Apprentice/ha-complete-irrigation/main/assets/screenshots/panel-4.png" />
<img width="1163" height="651" alt="Sensors tab: per-zone moisture sensors with live readings and the combined value used for decisions" src="https://raw.githubusercontent.com/HL-Apprentice/ha-complete-irrigation/main/assets/screenshots/panel-5.png" />
<img width="1164" height="999" alt="Weather tab: 3-day forecast, rain-lockout sensor picker, hot weather boost, and wind defer" src="https://raw.githubusercontent.com/HL-Apprentice/ha-complete-irrigation/main/assets/screenshots/panel-6.png" />
<img width="900" height="1284" alt="Settings tab: theme, schedule conflicts, inter-zone buffer, manual run default, and calendar feed" src="https://raw.githubusercontent.com/HL-Apprentice/ha-complete-irrigation/main/assets/screenshots/panel-7.png" />

</details>

## What it does

- **Hardware-agnostic** — auto-detects Rachio, Hydrawise, RainMachine, B-Hyve, OpenSprinkler, and Smart Irrigation via HA's entity registry; ESPHome/DIY relays and any other switch-based controller work via manual switch selection.
- **Schedules** with weekday, every-N-days, or every-N-hours recurrence, hours+minutes duration up to 8h, sunrise/sunset-anchored start or finish times, seasonal month windows, enable/disable. Persisted across restarts.
- **Manual Run-Now** per zone with duration popup and Stop button. Tile shows "Running — 4:52 left of 10 min" — hydrates after page reload via WS so external runs (Developer Tools, automations) also show the countdown.
- **Valve verification (check-back)** — re-reads the physical switch after every on/off command; retries once and raises a CRITICAL alert if the valve didn't actuate.
- **Calendar entity** (`calendar.irrigation` on a fresh install; existing installs keep their registered id) + **iCal feed** at `/api/complete_irrigation/calendar.ics` for phone calendar subscription.
- **Conflict resolver** — serializes runs one zone at a time with a 2-min auto-buffer. Overlapping runs are deferred (never moved earlier); past the 2-hour cascade cap, the runs ahead are compressed (down to a 70% floor) to make room — a scheduled run is never dropped.
- **Long runs on capped controllers (v1.25)** — a run longer than the controller's block size (default 58 min) is auto-delivered as back-to-back blocks with a 30-s gap; tune via `controller_max_run_minutes` / `block_gap_seconds`.
- **Rain lockout** — Tempest (or any rainfall sensor) triggers an ET-scaled system-wide lockout: lockout length = effective rain divided by the live daily ETo, so it lasts as many days as the rain actually replaced — hotter forecast, shorter lockout (heat-graduated ceiling), with a configurable rain floor. **Multi-rain** support: bind several rain sensors; first is primary for lockout, all show on the banner.
- **Hot weather boost** — temp threshold + runtime % boost.
- **Moisture-driven runtime** — per-zone min/target/max with 4-band controller (saturated → skip; light → reduced; normal → base; urgent → +50%).
- **Multi-sensor combine modes** per zone: average / lowest / highest / primary. Each sensor's reading + the combined value display side-by-side.
- **Per-zone temperature + humidity sensors** (display-only) — chips on the Zones tab show live averaged values across multiple sensors.
- **Notifications** via `notify.*` with master toggle, quiet hours window, morning summary, and **daily low-moisture alert** (fires once at quiet-hours-end if any zone sensor is below its min%).
- **"New grass" establishment mode** — start from any zone row with a 🌱 button. Bounded multi-cycle schedule that auto-expires.
- **Today's plan** — an advisory per-zone ordering for the day (priority / run / light / skip) that flips to what **actually** happened once a run fires: ✅ Ran on schedule, 💧 Running now, 🟠 Stopped early, ⏭️ Skipped.
- **Plant records + yard map** — per-plant WUCOLS water-need math, need-vs-delivered per loop, photo history with EXIF-GPS placement, and an aerial yard map with draggable markers. **Zoomable** (20–120 m) with markers re-projected through real lat/lon so they keep their true ground position.
- **Sharper aerials** — point the map at any keyless ArcGIS export (e.g. your county assessor's orthophotos, often ~10× sharper than the default global imagery) via a URL template.
- **Plant identification** — photo → species → care. A **curated care table** for ~37 common Southwest/low-desert species supplies the water-use/sun/cadence (small vision models name plants well but can't differentiate water-use). Runs on a **local** model, an **external** one (Claude / Grok / Gemini / any OpenAI-compatible), or local-with-fallback. Keys stay on your server.
- **Vision health checks** — a scheduled external job runs a local vision model (e.g. on a home GPU) to compare each plant's photos over time; verdicts are bounded by a safety rail and shown on the plant. Advisory only.
- **Light surveys (v1.35)** — give a plant an optimal lux range, place a roaming illuminance sensor at it, and a timed survey stores min/avg/max + a too-little/optimal/too-much verdict.
- **Care-task reminders (v1.35)** — recurring fertilize/prune/mulch/inspect reminders per plant or zone with last-done tracking; due tasks notify and re-nag weekly.
- **Watering diagnosis (v1.35)** — 🩺 per zone: cross-checks moisture, 14-day run history, and vision concerns into a Signs → Confirm → Suggestions card. Advisory only.
- **Custom sidebar panel** with Today / Schedules / Zones / Yard / History / Sensors / Weather / Notifications / Settings.
- **Light + Dark themes** — Auto follows HA/OS preference; ☀️/🌙 toggle on Today cycles Light → Dark → Auto. All text WCAG AA contrast verified.
- **User-arrangeable weather banner** — ⚙️ gear opens a modal to show/hide and reorder cells (condition, temp, humidity, wind, UV, sunrise/sunset, etc.). Layout persists in browser.
- **Modern 3-day forecast** — uses `weather.get_forecasts` service (HA 2024+ API) instead of the deprecated forecast attribute.
- **Iframe-sandboxed panel** so any future panel JS bug physically cannot affect HA's main frontend.

## Hardware support

| Controller integration | Auto-detected | Setup note |
|---|---|---|
| Rachio (official) / `rachio_local` | ✅ | ⚠️ **See Rachio note below** |
| Hunter Hydrawise | ✅ | — |
| RainMachine | ✅ | — |
| Orbit B-Hyve | ✅ | — |
| OpenSprinkler | ✅ | — |
| Smart Irrigation | ✅ | — |
| ESPHome / DIY relay boards | ✅ via manual switch selection | — |
| Any HA-installed irrigation integration | ✅ via manual switch selection | — |

### ⚠️ Rachio: set the manual run-time to 60 minutes

**If you use Rachio, do this once or your runs will be cut short.**

Rachio zones are **not dumb on/off relays** — each zone has its own
runtime. When this integration turns a zone on (via `switch.turn_on`),
the HA Rachio integration starts that zone for its configured
**manual run duration**, and Rachio's own hardware stops the zone when
that duration elapses. The HA Rachio integration **defaults this to 10
minutes** (`DEFAULT_MANUAL_RUN_MINS = 10`).

If a run block is longer than Rachio's manual duration, **Rachio stops
the zone first** — and the run shows in History as *"aborted — switch
turned off externally"* at exactly the Rachio cap (e.g. 10 min).

**Fix:**

1. Settings → Devices & Services → **Rachio** integration card → **Configure**
2. Set **"Run time"** (manual zone run duration, minutes) to at least
   the **controller block size** — the default block is 58 min, so `60`
   covers **any** schedule length. You do *not* need to match your
   longest schedule: since v1.25, a run longer than the block size is
   auto-delivered as back-to-back blocks with a 30-s gap between them.
3. Submit.

The integration's own auto-stop timer fires per block, ending each
block at its planned length; Rachio just acts as the relay and this
integration stays the authoritative timekeeper. The block size and gap
are tunable via `set_general_config` (`controller_max_run_minutes`,
`block_gap_seconds`).

> This is *not* related to Rachio's own schedules — deleting or
> disabling those does **not** change the manual run duration. The two
> settings are independent.

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

> **Upgrading?** The panel JS URL is version-stamped (`?v=<version>`), so browsers will auto-fetch the new file after a HACS upgrade — no hard refresh needed.

## Privacy & external connections

**This integration runs fully local by default.** It has **no analytics, no
telemetry, and no background "phone-home."** It reaches the internet only for the
three **opt-in** features below, and only while you actively use them — each one
exists to gather plant / irrigation data *for you*. You can run the whole thing
with **no external connections and no API keys** at all.

| Feature | Connects to | What is sent | API key? | When |
|---|---|---|---|---|
| **Aerial yard map** | Esri World Imagery (`services.arcgisonline.com`), or a custom GIS URL you set | your map's **bounding-box coordinates** (defaults to your HA location) | **No** | when you press *Set up / Refresh aerial* |
| **Verify plant name** | GBIF (`api.gbif.org`) | the **plant name text** you typed | **No** | when you press *Verify name* |
| **Auto ET — Open-Meteo** | Open-Meteo (`api.open-meteo.com`) | your **HA latitude/longitude** | **No** | only if you pick Open-Meteo as the ET source |
| **Plant identification** | your **local** vision model, *or* an external AI (Anthropic / xAI / Google / custom) you choose | plant **photos** + text prompts | Local: **No**. External: **Yes**, your own key | only if you use photo-ID / research **and** configured a model |

Notes:
- **Nothing is sent unless you use that feature.** Skip them and the integration
  never contacts the internet.
- **Your external-LLM API key** (if you add one) is stored only on your Home
  Assistant server, sent only as an `Authorization` header to the provider you
  picked, is **never returned to the browser or written to the logs**, and can be
  cleared any time.
- **Weather is *not* a connection this integration makes.** It reads
  forecast/rain from Home Assistant *weather entities* you already have (Tempest,
  OpenWeather, the built-in forecast…); those make their own calls under HA's
  control.
- Plant photos live locally under `www/complete_irrigation/`; their EXIF GPS is
  read in your browser only to place a map marker and is stripped from the stored
  file. Full detail in [SECURITY.md](SECURITY.md).

## Quick start

Almost everything is configurable from the panel — no Developer Tools required.

### Schedules (Schedules tab)
**+ Add Schedule** → name, zone, start time, hours + minutes duration, recurrence (weekdays, every-N-days, or every-N-hours), enable. Save fires it on the configured cadence.

### Moisture sensors per zone (Sensors tab)
Click **Configure** on a zone card to bind moisture sensors. If you pick more than one, the modal requires a combine mode (Average / Lowest / Highest / Primary — no silent default). The card shows each sensor's live reading and the combined value used for irrigation decisions; min-violations show red.

### Climate (temp + humidity) sensors per zone (Sensors tab)
Same Configure modal has an optional **Climate sensors** section. Bind one or more temperature and humidity sensors — they show as live chips in the Zones tab (averaged when multiple).

### Weather + rain lockout (Weather tab)
Multi-pick rainfall sensors (first checked = primary for the lockout calc; all bound sensors show on the Today banner). Pick a temperature sensor; set hot-weather threshold and boost %. The Today banner gets a customizable ⚙️ gear to show/hide and reorder cells.

### Notifications (Notifications tab)
Set notify target, master enable, quiet hours window, and toggle the daily low-moisture summary. **Send test** button verifies routing.

### New-planting establishment mode (Zones tab)
Click **🌱 New Planting** on the zone you replanted — new grass seed, shrubs, trees, anything getting established. Modal sets cycles per day, minutes per cycle, total days, start hour. The integration creates an auto-expiring schedule.

### Schedule conflicts (automatic)
When two schedules' run windows overlap, runs are serialized one zone at a time with a 2-min buffer: the overlapping run is deferred, and past the 2-hour cascade cap the runs ahead of it are compressed (down to a 70% floor of their requested minutes) so nothing is ever dropped. The Settings-tab policy picker (defer / shift / split) is retained for backward compatibility only — it no longer changes behavior.

### Calendar feed (Settings tab → Copy)
Subscribe to `/api/complete_irrigation/calendar.ics` from your phone calendar app for the next 30 days of planned runs.

## Yard & plants (Yard tab)

Tell the integration what's actually growing in each zone and it can do real
water math instead of guessing at runtimes.

### Add a plant
**+ Add plant** for a manual entry, or **📷 Add from photo** to snap a picture and
have it identified. Each plant carries a species, water-need category (WUCOLS),
canopy area, its zone/loop, and its drip emitters (count × GPH). The Yard report
then compares **water needed vs water actually delivered** per loop.

- **🔍 Identify** — photo → species + care sheet (see *Plant identification* below).
- **Research details** — you already know the species? Type it and skip the photo.
- **Duplicate** — copies species/water-use/canopy/zone/drips **without** photos or
  map position. Fast for a row of identical oleanders.
- **Photos** — tap a thumbnail for a full-size lightbox. EXIF-GPS places the plant
  on the map automatically the first time.

### Drip sizing
Emitter recommendations are **deterministic hydraulics, not AI** — from the plant's
water need, canopy, and your loop runtime, using real emitter sizes (0.5 / 1 / 2 /
4 GPH).

### Yard map
**Set up yard map** fetches an aerial of your property (centered on your HA
latitude/longitude); drag markers onto each plant.

- **Pan & zoom like a map** — **drag the aerial** to pan in fine increments and
  **scroll / pinch / the +− buttons** to zoom right in, so you can place each
  marker precisely on its plant. This is a smooth client-side view (no re-fetch),
  and the markers move with it. **⤢** resets to fit.
- **Detail level** — the *Zoom* dropdown picks how much ground the base image
  covers (20–120 m; smaller = sharper). Markers are re-projected through real
  lat/lon so **plants keep their true ground position** when you change it; a
  plant that falls outside a tighter view is un-placed (with a notification)
  rather than pinned to the edge.
- **Sharper imagery** — the default Esri World Imagery is coarse at yard scale (it
  won't render sharper than ~0.3 m/px, so a 60 m yard is fetched at ~200 px and
  upscaled). Many **county assessors / city GIS offices** publish far sharper
  orthophotos as a keyless ArcGIS export. Paste one into **Settings → Yard map
  imagery**:

  ```
  https://<gis-host>/arcgis/rest/services/Imagery/Aerials2026/MapServer/export?bbox={bbox}&bboxSR=4326&imageSR=4326&size={width},{height}&format=jpg&f=image
  ```

  Tokens: `{bbox}` (or `{west}`/`{south}`/`{east}`/`{north}`) plus `{width}` and
  `{height}`. Save → **Refresh aerial**. Blank restores Esri. Changing imagery does
  **not** move your markers (same bbox). *Tip: copying that URL from a browser
  percent-encodes the braces — that's fine, it's accepted.*

### Care tasks
Recurring fertilize / prune / mulch / inspect reminders per plant, with last-done
tracking. The **Seed a starter plan** row (button: **Seed plan**) creates a
sensible set from the plant type (tree / shrub / flower / cactus-succulent /
grass) in one tap.

## Plant identification & AI (all optional, all advisory-only)

Nothing here can change watering on its own: every model output goes through a
server-side validation rail (bounded enums + ranges) and applies only when **you**
tap Apply.

### Curated care comes first
For ~37 common Southwest / low-desert species, care is **not** guessed by a model —
it's a curated table (water-use, sun, frost/heat range, water + fertilize cadence).
A small vision model is decent at *naming* a plant but reliably bad at
*differentiating* water-use (it labels almost everything "moderate", which is the
one value that drives the irrigation math). So the model only has to get the **name**
right; the table supplies the care. Uncovered species fall back to the model.

### Choose your plant-ID model — Settings → Plant identification
Pick a **mode**:

| Mode | Behavior |
|---|---|
| **Local model only** | Default. Private + free. |
| **Local, with external fallback** | Try local first; only call the external AI when local fails or can't identify. Cheapest way to use an external model. |
| **External model only** | Always use the external AI. |

- **Local** — any OpenAI-compatible **vision** model, e.g. Ollama with
  `qwen2.5vl:7b` on a GPU box. Enter the FULL chat-completions URL
  (`http://<gpu-host>:11434/v1/chat/completions`) + the model tag.
- **External** — **Anthropic (Claude)**, **xAI (Grok)**, **Google (Gemini)**, or any
  OpenAI-compatible endpoint ("Custom"). Picking a provider auto-fills its URL;
  set the model id and paste your API key.

**Your API key stays on your server.** It's never sent back to the browser (the
config endpoint returns only *whether* a key is set) and never written to the logs.
**Clear key** removes it.

**Test connection** probes **every** configured endpoint — local *and* external,
whatever the mode — so you can verify a key works before switching modes. It saves
the form first, so you can paste a key and hit Test in one go:

```
local: OK (qwen2.5vl:7b -> OK); xAI (Grok): OK (grok-4.3 -> OK)
```

> Reasoning models (Gemini 2.5, Claude, Grok reasoning variants) spend hidden
> "thinking" tokens from the same budget, so the completion budget is 4000 — a
> smaller cap truncates the JSON mid-answer and the identify fails even though the
> model had the right species.

### Vision health checks (external job)
`tools/vision_health_job.py` compares each plant's photos over time (~biannual
per plant). Run it on the GPU host on a weekly cron/scheduler. Config via env:
`VH_HEALTH_URL`, `VH_HA_URL`, `VH_TOKEN_FILE` (a root-only file holding an
admin long-lived access token), `VH_VISION_URL`, `VH_VISION_MODEL`. It POSTs
verdicts to `set_plant_health`; the integration's rail bounds them.

### Watering advisor (external job)
`tools/watering_advisor_job.py` reads `health.json` (schedules, per-plant
need-vs-delivered, daily plan, ETo) and asks a local **text** model for at most
6 proposals in two shapes only: move a schedule's start time, or resize a
plant's drip set. Config via env: `WA_HEALTH_URL`, `WA_HA_URL`, `WA_TOKEN_FILE`,
`WA_LLM_URL` (the LLM host's local endpoint), `WA_LLM_MODEL`. Proposals appear
in the panel's 🤖 advisor card — each item applies only when you tap it.

## All services

Most users won't need these — the panel covers everything. Available for advanced
automations, and all documented in **Developer Tools → Actions** with field hints.

**Watering**
| Service | Purpose |
|---|---|
| `run_zone` / `stop_zone` | Manual control with auto-stop |
| `run_schedule` | Fire an existing schedule now |
| `add_schedule` / `update_schedule` / `delete_schedule` / `set_schedule_enabled` | Schedule CRUD |
| `start_establishment` | "New grass" multi-cycle schedule with auto-expiry |
| `set_conflict_policy` | Global conflict-resolution policy |
| `clear_run_history` | Wipe stored run history |

**Sensors, weather, notifications**
| Service | Purpose |
|---|---|
| `set_weather_config` | Rain sensor(s), temperature, hot boost |
| `set_zone_moisture` | Per-zone moisture/temp/humidity sensors + thresholds |
| `clear_rain_lockout` | Manually end rain lockout |
| `set_notification_config` | Where to send push, quiet hours, low-moisture toggle |
| `test_notification` | Send a sample notification |
| `set_general_config` | Catch-all settings: zone buffer, zone order, admin-only mode, controller block size, valve verification, plant-ID model (local + external provider/key/mode), yard-map imagery template |

**Plants & yard**
| Service | Purpose |
|---|---|
| `add_plant` / `update_plant` / `delete_plant` | Plant CRUD |
| `duplicate_plant` | Copy a plant without photos/position (returns the new id) |
| `add_plant_photo` | Attach a photo (EXIF-GPS auto-places it) |
| `add_plant_from_photo` | Photo-first creation + identification |
| `set_yard_map` | Fetch/refresh the aerial; `span_m` zooms (markers are re-projected) |
| `set_plant_health` | Store a vision-health verdict (rail-bounded) |
| `set_plant_light_range` / `start_light_survey` / `cancel_light_survey` | Per-plant lux target + roaming survey |

**Plant identification**
| Service | Purpose |
|---|---|
| `identify_plant_species` | Photo → species + care sheet |
| `research_plant_species` | Species by NAME → care (curated table first, model as fallback) |
| `apply_species_suggestion` / `dismiss_species_suggestion` | Accept or discard a suggestion |
| `test_vision_endpoint` | Probe every configured model (local + external) |

**Care tasks & advisor**
| Service | Purpose |
|---|---|
| `add_care_task` / `update_care_task` / `delete_care_task` / `complete_care_task` | Reminder CRUD + last-done |
| `seed_care_plan` | One-tap reminder set from the plant type |
| `propose_watering_advice` / `dismiss_watering_advice` | Store/clear advisor proposals (advisory only) |

## Pi 4 compatible

Verified on HA Container in Docker. ~50 MB RAM, <1% CPU steady state on a Pi 4 4GB.

## Architecture

Two layers:

- **Pure-logic deep modules** — zero HA dependency, ~740 unit tests. They import
  nothing from Home Assistant, so they're testable in CI without it:
  - *Watering*: `schedule`, `run_planner`, `conflict_resolver`, `moisture_gate`,
    `weather_gate`, `manual_run`, `run_guard`, `chunked_run`, `gap_insertion`,
    `run_history`, `coordinator_logic`, `notifications`
  - *Water math*: `hydraulics` (WUCOLS + emitter sizing), `et_calc` (FAO-56),
    `plant_design`, `daily_planner`, `watering_diagnosis`
  - *Plants*: `plant`, `plant_care_db` (curated species care), `species_id`
    (validation rail), `care_tasks`, `light_survey`, `vision_health`
  - *Map & media*: `yard_map` (georeferencing + marker re-projection),
    `image_type` (magic-byte detection)
  - *AI*: `llm_client` (provider resolution + fallback), `watering_advisor`
  - *Setup & helpers*: `helpers` (entity-descriptor zone filtering),
    `category_match` (zone-name → plant-category keyword matching)
- **HA-coupled adapters**: `coordinator.py`, `config_flow.py`, `services.py`,
  `calendar.py`, `binary_sensor.py`, `ws_api.py`, `ical_view.py`,
  `health_view.py`, `__init__.py`

The split is load-bearing, not cosmetic: **CI installs no Home Assistant**, so
anything genuinely pure gets tested for real there, while HA-coupled tests
`importorskip`. If a test needs `voluptuous`/`homeassistant` to import, the logic
under test probably belongs in a pure module.

Every release runs (items 1, 2, and 5 are Checks 1, 3, and 2 of the
[3-check protocol](CONTRIBUTING.md); the other two are extra release steps):
1. `scripts/check.sh` (**Check 1**) — ruff lint + format, pytest, **CI parity** (re-runs the
   suite in a throwaway venv built from `requirements-dev.txt` alone, so an import
   that only resolves on the dev machine can't sneak past), manifest sanity
2. `scripts/smoke-test.sh` (**Check 3**) — disposable HA in Docker, verifies no integration errors
3. Puppeteer click-tests in `dev/ui-test/` — actually exercise the panel UI in a real browser
4. `scripts/release.sh` — tags, pushes, creates the GitHub Release (HACS reads Releases, not tags)
5. **Check CI is green** (**Check 2**) — `gh run list` after the push. A green local script is
   not a green CI.

See [`docs/PRD.md`](docs/PRD.md) for the original v1.0 spec, [`docs/ADRs/`](docs/ADRs/) for design decisions, and [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full pre-push protocol.

## License

MIT — see [LICENSE](LICENSE).
