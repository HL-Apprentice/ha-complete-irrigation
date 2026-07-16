# PRD Gap Tracker

> **HISTORICAL: last audited at v1.10.0 (2026-05-22); not updated since — do not
> treat the open-gaps list or the Phase-2 exclusions below as current.** Several
> declared-Phase-2 items have since shipped (ET-based runtime adjustment:
> `et_calc.py` + `pyeto/`; anomaly detection: `watering_diagnosis.py`; plant
> vision health: `vision_health.py`). See the README and CHANGELOG for current
> behavior; re-audit against the current release before reusing this list.

Gap tracker (frozen) of every numbered user story in `docs/PRD.md` that was NOT yet fully implemented as of the last audit. Reviewed against shipped code in v1.7.0 (see audit run on 2026-05-21). The PRD itself is the historical spec; this file was the working "what's left" punch list so ideas weren't lost.

---

## Open gaps (by PRD story number)

### Installation & first-run
- **#3 — Multiple controllers per HA instance.** `config_flow` uses `_abort_if_unique_id_configured()` (one entry only). A user with two Rachios can't add both. *Effort:* medium — needs refactor to scoped coordinator per entry + entry-aware service routing.
- **#5 — Resource budget (<60 MB / <1% CPU on Pi 4).** No benchmark, no perf gate in CI. *Effort:* small — add a perf harness to `scripts/check.sh` that measures startup + tick cost.

### Controller / zone discovery
- **#13 — Rename zones during setup.** ✅ **Shipped in v1.9.0** — config flow gained an "aliases" step; aliases push into HA's entity registry.
- **#14 — "Replaces your controller's schedule" warning.** ✅ **Shipped in v1.9.0** — warning text added to step 1 of the config flow (strings.json + translations).

### Moisture sensor setup
- **#15-17 — Device-search dropdown + COOLO / THIRDREALITY brand auto-mapping at setup.** Sensors are bound only post-setup via `set_zone_moisture`. *Effort:* medium — extend config flow with an optional sensors step that lists discovered moisture devices.

### Zones / areas
- **#18-19 — Area/Position labels on zones (and sensors).** No `area` or `position` field on zone config. *Effort:* small — extend zone metadata; surface in the Zones tab card.
- **#20 — Name-based defaults.** ✅ **Shipped in v1.9.0** — `category_match.suggest_category()` matches keywords (lawn/grass/turf/veg/citrus/bush/tree/etc) and seeds the zone's category in coordinator config on setup.

### Plant categories (#22-27)
- **#25 — Category explanatory popup.** ✅ **Shipped in v1.8.0** — sensor modal now shows the matching info text from `PLANT_CATEGORIES` under the category select.
- **#23, #85 — "Calibrate sensor" shortcut from panel to HA device page.** ✅ **Shipped in v1.10.0** — each bound moisture sensor's friendly name in the Sensors-tab card is now a deep link to `/developer-tools/state?entity_id=<eid>` with `target="_top"`, opening the entity's full state + calibration in the parent HA window.

