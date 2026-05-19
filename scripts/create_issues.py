#!/usr/bin/env python3
"""Create the v1.0 vertical-slice issues on the GitHub repo.

Output of the matt pocock `to-issues` skill — every slice is a tracer-bullet
that cuts through all integration layers (config, logic, entities, UI, tests
where applicable).

Run once after the repo is initialized:
    python3 scripts/create_issues.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "HL-Apprentice/ha-complete-irrigation"
GH = "/opt/homebrew/bin/gh"

# ---------------------------------------------------------------------------
# Issue definitions — each cuts through every layer end-to-end.
# ---------------------------------------------------------------------------

ISSUES: list[dict] = [
    # -- Prototypes (HITL) ---------------------------------------------------
    {
        "title": "P1 — Prototype: ConflictResolver state machine",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
HITL (throwaway prototype — answer captured as an ADR, then deleted)

## Skill
`prototype` (LOGIC branch)

## Question being answered
Does the ConflictResolver state machine handle every realistic real-world
scenario before we commit to implementing it for real?

Specifically:
- Two-way conflicts with all three policies (defer / shift-existing-earlier / split-difference)
- Back-to-back schedules with the 2-min auto-buffer
- Cascading conflicts when one shift creates new overlaps
- Schedules that cross midnight
- Cascade cap behavior (2-hour limit or 3-deferral limit)
- The "Always use this option" preference learning

## Scope
Throwaway terminal app at `prototypes/p1_conflict_resolver/main.py`.
No tests, no abstractions, no persistence — just enough to learn fast.

## Acceptance criteria
- [ ] `python3 prototypes/p1_conflict_resolver/main.py` starts a REPL
- [ ] `add <zone> <HH:MM> <minutes>` queues a run
- [ ] `policy A|B|C` switches resolution strategy
- [ ] `resolve` prints the resolved timeline as ASCII Gantt
- [ ] `reset` clears all queued runs
- [ ] Demonstrates correct behavior for:
  - Two-way 15-min overlap
  - Back-to-back with auto-buffer
  - Three-way cascade
  - Midnight crossing
  - Cascade-cap activation

## Verdict capture
On completion, write `prototypes/p1_conflict_resolver/VERDICT.md`:
- What we learned
- Edge cases discovered
- Any policy that doesn't work as designed
- Recommended adjustments to ConflictResolver spec before implementation

## User stories covered
PRD: 34–38, 39–44 (informs)

## Blocked by
None — can start immediately.
""",
    },
    {
        "title": "P2 — Prototype: Panel layout variations",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
HITL (throwaway prototype — user picks winner)

## Skill
`prototype` (UI branch)

## Question being answered
What's the right layout for the custom panel? Three radically different
options, switchable via URL search param. Pick a winner before building.

## Scope
Throwaway Lit prototype at `prototypes/p2_panel_layout/`.
Single HTML page hosting three variants of the panel layout with the same
mock data. Switcher at the bottom of the page.

## Variants
1. **Single full-width column** — weather header → calendar → zone tiles
2. **Sidebar nav + main content** — left nav (zones / schedules / sensors / settings), right shows current section
3. **Tabbed top** — tabs across the top (Today / Schedule / Zones / Sensors / Settings), main content below

All three use the locked-in HA theme via CSS variables. Mock data:
3 zones (North/South/East), 1 Tempest weather entity, 3 moisture sensors,
2 active schedules.

## Acceptance criteria
- [ ] `python3 -m http.server` (or similar) from `prototypes/p2_panel_layout/` serves all three
- [ ] `?variant=1|2|3` switches between layouts
- [ ] Variant switcher visible at the bottom of every page
- [ ] All three layouts render the same mock data
- [ ] Light + dark HA theme variables both look reasonable

## Verdict capture
Write `prototypes/p2_panel_layout/VERDICT.md`:
- Which variant won and why
- Any modifications to the winning variant before full implementation
- Whether to keep elements from losing variants in v1.1

## User stories covered
PRD: 62–68

## Blocked by
None — can start immediately.
""",
    },

    # -- Slice 1: Install + see zones ----------------------------------------
    {
        "title": "Slice 1 — Install integration and see my zones in the sidebar panel",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK (with one HITL checkpoint: confirm layout matches P2 winner)

## What you'll be able to do after this slice
Install the integration via HACS. The full config flow walks you through
picking a controller (auto-detected if Rachio/Hydrawise/etc. is installed)
and selecting which switches are irrigation zones. After install, a custom
panel appears in the HA sidebar showing each zone as a read-only tile.

## Layers touched
- Config flow (multi-step wizard: detect controller → select zones → save)
- Coordinator that mirrors HA switch entity states for the selected zones
- Custom panel registration (sidebar item)
- Lit panel with zone-tile grid (read-only — Run-Now button comes in Slice 2)
- Translation strings updated

## Acceptance criteria
- [ ] Config flow detects all installed integrations matching `KNOWN_IRRIGATION_DOMAINS`
- [ ] User can pick one integration to import zones from, or "manual" for arbitrary switches
- [ ] Selected zones get human-friendly names imported from device registry
- [ ] Config entry persists across HA restarts
- [ ] Custom panel `complete-irrigation` registers in the sidebar with the sprinkler icon
- [ ] Panel renders zone tiles showing current on/off state from HA
- [ ] Panel respects HA's theme (light/dark via CSS variables)
- [ ] HACS validation still passes
- [ ] `hassfest` still passes

## User stories covered
PRD: 6–14, 62–68

## Blocked by
P2 (panel layout winner picked)
""",
    },

    # -- Slice 2: Manual Run-Now --------------------------------------------
    {
        "title": "Slice 2 — Manually run a zone from the panel",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
Click a Run-Now button on a zone tile. A duration popup appears, pre-filled
with 10 minutes (customizable). Confirm — zone turns on. Tile shows
"Running 7:21 remaining" countdown. Zone auto-stops when duration elapses.

## Layers touched
- New HA service: `complete_irrigation.run_zone` (zone, minutes)
- Coordinator gains a "running" state per zone
- Panel: Run-Now button on each tile → duration modal → service call
- Live status updates (HA WS events to panel)

## Acceptance criteria
- [ ] `complete_irrigation.run_zone` service works from Developer Tools → Services
- [ ] Service turns on the zone's underlying switch, schedules stop after N minutes
- [ ] If user pre-empts (manually toggles off), no orphaned auto-stop fires
- [ ] Run-Now button in panel opens a duration popup
- [ ] Popup pre-fills `DEFAULT_MANUAL_RUN_MINUTES` (10), editable via spinner
- [ ] User-customizable default in Options (per zone or global)
- [ ] Tile shows live "Running X:YZ remaining" while active
- [ ] Tile clears running state when zone stops (auto or manual)

## User stories covered
PRD: 82–84

## Blocked by
Slice 1
""",
    },

    # -- Slice 3: Schedules + Coordinator ------------------------------------
    {
        "title": "Slice 3 — Create a daily schedule and watch it fire",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
Open the panel, click "Add schedule." Fill in: zone, start time, duration,
day-of-week pattern. Save. At the scheduled time, the zone turns on for
the duration, then off. Schedule persists across HA restarts.

## Layers touched
- `ScheduleStore` — deep module, CRUD against HA storage helper
- `RunPlanner` (minimal v1) — given rules + now, returns the next runs
- Coordinator ticks every 60s, fires schedules whose start time has arrived
- Panel: Schedule editor (zone / time / duration / days-of-week)
- Panel: list of existing schedules with enable/disable toggle
- Tests for `ScheduleStore` (CRUD) and `RunPlanner` (next-run computation)

## Acceptance criteria
- [ ] Schedule editor in panel saves to `ScheduleStore`
- [ ] Schedules persist across HA restart
- [ ] Coordinator fires schedules at the right time
- [ ] Disabled schedules don't fire
- [ ] `ScheduleStore` and `RunPlanner` have unit tests through public interfaces
- [ ] Test coverage: at least one happy-path test per public method

## User stories covered
PRD: 28–31

## Blocked by
Slice 2
""",
    },

    # -- Slice 4: Calendar entity --------------------------------------------
    {
        "title": "Slice 4 — See my schedule as an HA calendar entity",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
A new `calendar.complete_irrigation` entity appears in HA. Opens in the
built-in Calendar dashboard. Shows the next 14 days of planned runs based
on your schedules. Read-only at this stage (two-way edit is Slice 15).

## Layers touched
- New `calendar.py` platform implementing HA's `CalendarEntity`
- `RunPlanner.plan_next_14_days(rules, now)` returns the event list
- Coordinator publishes calendar events on rule changes / startup
- Tests for `RunPlanner.plan_next_14_days` covering edge cases

## Acceptance criteria
- [ ] `calendar.complete_irrigation` entity exists after setup
- [ ] Visible in HA Calendar dashboard
- [ ] Shows next 14 days of planned runs from `ScheduleStore`
- [ ] Event titles like "North Lawn — 18 min"
- [ ] Event descriptions include reason (e.g., "Scheduled run", "Rain skip", etc.)
- [ ] Updates within 60s when a schedule is added/edited/disabled
- [ ] Unit tests on `plan_next_14_days` for weekly / interval / day-pattern rules

## User stories covered
PRD: 58–61

## Blocked by
Slice 3
""",
    },

    # -- Slice 5: Conflict popup ---------------------------------------------
    {
        "title": "Slice 5 — Schedule conflicts show a popup with three resolution options",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK (informed by P1 verdict)

## What you'll be able to do
Create a new schedule that overlaps with an existing one. A popup appears
with visual previews of three resolution options (A defer / B shift earlier
/ C split). Pick one, save. Calendar updates accordingly. "Always use this
option" checkbox remembers the choice for future conflicts.

## Layers touched
- `ConflictResolver` deep module — implements all three policies + 2-min buffer
- Coordinator runs `ConflictResolver` on every rule save
- Panel: conflict modal with ASCII-ish visual previews
- User preference for default conflict option stored in config entry
- Tests for `ConflictResolver` per P1 verdict

## Acceptance criteria
- [ ] `ConflictResolver` resolves two-way conflicts under each policy
- [ ] Handles three-way cascading conflicts
- [ ] Enforces 2-minute buffer between back-to-back zones
- [ ] Conflict modal appears when overlap detected at save-time
- [ ] All three resolution previews shown visually
- [ ] "Always use this option" persists user's choice
- [ ] "Cancel and pick a different time" button works
- [ ] Unit tests cover at least the scenarios validated in P1

## User stories covered
PRD: 34–38

## Blocked by
P1, Slice 4
""",
    },

    # -- Slice 6: Rain lockout -----------------------------------------------
    {
        "title": "Slice 6 — Tempest rain detection triggers system-wide watering lockout",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
Rain detected by your Tempest (or fallback HA weather entity) triggers a
system-wide lockout. Duration scales with rainfall amount (4h/6h/12h/24h
tiers). Panel shows a banner: "🌧️ Rain lockout active until 14:30 — all
zones paused" with a "Resume now" override button.

## Layers touched
- `WeatherGate` deep module — tracks rain lockout state
- Auto-detect Tempest at config flow (carry over from setup wizard)
- Coordinator queries weather sensors each tick, updates lockout state
- New entity: `sensor.rain_lockout_until` (timestamp or "inactive")
- Panel banner component
- Tests for `WeatherGate.evaluate_rain_lockout`

## Acceptance criteria
- [ ] Rain detection thresholds match `DEFAULT_RAIN_LOCKOUT_TIERS`
- [ ] Lockout suppresses all scheduled runs while active
- [ ] Lockout duration adjustable in Options
- [ ] Banner appears in panel during lockout with end-time
- [ ] "Resume now" button overrides the lockout immediately
- [ ] `sensor.rain_lockout_until` updates correctly
- [ ] Unit tests on tier matching + duration calculation

## User stories covered
PRD: 45–49

## Blocked by
Slice 3
""",
    },

    # -- Slice 7: Moisture sensors + categories ------------------------------
    {
        "title": "Slice 7 — Moisture sensors and plant categories drive runtime",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
Add moisture sensors during setup via device-search dropdown. COOLO CS-201Z
and ThirdReality sensors auto-map their entities. Each sensor gets an Area
label and a Position label (cardinal, color code, or custom). Each zone
gets a plant category (Lawn / Bushes / Veg / Citrus / Trees / Custom) with
min/target/max moisture defaults. Sensor reading drives runtime via 4-band
controller. Reasons appear in event descriptions.

## Layers touched
- Config flow extension: sensor device search + area/position prompts
- `MoistureGate` deep module — 4-band controller
- Plant category defaults (already in const.py) wired into UI
- Per-zone settings (category + min/target/max overrides) stored in config
- Panel: zone tile shows moisture % + sensor name + plant category
- Tests for `MoistureGate` covering each band + edge cases

## Acceptance criteria
- [ ] Device-search dropdown lists soil moisture devices
- [ ] COOLO entities auto-mapped (soil_moisture / temperature / humidity / battery)
- [ ] ThirdReality entities auto-mapped (TBD: confirm pattern when paired)
- [ ] Area + Position labels saved per sensor
- [ ] Plant category picker shown per zone
- [ ] Category defaults populate min/target/max moisture
- [ ] Override moisture values per zone (numeric inputs)
- [ ] `MoistureGate` returns adjusted runtime or skip with reason
- [ ] Below min → urgent boost; below target → normal+; below max → light; above max → skip
- [ ] Calendar event reasons reflect MoistureGate decisions
- [ ] Unit tests cover all four bands + edge cases

## User stories covered
PRD: 15–27, 32, 33

## Blocked by
Slice 3
""",
    },

    # -- Slice 8: Hot weather + wind -----------------------------------------
    {
        "title": "Slice 8 — Hot weather boost and wind defer",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
Configure a temperature threshold (default 100°F / 38°C) and boost
percentage (default 25%). When daily high exceeds threshold, all zone
runtimes are boosted that day. Separately, when wind speed exceeds a
threshold during a run window, that run is deferred. Settings adjustable
in Options with ⓘ tooltips.

## Layers touched
- `WeatherGate` extended with hot-weather and wind logic
- Config flow / Options: numeric inputs + unit handling (°F ↔ °C)
- Computed heat index value exposed (entity or just in panel)
- Tests for hot-weather boost calculation and wind defer

## Acceptance criteria
- [ ] Hot threshold + boost % editable, defaults `DEFAULT_HOT_WEATHER_F` / `DEFAULT_HOT_WEATHER_BOOST_PCT`
- [ ] Temperature respects HA unit preference (°F or °C)
- [ ] When daily high meets threshold, runtime boosted on each affected day
- [ ] Wind speed threshold editable
- [ ] When wind exceeds threshold during run window, run deferred (or split based on policy)
- [ ] Reason strings reflect each adjustment
- [ ] Unit tests cover threshold math + unit conversion

## User stories covered
PRD: 50–52

## Blocked by
Slice 6
""",
    },

    # -- Slice 9: Multi-sensor combine ---------------------------------------
    {
        "title": "Slice 9 — Multiple sensors per zone with combine modes",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
Assign 2+ moisture sensors to a single zone. Pick a combine mode:
Average / Lowest / Highest / Primary+info. The chosen mode determines what
single value feeds `MoistureGate`. Panel zone tile shows each sensor's
individual reading + the combined result.

## Layers touched
- Per-zone sensor list (multiple) in config
- Combine mode picker
- `MoistureGate` accepts combine policy
- Panel: zone tile shows per-sensor readings (with their position labels)
- Tests for each combine mode

## Acceptance criteria
- [ ] Config UI supports adding multiple sensors to one zone
- [ ] Combine mode dropdown (Avg / Min / Max / Primary)
- [ ] When mode = Primary, user picks which sensor is primary
- [ ] Combined value flows to `MoistureGate`
- [ ] Panel tile shows breakdown: "Avg 42% (N=45 / S=38 / W=43)"
- [ ] Unit tests cover each combine mode including edge cases (one sensor offline)

## User stories covered
PRD: 21, 22

## Blocked by
Slice 7
""",
    },

    # -- Slice 10: Push notifications ----------------------------------------
    {
        "title": "Slice 10 — Push notifications to the HA Companion app",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
Configure which notifications you want pushed to your phone via the HA
Companion app. Master on/off + per-event toggles + channel selection
(Push / In-app / Both / Log-only). Test-fire button verifies routing.
Events fire as typed HA events so power users can add custom routing
(Slack, email) via automations.

## Layers touched
- Notification settings model in config (per-event preferences)
- Companion app target auto-detection
- Each notification path in the integration becomes a typed event +
  optional `notify.<mobile_app>` call based on settings
- Panel: Options page with notification matrix
- Tests for notification matrix matching event → recipients

## Acceptance criteria
- [ ] At least 5 notification types fire correctly (run completed, deferred,
      skipped-rain, sensor offline, controller failure)
- [ ] Options UI shows per-event grid with channel column
- [ ] Test-fire button triggers a sample of each category
- [ ] HA event `complete_irrigation.notification` fires with category + type
- [ ] Companion app device(s) auto-detected at setup
- [ ] Unit tests for the notification matrix logic

## User stories covered
PRD: 53–57

## Blocked by
Slice 3
""",
    },

    # -- Slice 11: Help tooltips ---------------------------------------------
    {
        "title": "Slice 11 — Help tooltips (ⓘ) on every adjustable setting",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK (with visual review at the end — HITL light)

## What you'll be able to do
Every numeric input, dropdown, and section header in the integration's UI
has a ⓘ icon. Hover (desktop) or tap (mobile) opens a tooltip with: short
title, plain-English explanation, default value, range, and guidance on
when to change it. Content is in translation files so it can be localized.

## Layers touched
- New Lit component: `<irrigation-help-tip key="...">`
- Centralized help content in `translations/en.json`
- Audit every panel input → confirm it has a tip
- Accessibility: keyboard focus, ARIA, screen-reader friendly
- Mobile fallback: tip becomes small modal on narrow screens

## Acceptance criteria
- [ ] ⓘ icon appears on every adjustable field/section in Options
- [ ] Standard tooltip format (title / explanation / default / range / guidance)
- [ ] Hover opens on desktop, tap on touch devices
- [ ] Escape / tap-outside / × closes
- [ ] Keyboard navigation works (tab to ⓘ, Enter to open)
- [ ] ARIA `role="tooltip"` exposed for screen readers
- [ ] All content lives in `translations/en.json` (not hardcoded in components)
- [ ] Visual review pass: tooltips look consistent across all settings

## User stories covered
PRD: 69–72

## Blocked by
Slice 1
""",
    },

    # -- Slice 12: Runtime conflict guardrails -------------------------------
    {
        "title": "Slice 12 — Quiet hours, cascade caps, and morning summary",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
Configure quiet hours (default 10 PM – 7 AM) — no push notifications
during this window except hard failures. Cascade cap (default 2 hours
past original time, or 3 deferrals in a day) — runs that would exceed it
get skipped, not deferred. At end of quiet hours, a single consolidated
morning summary notification fires.

## Layers touched
- Quiet hours setting + per-event override flag
- Cascade tracking in `ConflictResolver`
- Morning summary notification (digest of overnight events)
- Tests for cascade cap logic

## Acceptance criteria
- [ ] Quiet hours configurable in Options
- [ ] During quiet hours, non-critical notifications are queued, not pushed
- [ ] Critical events (per Slice 10 matrix) push immediately regardless
- [ ] Cascade cap activates correctly when limit is hit (skip vs defer)
- [ ] Morning summary fires at end of quiet hours with overnight events
- [ ] Configurable summary time (default = quiet-hours end)
- [ ] Unit tests cover cascade-cap activation + summary aggregation

## User stories covered
PRD: 39–44

## Blocked by
Slice 5, Slice 10
""",
    },

    # -- Slice 13: New grass mode --------------------------------------------
    {
        "title": "Slice 13 — New grass establishment mode",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
Click "Planting new grass?" in the panel. Wizard appears: pick zone(s),
cycles/day, minutes/cycle, total days (defaults 3 × 10 min × 12 days).
Normal schedules for those zones pause. Establishment schedule fires
the bounded cycle. At end, prompts: continue / extend / switch to normal.

## Layers touched
- New schedule type: `EstablishmentSchedule` with start/end dates
- Pause regular schedules during establishment window
- Bypass moisture min/max during establishment (zones will be wet by design)
- Hot weather boost still applies on top
- Transition prompt on completion
- Tests for bounded schedule lifecycle

## Acceptance criteria
- [ ] "Planting new grass?" button in panel
- [ ] Wizard captures zone(s), cycles/day, duration, days
- [ ] Defaults match `DEFAULT_NEW_GRASS_*` constants
- [ ] Normal schedules on affected zones paused while active
- [ ] Cycles fire at evenly-spaced times (suggested defaults, user can drag)
- [ ] Moisture min/max bypassed; hot-weather boost still applies
- [ ] Completion prompt: continue / extend / switch back
- [ ] Unit tests for lifecycle states

## User stories covered
PRD: 73–77

## Blocked by
Slice 3, Slice 7
""",
    },

    # -- Slice 14: Weekly verification ---------------------------------------
    {
        "title": "Slice 14 — Weekly verification reminder",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
Sunday morning (configurable), a dismissible notification + panel banner
appears summarizing the last 7 days per zone (runs, avg moisture vs.
target). Buttons: "Looks good — dismiss" / "Review schedules" / "Snooze
30 days." Plus a one-time 7-day post-install reminder asking if setup is
working.

## Layers touched
- Scheduled job: weekly + one-time-post-install
- Per-zone stats aggregation (already needed for morning summary)
- Notification + panel banner UI
- Tests for the stats aggregation

## Acceptance criteria
- [ ] One-time reminder fires 7 days after install (configurable on/off)
- [ ] Weekly reminder fires Sundays at configurable time (default 8 AM)
- [ ] Reminder shows per-zone stats (runs, avg moisture, target)
- [ ] "Snooze 30 days" works
- [ ] Unit tests cover stats aggregation

## User stories covered
PRD: 78–81

## Blocked by
Slice 10
""",
    },

    # -- Slice 15: Calendar two-way edits ------------------------------------
    {
        "title": "Slice 15 — Calendar two-way edit (drag, skip, add)",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
Drag a run event in HA's calendar dashboard to a new time — the
integration honors the shift as a one-off override. Delete an event —
treated as a skip. Add an event with the right title format — creates a
one-off run. Rule-level changes (interval, conditions) still happen in
the panel only.

## Layers touched
- `CalendarEntity` implements `async_create_event`, `async_delete_event`,
  `async_update_event`
- Override store separate from rule store (one-off changes)
- Coordinator applies overrides when materializing the schedule
- Tests for create/update/delete event roundtripping

## Acceptance criteria
- [ ] Drag-to-move in HA Calendar dashboard creates a one-off shift
- [ ] Delete event → run skipped, logged
- [ ] Add event with parseable title (e.g., "North Lawn 15 min") → one-off run
- [ ] Edits persist across HA restart
- [ ] Rule changes are not editable via calendar (must use panel)
- [ ] Unit tests on event CRUD

## User stories covered
PRD: 59, 60

## Blocked by
Slice 4
""",
    },

    # -- Slice 16: iCal feed -------------------------------------------------
    {
        "title": "Slice 16 — iCal feed for external calendar subscription",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
Subscribe your phone's native calendar app (iOS / Google Calendar) to a
stable iCal URL exposed by the integration. Read-only — you see upcoming
runs alongside your other calendars. Instructions shown in the panel
Options.

## Layers touched
- New HTTP view: `/api/complete_irrigation/calendar.ics`
- Generates ICS from `RunPlanner.plan_next_14_days`
- Authentication handled by HA's standard token mechanism (or a public
  URL via setting if user opts in)
