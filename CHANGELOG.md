# Changelog

All notable changes to this integration. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [Semantic Versioning](https://semver.org/).

## v1.41.1 — 2026-07-13 — patch

**Today's plan now shows what actually ran.** A planned run stayed on its
forward-looking "🟢 On track — run as scheduled" even after it had already
watered. Each item on the **Today's plan** card now flips to its real outcome
once the run fires, matched against run history by schedule (then zone) for
today:

- **✅ Ran on schedule** — the scheduled run completed.
- **💧 Running now…** — the run is in progress.
- **🟠 Stopped early** — the run was aborted.
- **⏭️ Skipped today** — a gate (rain / moisture / wind) skipped it.

The card also refreshes run history whenever you open the Today tab and once a
minute while it's open, so the state updates without a manual reload.

## v1.41.0 — 2026-07-13 — minor

**Attach an external AI for plant identification.** The local vision model
(qwen on your LAN) is private and free but small — it often can't tell species
apart or returns garbled JSON. You can now attach a stronger **external** model —
**Anthropic (Claude)**, **xAI (Grok)**, **Google (Gemini)**, or any
OpenAI-compatible endpoint — and pick how it engages:

- **Local model only** — unchanged default.
- **Local, with external fallback** — try the local model first; only call the
  external AI when the local one fails or can't identify the plant. Cheapest.
- **External model only** — always use the external AI.

Set it up in **Settings → Plant identification**: pick a mode and provider (the
endpoint URL auto-fills), enter the model and your API key, and hit **Test
connection** to probe each configured model. The key is stored on your Home
Assistant server and is **never** sent back to the browser or written to the
logs. All four providers speak the same OpenAI chat API, so one setting covers
them; "Custom" lets you point at any other server.

## v1.40.12 — 2026-07-13 — minor

**Curated care beats the LLM's blind spot.** A small vision model (qwen2.5vl:7b)
reliably fails to *differentiate* water-use — it labels essentially every species
"moderate / full_sun", which is exactly the value that drives the irrigation math.
New `plant_care_db.py` ships a deterministic, curated care table for ~37 common
Southwest / low-desert landscape species (WUCOLS water-use category, sun class,
frost/heat range, care-plan preset, water + fertilize cadences). The species-ID
rail now consults this table **first** — so once identification gets the *name*
right (oleander, Texas ranger, sissoo, citrus, rose, rosemary…), the plant is
seeded with accurate low-desert care instead of the model's flat "moderate". The
model is only a fallback for species the table doesn't cover, and every curated
value is still bounded by `validate_suggestion` before it reaches a record.

## v1.40.11 — 2026-07-12 — minor

**Duplicate a plant.** Each plant now has a **Duplicate** button (next to
Edit/Delete) that creates a copy with the same species, water-use, canopy, zone,
drips, and care attributes — **minus the photos, map position, and health** — then
opens the copy in the editor so you can add a fresh photo and tweak the name.
Fast when you have many identical plants (a row of oleanders, lantanas, etc.).
New `duplicate_plant` service returns the new plant's id.

## v1.40.10 — 2026-07-12 — patch

**Plant photos open in a pop-up lightbox.** Tapping a plant's photo thumbnail now
opens the full-size image in an in-panel modal (dark backdrop, close button, tap
outside to dismiss) instead of navigating away to a new browser tab. Cmd/Ctrl-click
still opens it in a new tab if you prefer.

## v1.40.9 — 2026-07-12 — minor

**Research a plant's details by name.** When the vision model misidentifies a
plant, correct the **Plant species** field to the right name and click the new
**🔬 Research details** button — the LLM looks up that species by name (no photo)
and fills sun exposure, temperature tolerance, water-use category, watering /
fertilizing cadence, and a care plan. Your species text and canopy are kept.

- By-name lookup is far more accurate than a photo guess: verified live —
  "Nerium oleander" returned Oleander / full sun / 20–110 °F / moderate water-use
  / shrub, where the photo ID had guessed the wrong genus.
- New `research_plant_species` service (`plant_id` + optional `species`); reuses
  the same repair-aware JSON parsing and the configured vision endpoint in text
  mode. Applies the researched attributes and seeds the care plan.

## v1.40.8 — 2026-07-12 — patch

**Fix: species identification failed on a clear photo with a working endpoint.**
Root cause: small local vision models (e.g. qwen2.5vl:7b) echoed the prompt's
inline hints into their JSON — e.g. `"fertilize_every_days": 30 (0 = not
necessary)` and `"sunlight_class": "full_sun|bright_shade"` — producing invalid
JSON, so the reply never parsed and identification "failed."

- **Rewrote the vision prompt** to a valid-JSON skeleton with the enum choices
  described in prose, so the model can't copy pipe-lists or parenthetical notes
  into its output. Verified live against qwen2.5vl:7b — it now returns clean JSON.
- **Added a repair layer** (`extract_suggestion_json`): strips code fences /
  surrounding prose, `//` comments, a parenthetical annotation echoed after a
  numeric value, and trailing commas — belt-and-suspenders for any small model.
- **Enum robustness**: a `"a|b"` echo is now read as its first token, so
  sunlight/water-use/care-preset survive instead of being dropped.
- Note: identification now **succeeds** (returns a full suggestion you accept or
  edit). A 7B vision model's *species guess* can still be off — correct it in the
  **Plant species** field; the sun/temp/water-use/canopy/care attributes still fill.
- **Fix: saving a plant edit failed with "enter a valid number" on canopy.** The
  canopy input was `min="0.1" step="1"`, so the browser rejected every real canopy
  value (20, 12, 2.5 …) as a step mismatch. Changed to `step="any"` — any positive
  area saves. (Affected the manual add form too; photo-add was unaffected.)

## v1.40.7 — 2026-07-12 — minor

**Capture every attribute the plant ID returns, and turn the multi-schedule
warning into an informational section.**

- **Full identified-attribute set is now persisted.** The vision model returns
  common name, sunlight class, temperature tolerance (°F), care preset, water &
  fertilize cadence, a one-line note, plus ID confidence / model / timestamp —
  but the plant record only kept species, water-use, canopy, and light range.
  All of it is now stored on the plant and shown in a read-only **Identified
  attributes** panel in the plant editor. Old records load with empty defaults;
  `add_plant_from_photo` / **Identify species** fill them.
- **"Scheduled for this loop" section replaces the warning.** A loop watered by
  more than one schedule showed a ⚠ *"Also watered by N other schedule(s)"*
  warning. That's now a plain **Scheduled for this loop** list in the loop's
  design card — every schedule with its runtime and runs/week, the biggest one
  marked *primary*. (A loop with **no** schedule still warns — that's actionable.)
- Note on identification: a plant added **before** the vision endpoint was
  connected saves as a provisional stub (species blank); use **Identify species**
  on it to fill the species and all the attributes above.

## v1.40.6 — 2026-07-12 — patch

**Edit plants easily — the plant list is now a mobile-friendly card list.**
Editing a plant after creation already existed (Yard tab → each plant's **Edit**
button → change name, species, zone/loop, water-use, canopy, drips, light
range), but the list was a wide 5-column table whose Edit/Delete buttons
scrolled off-screen on a phone. It's now a stacked card per plant — name +
species + zone + category + area, with **Edit** and **Delete** always visible
and full-width on mobile.

## v1.40.5 — 2026-07-12 — patch

**Plant add: separate "Plant species" and "Friendly name" fields.**

- The photo-add card's single "Name" field is now two: **Plant species**
  (what it is — auto-identified from the photo, or type your own) and
  **Friendly name** (what you call it — e.g. "Front-yard lemon"). The manual
  add form uses the same two labels.
- A typed **species is kept** over the vision guess (and survives an identify
  failure) — you still get the vision care plan / water-use category / canopy.
  `add_plant_from_photo` now accepts an optional `species`.
- Backend note (not a bug): a plant added while the vision endpoint is
  unreachable saves correctly with its photo on the chosen zone, but stays at
  provisional defaults (species blank, moderate / 20 sqft) until identified —
  configure the vision endpoint or use **Identify species** to fill it.

## v1.40.4 — 2026-07-12 — patch

**Plant photo-add flow: clearer, confirmable, and camera-vs-library aware.**

- **Thumbnail + explicit "Add plant" submit.** Taking or choosing a photo used
  to auto-submit with only a subtle busy line — so if a camera capture silently
  returned no file (e.g. the HA app lacking camera permission), it looked like
  nothing happened. Now the photo is staged with a **thumbnail** (a small icon
  copy so you can see it took), and you tap **Add plant** to submit. The
  thumbnail stays visible with a "⏳ Submitting…" tag while the add runs.
- **Take photo vs Choose photo.** Two distinct buttons: **📷 Take photo** opens
  the camera (`capture`); **🖼 Choose photo** opens your photo library (no
  `capture`). A library photo keeps its **GPS EXIF**, so the plant auto-places on
  the yard map — the upload pipeline reads location from the original bytes
  before downsizing (a camera capture usually has no location metadata).
- The **zone picker** already lists only configured controller zones (v1.40.3).

**Rain-lockout override + the ceiling now truly reads the forecast.** Follows
v1.40.2; multi-model regression gated.

- **Override button on the Today and Scheduling tabs.** When a rain lockout is
  active, both tabs now show an **Override** button that ends the current lockout
  immediately so watering resumes — the next rain re-arms it normally. Previously
  this only existed on the Weather tab (all three now read "Override" for
  consistency). One tap, self-limiting — nothing to leave switched on by accident.
- **Ceiling responds to the forecast high, not just the current temp.** Rain
  usually falls during a cool storm cell, so the heat-graduated ceiling was being
  set from an 85–90°F reading even when the day's high was 112°F. The coordinator
  now caches the near-term forecast daily high (today + tomorrow, from the ETo
  forecast it already fetches) and feeds the **hottest of {current temp, weather
  temp, forecast high}** into the ceiling. A big rain now resumes on the schedule
  the *heat* dictates, not the storm's momentary chill.
- **Unit-safe temperature.** All three heat signals are normalized to °F
  (Celsius/Kelvin converted) — a latent mis-scale for metric weather entities.
- **Test coverage for the coordinator glue.** Added coordinator-level tests for
  the exact wiring the earlier bug lived in — unit-safe rainfall (mm→in), the
  config floor, the F-normalized heat signal, rising-edge arming, and the
  forecast-high cache — plus pure tests for `rain_to_inches` / `temp_to_fahrenheit`.
- **Fixed: the plant "Zone / loop" picker listed every switch in HA.** When
  adding a plant (from a photo or the manual form), the zone dropdown offered
  *all* `switch.*` entities — lights, plugs, fans — instead of just the
  configured irrigation-controller zones. It now lists only the controller zones.

## v1.40.2 — 2026-07-12 — minor

**Rain lockout reworked for the desert** — brief monsoon cells no longer strand
plants in the heat, and the lockout now adapts to temperature two ways. 4-model
agronomy + implementation gated (Grok + Gemini + Qwen external panel).

- **ET-scaled lockout duration.** The lockout length is now
  `effective_rain / daily_ETo`, using the integration's own live FAO-56 ETo,
  instead of fixed rainfall tiers. `effective_rain = rain − 0.10"` interception
  (canopy + arid surface evaporation). Because ETo rises with temperature, a
  hotter forecast automatically produces a **shorter** lockout — the same 0.25"
  locks out ~12 h in summer vs 48 h in cool winter.
- **Temperature-graduated ceiling.** The maximum lockout is no longer a fixed
  cap — it shrinks smoothly with the day's heat (the hotter of the current temp
  and the forecast high): ~48 h when mild (≤85 °F), 24 h at 105 °F, 18 h in
  extreme heat (≥115 °F). So a big rain never strands the yard more than about a
  day when it's very hot, while a cool-weather rain can hold up to two days. If
  the temperature signal is missing, the ceiling fails safe to 24 h (assume it
  may be hot — never default a blind sensor to the longest lockout).
- **Configurable rain floor.** New `rain_lockout_min_inches` (default 0.10")
  — rainfall below it never pauses watering. Raise it (e.g. 0.20") outside
  monsoon so passing cells that drop a fraction of an inch don't lock out.
  Exposed in the Weather tab and via `set_weather_config`.
- **Unit-safe rainfall reading.** The rain sensor's `unit_of_measurement` is now
  honored (mm/cm → inches). Fixes spurious lockouts where a millimetre reading
  was treated as inches (e.g. a Tempest reporting 0.25 mm ≈ 0.01").

## v1.40.1 — 2026-07-12 — patch

- **Integration brand icon (HA 2026.3+).** Ships `brand/icon.png` +
  `brand/icon@2x.png` so the integration's own icon shows in the UI (the
  home-assistant/brands repo no longer accepts custom-integration icons —
  local brand images are served directly and take priority over the CDN).

## v1.40.0 — 2026-07-12 — minor

**Scheduling power + hardware verification** (features inspired by a review of
Irrigation Unlimited). 4-model reviewed (Grok + Gemini + Qwen external panel
raised 7 concerns; Claude's adversarial verification refuted ALL 7 against the
real code — each was already guarded — so the gate shipped clean).

- **Check-back valve verification.** After the integration commands a zone
  on or off, it re-reads the physical switch ~30 s later; on mismatch it
  retries the command once, and on a second failure raises a CRITICAL alert
  ("water may not be flowing" on-fail, "valve may be STUCK OPEN" off-fail) and
  records the run aborted. Hardens against Rachio-cloud/Wi-Fi command loss —
  software answer to "did the valve actually actuate?". Configurable
  (`verify_switch_seconds`, 0 disables; default 30). The verify decision
  re-reads live run-state at fire time, so it can never re-open a legitimately
  stopped valve, and never fires for externally-detected offs.
- **Sunrise / sunset anchored schedules.** A schedule can start at sunrise or
  sunset ± an offset, and can be **finish-at** anchored (the run COMPLETES at
  the anchor — e.g. "finish watering by sunrise"), with correct multi-block
  span back-up across midnight. Falls back to the fixed time when sun data is
  unavailable. Fully guarded for polar day/night and DST.
- **Seasonal month windows.** A schedule can be limited to a range of months
  (e.g. water only May–September).
- **`tools/LLM_OPERATIONS_PROMPT.md`** — a living, light-model-friendly
  operations prompt encoding every learned ground truth and failure signature
  (block chunking, independent zones, gap insertion, skip-don't-backfire,
  plant-id vs zone-id, check-back semantics); the advisor job can load it via
  `WA_PROMPT_FILE`.

## v1.39.3 — 2026-07-11 — patch

- **CI fix (no functional change):** a new test file imported `voluptuous` at
  top level instead of via `pytest.importorskip`, breaking test collection on
  the CI runner (which has no HA installed). The shipped integration was
  unaffected. Matched the repo's skip-in-bare-env test convention.

## v1.39.2 — 2026-07-11 — patch — CRITICAL setup fix

**Fixes a setup crash introduced in v1.39.1 — the integration failed to load.**
A stray extra argument in the `dismiss_watering_advice` service registration
raised `TypeError: async_register() got multiple values for 'schema'` during
setup, so everything registered after it (the WebSocket API, health.json view,
and the sidebar panel) never came up — the sidebar entry disappeared and the
panel/API 404'd. (The watering coordinator itself still started, so schedules
kept running.) Fixed the registration; added an AST regression test that fails
the build if ANY `services.async_register` call ever has the wrong number of
positional arguments again. If you were on v1.39.1: update to v1.39.2 and
restart; the sidebar returns.

## v1.39.1 — 2026-07-11 — patch

- **"Test connection" for the vision endpoint** (Settings) — probes the
  configured endpoint with a tiny request and shows ✓ Connected / ✗ error
  inline, so a bad URL or model name surfaces before your first Identify.
  New response-returning service `test_vision_endpoint`.
- **README: "Local AI setup"** — accurate walkthroughs for all three local-AI
  surfaces (species-ID endpoint, vision-health job, watering-advisor job).

## v1.39.0 — 2026-07-10 — minor — the "app complete" release

**The full smart-watering loop, closed.** Gap-aware scheduling, real delivered-
water math, and the approval-gated LLM watering advisor — plus a chained-run
loss fix. 4-model reviewed (Grok + Gemini clean; Claude's adversarial gate
found 4 watering-critical defects in the new restart/insertion interplay, all
fixed pre-release with regression tests proven to fail against the old code).

- **Gap-aware short-run insertion** — a short scheduled run now slots into a
  long chunked run's idle inter-block gap instead of deferring for hours.
  One-valve-at-a-time is enforced with margins on both sides, proven
  minute-by-minute in simulation; restart-ghost plans, over-cap runs, rain
  lockout, and concurrent resumes are all explicitly guarded (the 4 gate fixes).
- **Chained-run loss fix** — the second of two short runs deferred behind a
  big-gap chunked run is no longer silently lost (rescue via displaced-
  occurrence ledger; skip-don't-backfire preserved through rain lockout).
- **Delivered-water math** — installed drips (count × GPH × runtime) now show
  what each plant actually receives vs its need (OK/UNDER/OVER) in the yard
  report, alongside the recommended set.
- **LLM watering advisor** — an external job (your LLM host; Qwen on the mini)
  reads health.json and proposes at most 6 items in exactly two shapes: move a
  schedule's start time, or resize a plant's drip set toward its real need. A
  server-side rail bounds everything (unknown ids dropped, HH:MM enforced,
  values clamped); the panel's 🤖 advisor card applies each item ONLY when you
  tap it, through the same validated services you use manually.
- 58 new tests since v1.38.1 (624 total).

## v1.38.1 — 2026-07-10 — patch — IMPORTANT watering fix

**Chunked-run blocks 2..n never fired on modern HA (2024.5+).** The block timer
callback ran in a worker thread; HA's thread-safety guard raised RuntimeError
before the block could dispatch — so a long run delivered only its FIRST 58-min
block of water, and run history only ever showed "(block 1/N)" (the symptom
that surfaced this). Confirmed in the live HA log. The same defect silently
disabled the restart-resume path and the post-install reminder. All four call
sites are now proper event-loop callbacks; every block dispatches, waters, and
records its own "(block i/n)" history row, closed by its own auto-stop.
+3 regression tests incl. a job-type lock (572 total).

## v1.38.0 — 2026-07-09 — minor

**Photo-first plants: take a picture, get a plant.** (Also ships the unreleased
v1.37.1 ✓-Saved fix below.) "📷 Add from photo" is now the primary Yard action:
pick the zone it's watered by (plus optionally its drip emitters — count × GPH),
snap or choose a photo (phones open the camera), and the vision model fills the
rest — name, species, water-use category, a canopy estimate from the photo,
light range — and seeds the starter care plan, all auto-applied on the new
plant and all editable afterwards. EXIF GPS still auto-places the map marker.

- **Installed drip emitters on every plant** (count × GPH, both-or-neither,
  clearable) — the ground truth for delivered-water math; shown on the plant
  ("Drips: N × X GPH") and editable in the form.
- Species suggestions now include a photo-based **canopy estimate**;
  `apply_species_suggestion` gains `apply_canopy` (off by default — applying a
  suggestion never clobbers a user-entered canopy; the photo-first flow opts in).
- 4-model review (Grok/Gemini/Claude) — fixed pre-release: a duplicated
  schema/registration that silently dropped the emitter fields (critical),
  one-sided emitter input crashing instead of validating (high), photo-store
  failure stranding a photo-less guess-record with a false success notice
  (medium), no way to clear emitters, and a name-length mismatch.

## v1.37.1 — 2026-07-09 — patch

- **Vision-endpoint save now confirms visibly** — the Save button flips to
  "✓ Saved" for a moment after a successful save (it previously saved silently,
  which read as doing nothing).

## v1.37.0 — 2026-07-09 — minor

**"Plant Brain": photo → species identification + one-tap care sheet, on your
own GPU.** Point the integration at a local OpenAI-compatible vision endpoint
(Settings → Vision endpoint; e.g. Ollama on a LAN box), tap 🔍 Identify on any
plant with a photo, and a bounded SUGGESTION lands on the plant — species +
common name, confidence, sunlight class, temperature tolerance, WUCOLS category,
care-plan preset, watering/fertilizing cadence. Apply sets species + optimal-lux
range + WUCOLS and seeds the starter care plan in one tap; Dismiss discards.
Nothing applies without you. New rail `species_id.py` bounds every field
(enum/isinstance guards, control-char + bidi stripping, finite-number checks);
a tampered store degrades to "no suggestion", never a crash. Also: new official
icon (smart-home + water drop). 4-model review (Grok/Gemini/Claude; Qwen
timed out) — both HIGH gate findings fixed pre-release (vision endpoint
persistence; unhashable-enum load crash), plus response-size cap, path
containment, and text-sanitizer hardening. 569 tests.

## v1.36.0 — 2026-07-08 — minor

**Stress triangulation + one-tap care plans.** (Also carries the v1.35.1
yard-map fix below — released together as one tag.)

- **Light verdicts join the watering diagnosis** as corroborating signs: a
  plant surveyed at MORE light than optimal raises water demand (and scorch
  can mimic underwatering); LESS light means slower-drying soil and
  corroborates overwatering. Light alone never trips a verdict — soil
  evidence stays in charge. Fresh verdicts (<=120 days) only.
- **Vision job context**: health.json now carries each plant's species, lux
  range, and latest light verdict; the NAS vision job appends them to its
  prompt so health assessments account for the plant's light situation.
- **One-tap care plans**: `seed_care_plan` service + panel row — pick
  tree / shrub / flower / cactus & succulent / grass and the starter
  fertilize/prune/mulch/inspect reminders are created (idempotent).

## v1.35.1 — 2026-07-08 — patch

**Fixes "unable to fetch" on yard-map setup.** Esri's World Imagery export
service changed server-side: it now returns HTTP 500 for any request sharper
than its imagery cache (~0.3 m/px) instead of upsampling — a ~60 m residential
yard at 1536 px asked for 0.04 m/px, so every fetch failed. The service now
fetches at the sharpest scale Esri will serve (`safe_export_size`, floor
0.3 m/px — verified live against the measured 0.25-works/0.22-fails cliff) and
upscales locally with Pillow for crisp display. The stored bbox is unchanged,
so existing plant-marker positions and GPS auto-placement are unaffected.

## v1.35.0 — 2026-07-08 — minor

**Plant-care bundle: light surveys with a roaming lux sensor, recurring care-task
reminders, species on plant records, and an advisory over/under-watering diagnosis
card.** Feature set modeled on the best of the consumer plant-care apps
(Picture This / SmartyPlants), adapted to an irrigation controller that can act on
its own data. All new surfaces are advisory — nothing here changes watering.

- **Light surveys (roaming lux sensor).** Each plant can carry an optimal
  illuminance range (`set_plant_light_range`, or presets in the panel). Place a
  portable lux sensor (e.g. an ESP32/VEML7700 Zigbee stake) at the plant and
  `start_light_survey` samples it once a minute for a timed window (default
  10 min), then stores min/avg/max + a verdict — too little / optimal / too much
  light — on the plant (bounded history, newest-first) and notifies the result.
  New rail `light_survey.py` bounds every stored value; a flaky sensor degrades
  to a failed-survey notice, never a stored lie.
- **Care-task reminders.** Recurring non-watering care (fertilize / prune / mulch /
  inspect / custom) per plant or zone with last-done tracking (`add_care_task`,
  `update_care_task`, `delete_care_task`, `complete_care_task` + a panel card).
  Due tasks notify and re-nag weekly until marked done; completing re-arms the
  interval from today. New pure module `care_tasks.py` + its own storage key.
- **Species on plants.** `add_plant`/`update_plant` accept a free-text `species`
  (common or scientific name) shown in the plant editor.
- **Watering diagnosis card.** New `watering_diagnosis.py` rail cross-checks a
  zone's current moisture vs thresholds, its 14-day run history (completed /
  already-moist skips / aborted), and vision-health concern keywords, and renders
  a Signs → How to confirm → Suggestions card (🩺 Diagnose on each zone row).
  Soil evidence outranks keywords — vision concerns alone never trip a verdict;
  conflicting evidence reports "unknown" with a sensor sanity-check prompt.
  Advisory only: it suggests schedule changes, it never applies them.
- New WS commands: `list_care_tasks`, `watering_diagnosis`; `list_plants` now also
  returns live light-survey progress. All admin-gated like the rest of the API.

## v1.34.0 — 2026-07-07 — minor

**Fixes a real watering outage: after an app update restarted HA mid-run, short
schedules (a birdbath on its own water line) silently stopped firing.**

- **Independent-supply zones.** A zone can now be marked as being on its own water
  line (config `independent_zones`) — it is exempt from one-zone-at-a-time
  serialization, so it fires on schedule, concurrently with any other zone, and
  never blocks (or is blocked by) the shared controller. This is the correct model
  for a birdbath fill that doesn't share the drip manifold's pressure; its midday
  runs were being deferred behind the multi-hour Citrus/Trees runs and dropped.
- **Orphaned-session self-heal.** A chunked run interrupted before its final block
  (e.g. an HA restart mid-sequence) never cleared its run session; the stale record
  then *phantom-reserved* the one-zone controller for its whole original window,
  silently blocking other zones. Finished sessions are now pruned every tick (and on
  the next tick after a restart), so an interrupted run can no longer starve the yard.
  This is the direct cause of the birdbath outage after the HACS update to v1.33.

Also rolls up the watering-core + vision-job hardening from the 4-model loop review
(Claude + Grok + Gemini + Qwen, adversarially verified) — no regressions, every
prior guard re-verified intact:

- **Fixed: auto-soak could truncate a live same-zone run.** The soak *re-cycle*
  branch was missing the "this zone's valve is already on → let the run finish"
  guard that the first-cycle branch has, so a soak cycle-2 firing during the soak
  wait could supersede and cut short a scheduled/manual run on the same zone
  (under-watering). Added the matching guard; both soak branches now agree.
- **Fixed: hot-weather boost could silently drop a run on the hottest days.** A
  long base run × a large boost could exceed the 480-minute cap; because the run
  is dispatched fire-and-forget, the boundary's rejection was raised in a detached
  task and swallowed — the run just vanished. The boost is now clamped to the cap
  before dispatch. `MAX_SCHEDULE_DURATION_MIN` moved to `const.py` so the boundary
  and the boost share one ceiling.
- **Hardened the external vision-health job** (`tools/vision_health_job.py`):
  rejects any `/local/` photo path containing `..` (defense-in-depth path-traversal
  guard, matching the job's "never trust health.json" design), and a non-numeric
  `VH_MAX_PLANTS` now falls back to the default instead of crashing at import.
- +4 regression tests driving the real coordinator paths (511 total).

## v1.33.2 — 2026-07-05 — patch

- **Fixed: the Today screen showed the wrong version (stuck at "v1.19.0").** The panel's
  on-screen version was a hardcoded JS constant that was never bumped past v1.19.0, so a
  correctly-installed newer version still displayed 1.19. Now shows the real version, and
  `release.sh` gates on it so it can't drift again.
- Rolls up the packaging/CI fixes that landed after 1.33.1: `hacs.json` reduced to the
  form current HACS accepts (dropped `render_readme`), all number-selector `step`s use
  `any` (hassfest), and the vision-health actuation filter broadened + NFKC-normalized.

## v1.33.1 — 2026-07-03 — patch

- **Vision-health: a NaN/Inf confidence can no longer be stored.** A model returning a
  non-finite confidence used to slip past the clamp and get written to `.storage` as
  invalid JSON (`NaN`), which could corrupt the plant store. It now falls back to the
  default, mirroring the codebase's existing `math.isfinite` discipline. (Found by an
  independent Claude review pass; the external Grok/Gemini/Qwen panel had missed it.)
- Broadened the advisory actuation-phrase filter (open/enable/trigger/begin/energize/
  engage) — cosmetic; the "advisory only" guarantee is structural, not regex-based.
- **CI:** trimmed `hacs.json` to the fields the current `hacs/action` schema accepts
  (dropped `country`, bumped the min-HA hint) so HACS validation on `main`/tags passes.

## v1.33.0 — 2026-07-03 — plant vision-health + full-app hardening

### Added — biannual plant vision-health (advisory)

- **Plants can be checked by a local vision model over time.** A new
  `set_plant_health` service stores a structured health verdict on a plant (state,
  confidence, changes-since-last, concerns, suggested care); the plant editor shows
  the latest verdict, and a concern fires an "important" notification. The health feed
  (`health.json`) now lists each plant's prior verdict, whether it's **due for review**,
  and the exact photos to compare — so an external job knows what to assess.
- **Safety rail.** The verdict is bounded server-side (`vision_health.validate_verdict`)
  before storage: an out-of-range state becomes "unknown", confidence is clamped, text
  is truncated + list lengths capped, and any care/concern item that reads like a
  control instruction (turn on / run_zone / a service call) is **stripped**. A
  hallucinating vision model can inform you; it can never smuggle in an action.
- **`tools/vision_health_job.py`** — a stdlib-only companion (deploy on your GPU/LLM
  host) that reads the feed, asks a local vision model (e.g. Qwen2.5-VL on the RTX
  5060) to compare each due plant's latest photo to a baseline, and posts the verdict
  back. Advisory only; never touches watering. See `tools/README.md`.

### Hardened — full-app multi-model review (security + stability)

A whole-app review (Claude multi-agent + Grok/Gemini/Qwen, adversarially verified)
found and fixed: **auto-soak now respects one-zone-at-a-time** (it could open a second
valve while another zone watered); **the schedule + run-history stores tolerate a
corrupt record** instead of failing the whole integration load; **moisture/weather
gates reject NaN/Inf** readings (they were silently over-watering); **quiet-hours is
validated** so a bad value can't suppress critical alerts or break startup; the
**health/calendar HTTP feeds require admin** (parity with the WS commands); a
**restart-resume freshness guard** stops it force-closing a newer run; the
**rain-lockout arms on a rising edge** so a daily-total sensor can't pin it on; plus a
frontend timer-leak fix and smaller cleanups.

## v1.32.0 — 2026-06-29 — photo DB, security hardening, one-zone serialization, daily plan

### Added — daily plan (advisory "Today's Plan", groundwork for the LLM scheduler)

- **A prioritized daily briefing of the day's watering.** Each day's planned runs
  are ranked by urgency — combining each zone's weekly water deficit, the current ET
  demand, and live moisture — so the driest / most heat-stressed zones surface first,
  and a saturated zone is flagged to **skip today**. It's **advisory only**: nothing
  about what actually fires changes (the scheduler, moisture/weather gates, and the
  one-zone resolver still own watering) — the plan is guidance, shown as a **"Today's
  plan" card** on the Today tab and exposed in the health feed for the LLM advisor.
- **A safety rail for the LLM advisor.** The local model may re-order the day by its
  own reasoning, but only within bounds: it must keep the same set of zones and can
  never un-skip a saturated zone or invent a run — otherwise the deterministic order
  stands. The model can make the plan smarter; it can't make it unsafe. 16 new tests.

### Added — one-zone-at-a-time across LIVE runs

- **A scheduled run now waits for a run already in progress instead of firing a
  second zone into it.** Rachio (and most controllers) water one zone at a time;
  the conflict resolver already serialized *scheduled* runs, but a **manual run**,
  or a run **resumed after a restart**, was invisible to it. Those in-progress runs
  are now fed to the resolver as fixed "committed" blockers (from the live run
  registry), so a due schedule is **deferred past them** — never overlapped. The
  block honors the inter-zone buffer at the boundary (a run scheduled right as a
  live one ends still lands a couple minutes later, not the same second), and a
  committed run is never re-fired even if it started moments ago. A run record with
  corrupt timestamps fails safe (it can't freeze all watering) but is now logged so
  the rare unsafe state is visible. 11 new tests; reviewed by a 3-model panel.

### Fixed — schedule editor number inputs no longer clip 2-digit values

- The compact hour/minute and per-zone duration boxes in the schedule editor were
  cutting off two-digit numbers ("30" showed as "3C") — the browser's number-spinner
  arrows plus padding left no room for the digits, especially in the HA macOS app's
  WKWebView. The spinner arrows are now hidden (the min/max/step already constrain the
  value) and the boxes are sized so the full number is always visible.

### Added — per-plant photo history (groundwork for biannual vision health)

- **Plants can carry a photo history** for monitoring health over time. A new
  `add_plant_photo` service stores an image (base64) under
  `www/complete_irrigation/plants/<id>/`, newest-first, capped per plant; the
  plant's first photo with embedded GPS can auto-place its map marker (EXIF-GPS),
  and every later update is tied to the **selected** plant (selection owns
  identity, manual drag owns position).
- **Photo upload + gallery in the Yard tab.** Editing a plant now shows an "Add
  photo" button and a thumbnail gallery of its history. Pick or snap a photo and
  the panel reads its GPS (if present), downsizes it client-side (longest edge
  1280 px, JPEG) to keep the upload small, and calls `add_plant_photo`. If the
  plant isn't placed on the map yet and the photo carries location data, it drops
  the marker for you — drag to fine-tune. The downsize re-encodes through a canvas,
  so the stored image carries no embedded EXIF/GPS.

### Security — recursive multi-model audit (Grok + Gemini + Qwen + Claude)

- **Access control, secure-by-default.** Services that mutate configuration,
  delete data, write files, or make outbound requests now require an **admin**
  user unconditionally (schedule/plant CRUD, the `set_*_config` setters,
  `start_establishment`, `clear_run_history`, `add_plant_photo`, `set_yard_map`).
  Operational controls a household member legitimately uses (`run_zone`,
  `stop_zone`, `run_schedule`, `set_schedule_enabled`, `set_zone_moisture`,
  `clear_rain_lockout`, `test_notification`) stay on the opt-in
  `admin_only_services` gate. The admin check **passes system/automation
  context**, so no Home Assistant automation is affected — only non-admin *users*
  are blocked from reconfiguring or destroying data.
- **Path-traversal hardening.** A plant `id` is used as a filesystem path
  segment, so it is now constrained to a safe slug (`[A-Za-z0-9_-]{1,64}`) at
  record construction *and* on `.storage` load — a tampered store entry with
  `../` / slashes / an absolute path is rejected (dropped on load), and
  `add_plant_photo` adds a realpath-containment check at the write itself.
- **Upload validation.** `add_plant_photo` (and the fetched yard aerial) now
  verify image **magic bytes** (JPEG/PNG/GIF/WEBP/BMP) before writing, so a
  crafted SVG/HTML payload can't be stored as a `.jpg` and served from `/local`
  (stored-XSS / content-sniffing vector).
- 11 new security tests; the audit ran three adversarial rounds to a clean
  consensus (all engines `secure`) and passed the HA Docker smoke test.

## v1.31.0 — 2026-06-27

### Added — 2D yard map with draggable plant markers

- **A new aerial yard map in the Yard tab.** "Set up yard map" fetches a high-resolution
  aerial photo of your property — **Esri World Imagery, no API key**, centered on your Home
  Assistant location — and caches it locally as the backdrop. Place each plant as a marker
  on the image and **drag it to its real spot**; positions persist.
- **Plants gain a map position.** `PlantRecord` now carries normalized `map_x` / `map_y`
  (each 0–1, independent of screen size); pre-v1.31 records load as "unplaced." Drag a
  marker (or tap an unplaced plant to drop it at center) and `update_plant` saves it.
- **New `set_yard_map` service** (center defaults to your HA latitude/longitude; optional
  `span_m`, default 60 m). The fetch is server-side and degrades gracefully — a network or
  service error leaves the previous map untouched and never crashes.
- **Georeferencing groundwork:** a pure, unit-tested `yard_map.py` converts between GPS
  coordinates and normalized map positions (exact + linear at yard scale). This is the same
  math future EXIF-GPS auto-placement will reuse to drop a marker from a photo's embedded
  location. The integration stays **dependency-free** (no imaging libraries).
- 12 new tests (georeferencing + plant coordinates).

## v1.30.0 — 2026-06-25

### Added — restart fail-over (resume an interrupted run)

- **If Home Assistant restarts mid-run, the run now resumes until its scheduled
  end** instead of being lost. Each in-progress run persists a session (its full
  duration + planned end); on startup the integration waits ~30 s for switch
  integrations to come back, then for each session: if it's still within its
  window, it resumes the **remaining** time (re-chunking a long run correctly,
  not just the current block); if it's already past its end, it closes the run
  out and forces the valve off. This also clears any "zombie" running record.
  Fails safe — a session with bad/missing data is closed (valve off), never
  resumed on a guess. A switch that's slow to reconnect is retried (bounded), not
  dropped; a zone waiting to resume shows as idle, never a false "Running".

### Fixed — run-state display fidelity

- **Today zone card no longer shows "Idle" while the schedule shows "Running
  now."** A chunked run's 30 s inter-block gaps (and Rachio state-poll lag) read
  the switch as off; the card now uses the active run/session — the same truth the
  calendar uses — so the two agree (and a rain/moisture/wind-gated run that never
  fired is not shown as running).
- **History triggers column de-cluttered.** It listed every gate on every row
  ("moisture, wind, hot_weather"), including disabled / no-effect breadcrumbs.
  Now only triggers that actually gated or adjusted the run are shown; the full
  blob is still available on expand.
- **Chunked runs collapse into one History row with a progress bar.** A long run
  delivered as N blocks ("Citrus (block 1/5)") is now one session row showing
  completed / total blocks, instead of N separate rows.

### Changed — CI

- HACS validation is scoped to pushes on `main` + tags (plus the daily schedule
  and manual dispatch). It no longer red-X's on transient feature-branch pushes
  whose ref is deleted at squash-merge.

## v1.29.0 — 2026-06-25

### Added — per-plant top-up recommendations on shared loops

- **The Yard report now recommends supplemental short runs for a plant that the
  loop's main schedule leaves under-watered** — the classic "thirsty plant on a
  shared loop" (e.g. a poor-soil potted plant that wants ~1 min/day sitting on the
  Shrubs loop). The fix for that plant is watering more *often*, not more emitters,
  so a top-up adds short whole-loop runs up to a daily cadence to close its weekly
  deficit.
- **Honest about the cost:** because running the loop waters *every* plant on it, each
  top-up reports the extra water its co-plants receive (e.g. "also waters Rosemary
  +25%"), so you can weigh a top-up against splitting the loop. When frequency alone
  can't close the deficit (the plant already runs daily, or the gap is too large even
  at a max-length top-up), it says so and points you to a dedicated loop / longer main
  run instead of inventing an absurd runtime.
- **Advisory by design:** top-ups are *computed and surfaced* (Yard tab + `yard_report`
  WS + `health.json`), not auto-fired — execution stays on the proven schedule path.
  Materializing a top-up as a real supplemental schedule is a planned follow-up.
- Pure logic in `hydraulics.recommend_topups()` (HA-free, unit-tested): 6 new tests
  (403 total).

## v1.28.0 — 2026-06-23

### Added — automatic reference ET from the weather forecast (FAO-56)

- **Reference ET (ETo) can now be computed automatically from your weather
  entity's daily forecast** instead of being entered by hand. Turn on
  *Auto reference ET from weather forecast* in the Yard tab (or pass
  `eto_auto: true` to `complete_irrigation.set_weather_config`) and the
  integration recomputes the weekly ETo daily at 03:00 — before the morning
  watering window — plus immediately when you toggle it on.
- **Physics:** full **FAO-56 Penman-Monteith** when the forecast carries humidity
  and wind; a temperature-only **Hargreaves** estimate when it doesn't (solar
  radiation is estimated from the daily temperature range when not measured). So
  it yields a sane ETo even on a sparse forecast. Computation is unit-aware
  (handles °F/°C and mph / km/h / kn / etc.) and uses your HA latitude + elevation.
- **Graceful by design:** `effective_eto()` falls back to your manual `eto_in_week`
  whenever auto is off, has never produced a value, is stale (older than 36 h — so
  a weather feed going dark can't strand watering on a days-old number), or is
  **outside the sane 0–10 in/week envelope** the manual field already enforces (so a
  flaky forecast — a provider field-swap or unit glitch — can never over-water the
  yard). The design math always gets a positive number; it never raises. The manual
  field becomes the labeled fallback when auto is on. A forecast spanning year-end
  is handled (day-of-year wraps 1–366 instead of tripping the ETo range check).
- **New `set_weather_config` fields:** `eto_auto` (bool) and `weather_entity`
  (which `weather.*` entity to read; defaults to the first available). The
  `health.json` feed gains an `eto` block (source / manual / auto value +
  timestamp / weather entity) for the LLM monitor.
- **Attribution:** bundles a trimmed copy of **PyETO** (FAO-56 / Hargreaves
  equations) by Mark Richards, BSD-3-Clause — see
  `custom_components/complete_irrigation/pyeto/LICENSE.txt`.

## v1.27.0 — 2026-06-22

### Fixed — run-history stays bounded between restarts

- **The 1000-record hard cap is now enforced on every insert**, not only at load.
  `prune()` (which also does age-based retention) runs only at coordinator startup,
  so between restarts `run_history` could grow past the cap — a slow leak that also
  made the new `health.json` scans O(n) in record count. `start_run` /
  `record_skipped` now trim the oldest records beyond `HARD_RECORD_CAP` on insert,
  so the list is always bounded. (Surfaced as a LOW finding by the v1.26
  health-endpoint security gate; the stale "pruned on each tick" comment is
  corrected.)

## v1.26.0 — 2026-06-22

### Added — health.json status feed (foundation for the LLM monitor)

- **New read-only endpoint `GET /api/complete_irrigation/health.json`** exposing
  everything a watchdog needs to enforce "every plant gets its water, every time":
  the schedules + cadence + each schedule's next planned run; the plants on each
  loop with their drips / volume (WUCOLS x ETo design); recent run outcomes + what
  is running now; and a **pre-computed list of misses** (aborted / skipped / short
  runs in the last 24 h) so a consumer has a deterministic alarm signal even when
  the LLM is unavailable.
- `requires_auth=True` (same as the iCal feed) — schedule / plant / occupancy data
  leaks landscaping patterns, so the feed is not reachable unauthenticated; a
  monitor authenticates with a long-lived access token.
- Pure helpers `health_view._detect_misses` / `_cadence_summary` covered by
  `test_health_view.py`. This is the first half of the LLM irrigation monitor; the
  mini-side watchdog (Qwen reasoning against the prime directive) consumes it.

### CI

- HACS validation no longer runs on `pull_request` events. `hacs/action` re-fetches
  the PR head branch, which 404s ("branch removed / not compliant") if the PR was
  merged + branch-deleted while the job was queued — a spurious red on the closed
  PR. Push/main/tag runs still validate the structure.

## v1.25.0 — 2026-06-21

### Added — long runs delivered in controller-cap blocks (batch runs)

- **Runs longer than the controller's per-activation cap are now split into
  back-to-back blocks.** Rachio (and similar cloud controllers) cap each zone
  activation at a fixed number of minutes (~60); v1.24 stopped rejecting long
  runs, but a single >60-min activation is still truncated by the controller. A
  long run is now delivered as cap-sized **blocks** with a short **gap** between
  each (off -> gap -> on = a fresh activation the controller accepts, with its
  manual-run timer reset). Example: Trees (300 min) -> `[58, 58, 58, 58, 58, 10]`,
  six blocks, ~302 min wall-clock. The run history records each as
  *"Trees (block 3/6)"*.
- **Chunking lives in `run_zone`** — the single service both scheduled
  (`_fire_run`) *and* manual runs funnel through — so a manual "Run Now" of a
  long duration chunks identically. Each block re-uses the normal single-
  activation path (auto-stop + external-off monitor per block); a run within the
  cap takes that path unchanged.
- **Default block size is 58 min**, two minutes under Rachio's 60-min limit so
  our auto-stop ends each block before Rachio's own cutoff (no boundary race).
  Tunable per install via two new `set_general_config` keys:
  `controller_max_run_minutes` (block size) and `block_gap_seconds` (gap).
- **Schedule editor notice:** when a schedule's duration exceeds the controller
  cap, the editor now shows how many blocks it will be split into and why
  (Rachio's per-zone limit).
- **Manual "Run Now" cap raised to 8 h** in the panel (was 60 min), so a manual
  deep-watering run can be requested at full duration and chunked.
- **Stop / delete / reload truly stop a chunked run.** Each block timer is tracked
  and the sequence is tagged with a per-zone generation; pressing Stop, deleting
  the schedule, or reloading the integration cancels every pending block (and a
  block that already slipped onto the loop re-checks the generation before
  firing), so the valve can never re-open after you stop a long run. A new run on
  a zone supersedes any still-pending sequence.

## v1.24.0 — 2026-06-21

### Fixed — long schedules never fired (the bug starving the deep-watering zones)

- **CRITICAL: any scheduled run longer than 60 minutes was silently rejected
  and never ran.** `run_zone` — the single service every run (manual *and*
  scheduled) dispatches through — capped `minutes` at a 60-minute "manual safety
  cap" (both the voluptuous schema and `validate_run_duration`). Because
  `coordinator._fire_run` fires scheduled runs through that same service, every
  schedule over 60 min was rejected *before it could start*: **Shrubs (135),
  Citrus (240), and Trees (300) produced zero run records and delivered no
  water**, while the short zones (grass, garden, 60-min Flowers) ran fine. Plants
  starved silently. The cap is now the schedule maximum (`MAX_SCHEDULE_DURATION_MIN`
  = 480 min / 8 h), so long deep-watering schedules fire. (Rachio's separate
  60-min *per-activation* hardware cap is handled by the v1.22 external-off
  ride-through.) Regression tests added for both the schema and the duration
  validator.

## v1.23.0 — 2026-06-18

### Added — plant-aware irrigation (the "Yard" tab)

A new **🪴 Yard** tab turns the integration from a scheduler into a drip-design
assistant: tell it what plants are on each zone and it sizes the emitters so
every plant gets the right amount of water — even when they share a loop.

- **Plants.** Add/edit/delete plants (name, **WUCOLS** water-use category,
  canopy area, and the zone/loop they're on) via new `add_plant` /
  `update_plant` / `delete_plant` services. Persisted in `.storage`.
- **Reference ET.** `set_weather_config` now takes `eto_in_week` (+ optional
  `drip_efficiency`) — the climate driver for the water math. Manual for now.
- **Per-loop design report.** For each loop the Yard tab shows every plant's
  weekly need, the **recommended emitter set** (e.g. "2×2 GPH"), delivered
  water, and an OK / UNDER / OVER badge — using the standard landscape formula
  `need = ETo × plant_factor × area × 0.623 / drip_efficiency`, then sizing
  emitters to the loop's shared runtime.
- **Honest warnings.** It flags what emitter tuning *can't* fix: plants too
  mismatched to share a loop ("split the loop"), a loop whose total flow
  exceeds the line capacity, and a zone that has plants but **no schedule
  watering it** — instead of emitting a confident-but-wrong config.
- New websocket reads `list_plants` and `yard_report` back the panel.

All new logic is pure + unit-tested (hydraulics core, plant store, design
bridge); the storage load path rejects non-finite/oversized values and skips a
single corrupt record rather than failing the whole load. Pre-publish security
gate run (privacy scrub + adversarial audit): zero blockers.

## v1.22.0 — 2026-06-16

### Fixed — long runs survive the Rachio per-zone cap

- **A run no longer dies at the controller's manual-run cap.** Rachio stops
  a zone at its configured "manual run minutes" (e.g. 60), so a long
  Complete Irrigation run (Trees 270, Citrus 240, Shrubs 135) used to abort
  the moment Rachio cut it. The external-off guard now distinguishes a
  **healthy cap-stop** (the zone ran at least `EXTERNAL_OFF_HEALTHY_RUN_SECONDS`
  = 120 s before stopping) from a quick flap/fault: on a healthy stop it
  **re-asserts the switch on AND refills the re-assert budget**, so the run
  rides cap after cap all the way to its full CI duration. A zone that
  *can't* stay on past the threshold still spends the bounded budget and
  aborts quickly — no infinite hammering of a genuinely dead valve, and a
  run that reaches its deadline always stops (no overrun).
- **Why this matters with your Rachio setting:** Rachio rejects a
  manual-run value above its own max (~180), so you can't just raise it to
  cover a 270-minute zone — setting it to 400 makes the *start command fail*
  outright. With this change you can leave it at a safe value (60) and CI
  re-asserts through the cap to deliver the full run. (You can also raise it
  to Rachio's ceiling, e.g. 120/180, to reduce the number of re-asserts.)
  Best paired with Rachio **Cycle & Soak off** for those zones, so CI owns
  the timing instead of fighting Rachio's pauses.

## v1.21.0 — 2026-06-16

### Fixed — no more bogus "switch turned off externally" aborts

- **A zone reporting "off" mid-run is now debounced, not instantly aborted.**
  Root cause (confirmed from the recorder): cloud integrations like Rachio
  flap a zone "off" for 2–3 seconds right after we turn it on — a stale
  cloud poll racing our optimistic state update. The zone is *physically
  running*; the "off" is a lie. The old monitor aborted on that first off
  event, so runs died at 0–3 minutes (weeks of Shrubs/Flowers/Back-Grass
  aborts). Now a mid-run off waits `EXTERNAL_OFF_DEBOUNCE_SECONDS` (90 s),
  re-reads the live state, and:
  - **back on** → the flap self-corrected, the run continues untouched;
  - **still off, attempts left, before deadline** → **re-assert** the switch
    on (up to `EXTERNAL_OFF_MAX_REASSERTS` = 2) to recover a genuine drop;
  - **still off, out of attempts / past deadline** → abort with an
    actionable "Run remainder / Open Logbook" alert.
- Decision logic extracted to a pure, unit-tested `run_guard` module. The
  run's auto-stop deadline still governs total runtime, so re-asserts can't
  extend a run. **Note:** to stop a CI-managed run, use CI's Stop button —
  a manual off elsewhere may be re-asserted while the deadline stands.

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
