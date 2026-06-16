# Changelog

All notable changes to this integration. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [Semantic Versioning](https://semver.org/).

## v1.20.0 — 2026-06-16

### Changed — scheduling never drops a run

- **No more silently dropped runs.** When too many schedules overlap for
  one-zone-at-a-time, the conflict resolver used to mark the squeezed run
  `skipped: cascade-cap` and drop it entirely (this is what stranded
  "Garden Morning" on a multi-giant morning). It now **defers the run late
  and still fires it** — a run is never dropped, only delayed. A run that
  lands past the cascade cap is tagged `deferred-late`.
- **Planning window now spans the longest run.** The resolver's look-back
  was a fixed 2 hours, so it was blind to a 4–5 hour Trees/Citrus cycle
  that began earlier and was still running — and would let another zone
  fire into a valve the controller still had open. The window is now sized
  to the longest enabled run (`max(2 h, longest run + buffer)`) on both
  sides, so long runs are seen and everything serializes around them.
- **Late-run alert.** When a run is deferred past the cap, a notification
  fires ("running late — the schedule is oversubscribed; consider spacing
  your start times"), honoring `notify_on_missed`. The run still waters.

### Added (engine — staged, not yet enabled)

- The resolver can **compress** the long runs (down to a `compress_floor_pct`
  floor, default 70%) to keep a squeezed run closer to on-time, and treats
  already-firing runs (`REASON_COMMITTED`) as fixed busy intervals to route
  around. This is fully built and covered by a 4 000-scenario property
  fuzz plus a multi-tick firing simulation (no run lost, no double-booking),
  but is **held off in v1.20.0** (`compress_floor_pct=100`) until in-progress
  run tracking lands in v1.21 — shrinking a run that has already started
  could otherwise fire a zone into a still-running one.

## v1.19.0 — 2026-05-31

### Added

- **Auto-soak recovery.** Per-zone closed loop (Sensors editor → "Water
  automatically when below Min %"): when effective moisture drops below
  `min_pct`, run the zone for *Run (min)* (default 10), wait *Soak wait*
  (default 30) for absorption, re-read the sensors, and repeat until
  moisture recovers — capped at *Max cycles* (default 4). On give-up:
  notification + 6 h cooldown, so a stuck-low sensor can't water all
  day. Paused during rain lockout; skips zones already running; runs
  appear in History as "Auto-soak cycle n/N" with a triggers blob.
  Abandoned (safely) across HA restarts.
- **Per-sensor "in avg" toggle.** Each selected moisture sensor in the
  Sensors editor gets an "in avg" mini-checkbox. Unchecked = the
  reading stays visible everywhere (chips, tiles, tooltips — marked
  "excluded from analysis") but is filtered out of the combine/average
  used by the moisture gate and auto-soak. For sensors that
  consistently read wrong and skew the math. Excluding *all* sensors
  is blocked in the editor (use "Ignore moisture" instead). New
  `moisture_excluded` field on `set_zone_moisture`.

## v1.18.4 — 2026-05-31

### Added

- **Per-zone "Ignore moisture for watering decisions" toggle** (Sensors
  editor). When on, the moisture gate is a no-op for that zone: no
  saturated-skip, no runtime boost/reduction, and "Skip run if no
  moisture reading" is moot. Sensors stay bound, so the Zones-tab chips
  and Today tile keep showing live readings — now with a "· gate off"
  badge (and the low-moisture red flag is suppressed, since the gate
  won't act on it). History records `moisture: {disabled_by_config}`
  in the triggers blob so bypassed gates stay auditable. New
  `moisture_disabled` field on `set_zone_moisture`.

## v1.18.3 — 2026-05-31

### Fixed

- **Mobile: page no longer snaps to the top while reading.** Every
  render rebuilds the shadow DOM via `innerHTML`, resetting all scroll
  containers to 0. Background renders (HA state updates, the 60s
  now-line tick on Today) were yanking scrolled users back to the top
  — most visible on mobile. Now: (a) scroll positions of `main`, the
  day-calendar columns, modals, and other internal scrollers are
  captured before each render and restored after; (b) background
  renders are deferred while the user is actively scrolling so
  momentum isn't killed mid-flick; (c) tab switches still land at the
  top (restore is keyed to the section in the DOM).

## v1.18.2 — 2026-05-31

### Fixed

- **Sensor search filter actually hides rows.** The v1.17.7 filter set
  `row.hidden = true`, but the author rule `.sensor-pick{display:flex}`
  overrode the UA `[hidden]{display:none}` rule, so rows never hid.
  Added `.sensor-pick[hidden]{display:none}`.

## v1.18.1 — 2026-05-30

### Added

- **Moisture min/avg/max on the Today zone tiles.** Multi-sensor
  zones: `💧 33% avg · 22 min · 41 max · 3 sensors`; single-sensor:
  `💧 33% (min 21%)`. Red when avg is below the zone's configured
  `min_pct`. Hover shows the per-sensor breakdown.

## v1.18.0 — 2026-05-30

### Added

- **Schedule color-coding.** Each schedule can be assigned a color from
  a 12-swatch palette in the editor. Surfaces: left-edge stripe on the
  Schedules tab, tinted pills on the day calendar. Color flows to the
  calendar via the `list_planned_runs` WS endpoint. `Schedule.color`
  field (hex string or None).
- **Fail-closed moisture option.** Per-zone "Skip run if no moisture
  reading" toggle (Sensors editor). When on, a scheduled run is
  skipped if *every* moisture sensor for the zone is offline, instead
  of watering blind. Default off (legacy fail-open). Individual
  offline sensors were already excluded from the combined reading;
  this governs the all-dark case.

## v1.17.14 — 2026-05-30

### Fixed

- **Missing notify target logs at WARNING, not ERROR.** When a
  configured `notify.<service>` no longer exists (uninstalled
  integration), `_push_now` now catches `ServiceNotFound` and logs a
  helpful warning instead of an ERROR traceback. Genuine delivery
  failures still log ERROR.

## v1.17.13 — 2026-05-30

### Added

- **"▶ Run" button on each schedule row.** Executes the schedule's
  full run sequence on demand (multi-zone chains fire zone-by-zone
  with the inter-zone buffer). Bypasses weather gates (manual
  override). New `run_schedule` service. Honors the admin-only-services
  guard.

## v1.17.12 — 2026-05-24

### Fixed

- **Sticky Cancel/Save in modals actually visible.** v1.17.9 set
  `bottom: -24px` on the sticky footer, which pushed it 24px *below*
  the visible scroll viewport — buttons went offscreen on tall modals
  (Schedule editor). Now `bottom: 0` + `padding-bottom: 0` on the
  modal. Applies to every modal.

## v1.17.11 — 2026-05-24

### Security

- **Opt-in admin-only services (G3).** New `admin_only_services` flag
  (Settings → Security, default off). When on, all 15 hardware /
  CRUD service handlers require an admin caller; system-initiated
  calls (our own notification action handlers) always pass through.

## v1.17.10 — 2026-05-24

### Security

- **Admin-only guard on 3 WS commands (G1+G2).** `list_planned_runs`
  (added v1.16.0, missed the guard), `list_schedules`, and
  `get_active_runs` are now `@require_admin` — consistent with the
  admin-only panel.

## v1.17.9 — 2026-05-24

### Fixed

- **Tall modals scroll.** `.modal` capped at 90vh with internal
  scroll. (Sticky-footer positioning was corrected in v1.17.12.)

## v1.17.8 — 2026-05-23

### Added

- **Cut-short notifications.** When a scheduled run is turned off
  externally at <90% of planned duration, send a notification with
  "Run remainder" + "Open Logbook" actions. New `notify_on_aborted`
  toggle.

## v1.17.7 — 2026-05-23

### Added

- **Live search above each sensor list** (soil / temperature /
  humidity) in the Sensors editor. Filters by friendly name + entity
  id as you type; no re-render so checkbox state + focus persist.

## v1.17.6 — 2026-05-23

### Fixed

- **Info-bubble (ⓘ) tooltips work on touch + show instantly.**
  Replaced the native `title` attribute (delayed on desktop, silent
  on touch) with a custom popover shown on hover / focus / tap.

## v1.17.5 — 2026-05-23

### Fixed

- **update_schedule accepts empty weekdays** for interval /
  interval_hours modes. The strict `vol.Length(min=1)` blocked editing
  non-weekdays schedules (e.g. saving Bird Bath with the new
  ignore-gates toggles).

## v1.17.4 — 2026-05-23

### Fixed

- **WS `list_planned_runs` had the same UTC bug as v1.17.1.** The day
  calendar display showed schedules at the wrong time even after the
  coordinator firing was fixed. Now converts to local before planning.

## v1.17.3 — 2026-05-23

### Added

- **Per-schedule "ignore weather gates" toggles** (`ignore_wind`,
  `ignore_hot_weather`, `ignore_rain_lockout`). Useful for fixed-fill
  zones like a bird bath. Default off.

## v1.17.2 — 2026-05-24

### Improved

- **Clearer "now" line on the day calendar.** Was a 2px red line with
  hover-only tooltip; now a 3px line with a pinned `8:34 AM` label chip
  on the left edge, a subtle 2.4s pulse for visibility, and an
  auto-tick that re-renders Today every 60 s so the line drifts down
  on its own (the tick runs only while Today is open and stops itself
  on tab change / panel disconnect).

## v1.17.1 — 2026-05-24

### Fixed (critical)

- **Scheduled runs fired ~UTC-offset hours early.** Coordinator's
  `_tick` callback receives `now` from `async_track_time_interval` as
  a UTC datetime. The pure-logic planner used `from_dt.tzinfo` to
  localize each schedule's `start_time`, so a user-entered "14:30"
  became "14:30 UTC" — which fires at 07:30 in MST/PDT (UTC−7),
  exactly the 7-hour offset users reported. Fix: convert `now` to
  local via `dt_util.as_local()` at the top of `_tick`, and convert
  the persisted `_last_tick` on startup so post-fix restarts don't
  reintroduce the offset. Bug has existed since v0.x; only became
  visible when daytime-anchored schedules were created. Added
  `test_planner_localizes_start_time_to_from_dt_tzinfo` regression
  test (existing tests used naive datetimes and couldn't catch it).

## v1.17.0 — 2026-05-23

### Added

- **Missed-run recovery via actionable "Run now?" notifications.**
  When a scheduled run is skipped (conflict resolver, moisture / wind /
  rain gate, or HA was down at the firing minute), the integration
  sends a notification with a "Run now (X min)" action button. Tapping
  it runs the zone with the schedule's original planned duration.
  Mechanism: action payload packed into the Companion notification's
  `data.actions`; `mobile_app_notification_action` event listener
  catches the tap and calls `run_zone` with the original parameters.
  Non-Companion notify targets (Telegram, etc.) get plain text — no
  buttons but the notification still arrives. New
  `notify_on_missed` setting (Notifications tab → toggle, default on).
- **Restart-missed runs are now caught.** `_last_tick` persists to the
  config blob every minute. On startup, if the saved tick is ≤5 min
  old, the coordinator resumes from there instead of "now" — runs in
  the gap get processed normally. Long outages (>5 min) still fall
  back to "now" so a half-day outage doesn't burst-fire every missed
  run.
- **Today screen "N runs skipped today" banner.** Soft-yellow banner
  at the top of Today when any of today's history records are SKIPPED.
  Click → History tab pre-filtered to today's skips.

## v1.16.2 — 2026-05-23

### Added

- **Copy button on each schedule row.** Edit / Copy / Delete. Copy
  opens the editor pre-filled with a clone of the source: id cleared
  (save creates a new schedule), name suffixed `" (copy)"`, all
  fields copied (zone_steps, weekdays, mode, interval settings,
  active period). Two-click flow for second daily runs.
- **Conflict-resolver skips now recorded in History.** Previously the
  tick loop just `_LOGGER.info`'d when the resolver dropped a run
  (e.g. `skipped:cascade-cap`); now those drops also write a SKIPPED
  record so they're visible in the History tab like any other skip.
  Closes the gap that hid silent drops.

## v1.16.1 — 2026-05-23

### Fixed

- **`_panel_version` no longer blocks the event loop.** Was reading
  `manifest.json` synchronously during `async_setup_entry`, which
  HA 2025+ flags as a blocking call. Replaced with
  `async_get_integration(hass, DOMAIN).version` — no file I/O, just
  a dict lookup against HA's loader cache.

## v1.16.0 — 2026-05-23

### Fixed (correctness)

- **Frontend planner dedup via new WS endpoint.** Today calendar +
  Zones strip re-implemented schedule expansion in JS, ignoring
  `start_date`, `repeat_annually`, configurable `zone_buffer_seconds`,
  and conflict resolution — so the panel could show runs that
  wouldn't fire (or miss runs that would). New
  `complete_irrigation/list_planned_runs` WS endpoint returns
  server-side `next_runs() → resolve_conflicts()` output, cached
  client-side by local date. Schedule mutations invalidate
  automatically.

### Refactored

- `_fire_run` (158 lines) split into 3 gate-evaluator helpers
  (`_evaluate_moisture_gate`, `_evaluate_wind_gate`,
  `_evaluate_hot_weather_gate`).
- `Schedule.__post_init__` (60 lines) split into per-mode validators.
- 4 pure-logic helpers extracted from `coordinator.py` into new
  `coordinator_logic.py` (shrinks the HA-coupled file).
- `handle_run_zone` extracted `_notify_zone_failed` and
  `_record_run_start_to_history`.
- `conflict_resolver`: extracted shared `_apply_defer` (was
  copy-pasted in 3 policies); `DEFAULT_CASCADE_CAP_MINUTES` derived
  from `DEFAULT_CASCADE_CAP_HOURS` in const.py.
- `set_general_config` switched from if-arm junk-drawer to
  `_GENERAL_CONFIG_FIELDS` table.
- `notifications.update_config` got an explicit allowlist + warning
  on unknown keys.
- `calendar.event` cached for 60 s — was running 30-day expansion on
  every HA poll.
- Inline imports promoted in `run_planner.py`; type annotations added.
- `DEFAULT_ZONE_{MIN,TARGET,MAX}_PCT` constants in const.py.
- Dead `due_runs` method + its tests removed.
- `PANEL_VERSION` constant (was duplicated in 10+ render sites).
- `fmtTimeOfDay` hoisted to module level (was inline-defined twice).
- `_stateSignature` now tracks every per-zone bound sensor + global
  weather sensors — Zones chips refresh on manually-bound sensors.
- `_renderExtraStepRow` no longer regex-mutates rendered HTML.

## v1.15.0 — 2026-05-23

### Added

- **Notification target editor.** Each notify target gets its own row
  with a dropdown of available `notify.*` services. + Add target /
  × remove. Stale targets (unloaded integrations) still appear with
  "(not loaded)" label so they can be removed cleanly.
- **Auto-discover sibling temp/humidity sensors.** When a zone's
  bound moisture sensor has companion entities on the same device
  (entity_ids differing only by "moisture" → "temperature" or
  "humidity"), the climate chips appear automatically with a dashed
  border + "(auto-detected)" tooltip. Vanishes once the user binds
  temp/humidity explicitly in the Sensors editor.

### Security (review batch — see below for context)

- **S1**: iCal feed now `requires_auth=True` + `_ics_escape` strips
  control chars, so user-controlled schedule names can't CRLF-inject
  into the VCALENDAR seen by external calendar subscribers.
- **S2**: WS commands `get_config` + `list_run_history` now
  `@websocket_api.require_admin` — read-only HA users can no longer
  dump notify_targets, sensor entity_ids, or 90 days of history.
- **S3**: Panel registered with `require_admin=True`. Hardware control
  (run/stop) and schedule edits are admin-only.
- **S4**: `notify_targets` rejects anything not in the `notify.*`
  domain (both voluptuous schema + dispatcher's coerce loop).
  Prevents installing arbitrary `domain.service` callouts via
  `set_notification_config`.
- **S5**: Theme value injection into `<style>` sanitized — keys must
  match `/^[a-zA-Z0-9_-]+$/`, values strip CSS-syntactic chars.
- **S6**: run-history `triggers` keys escaped via `escapeHtml` in the
  History table button label.
- **S7**: Repository link gets `rel="noopener noreferrer"` (reverse
  tab-nabbing) + `escapeAttr` on href.
- **S8**: Dead `_renderTodaysTimeline` (which called undefined
  `_renderTomorrowList`) removed entirely. 65 lines deleted.

### Refactored

- `Schedule.with_changes(**overrides)` helper — collapses the
  field-by-field rebuild in `handle_set_schedule_enabled` /
  `handle_update_schedule` from 30+ lines to ~5. Eliminates a real
  bug class (adding a Schedule field, forgetting to copy in the
  handler).
- `_find_coordinator` deduped into `__init__.py.find_coordinator`.
  ws_api / services / ical_view all use the single source of truth.

## v1.14.1 — 2026-05-23

### Added

- **Daily-window cap for `interval_hours` schedules.** Optional "Stop
  firing after" time. When set, the schedule fires every N hours
  starting at `start_time` EACH DAY, capped at the stop-after time
  (inclusive). E.g. "every 2h from 06:00 to 20:00" → 06, 08, 10, 12,
  14, 16, 18, 20 each day; nothing overnight. When unset, legacy
  continuous-across-days behavior preserved. New `interval_end_time`
  field on `Schedule` (`time | None`).

## v1.14.0 — 2026-05-23

### Added

- **Run History tab.** Every completed / skipped / aborted / manual
  run recorded with: date/time, zone, schedule, requested vs actual
  duration, status badge + reason, expandable triggers blob
  (moisture readings, wind, hot-weather decisions).
- New `custom_components/complete_irrigation/run_history.py` — pure
  logic, 22 unit tests covering the lifecycle.
- Persistent JSON store under HA's `.storage/`, 90-day rolling
  retention, hard cap of 1000 records to keep WS payload reasonable.
- Coordinator hooks: scheduled-run starts stash trigger metadata for
  the service handler; skips record directly to history with full
  reason.
- `services.py`: `run_zone` records the start, auto-stop records
  completion, external switch-off records abort, `stop_zone` records
  abort with reason "user pressed Stop".
- New WS command `complete_irrigation/list_run_history`.
- New service `complete_irrigation.clear_run_history`.
- Frontend: zone / schedule / status / date-range filters and
  expandable triggers panel per row; auto-refresh after run_zone /
  stop_zone when the History tab is currently open.

## v1.13.5 — 2026-05-23

### Added

- **2-day calendar window** on the Today screen. Side-by-side columns
  (today + tomorrow); ← / → shifts the window by one day; "Today"
  snaps back. Mobile stacks them vertically.
- **Hover-expand pills.** Mouse over any pill and it grows to ~78 px
  with full zone + schedule name + meta visible instantly. Click
  still opens the schedule editor.

## v1.13.4 — 2026-05-23

### Fixed

- **Mobile: default sidebar to collapsed.** On phone-width viewports
  (≤700 px), the panel's expanded sidebar was a `position: fixed`
  overlay covering the whole main column — first-time mobile users
  opened the app and saw only the sidebar. Now defaults `_collapsed`
  to true when `matchMedia("(max-width: 700px)")` matches AND no
  preference is stored. User toggles still persist.

## v1.13.3 — 2026-05-23

### Added

- **Day calendar 24-hour time grid.** Today calendar became a true
  time-grid (1 px = 1 min, 24 h tall, 600 px scrollable viewport)
  instead of a list of rows. Solid 1 px line at the top of each
  hour, 50%-opacity ::after pseudo at the half-hour. Each scheduled
  run rendered as an absolutely-positioned `.day-cal-pill` at top =
  `start_minutes`, height = `max(18, duration_minutes)`.

## v1.13.2 — 2026-05-23

### Fixed

- **Multi-zone schedules show on every bound zone's 7-day strip.**
  `_schedulesFiringOn` only matched the primary `zone_entity_id`;
  now also matches `zone_steps[i].zone_entity_id`. Verified live:
  both zones in a multi-zone schedule show 7/7 days with fires.

## v1.13.1 — 2026-05-23

### Added

- **Vertical day calendar with prev/next navigation.** Replaces the
  old horizontal timeline + tomorrow list on the Today screen.
  Vertical list of the selected day's runs; ← / → buttons navigate
  by day; "Today" button appears once offset ≠ 0; click any row to
  edit the underlying schedule.

---

## Earlier releases

For v1.12.x and earlier, see the [GitHub Releases](https://github.com/HL-Apprentice/ha-complete-irrigation/releases)
page. Highlights from prior work:

- **v1.12** — schedule active period (start_date / end_date /
  repeat_annually)
- **v1.11** — "every N hours" scheduling mode
- **v1.10** — provenance tracking, configurable zone buffer, weekly
  stats, snooze, deep-links
- **v1.9** — sticky sidebar, dark theme readability, various fixes
- **v1.8** — multi-zone schedules, HA theme picker
- **v1.7-v1.6** — refinements + dark theme polish
- **v1.5** — sidebar navigation
- **v1.4** — interval scheduling mode, hours+minutes durations
- **v1.3-v1.0** — initial release through Slice 17 (establishment +
  iCal), full schedule engine + storage + CRUD services