### Schedules
- (#28-31 all shipped — weekday + interval, multi-zone added in v1.8.0)

### Conflict resolution
- **#34, #36 — Visual conflict popup with timeline previews + "cancel & pick different time".** v1.7.0 ships a global policy picker (#37 satisfied) but no create-time conflict modal. *Effort:* large — needs a WS endpoint to compute conflicts pre-save + a timeline UI.
- **#38 — 2-min auto-buffer between back-to-back zone runs.** ✅ **Shipped in v1.10.0** — `next_runs()` and `_expand_steps()` accept a `zone_buffer_seconds` parameter; coordinator threads `config["zone_buffer_seconds"]` through. New `set_general_config` service + Settings tab "Schedule timing" card lets users adjust 0-600 seconds.

### Runtime conflicts (sleep)
- **#39 — Small-shift (<15 min) silent absorption.** Constant `DEFAULT_SMALL_SHIFT_TOLERANCE_MIN` exists; no notification path currently notifies on shifts, so behavior is implicitly silent. Marker not yet wired into resolver `reason` field. *Effort:* tiny — once shift notifications are added, gate them by the tolerance.
- **#43, #44 — Hard-failure immediate push (zone won't start, controller offline) + master silence-into-summary toggle.** ✅ **Shipped in v1.9.0 (#43)** — `handle_run_zone` now checks the entity is available before starting, catches turn_on failures, and fires `CATEGORY_CRITICAL` notifications that bypass quiet hours. **#44 (silence-into-summary toggle) still open** — would need a per-event opt-out matrix from #54.

### Weather
- **#52 — Wind-based defer.** ✅ **Shipped in v1.9.0** — `weather_gate.evaluate_wind_defer()` + `set_weather_config(wind_sensor, wind_defer_mph)` + coordinator hook. Skips scheduled runs when current wind ≥ threshold. UI exposed in Weather tab.

### Notifications
- **#54 — Per-event control matrix (~25 event types).** Only master `enabled` + `low_moisture_alerts` toggle exist. *Effort:* large — needs an event-type → on/off map in config + UI.
- **#57 — Sensor-offline immediate push.** ✅ **Shipped in v1.9.0** — coordinator's per-tick `_check_sensor_availability` calls `detect_sensor_offline_transitions()` (pure function, 8 tests). Fires once per crossing for both offline and recovered states.

### Calendar
- **#59 (partial) — Two-way calendar edit.** `async_create_event` works; delete + drag-update are out. *Effort:* medium — implement `async_delete_event` + `async_update_event` against `ScheduleStore`.
- **#60 — Panel-as-source-of-truth guardrail.** ✅ **Shipped in v1.10.0** — `Schedule.created_via` field tracks origin: `panel` (default, back-compat), `service`, `calendar`, or `establishment`. Round-trip-safe in storage; calendar.py + start_establishment mark accordingly. Unblocks #59 two-way calendar edit when it lands.

### Custom UI panel
- **#65 — Heat index (computed).** ✅ **Shipped in v1.8.0** — banner shows the NWS Rothfusz heat index when temp ≥ 80°F and humidity ≥ 40%.

### Tooltips
- **#71 — Tooltip text in translation files.** Tooltips are hard-coded English strings in the panel JS; `translations/en.json` doesn't carry them. *Effort:* medium — move strings to JSON + lookup at render.

### "New grass" establishment mode
- **#76 — End-of-establishment transition prompt.** ✅ **Shipped in v1.9.0** — `_fire_establishment_end_prompts` (daily at quiet-hours-end) walks schedules; any with `end_date <= today` and not already prompted fires a CATEGORY_IMPORTANT notification. Pure-function tested.

### Verification reminders
- **#78 — One-shot 7-day post-install reminder.** ✅ **Shipped in v1.9.0** — coordinator stores `first_run_at` + `post_install_reminder_fired` in config; `async_call_later` schedules the notification 7 days after first setup.
- **#80 — Per-zone weekly stats in the reminder.** ✅ **Shipped in v1.10.0** — new pure helper `build_weekly_zone_summary()` (6 unit tests) builds a per-zone digest: schedule count, current combined moisture (avg of bound sensors), target%, "sensors offline" badge if all sensors unavailable. Replaces the old single-line aggregate.
- **#81 — "Snooze 30 days" on the reminder.** ✅ **Shipped in v1.10.0** — coordinator checks `weekly_reminder_snoozed_until` before firing. Settings tab card has a "Snooze 30 days" button + "Resume now" link when snoozed. Persisted in coordinator config via `set_general_config`.

### Manual run control
- **#84 — Customize manual-run default.** ✅ **Shipped in v1.8.0** — Settings → "Manual run default" persists a per-browser default via localStorage.

---

## Changelog

- **2026-05-22** v1.10.0: closed #23/#85 (sensor deep-link), #38 (configurable inter-zone buffer + Settings UI), #60 (created_via marker), #80 (per-zone weekly stats), #81 (snooze weekly reminder). 12 new unit tests. Total now 198.
- **2026-05-22** v1.9.1-v1.9.3: UX polish — start time width, "New Planting" rename, multi-notify targets, hide-controls removed from Today, wind to 1 decimal, today's runs timeline, sticky left sidebar.
- **2026-05-21** v1.9.0: closed #13 (rename zones), #14 (replaces-controller warning), #20 (name-based category defaults), #43 (hard-failure push), #52 (wind defer), #57 (sensor-offline push), #76 (establishment end prompt), #78 (7-day post-install reminder). 34 new unit tests added.
- **2026-05-21** v1.8.0: closed #25 (category info popup), #65 (heat index), #84 (manual-run default). Multi-zone schedules added (new feature — not in original PRD).
- **2026-05-21** v1.7.0: closed #37 (always-use-this-option = global conflict policy), #73-77 (establishment mode UI).
- **2026-05-20** Audit baseline established. ~55 of 85 PRD stories shipped at v1.7.0.

---

## Out of scope for v1 (per PRD)

These are explicitly Phase 2 and not tracked as gaps:
- Pressure / flow rate sensors (water meter integration)
- Anomaly detection ("zone ran but moisture didn't change")
- ET-based runtime adjustment using solar radiation
- Voice control beyond HA's assistant
- Soil moisture sensor calibration UI
- Multi-language UI (English only at v1)
- Mobile app of our own
- Direct controller API calls (we go through HA's integrations)
- Cloud sync of config