- Documentation in panel Options with copy-button URL

## Acceptance criteria
- [ ] ICS URL returns valid iCal format
- [ ] Subscription works in iOS Calendar (test instructions in docs)
- [ ] Subscription works in Google Calendar (test instructions in docs)
- [ ] URL is shown in panel with copy button
- [ ] Auth model documented (default: requires HA long-lived access token)
- [ ] Unit test on ICS generation (validate against ical lib)

## User stories covered
PRD: 61

## Blocked by
Slice 4
""",
    },

    # -- Slice 17: HACS release ----------------------------------------------
    {
        "title": "Slice 17 — v1.0 HACS release prep",
        "labels": ["enhancement", "ready-for-agent", "vertical-slice"],
        "body": """## Type
AFK

## What you'll be able to do
Anyone with HACS installed can add this integration via the custom
repository, see it in the search, install it, and have it work. Version
1.0 tagged on GitHub. README updated with installation steps.

## Layers touched
- Final README pass (install steps, screenshots, feature matrix)
- Bump version in manifest.json to 1.0.0
- Tag `v1.0.0` release
- Confirm `hassfest` + `hacs/action` workflows still pass
- Submit to HACS default repository (optional — can stay custom repo)

## Acceptance criteria
- [ ] manifest.json version = 1.0.0
- [ ] README has clear HACS install instructions
- [ ] Screenshots of panel + config flow + key features
- [ ] All slices 1–16 complete and verified
- [ ] `hassfest` passes on latest commit
- [ ] `hacs/action` passes on latest commit
- [ ] v1.0.0 tag pushed
- [ ] GitHub release created with changelog

## User stories covered
PRD: 1, 4

## Blocked by
All previous slices
""",
    },
]


def create_issue(item: dict, repo: str) -> str:
    """Call `gh issue create` with body via temp file."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as f:
        f.write(item["body"])
        body_path = f.name

    try:
        result = subprocess.run(
            [
                GH,
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                item["title"],
                "--label",
                ",".join(item["labels"]),
                "--body-file",
                body_path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(body_path).unlink(missing_ok=True)

    if result.returncode != 0:
        print(f"  FAIL: {item['title']}", file=sys.stderr)
        print(f"    stderr: {result.stderr}", file=sys.stderr)
        return ""
    return result.stdout.strip()


def main() -> int:
    print(f"Creating {len(ISSUES)} issues on {REPO}...\n")
    created = []
    for idx, item in enumerate(ISSUES, start=1):
        print(f"[{idx:>2}/{len(ISSUES)}] {item['title']}")
        url = create_issue(item, REPO)
        if url:
            print(f"         -> {url}\n")
            created.append(url)
        else:
            print("         -> FAILED\n")

    print(f"\nDone. {len(created)}/{len(ISSUES)} issues created.")
    return 0 if len(created) == len(ISSUES) else 1


if __name__ == "__main__":
    sys.exit(main())
