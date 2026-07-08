/**
 * Complete Irrigation Panel — v1.1.0.
 *
 * Vanilla Web Component. Variant 2 layout (sidebar + main) per ADR-0002,
 * collapsible. Sections:
 *   • Today      — weather/calendar (placeholder) + zone tiles with Run-Now
 *   • Schedules  — list + add/edit/delete schedules (NEW in v0.4.0)
 *   • Others     — placeholders, land in later slices
 *
 * Properties HA sets on us:
 *   this.hass   — full HA state object (states, callService, callWS, etc.)
 *   this.panel  — { config: { zones, controller_domain, _panel_custom } }
 */

(function () {
  "use strict";

  const SIDEBAR_STORAGE_KEY = "complete_irrigation_sidebar_collapsed";
  const HIDDEN_ZONES_STORAGE_KEY = "complete_irrigation_hidden_zones";
  const THEME_STORAGE_KEY = "complete_irrigation_theme"; // "light" | "dark" | "auto"
  const HA_THEME_STORAGE_KEY = "complete_irrigation_ha_theme"; // "" or installed-theme-name
  const BANNER_LAYOUT_STORAGE_KEY = "complete_irrigation_banner_layout";
  const MANUAL_DEFAULT_STORAGE_KEY = "complete_irrigation_manual_default_min";

  // Plant-category info — mirrors PLANT_CATEGORIES in const.py. Lets the
  // Sensor modal show a small contextual hint under the category select
  // (PRD #25 — explanatory text for each category).
  const CATEGORY_INFO = {
    lawn: "Lawn: typical optimal soil moisture 21-40% at 3-4\" depth. Defaults: min 21 / target 31 / max 40.",
    bushes: "Bushes: optimal 21-60% at 3-4\" depth. Defaults: min 21 / target 41 / max 60.",
    vegetable_garden: "Vegetable garden: optimal 41-80% at 3-4\" depth. Defaults: min 41 / target 61 / max 80.",
    citrus: "Citrus: optimal 21-40% at 3-4\" depth. Defaults: min 21 / target 31 / max 40.",
    trees: "Trees: deep watering. Lower min%, occasional deep cycles work better than frequent shallow ones.",
    custom: "Custom: pick your own min/target/max thresholds based on your soil + plant type.",
  };
  // v1.35 — light presets for the plant form. Value is "low:high" in lux;
  // choosing one fills the two editable Lux low/high number inputs.
  const LIGHT_PRESETS = [
    ["", "(none)"],
    ["32000:100000", "Full sun 32000–100000 lux"],
    ["10000:32000", "Partial sun 10000–32000 lux"],
    ["3000:10000", "Bright shade 3000–10000 lux"],
    ["500:3000", "Deep shade 500–3000 lux"],
  ];
  // v1.35 — survey verdict → human label. Badge color comes from the
  // matching .light-<verdict> CSS class.
  const LIGHT_VERDICT_META = {
    too_low: "Too low",
    optimal: "Optimal",
    too_high: "Too high",
    no_range: "No range",
  };
  const ELEMENT_NAME = "complete-irrigation-panel";
  // v1.19.0 — scroll containers whose positions must survive an
  // innerHTML rebuild. `main` is the page scroller; the rest scroll
  // internally. Used by _captureScrollPositions/_restoreScrollPositions.
  const SCROLL_SELECTORS = [
    "main",
    ".day-cal-grid",
    ".modal",
    ".sensor-pick-list",
    ".history-table-wrap",
    ".zone-row-strip",
  ];
  // v1.16: one constant fed to every version-pill render + the console
  // banner. Pre-v1.16 the version was hard-coded in 10+ places and got
  // out of sync with manifest.json on most releases.
  const PANEL_VERSION = "v1.35.0";
  const DEFAULT_MANUAL_MINUTES = 10;
  const MAX_MANUAL_MINUTES = 480; // 8 h — matches the backend schedule cap; long
  // runs are delivered in controller-cap blocks (v1.25). Was 60, which blocked
  // manually running the deep-watering zones (Trees/Citrus/Shrubs) to full time.
  const MAX_SCHEDULE_MINUTES = 480; // 8 hours

  // Shared time-of-day formatter (v1.16 — extracted from two inline
  // copies inside _renderTodaysTimeline/_renderDayColumn). Input is
  // minutes-since-midnight; output is a US 12-hour clock string.
  function fmtTimeOfDay(m) {
    const h = Math.floor(m / 60);
    const mm = String(m % 60).padStart(2, "0");
    const ampm = h >= 12 ? "PM" : "AM";
    const h12 = h % 12 || 12;
    return `${h12}:${mm} ${ampm}`;
  }

  if (customElements.get(ELEMENT_NAME)) return;

  const SECTIONS = [
    { id: "today", label: "Today", icon: "📅" },
    { id: "schedules", label: "Schedules", icon: "⏰" },
    { id: "zones", label: "Zones", icon: "🌱" },
    { id: "yard", label: "Yard", icon: "🪴" },
    { id: "history", label: "History", icon: "📜" },
    { id: "sensors", label: "Sensors", icon: "📊" },
    { id: "weather", label: "Weather", icon: "🌧️" },
    { id: "notifications", label: "Notifications", icon: "🔔" },
    { id: "settings", label: "Settings", icon: "⚙️" },
  ];

  const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  // v1.18 — schedule color palette. Mirrors SCHEDULE_COLOR_PALETTE in
  // const.py. The editor renders one swatch per entry plus a "none"
  // option; the row stripe + calendar pill tint read schedule.color.
  const SCHEDULE_COLORS = [
    "#e53935", "#fb8c00", "#fdd835", "#43a047",
    "#00acc1", "#1e88e5", "#3949ab", "#8e24aa",
    "#d81b60", "#6d4c41", "#546e7a", "#00897b",
  ];

  // HA's `weather.*` entity state → user-facing emoji + label. Covers every
  // condition string in the HA weather component docs. Unknown strings
  // fall through to a generic cloud + the raw value.
  const WEATHER_CONDITION_MAP = {
    "clear-night": { icon: "🌙", label: "Clear night" },
    cloudy: { icon: "☁️", label: "Cloudy" },
    exceptional: { icon: "❗", label: "Exceptional" },
    fog: { icon: "🌫️", label: "Fog" },
    hail: { icon: "🧊", label: "Hail" },
    lightning: { icon: "⚡", label: "Lightning" },
    "lightning-rainy": { icon: "⛈️", label: "Storm" },
    partlycloudy: { icon: "⛅", label: "Partly cloudy" },
    pouring: { icon: "🌧️", label: "Pouring" },
    rainy: { icon: "🌧️", label: "Rainy" },
    snowy: { icon: "❄️", label: "Snow" },
    "snowy-rainy": { icon: "🌨️", label: "Sleet" },
    sunny: { icon: "☀️", label: "Sunny" },
    windy: { icon: "💨", label: "Windy" },
    "windy-variant": { icon: "🌬️", label: "Windy" },
  };

  function _todayIso() {
    const d = new Date();
    return (
      d.getFullYear() +
      "-" +
      String(d.getMonth() + 1).padStart(2, "0") +
      "-" +
      String(d.getDate()).padStart(2, "0")
    );
  }

  // v2 — Yard tab plant add/edit draft (null id = creating new).
  function emptyPlantEditor() {
    return {
      id: null,
      name: "",
      wucols_category: "moderate",
      canopy_area_sqft: "",
      zone_entity_id: "",
      photos: [], // v1.32 — gallery of {ts, path, note} for the open editor
      species: "", // v1.35 — optional botanical/common species name
      lux_low: "", // v1.35 — light range draft (strings; empty = unset)
      lux_high: "",
      light_survey_sensor: "", // v1.35 — survey controls draft (edit form only)
      light_survey_minutes: "10",
    };
  }

  // v1.32 — a human label for a stored photo ({ts: epoch-seconds, note}).
  function photoLabel(p) {
    const d = p && p.ts ? new Date(p.ts * 1000) : null;
    const when = d ? d.toLocaleDateString() : "";
    const note = (p && p.note) || "";
    return note ? `${when} — ${note}` : when;
  }

  // v1.32 — minimal EXIF GPS reader: scan a JPEG ArrayBuffer for the APP1/TIFF
  // GPS IFD and return decimal {lat, lon}, or null if absent/unparseable. No
  // dependency — we only need lat/lon for first-time auto-placement (phone GPS
  // is ~3-5 m and the user can drag to correct), so a tolerant best-effort read
  // is enough. Returns null on anything unexpected rather than throwing.
  function exifGps(buf) {
    try {
      const view = new DataView(buf);
      const len = view.byteLength;
      if (len < 4 || view.getUint16(0) !== 0xffd8) return null; // not a JPEG
      let off = 2;
      while (off + 4 <= len) {
        const marker = view.getUint16(off);
        if (marker === 0xffda) break; // start of scan — no metadata past here
        if ((marker & 0xff00) !== 0xff00) return null; // misaligned
        if (marker !== 0xffe1) {
          off += 2 + view.getUint16(off + 2); // skip non-APP1 segment
          continue;
        }
        const segLen = view.getUint16(off + 2);
        const tiff = off + 4;
        if (tiff + 8 > len || view.getUint32(tiff) !== 0x45786966) {
          off += 2 + segLen; // APP1 but not "Exif" — keep scanning
          continue;
        }
        const t0 = tiff + 6; // TIFF header start
        const le = view.getUint16(t0) === 0x4949; // II = little-endian
        const u16 = (o) => view.getUint16(o, le);
        const u32 = (o) => view.getUint32(o, le);
        if (u16(t0 + 2) !== 0x002a) return null;
        const ifd0 = t0 + u32(t0 + 4);
        let gpsIfd = 0;
        const n0 = u16(ifd0);
        for (let i = 0; i < n0; i++) {
          const ent = ifd0 + 2 + i * 12;
          if (u16(ent) === 0x8825) {
            gpsIfd = t0 + u32(ent + 8);
            break;
          }
        }
        if (!gpsIfd || gpsIfd + 2 > len) return null;
        const rational = (o) => u32(o) / (u32(o + 4) || 1);
        const dms = (o) => rational(o) + rational(o + 8) / 60 + rational(o + 16) / 3600;
        let latRef, lonRef, lat, lon;
        const n = u16(gpsIfd);
        for (let i = 0; i < n; i++) {
          const ent = gpsIfd + 2 + i * 12;
          const tag = u16(ent);
          const valOff = t0 + u32(ent + 8);
          if (tag === 1) latRef = String.fromCharCode(view.getUint8(ent + 8));
          else if (tag === 2) lat = dms(valOff);
          else if (tag === 3) lonRef = String.fromCharCode(view.getUint8(ent + 8));
          else if (tag === 4) lon = dms(valOff);
        }
        if (lat == null || lon == null) return null;
        if (latRef === "S") lat = -lat;
        if (lonRef === "W") lon = -lon;
        if (!isFinite(lat) || !isFinite(lon)) return null;
        return { lat, lon };
      }
    } catch (_) {
      /* malformed EXIF — treat as no GPS */
    }
    return null;
  }

  // v1.32 — load an image File, downscale so the longer edge <= maxPx, and return
  // base64 JPEG (no data: prefix). Keeps uploads small and strips EXIF from the
  // stored bytes (we already pulled GPS separately). The backend re-validates the
  // JPEG magic bytes, so canvas output (real image/jpeg) passes.
  function downscaleToJpegB64(file, maxPx, quality) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        URL.revokeObjectURL(url);
        let w = img.naturalWidth || img.width;
        let h = img.naturalHeight || img.height;
        if (!w || !h) return reject(new Error("empty image"));
        if (w > maxPx || h > maxPx) {
          if (w >= h) {
            h = Math.round((h * maxPx) / w);
            w = maxPx;
          } else {
            w = Math.round((w * maxPx) / h);
            h = maxPx;
          }
        }
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        canvas.getContext("2d").drawImage(img, 0, 0, w, h);
        const dataUrl = canvas.toDataURL("image/jpeg", quality || 0.82);
        const comma = dataUrl.indexOf(",");
        resolve(comma >= 0 ? dataUrl.slice(comma + 1) : "");
      };
      img.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("could not decode the image"));
      };
      img.src = url;
    });
  }

  function emptyEditor() {
    return {
      id: null, // null = creating new
      name: "",
      zone_entity_id: "",
      start_time: "06:00",
      duration_minutes: 15,
      weekdays: [0, 1, 2, 3, 4],
      enabled: true,
      mode: "weekdays",
      interval_days: 5,
      interval_hours: 6,
      interval_anchor: _todayIso(),
      // Optional daily-window cap for interval_hours mode (v1.15.0).
      // Empty string = no cap (legacy continuous-across-days behavior).
      interval_end_time: "",
      // Active period (v1.12). Empty strings mean "no bound".
      start_date: "",
      end_date: "",
      repeat_annually: false,
      // v1.19.0 — per-schedule weather-gate opt-outs
      ignore_wind: false,
      ignore_hot_weather: false,
      ignore_rain_lockout: false,
      // v1.18 — optional color for visual identification. "" = none.
      color: "",
      // Multi-zone: additional zones after the primary. Each is
      // {zone_entity_id, duration_minutes}. Empty = single-zone schedule.
      // The primary (top-level zone_entity_id + duration_minutes) is
      // always step 0; this array is steps 1..N.
      extra_steps: [],
    };
  }

  class CompleteIrrigationPanel extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._hass = null;
      this._panel = null;
      // On mobile (≤700px) the expanded sidebar is a fixed-position overlay
      // that covers the main content, so default to collapsed there unless
      // the user has explicitly set a preference. Desktop default stays
      // expanded.
      const isMobile =
        typeof window !== "undefined" &&
        window.matchMedia &&
        window.matchMedia("(max-width: 700px)").matches;
      this._collapsed = isMobile;
      this._theme = "auto"; // "light" | "dark" | "auto"
      this._haTheme = ""; // "" = use built-in light/dark; else HA installed theme name
      this._haThemes = {}; // populated from frontend/get_themes WS call
      this._bannerLayout = null; // {visible: {key: bool}, order: [key, ...]}
      try {
        const stored = localStorage.getItem(SIDEBAR_STORAGE_KEY);
        if (stored !== null) this._collapsed = stored === "true";
        this._theme = localStorage.getItem(THEME_STORAGE_KEY) || "auto";
        this._haTheme = localStorage.getItem(HA_THEME_STORAGE_KEY) || "";
        const layout = localStorage.getItem(BANNER_LAYOUT_STORAGE_KEY);
        if (layout) this._bannerLayout = JSON.parse(layout);
      } catch (_) {}
      this._currentSection = "today";

      // Run history (v1.14) — lazy-loaded when the History tab opens.
      this._runHistory = [];
      this._runHistoryLoaded = false;
      // Planned runs (v1.16) — fetched from the server-side run_planner
      // + conflict_resolver instead of being reimplemented in JS.
      // `_plannedRunsByDate` is a Map<YYYY-MM-DD, runs[]> keyed by the
      // run's local-date, built from the WS response. Loaded on entry
      // to Today/Zones and invalidated on any schedule mutation.
      this._plannedRunsByDate = new Map();
      this._plannedRunsLoaded = false;
      // Notification targets editor draft (v1.15). Array of strings,
      // each typically "notify.mobile_app_pete". Empty slots are
      // unfinished rows the user just added. Hydrated from config when
      // the Notifications tab opens.
      this._notifyDraft = null;
      // Filter state for the History tab — kept on the instance so it
      // survives re-renders within the same session (not persisted).
      this._historyFilters = { zone: "", schedule: "", status: "", days: 7 };
      this._historyExpanded = new Set();  // record ids whose trigger blob is open

      // Manual-run modal state
      this._runModalOpen = false;
      this._runModalEntityId = null;
      this._runModalZoneName = "";

      // Schedule modal state + cached list
      this._scheduleModalOpen = false;
      this._scheduleEditor = emptyEditor();
      this._schedules = [];
      this._schedulesLoaded = false;

      // v2 — Yard tab (plant-aware irrigation) state.
      this._plants = [];
      this._yardReports = [];
      this._yardEto = null;
      this._yardEff = null;
      this._yardEtoStatus = null; // v1.28 — {eto_source, eto_auto, eto_manual, eto_auto_value, eto_auto_at, weather_entity}
      this._pendingAutoEto = null; // v1.28 — optimistic auto-ET toggle state while a toggle call is in flight
      this._yardMap = null; // v1.30 — {image_path, bbox, width, height, center_lat/lon, span_m, version}
      this._mapDrag = null; // v1.30 — in-flight marker drag {plantId, el, rect}
      this._mapBusy = false; // v1.30 — set_yard_map fetch in flight
      this._yardLoaded = false;
      this._plantEditor = null; // null = form hidden; object = add/edit draft
      // v1.35 — light surveys + care tasks + watering diagnosis state.
      this._activeLightSurveys = {}; // plant_id -> {sensor, started, until, minutes, samples}
      this._lightSurveyPoll = null; // 30s list_plants refetch while a survey runs (Yard open)
      this._careTasks = []; // list_care_tasks rows (fetched with the yard)
      this._careDraft = {
        // add-form draft so background re-renders don't wipe typing
        care_kind: "fertilize",
        care_label: "",
        care_interval: "90",
        care_subject: "",
      };
      this._zoneDiagnosis = {}; // zone entity_id -> diagnosis result (expanded row)

      // Sensor (moisture) modal state
      this._sensorModalOpen = false;
      this._sensorEditor = null;

      // Weather banner customization modal state
      this._bannerModalOpen = false;

      // Day calendar (Today screen): offset in days from today.
      // 0 = today, +1 = tomorrow, -1 = yesterday, etc. Prev/Next buttons
      // shift this; "Today" button resets to 0.
      this._calendarDayOffset = 0;

      // Forecast cache: HA 2024+ deprecated weather.* attributes.forecast
      // in favor of the weather.get_forecasts service. We call it on demand
      // (when the Weather tab opens) and cache by entity_id.
      this._forecastCache = {};

      // Establishment mode ("New grass") modal state
      this._establishmentModalOpen = false;
      this._establishmentEditor = null;

      // Weather + config cached from WS API
      this._config = {};
      this._configLoaded = false;

      // Local manual-run countdowns: entity_id -> deadline epoch ms
      this._localRuns = {};
      // v1.30 — active run SESSIONS: entity_id -> whole-run deadline epoch ms.
      // Spans a chunked run's inter-block gaps; a gated (never-fired) run has no
      // session, so this is the truthful "is the zone running" signal for the card.
      this._activeSessions = {};
      // The total run length for each active run, in minutes. Lets the
      // tile show "4:52 left of 10 min" instead of just "4:52 left".
      this._localRunDurations = {};
      this._countdownTimer = null;
      // v1.19.0 — minute-tick so the day calendar's "now" line drifts
      // down automatically without waiting for an HA state change to
      // trigger a re-render. Only active while the Today tab is open
      // (set + cleared in connectedCallback / _navigateTo).
      this._nowLineTimer = null;

      // Hidden zones (per-browser, persisted). Toggled from the Zones
      // tab only; Today simply filters them out of view.
      this._hiddenZones = new Set();
      try {
        const stored = localStorage.getItem(HIDDEN_ZONES_STORAGE_KEY);
        if (stored) this._hiddenZones = new Set(JSON.parse(stored));
      } catch (_) {}

      this._renderScheduled = false;
      this._onClick = this._onClick.bind(this);
      this._onSubmit = this._onSubmit.bind(this);
      this._onChange = this._onChange.bind(this);
      this._onInput = this._onInput.bind(this);
      this._onMapPointerDown = this._onMapPointerDown.bind(this);
      this._onMapPointerMove = this._onMapPointerMove.bind(this);
      this._onMapPointerUp = this._onMapPointerUp.bind(this);

      // v1.19.0 — scroll-position preservation across renders. _render()
      // rebuilds the whole shadow DOM via innerHTML, which resets every
      // scroll container to the top. Background renders (hass updates,
      // the 60s now-line tick) were yanking mobile users back to the
      // top of the Today screen mid-read. We (a) save/restore scroll
      // positions around each render and (b) defer BACKGROUND renders
      // while the user is actively scrolling so momentum isn't killed.
      this._lastScrollAt = 0;
      this._restoringScroll = false;
      this._deferredRenderTimer = null;
      this._renderedSection = null; // section currently in the DOM
      this._onAnyScroll = this._onAnyScroll.bind(this);
    }

    // ── HA-set properties ──────────────────────────────────────────
    set hass(value) {
      const isFirstHass = !this._hass;
      const prevSig = this._stateSignature();
      this._hass = value;
      if (isFirstHass) {
        this._fetchSchedules();
        this._fetchConfig();
        this._fetchActiveRuns();
        this._fetchHaThemes();
        this._scheduleRender();
        return;
      }
      // Only re-render if something we display actually changed. Avoids
      // re-rendering 39KB of HTML on every unrelated HA state change.
      if (this._stateSignature() !== prevSig) {
        this._scheduleRender();
      }
    }

    /** A compact signature of just the entities the UI cares about,
     * so we can short-circuit re-renders on unrelated hass updates. */
    _stateSignature() {
      if (!this._hass?.states) return "";
      const zones = (this._panel?.config?.zones) || [];
      const parts = [];
      const watched = new Set(zones);
      watched.add("sun.sun");
      // v1.16 — include every per-zone sensor the user has bound so a
      // moisture/temperature/humidity reading change re-renders the
      // Zones chips. Pre-v1.16, only auto-detected weather sensors
      // were tracked, so manually-bound moisture sensors (e.g.
      // sensor.acurite_*) wouldn't trigger a render.
      const zonesCfg = (this._config?.zones) || {};
      for (const zc of Object.values(zonesCfg)) {
        for (const arr of [
          zc.moisture_entities,
          zc.temperature_entities,
          zc.humidity_entities,
        ]) {
          if (Array.isArray(arr)) {
            for (const eid of arr) watched.add(eid);
          }
        }
      }
      // Global weather sensors
      for (const k of ["rain_sensor", "temperature_sensor", "wind_sensor"]) {
        const v = this._config?.[k];
        if (typeof v === "string" && v) watched.add(v);
      }
      const arr = this._config?.rain_sensors;
      if (Array.isArray(arr)) for (const v of arr) watched.add(v);
      // Auto-detected Tempest / WeatherFlow sensors (for the banner)
      for (const eid of Object.keys(this._hass.states)) {
        if (/^sensor\.(tempest|weatherflow)_/.test(eid)) watched.add(eid);
      }
      for (const eid of watched) {
        const s = this._hass.states[eid];
        parts.push(s ? `${eid}=${s.state}` : `${eid}=_`);
      }
      return parts.join("|");
    }
    get hass() {
      return this._hass;
    }

    set panel(value) {
      this._panel = value;
      this._scheduleRender();
    }
    get panel() {
      return this._panel;
    }

    connectedCallback() {
      this.shadowRoot.addEventListener("click", this._onClick);
      this.shadowRoot.addEventListener("submit", this._onSubmit);
      this.shadowRoot.addEventListener("change", this._onChange);
      this.shadowRoot.addEventListener("input", this._onInput);
      // v1.30 — yard-map marker drag (pointer events; move/up on the shadow
      // root so a fast drag that leaves the marker still tracks + releases).
      this.shadowRoot.addEventListener("pointerdown", this._onMapPointerDown);
      this.shadowRoot.addEventListener("pointermove", this._onMapPointerMove);
      this.shadowRoot.addEventListener("pointerup", this._onMapPointerUp);
      this.shadowRoot.addEventListener("pointercancel", this._onMapPointerUp);
      // v1.19.0 — scroll events don't bubble, but they DO run the
      // capture phase, so a capture listener on the shadow root sees
      // scrolls from every descendant (main, day-cal-grid, modals…).
      this.shadowRoot.addEventListener("scroll", this._onAnyScroll, true);
      this._scheduleRender();
      // v1.19.0 — Today is the initial section, so kick off the
      // now-line tick now (won't double-up because _startNowLineTimer
      // is idempotent).
      if (this._currentSection === "today") this._startNowLineTimer();
    }

    disconnectedCallback() {
      this.shadowRoot.removeEventListener("click", this._onClick);
      this.shadowRoot.removeEventListener("submit", this._onSubmit);
      this.shadowRoot.removeEventListener("change", this._onChange);
      this.shadowRoot.removeEventListener("input", this._onInput);
      this.shadowRoot.removeEventListener("pointerdown", this._onMapPointerDown);
      this.shadowRoot.removeEventListener("pointermove", this._onMapPointerMove);
      this.shadowRoot.removeEventListener("pointerup", this._onMapPointerUp);
      this.shadowRoot.removeEventListener("pointercancel", this._onMapPointerUp);
      this.shadowRoot.removeEventListener("scroll", this._onAnyScroll, true);
      this._stopNowLineTimer();
      if (this._deferredRenderTimer) {
        clearTimeout(this._deferredRenderTimer);
        this._deferredRenderTimer = null;
      }
      // Clear the 1s countdown interval too — otherwise it keeps firing against a
      // detached shadow DOM and accumulates one leaked interval per panel reopen.
      if (this._countdownTimer) {
        clearInterval(this._countdownTimer);
        this._countdownTimer = null;
      }
      // v1.35 — and the 30s light-survey poll, for the same reason.
      this._stopLightSurveyPoll();
    }

    _onAnyScroll() {
      // Programmatic restores (in _restoreScrollPositions) also fire
      // scroll events; ignore those so they don't keep deferring
      // background renders forever.
      if (!this._restoringScroll) this._lastScrollAt = Date.now();
    }

    _startNowLineTimer() {
      // v1.19.0 — re-render every minute so the day-cal-now line drifts
      // down. Idempotent: no-op if already running.
      if (this._nowLineTimer) return;
      this._nowLineTimer = setInterval(() => {
        if (this._currentSection === "today") this._scheduleRender();
      }, 60000);
    }

    _stopNowLineTimer() {
      if (this._nowLineTimer) {
        clearInterval(this._nowLineTimer);
        this._nowLineTimer = null;
      }
    }

    _syncLightSurveyPoll() {
      // v1.35 — while an illuminance survey is running AND the Yard tab is
      // open, refetch list_plants every 30s so "Surveying… N readings"
      // ticks up and the controls flip back when the backend finishes.
      // Idempotent: never stacks a second interval; self-clears when no
      // surveys remain or the user leaves the Yard tab.
      const wanted =
        this._currentSection === "yard" &&
        Object.keys(this._activeLightSurveys || {}).length > 0;
      if (!wanted) return this._stopLightSurveyPoll();
      if (this._lightSurveyPoll) return;
      this._lightSurveyPoll = setInterval(() => this._pollLightSurveys(), 30000);
    }

    _stopLightSurveyPoll() {
      if (this._lightSurveyPoll) {
        clearInterval(this._lightSurveyPoll);
        this._lightSurveyPoll = null;
      }
    }

    async _pollLightSurveys() {
      // Light refetch: list_plants only — it carries both the survey
      // history and active_light_surveys; the yard report + care tasks
      // didn't change, so skip them.
      if (!this._hass?.callWS) return;
      try {
        const res = await this._hass.callWS({
          type: "complete_irrigation/list_plants",
        });
        this._plants = (res && res.plants) || [];
        this._activeLightSurveys = (res && res.active_light_surveys) || {};
        this._syncLightSurveyPoll(); // self-clear once every survey completes
        this._scheduleRender();
      } catch (err) {
        console.error("[complete-irrigation] light-survey poll failed:", err);
      }
    }

    _onClick(e) {
      const path = e.composedPath ? e.composedPath() : [];

      // v1.19.0 — info-bubble popover toggle.
      // Touch devices have no hover, so tapping the ⓘ bubble has to
      // toggle the popup explicitly. We also close any open popup when
      // the click lands anywhere else (the path-doesn't-contain-help-tip
      // case below). Done at the very top of _onClick before action
      // dispatch so the toggle wins over any incidental data-action
      // ancestor.
      const tipEl = path.find(
        (n) => n instanceof HTMLElement && n.classList.contains("help-tip")
      );
      if (tipEl) {
        const wasOpen = tipEl.classList.contains("help-tip-open");
        // Close any other open tips so only one shows at a time
        this.shadowRoot
          .querySelectorAll(".help-tip-open")
          .forEach((el) => el.classList.remove("help-tip-open"));
        if (!wasOpen) tipEl.classList.add("help-tip-open");
        e.stopPropagation();
        return;
      }
      // Click landed outside any help-tip → close all open popups
      const opens = this.shadowRoot.querySelectorAll(".help-tip-open");
      if (opens.length > 0) opens.forEach((el) => el.classList.remove("help-tip-open"));

      for (const node of path) {
        if (!node || node === this.shadowRoot || node === this) break;
        if (!(node instanceof HTMLElement)) continue;

        // Class-based actions
        if (node.classList.contains("collapse-btn")) return this._toggleSidebar();
        if (node.classList.contains("modal-cancel")) return this._closeAllModals();
        if (node.classList.contains("modal-backdrop")) return this._closeAllModals();

        // Sidebar navigation (data-section) — checked BEFORE data-action so
        // a button without data-action but with data-section still navigates.
        if (node.dataset.section) {
          e.stopPropagation();
          return this._navigateTo(node.dataset.section);
        }

        // Action buttons (data-action)
        const action = node.dataset.action;
        if (!action) continue;
        e.stopPropagation();

        if (action === "run-now")
          return this._openRunModal(node.dataset.entityId, node.dataset.zoneName);
        if (action === "stop") return this._stopZone(node.dataset.entityId);
        if (action === "add-schedule") return this._openNewSchedule();
        if (action === "edit-schedule")
          return this._openEditSchedule(node.dataset.scheduleId);
        if (action === "copy-schedule")
          return this._openCopyOfSchedule(node.dataset.scheduleId);
        if (action === "delete-schedule")
          return this._deleteSchedule(node.dataset.scheduleId);
        if (action === "run-schedule")
          return this._runSchedule(node.dataset.scheduleId, node.dataset.scheduleName);
        if (action === "pick-schedule-color") {
          // v1.18 — set the editor's color + re-render so the selected
          // swatch updates. dataset.color === "" means clear.
          this._scheduleEditor.color = node.dataset.color || "";
          return this._renderNow();
        }
        if (action === "toggle-schedule")
          return this._toggleSchedule(
            node.dataset.scheduleId,
            node.dataset.enabled === "true"
          );
        if (action === "hide-zone") return this._toggleZoneHidden(node.dataset.entityId);
        if (action === "show-zone") return this._toggleZoneHidden(node.dataset.entityId);
        if (action === "configure-sensor")
          return this._openConfigureSensor(node.dataset.entityId);
        if (action === "clear-rain-lockout") return this._clearRainLockout();
        // v2 — Yard tab plant CRUD + ETo.
        if (action === "add-plant") {
          this._plantEditor = emptyPlantEditor();
          return this._renderNow();
        }
        if (action === "edit-plant") {
          const p = this._plants.find((x) => x.id === node.dataset.plantId);
          if (p) {
            this._plantEditor = {
              id: p.id,
              name: p.name,
              wucols_category: p.wucols_category,
              canopy_area_sqft: p.canopy_area_sqft,
              zone_entity_id: p.zone_entity_id,
              photos: Array.isArray(p.photos) ? p.photos : [],
              health: p.health || null,
              // v1.35 — species + light range (draft strings; empty = unset)
              species: p.species || "",
              lux_low: p.lux_low != null ? String(p.lux_low) : "",
              lux_high: p.lux_high != null ? String(p.lux_high) : "",
              _hadLightRange: p.lux_low != null && p.lux_high != null,
              // Survey controls: default the sensor from the most recent survey.
              light_survey_sensor:
                (Array.isArray(p.light_surveys) && p.light_surveys[0]?.sensor) || "",
              light_survey_minutes: "10",
            };
          }
          return this._renderNow();
        }
        if (action === "cancel-plant") {
          this._plantEditor = null;
          return this._renderNow();
        }
        if (action === "delete-plant")
          return this._deletePlant(node.dataset.plantId, node.dataset.plantName);
        if (action === "apply-eto") return this._applyEto();
        if (action === "setup-yard-map") return this._setupYardMap();
        if (action === "place-plant") return this._placePlant(node.dataset.plantId);
        if (action === "toggle-theme") return this._cycleTheme();
        if (action === "open-banner-settings") {
          this._bannerModalOpen = true;
          return this._renderNow();
        }
        if (action === "banner-up") return this._bannerReorder(node.dataset.key, -1);
        if (action === "banner-down") return this._bannerReorder(node.dataset.key, 1);
        if (action === "test-notification") return this._testNotification();
        if (action === "copy-ical") return this._copyICalUrl();
        if (action === "weekly-snooze-30") return this._weeklySnooze(30);
        if (action === "weekly-unsnooze") return this._weeklySnooze(0);
        if (action === "open-establishment")
          return this._openEstablishmentModal(node.dataset.entityId, node.dataset.zoneName);
        // v1.35 — light surveys + care tasks + watering diagnosis.
        if (action === "start-light-survey")
          return this._startLightSurvey(node.dataset.plantId);
        if (action === "cancel-light-survey")
          return this._cancelLightSurvey(node.dataset.plantId);
        if (action === "care-task-done")
          return this._completeCareTask(node.dataset.taskId);
        if (action === "care-task-delete")
          return this._deleteCareTask(node.dataset.taskId, node.dataset.taskName);
        if (action === "care-task-add") return this._addCareTask();
        if (action === "zone-diagnose")
          return this._diagnoseZone(node.dataset.entityId);
        if (action === "zone-move-up") return this._reorderZone(node.dataset.entityId, -1);
        if (action === "zone-move-down") return this._reorderZone(node.dataset.entityId, 1);
        if (action === "day-cal-prev") {
          this._calendarDayOffset = (this._calendarDayOffset || 0) - 1;
          return this._renderNow();
        }
        if (action === "day-cal-next") {
          this._calendarDayOffset = (this._calendarDayOffset || 0) + 1;
          return this._renderNow();
        }
        if (action === "day-cal-today") {
          this._calendarDayOffset = 0;
          return this._renderNow();
        }
        if (action === "history-refresh") {
          this._fetchRunHistory();
          return;
        }
        if (action === "history-clear") {
          if (!confirm("Delete every run-history record? This cannot be undone.")) return;
          this._clearRunHistory();
          return;
        }
        if (action === "notify-target-add") {
          if (!Array.isArray(this._notifyDraft)) this._hydrateNotifyDraft();
          this._notifyDraft.push("");  // new empty row, user picks via dropdown
          return this._renderNow();
        }
        if (action === "notify-target-remove") {
          const idx = parseInt(node.dataset.idx, 10);
          if (!Array.isArray(this._notifyDraft)) return;
          if (Number.isFinite(idx) && idx >= 0 && idx < this._notifyDraft.length) {
            this._notifyDraft.splice(idx, 1);
            return this._renderNow();
          }
          return;
        }
        if (action === "clear-interval-end-time") {
          this._scheduleEditor.interval_end_time = "";
          return this._renderNow();
        }
        if (action === "go-to-history-skipped") {
          // v1.17 — banner on Today screen → History with today's skips
          this._historyFilters = { zone: "", schedule: "", status: "skipped", days: 1 };
          return this._navigateTo("history");
        }
        if (action === "history-toggle-triggers") {
          const id = node.dataset.recordId;
          if (!id) return;
          if (this._historyExpanded.has(id)) this._historyExpanded.delete(id);
          else this._historyExpanded.add(id);
          return this._renderNow();
        }
        if (action === "open-schedule-edit") {
          // Click on a Today-timeline pill or Tomorrow-list row →
          // jump to the Schedules tab and open the edit modal for
          // that schedule (so the user can tweak it inline).
          const sid = node.dataset.scheduleId;
          if (!sid) return;
          this._currentSection = "schedules";
          if (!this._schedulesLoaded) this._fetchSchedules();
          this._renderNow();
          // Wait a beat for the schedules-list render so _openEditSchedule
          // can find the schedule in the cached list, then open it.
          setTimeout(() => this._openEditSchedule(sid), 50);
          return;
        }
        if (action === "weekday-preset") {
          const preset = node.dataset.preset;
          const map = {
            all: [0, 1, 2, 3, 4, 5, 6],
            weekdays: [0, 1, 2, 3, 4],
            weekends: [5, 6],
          };
          if (map[preset]) {
            this._scheduleEditor.weekdays = map[preset];
            return this._renderNow();
          }
        }
        if (action === "add-extra-step") {
          // Append a default step (use the primary zone if non-hidden,
          // else the first non-hidden zone, 10 min).
          const e = this._scheduleEditor;
          const allZones = this._panel?.config?.zones || [];
          const primaryUsable =
            e.zone_entity_id && !this._hiddenZones.has(e.zone_entity_id)
              ? e.zone_entity_id
              : null;
          const firstVisible = allZones.find(
            (z) => !this._hiddenZones.has(z)
          );
          const defaultZone =
            primaryUsable || firstVisible || allZones[0] || "";
          e.extra_steps = [
            ...(e.extra_steps || []),
            { zone_entity_id: defaultZone, duration_minutes: 10 },
          ];
          return this._renderNow();
        }
        if (action === "remove-extra-step") {
          const e = this._scheduleEditor;
          const idx = parseInt(node.dataset.stepIdx, 10);
          if (!Number.isNaN(idx)) {
            e.extra_steps = (e.extra_steps || []).filter((_, i) => i !== idx);
            return this._renderNow();
          }
        }
      }
    }

    _onSubmit(e) {
      if (e.target?.classList.contains("run-form")) {
        e.preventDefault();
        const minutes = parseInt(
          e.target.querySelector('input[name="minutes"]').value || "0",
          10
        );
        if (!minutes || minutes < 1 || minutes > MAX_MANUAL_MINUTES) {
          alert("Duration must be between 1 and " + MAX_MANUAL_MINUTES);
          return;
        }
        this._runZone(this._runModalEntityId, minutes);
        this._closeAllModals();
        return;
      }
      // dataset.form check first — overrides class-based dispatch so
      // forms can share styling classes (e.g., .weather-form) without
      // colliding with the catch-all weather handler below.
      if (e.target?.dataset?.form === "notifications") {
        e.preventDefault();
        this._saveNotificationConfig(e.target);
        return;
      }
      if (e.target?.dataset?.form === "conflict-policy") {
        e.preventDefault();
        this._saveConflictPolicy(e.target);
        return;
      }
      if (e.target?.dataset?.form === "ha-theme") {
        e.preventDefault();
        this._saveHaTheme(e.target);
        return;
      }
      if (e.target?.dataset?.form === "manual-default") {
        e.preventDefault();
        this._saveManualDefault(e.target);
        return;
      }
      if (e.target?.dataset?.form === "zone-buffer") {
        e.preventDefault();
        this._saveZoneBuffer(e.target);
        return;
      }
      if (e.target?.dataset?.form === "admin-only-services") {
        e.preventDefault();
        this._saveAdminOnlyServices(e.target);
        return;
      }
      if (e.target?.classList.contains("plant-form")) {
        e.preventDefault();
        this._savePlant();
        return;
      }
      if (e.target?.classList.contains("schedule-form")) {
        e.preventDefault();
        this._saveSchedule();
        return;
      }
      if (e.target?.classList.contains("sensor-form")) {
        e.preventDefault();
        this._saveSensorConfig();
        return;
      }
      if (e.target?.classList.contains("banner-settings-form")) {
        e.preventDefault();
        this._saveBannerLayout(e.target);
        return;
      }
      if (e.target?.classList.contains("establishment-form")) {
        e.preventDefault();
        this._saveEstablishment(e.target);
        return;
      }
      if (e.target?.classList.contains("weather-form")) {
        e.preventDefault();
        this._saveWeatherConfig(e.target);
        return;
      }
    }

    _onChange(e) {
      const t = e.target;
      if (!t) return;
      // History filters — driven by data-action, not name.
      const action = t.dataset?.action;
      // v2 — Yard plant editor fields keep the draft in sync (selects fire
      // change; text/number also handled in _onInput so typing survives a
      // background re-render).
      if (action === "plant-field") {
        if (this._plantEditor && t.name in this._plantEditor) {
          this._plantEditor[t.name] = t.value;
        }
        return; // value already shown by the control; no re-render
      }
      if (action === "light-preset") {
        // v1.35 — preset fills the two editable lux inputs. "" = (none)
        // → leave whatever the user already has in the fields.
        if (this._plantEditor && t.value) {
          const [lo, hi] = t.value.split(":");
          this._plantEditor.lux_low = lo;
          this._plantEditor.lux_high = hi;
          return this._renderNow(); // re-render so the number inputs update
        }
        return;
      }
      if (action === "care-field") {
        // v1.35 — care-task add form draft (survives background re-renders).
        if (this._careDraft && t.name in this._careDraft) {
          this._careDraft[t.name] = t.value;
        }
        return; // value already shown by the control; no re-render
      }
      if (action === "photo-file") {
        const file = t.files && t.files[0];
        if (file) this._addPlantPhoto(file, t.dataset.plantId);
        t.value = ""; // allow re-selecting the same file
        return;
      }
      if (action === "notify-target-change") {
        const idx = parseInt(t.dataset.idx, 10);
        if (!Array.isArray(this._notifyDraft)) this._hydrateNotifyDraft();
        if (Number.isFinite(idx) && idx >= 0 && idx < this._notifyDraft.length) {
          this._notifyDraft[idx] = t.value;
        }
        return;  // no re-render — the dropdown already shows the chosen value
      }
      if (action === "toggle-auto-eto") {
        return this._toggleAutoEto(!!t.checked);
      }
      if (action === "history-filter-zone") {
        this._historyFilters.zone = t.value;
        return this._renderNow();
      }
      if (action === "history-filter-schedule") {
        this._historyFilters.schedule = t.value;
        return this._renderNow();
      }
      if (action === "history-filter-status") {
        this._historyFilters.status = t.value;
        return this._renderNow();
      }
      if (action === "history-filter-days") {
        this._historyFilters.days = parseInt(t.value, 10) || 0;
        return this._renderNow();
      }
      if (!t.name) return;
      // Sensor modal — track moisture checkbox toggles + the other fields
      if (this._sensorModalOpen && this._sensorEditor) {
        if (
          t.name === "moisture_entity" ||
          t.name === "temperature_entity" ||
          t.name === "humidity_entity"
        ) {
          const field =
            t.name === "moisture_entity"
              ? "moisture_entities"
              : t.name === "temperature_entity"
              ? "temperature_entities"
              : "humidity_entities";
          const set = new Set(this._sensorEditor[field]);
          if (t.checked) set.add(t.value);
          else set.delete(t.value);
          this._sensorEditor[field] = Array.from(set);
          // v1.19 — show/remove the row's "in avg" toggle without a
          // full re-render (which would wipe the search filter text).
          if (t.name === "moisture_entity") {
            this._syncMoistureUseToggle(t.closest(".sensor-pick"), t.value, t.checked);
          }
          return;
        }
        if (t.name === "moisture_exclude") {
          // v1.19 — "in avg" mini-checkbox. CHECKED = include in the
          // analysis (default); UNCHECKED = display-only, add to the
          // excluded list.
          const set = new Set(this._sensorEditor.moisture_excluded || []);
          if (t.checked) set.delete(t.value);
          else set.add(t.value);
          this._sensorEditor.moisture_excluded = Array.from(set);
          return;
        }
        if (t.name === "require_moisture_reading") {
          this._sensorEditor.require_moisture_reading = t.checked; // v1.18
          return;
        }
        if (t.name === "moisture_disabled") {
          this._sensorEditor.moisture_disabled = t.checked; // v1.19.0
          return;
        }
        if (t.name === "auto_soak_enabled") {
          this._sensorEditor.auto_soak_enabled = t.checked; // v1.19
          return;
        }
        if (
          t.name === "combine_mode" ||
          t.name === "category" ||
          t.name === "min_pct" ||
          t.name === "target_pct" ||
          t.name === "max_pct" ||
          t.name === "soak_run_minutes" ||
          t.name === "soak_wait_minutes" ||
          t.name === "soak_max_cycles"
        ) {
          this._sensorEditor[t.name] = t.value;
          // Live-update the category info hint without a full re-render
          // (so the user's cursor stays put in the other fields).
          if (t.name === "category") {
            const hint = this.shadowRoot?.querySelector("[data-category-info]");
            if (hint) {
              const txt = CATEGORY_INFO[t.value] || "Pick a category for a typical moisture range.";
              hint.textContent = txt;
            }
          }
          return;
        }
      }
      if (t.name === "weekday") {
        const day = parseInt(t.value, 10);
        const set = new Set(this._scheduleEditor.weekdays);
        if (t.checked) set.add(day);
        else set.delete(day);
        this._scheduleEditor.weekdays = Array.from(set).sort((a, b) => a - b);
      } else if (t.name === "enabled") {
        this._scheduleEditor.enabled = t.checked;
      } else if (t.name === "repeat_annually") {
        this._scheduleEditor.repeat_annually = t.checked;
      } else if (
        t.name === "ignore_wind" ||
        t.name === "ignore_hot_weather" ||
        t.name === "ignore_rain_lockout"
      ) {
        this._scheduleEditor[t.name] = t.checked;
      } else if (t.name === "mode") {
        // Mode toggle flips which fields show — re-render the modal.
        this._scheduleEditor.mode = t.value;
        this._renderNow();
      } else if (t.name === "duration_h" || t.name === "duration_m") {
        this._syncDurationFromForm();
      } else if (t.name === "start_time_h" || t.name === "start_time_m") {
        this._syncStartTimeFromForm();
      } else if (t.name === "interval_end_time_h" || t.name === "interval_end_time_m") {
        this._syncIntervalEndTimeFromForm();
      } else if (
        t.name === "extra_zone" ||
        t.name === "extra_dur_h" ||
        t.name === "extra_dur_m"
      ) {
        this._syncExtraStepFromForm(parseInt(t.dataset.stepIdx, 10));
      } else if (t.name in this._scheduleEditor) {
        this._scheduleEditor[t.name] = t.value;
      }
    }

    _onInput(e) {
      // Keep schedule editor state in sync as user types (so re-renders
      // triggered by other changes don't blow away unsaved edits).
      const t = e.target;
      if (!t) return;
      // v2 — Yard plant editor: keep draft current per keystroke so a
      // background re-render doesn't blow away unsaved typing.
      if (t.dataset?.action === "plant-field") {
        if (this._plantEditor && t.name in this._plantEditor) {
          this._plantEditor[t.name] = t.value;
        }
        return;
      }
      // v1.35 — care-task add form: same keep-typing-alive treatment.
      if (t.dataset?.action === "care-field") {
        if (this._careDraft && t.name in this._careDraft) {
          this._careDraft[t.name] = t.value;
        }
        return;
      }
      // v1.19.0 — live filter the sensor checkbox list as the user
      // types. Pure DOM operation; no re-render so checkbox state +
      // input focus + cursor position stay put while typing.
      if (t.dataset && t.dataset.action === "filter-sensor-list") {
        const query = (t.value || "").trim().toLowerCase();
        const picker = t.closest(".sensor-picker");
        if (!picker) return;
        const rows = picker.querySelectorAll(".sensor-pick");
        let visible = 0;
        for (const row of rows) {
          const match = !query || (row.dataset.searchText || "").includes(query);
          row.hidden = !match;
          if (match) visible++;
        }
        const noMatch = picker.querySelector(".sensor-pick-no-match");
        if (noMatch) noMatch.hidden = visible !== 0;
        return;
      }
      if (!t.name) return;
      if (t.name === "duration_h" || t.name === "duration_m") {
        this._syncDurationFromForm();
        return;
      }
      if (t.name === "start_time_h" || t.name === "start_time_m") {
        this._syncStartTimeFromForm();
        return;
      }
      if (t.name === "interval_end_time_h" || t.name === "interval_end_time_m") {
        this._syncIntervalEndTimeFromForm();
        return;
      }
      if (
        t.name === "extra_zone" ||
        t.name === "extra_dur_h" ||
        t.name === "extra_dur_m"
      ) {
        this._syncExtraStepFromForm(parseInt(t.dataset.stepIdx, 10));
        return;
      }
      if (
        t.name !== "weekday" &&
        t.name !== "enabled" &&
        t.name !== "mode" &&
        t.name in this._scheduleEditor
      ) {
        this._scheduleEditor[t.name] = t.value;
      }
    }

    _syncDurationFromForm() {
      // duration_minutes (canonical) = h*60 + m, read from both inputs.
      const form = this.shadowRoot?.querySelector(".schedule-form");
      if (!form) return;
      const h = parseInt(form.querySelector('[name="duration_h"]')?.value, 10) || 0;
      const m = parseInt(form.querySelector('[name="duration_m"]')?.value, 10) || 0;
      this._scheduleEditor.duration_minutes = h * 60 + m;
    }

    _syncStartTimeFromForm() {
      // start_time (canonical "HH:MM") composed from two number inputs.
      // We use number inputs instead of <input type="time"> because the
      // native time picker crashes WKWebView in the macOS HA app.
      const form = this.shadowRoot?.querySelector(".schedule-form");
      if (!form) return;
      const rawH = parseInt(form.querySelector('[name="start_time_h"]')?.value, 10);
      const rawM = parseInt(form.querySelector('[name="start_time_m"]')?.value, 10);
      const h = Number.isFinite(rawH) ? Math.max(0, Math.min(23, rawH)) : 0;
      const m = Number.isFinite(rawM) ? Math.max(0, Math.min(59, rawM)) : 0;
      this._scheduleEditor.start_time =
        String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0");
    }

    _syncIntervalEndTimeFromForm() {
      // Mirror of _syncStartTimeFromForm for the interval_hours daily-
      // window cap. Empty inputs → empty string (= no cap).
      const form = this.shadowRoot?.querySelector(".schedule-form");
      if (!form) return;
      const hEl = form.querySelector('[name="interval_end_time_h"]');
      const mEl = form.querySelector('[name="interval_end_time_m"]');
      if (!hEl || !mEl) return;
      const hStr = hEl.value.trim();
      const mStr = mEl.value.trim();
      if (hStr === "" && mStr === "") {
        this._scheduleEditor.interval_end_time = "";
        return;
      }
      const rawH = parseInt(hStr, 10);
      const rawM = parseInt(mStr, 10);
      const h = Number.isFinite(rawH) ? Math.max(0, Math.min(23, rawH)) : 0;
      const m = Number.isFinite(rawM) ? Math.max(0, Math.min(59, rawM)) : 0;
      this._scheduleEditor.interval_end_time =
        String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0");
    }

    _syncExtraStepFromForm(idx) {
      // Read the row's zone select + h/m inputs and write them into
      // _scheduleEditor.extra_steps[idx]. No re-render on input changes
      // so the user's cursor and focus stay put.
      if (!Number.isFinite(idx) || idx < 0) return;
      const form = this.shadowRoot?.querySelector(".schedule-form");
      if (!form) return;
      const row = form.querySelector(`.extra-step-row[data-step-idx="${idx}"]`);
      if (!row) return;
      const zoneSel = row.querySelector('select[name="extra_zone"]');
      const hIn = row.querySelector('input[name="extra_dur_h"]');
      const mIn = row.querySelector('input[name="extra_dur_m"]');
      const h = parseInt(hIn?.value, 10) || 0;
      const m = parseInt(mIn?.value, 10) || 0;
      const steps = this._scheduleEditor.extra_steps || [];
      if (!steps[idx]) return;
      steps[idx] = {
        zone_entity_id: zoneSel?.value || steps[idx].zone_entity_id,
        duration_minutes: h * 60 + m,
      };
      this._scheduleEditor.extra_steps = steps;
    }

    _scheduleRender() {
      if (this._renderScheduled) return;
      this._renderScheduled = true;
      // requestAnimationFrame batches at 60fps which is plenty responsive
      // and prevents the microtask queue from drowning under rapid hass
      // updates. User-triggered renders bypass via _renderNow().
      requestAnimationFrame(() => {
        this._renderScheduled = false;
        // v1.19.0 — don't rebuild the DOM out from under an active
        // scroll. A background render mid-flick kills the momentum
        // (the element being scrolled is destroyed) even when the
        // position is restored afterward. Defer until the user has
        // been still for ~1s; data is a second or two stale during a
        // long scroll, which nobody notices — unlike the viewport
        // snapping to the top, which everybody notices.
        if (Date.now() - this._lastScrollAt < 1000) {
          if (this._deferredRenderTimer) clearTimeout(this._deferredRenderTimer);
          this._deferredRenderTimer = setTimeout(() => {
            this._deferredRenderTimer = null;
            this._scheduleRender();
          }, 1100);
          return;
        }
        this._safeRender();
      });
    }

    _renderNow() {
      // Synchronous render for user actions (navigation, modal toggles)
      // so the UI feels instantaneous instead of waiting on the next frame.
      this._renderScheduled = false;
      this._safeRender();
    }

    _captureScrollPositions() {
      const items = [];
      for (const sel of SCROLL_SELECTORS) {
        this.shadowRoot.querySelectorAll(sel).forEach((el, i) => {
          if (el.scrollTop || el.scrollLeft) {
            items.push({ sel, i, top: el.scrollTop, left: el.scrollLeft });
          }
        });
      }
      // Tag with the section currently IN THE DOM (_renderedSection,
      // set at the end of the previous render) — NOT _currentSection,
      // which _navigateTo mutates before calling _renderNow(). Restore
      // is skipped when the section changes so tab switches land at
      // the top like a fresh page, instead of inheriting the old
      // tab's scroll offset.
      return { section: this._renderedSection, items };
    }

    _restoreScrollPositions(saved) {
      if (saved.section !== this._currentSection || !saved.items.length) return;
      saved = saved.items;
      this._restoringScroll = true;
      for (const s of saved) {
        const el = this.shadowRoot.querySelectorAll(s.sel)[s.i];
        if (el) {
          el.scrollTop = s.top;
          el.scrollLeft = s.left;
        }
      }
      // Programmatic scrollTop sets dispatch scroll events on a later
      // task; keep the suppress flag up briefly so _onAnyScroll skips
      // them (otherwise every render would defer the next one ~1s).
      setTimeout(() => {
        this._restoringScroll = false;
      }, 50);
    }

    _safeRender() {
      // v1.30 — never rebuild the DOM out from under an active marker drag;
      // it would orphan the element being dragged. The pointer-up handler
      // refetches + re-renders once the drag finishes.
      if (this._mapDrag) return;
      // v1.19.0 — innerHTML rebuild resets every scroll container to
      // the top. Save positions before, restore after, so background
      // renders (hass updates, the 60s now-line tick) are invisible
      // to a user who has scrolled down the page.
      const savedScroll = this._captureScrollPositions();
      try {
        this._render();
        this._restoreScrollPositions(savedScroll);
        this._renderedSection = this._currentSection;
      } catch (err) {
        console.error("[complete-irrigation] render failed:", err);
        this.shadowRoot.innerHTML =
          '<div style="padding:24px;color:#db4437;font-family:sans-serif;">' +
          "<h3>Irrigation panel error</h3>" +
          "<p>The panel failed to render. Check browser console for details.</p>" +
          "</div>";
      }
    }

    _toggleSidebar() {
      this._collapsed = !this._collapsed;
      try {
        localStorage.setItem(SIDEBAR_STORAGE_KEY, String(this._collapsed));
      } catch (_) {}
      this._renderNow();
    }

    _navigateTo(sectionId) {
      this._currentSection = sectionId;
      if (sectionId === "schedules" && !this._schedulesLoaded) this._fetchSchedules();
      if (sectionId === "history") this._fetchRunHistory();  // always refetch on open
      // v2 — Yard: always refetch on open (report depends on schedules + ETo).
      if (sectionId === "yard") this._fetchYard();
      // v1.17 — Today screen's missed-runs banner reads from run history,
      // so load it lazily on first Today open if not already cached.
      if (sectionId === "today" && !this._runHistoryLoaded) this._fetchRunHistory();
      // v1.19.0 — keep the now-line drifting only while Today is open.
      if (sectionId === "today") this._startNowLineTimer();
      else this._stopNowLineTimer();
      // v1.35 — survey-status poll only runs while Yard is open (entering
      // Yard re-arms it via the _fetchYard above once surveys are known).
      if (sectionId !== "yard") this._stopLightSurveyPoll();
      // v1.32 — advisory "Today's plan" card; refetch on each Today open.
      if (sectionId === "today") this._fetchDailyPlan();
      // Today + Zones both rely on the cached PlannedRuns for their
      // calendar / strip rendering. Fetch lazily on first open and
      // again whenever schedules mutate (handled in _saveSchedule etc).
      if ((sectionId === "today" || sectionId === "zones") && !this._plannedRunsLoaded) {
        this._fetchPlannedRuns();
      }
      if (sectionId === "notifications") this._hydrateNotifyDraft();
      if (sectionId === "weather") {
        const w = this._findWeatherEntity();
        if (w && !this._forecastCache[w.entity_id]) {
          this._fetchForecast(w.entity_id);
        }
      }
      this._renderNow();
    }

    _hydrateNotifyDraft() {
      // Pull the currently-configured targets from config into a local
      // editable array. Called when entering the Notifications tab so the
      // draft always reflects saved state on first render. Subsequent
      // add/remove clicks mutate this array directly without re-pulling.
      const n = (this._config && this._config.notifications) || {};
      const targetsList = Array.isArray(n.notify_targets)
        ? n.notify_targets
        : n.notify_target
        ? [n.notify_target]
        : [];
      this._notifyDraft = targetsList.slice();  // copy
    }

    _openRunModal(entityId, zoneName) {
      this._runModalOpen = true;
      this._runModalEntityId = entityId;
      this._runModalZoneName = zoneName || entityId;
      this._renderNow();
    }

    _openNewSchedule() {
      this._scheduleEditor = emptyEditor();
      const zones = (this._panel?.config?.zones) || [];
      // Pre-select the first NON-HIDDEN zone for new schedules. Falls
      // back to the first zone overall if every zone is hidden (so
      // the modal isn't blank in that edge case).
      const firstVisible = zones.find((z) => !this._hiddenZones.has(z));
      const pick = firstVisible || zones[0];
      if (pick) this._scheduleEditor.zone_entity_id = pick;
      this._scheduleModalOpen = true;
      this._renderNow();
    }

    _openEditSchedule(scheduleId) {
      const found = this._schedules.find((s) => s.id === scheduleId);
      if (!found) return;
      // zone_steps[0] mirrors top-level; we only edit steps 1..N
      const allSteps = Array.isArray(found.zone_steps) ? found.zone_steps : [];
      const extra = allSteps.length > 1 ? allSteps.slice(1) : [];
      this._scheduleEditor = {
        id: found.id,
        name: found.name,
        zone_entity_id: found.zone_entity_id,
        start_time: found.start_time,
        duration_minutes: found.duration_minutes,
        weekdays: [...(found.weekdays || [])],
        enabled: found.enabled,
        mode: found.mode || "weekdays",
        interval_days: found.interval_days || 5,
        interval_hours: found.interval_hours || 6,
        start_date: found.start_date || "",
        end_date: found.end_date || "",
        repeat_annually: !!found.repeat_annually,
        interval_anchor: found.interval_anchor || _todayIso(),
        interval_end_time: found.interval_end_time || "",
        ignore_wind: !!found.ignore_wind,
        ignore_hot_weather: !!found.ignore_hot_weather,
        ignore_rain_lockout: !!found.ignore_rain_lockout,
        color: found.color || "",
        extra_steps: extra.map((s) => ({
          zone_entity_id: s.zone_entity_id,
          duration_minutes: s.duration_minutes,
        })),
      };
      this._scheduleModalOpen = true;
      this._renderNow();
    }

    _openCopyOfSchedule(scheduleId) {
      // v1.19.0 — clone an existing schedule into the editor with a
      // null id (so save creates a new schedule, not overwriting the
      // source) and a name suffixed " (copy)" so the duplicate is
      // identifiable in lists before the user picks a better name.
      // Typical flow: click Copy on an existing schedule, change just
      // the start time, click Create. Two clicks to a second daily run.
      const found = this._schedules.find((s) => s.id === scheduleId);
      if (!found) return;
      const allSteps = Array.isArray(found.zone_steps) ? found.zone_steps : [];
      const extra = allSteps.length > 1 ? allSteps.slice(1) : [];
      this._scheduleEditor = {
        id: null,  // new schedule — modal title will say "New Schedule"
        name: `${found.name} (copy)`,
        zone_entity_id: found.zone_entity_id,
        start_time: found.start_time,
        duration_minutes: found.duration_minutes,
        weekdays: [...(found.weekdays || [])],
        enabled: found.enabled,
        mode: found.mode || "weekdays",
        interval_days: found.interval_days || 5,
        interval_hours: found.interval_hours || 6,
        start_date: found.start_date || "",
        end_date: found.end_date || "",
        repeat_annually: !!found.repeat_annually,
        interval_anchor: found.interval_anchor || _todayIso(),
        interval_end_time: found.interval_end_time || "",
        ignore_wind: !!found.ignore_wind,
        ignore_hot_weather: !!found.ignore_hot_weather,
        ignore_rain_lockout: !!found.ignore_rain_lockout,
        color: found.color || "",
        extra_steps: extra.map((s) => ({
          zone_entity_id: s.zone_entity_id,
          duration_minutes: s.duration_minutes,
        })),
      };
      this._scheduleModalOpen = true;
      this._renderNow();
    }

    _closeAllModals() {
      this._runModalOpen = false;
      this._scheduleModalOpen = false;
      this._sensorModalOpen = false;
      this._sensorEditor = null;
      this._bannerModalOpen = false;
      this._establishmentModalOpen = false;
      this._establishmentEditor = null;
      this._renderNow();
    }

    _toggleZoneHidden(entityId) {
      if (this._hiddenZones.has(entityId)) {
        this._hiddenZones.delete(entityId);
      } else {
        this._hiddenZones.add(entityId);
      }
      try {
        localStorage.setItem(
          HIDDEN_ZONES_STORAGE_KEY,
          JSON.stringify([...this._hiddenZones])
        );
      } catch (_) {}
      this._renderNow();
    }

    async _reorderZone(entityId, direction) {
      // Move a zone up (-1) or down (+1) in the user-set order. Renders
      // optimistically (no waiting on the WS round-trip), then persists.
      const order = this._orderedZoneIds();
      const idx = order.indexOf(entityId);
      if (idx === -1) return;
      const target = idx + direction;
      if (target < 0 || target >= order.length) return;
      // Swap idx ↔ target
      const newOrder = order.slice();
      [newOrder[idx], newOrder[target]] = [newOrder[target], newOrder[idx]];
      // Optimistic local update: write into our cached config and
      // re-render before the WS call returns.
      this._config = { ...(this._config || {}), zone_order: newOrder };
      this._renderNow();
      try {
        await this._hass.callService("complete_irrigation", "set_general_config", {
          zone_order: newOrder,
        });
        // Re-pull canonical state. The server returns the same order
        // so this is just defense against an out-of-band edit.
        await this._fetchConfig();
      } catch (err) {
        alert("Failed to save zone order: " + (err?.message || err));
        // Reload server state to undo the optimistic change on failure.
        await this._fetchConfig();
      }
    }

    _startLocalCountdown(entityId, minutes) {
      this._localRuns[entityId] = Date.now() + minutes * 60 * 1000;
      this._localRunDurations[entityId] = minutes;
      // Full re-render ONCE to swap Run → Stop button and inject countdown
      // <span data-countdown-for>. Subsequent ticks only update text content
      // (no shadowRoot rebuild → no flicker, no shifting click targets).
      if (!this._countdownTimer) {
        this._countdownTimer = setInterval(() => this._tickCountdowns(), 1000);
      }
      this._renderNow();
    }

    _stopLocalCountdown(entityId) {
      delete this._localRuns[entityId];
      delete this._localRunDurations[entityId];
      if (Object.keys(this._localRuns).length === 0 && this._countdownTimer) {
        clearInterval(this._countdownTimer);
        this._countdownTimer = null;
      }
      // Re-render so Stop button reverts to Run Now.
      this._renderNow();
    }

    _tickCountdowns() {
      const now = Date.now();
      const expired = [];
      for (const eid of Object.keys(this._localRuns)) {
        if (this._localRuns[eid] <= now) expired.push(eid);
      }

      if (expired.length > 0) {
        // Someone's countdown reached zero — clear and do a full re-render
        // once so Stop reverts to Run Now and the status text reflects idle.
        for (const eid of expired) {
          delete this._localRuns[eid];
          delete this._localRunDurations[eid];
        }
        if (Object.keys(this._localRuns).length === 0 && this._countdownTimer) {
          clearInterval(this._countdownTimer);
          this._countdownTimer = null;
        }
        this._renderNow();
        return;
      }

      // No expiries — just update countdown text content. NO DOM rebuild,
      // so :hover and any in-flight clicks are unaffected.
      for (const eid of Object.keys(this._localRuns)) {
        const remaining = Math.max(0, this._localRuns[eid] - now);
        const nodes = this.shadowRoot.querySelectorAll(
          `[data-countdown-for="${cssEscape(eid)}"]`
        );
        const text = _formatRemaining(remaining);
        nodes.forEach((n) => {
          if (n.textContent !== text) n.textContent = text;
        });
      }
    }

    // ── HA calls ───────────────────────────────────────────────────
    async _fetchSchedules() {
      if (!this._hass || !this._hass.callWS) return;
      try {
        const res = await this._hass.callWS({
          type: "complete_irrigation/list_schedules",
        });
        this._schedules = (res && res.schedules) || [];
        this._schedulesLoaded = true;
        this._scheduleRender();
        // v1.16 — schedules drive the planner output, so any schedule
        // refresh should invalidate + re-fetch the planned-runs cache.
        // Fire-and-forget; the render callback inside picks up the
        // new data when it arrives.
        this._plannedRunsLoaded = false;
        this._fetchPlannedRuns();
      } catch (err) {
        console.error("[complete-irrigation] list_schedules failed:", err);
      }
    }

    async _fetchConfig() {
      if (!this._hass || !this._hass.callWS) return;
      try {
        this._config = await this._hass.callWS({
          type: "complete_irrigation/get_config",
        }) || {};
        this._configLoaded = true;
        this._scheduleRender();
      } catch (err) {
        console.error("[complete-irrigation] get_config failed:", err);
      }
    }

    async _fetchYard() {
      // v2 — pull the plant list + the computed per-loop design report.
      if (!this._hass?.callWS) return;
      try {
        const [plantsRes, reportRes, tasksRes] = await Promise.all([
          this._hass.callWS({ type: "complete_irrigation/list_plants" }),
          this._hass.callWS({ type: "complete_irrigation/yard_report" }),
          this._hass.callWS({ type: "complete_irrigation/list_care_tasks" }),
        ]);
        this._plants = (plantsRes && plantsRes.plants) || [];
        // v1.35 — in-flight illuminance surveys ride along on list_plants.
        this._activeLightSurveys =
          (plantsRes && plantsRes.active_light_surveys) || {};
        this._careTasks = (tasksRes && tasksRes.tasks) || [];
        this._yardReports = (reportRes && reportRes.reports) || [];
        this._yardEto = reportRes ? reportRes.eto_in_week : null;
        this._yardEff = reportRes ? reportRes.drip_efficiency : null;
        // v1.28 — auto-ETo source/values (manual field is eto_manual so the
        // input shows the editable fallback, not the auto-computed number).
        this._yardEtoStatus = reportRes
          ? {
              eto_source: reportRes.eto_source,
              eto_auto: reportRes.eto_auto,
              eto_manual: reportRes.eto_manual,
              eto_auto_value: reportRes.eto_auto_value,
              eto_auto_at: reportRes.eto_auto_at,
              weather_entity: reportRes.weather_entity,
            }
          : null;
        this._yardMap = reportRes ? reportRes.yard_map || null : null; // v1.30
        this._yardLoaded = true;
        // v1.35 — start/stop the 30s survey-status poll to match the
        // just-fetched active_light_surveys (idempotent).
        this._syncLightSurveyPoll();
        this._scheduleRender();
      } catch (err) {
        console.error("[complete-irrigation] yard fetch failed:", err);
      }
    }

    async _fetchCareTasks() {
      // v1.35 — light re-fetch after a care-task mutation (the full
      // _fetchYard is unnecessary — nothing else changed).
      if (!this._hass?.callWS) return;
      try {
        const res = await this._hass.callWS({
          type: "complete_irrigation/list_care_tasks",
        });
        this._careTasks = (res && res.tasks) || [];
        this._scheduleRender();
      } catch (err) {
        console.error("[complete-irrigation] list_care_tasks failed:", err);
      }
    }

    async _fetchDailyPlan() {
      // v1.32 — the advisory "Today's plan" (zones prioritized by urgency).
      if (!this._hass?.callWS) return;
      try {
        this._dailyPlan = await this._hass.callWS({
          type: "complete_irrigation/get_daily_plan",
        });
        this._scheduleRender();
      } catch (err) {
        console.error("[complete-irrigation] daily plan fetch failed:", err);
      }
    }

    _renderDailyPlanCard() {
      const plan = this._dailyPlan;
      if (!plan || !Array.isArray(plan.items) || plan.items.length === 0) return "";
      const meta = {
        priority: { icon: "🔴", label: "Priority" },
        run: { icon: "🟢", label: "Run" },
        light: { icon: "🔵", label: "Light" },
        skip: { icon: "⚪", label: "Skip" },
      };
      const rows = plan.items
        .map((it) => {
          const m = meta[it.recommendation] || meta.run;
          return (
            `<li class="plan-item plan-${escapeAttr(it.recommendation)}">` +
            `<span class="plan-rec" title="${escapeAttr(m.label)}">${m.icon}</span>` +
            `<span class="plan-zone">${escapeHtml(it.zone_name)}</span>` +
            `<span class="plan-reason">${escapeHtml(it.reason)}</span>` +
            `</li>`
          );
        })
        .join("");
      return (
        `<section class="daily-plan-card">` +
        `<div class="section-title-row">` +
        `<h3 class="section-title">Today's plan</h3>` +
        `<span class="section-hint" style="margin:0">Advisory — watering still follows your schedules + gates.</span>` +
        `</div>` +
        `<p class="plan-summary">${escapeHtml(plan.summary || "")}</p>` +
        `<ul class="plan-list">${rows}</ul>` +
        `</section>`
      );
    }

    async _fetchPlannedRuns() {
      // v1.16 — pull resolved PlannedRuns from the server (covers
      // start_date / repeat_annually / configurable zone_buffer /
      // conflict resolution that the old JS reimplementation skipped).
      // Window: 8 days from today midnight so a 7-day strip + a 2-day
      // calendar window both fit without a refetch.
      if (!this._hass?.callWS) return;
      const now = new Date();
      const from = new Date(now);
      from.setHours(0, 0, 0, 0);
      const until = new Date(from.getTime() + 8 * 86400000);
      try {
        const res = await this._hass.callWS({
          type: "complete_irrigation/list_planned_runs",
          from_dt: from.toISOString(),
          until_dt: until.toISOString(),
        });
        const runs = (res && res.runs) || [];
        // Index by local YYYY-MM-DD so renderers can lookup per-day O(1).
        const byDate = new Map();
        for (const r of runs) {
          if ((r.reason || "").startsWith("skipped:")) continue;
          const d = new Date(r.start_at);
          const iso =
            d.getFullYear() +
            "-" +
            String(d.getMonth() + 1).padStart(2, "0") +
            "-" +
            String(d.getDate()).padStart(2, "0");
          if (!byDate.has(iso)) byDate.set(iso, []);
          byDate.get(iso).push({
            start_minutes: d.getHours() * 60 + d.getMinutes(),
            zone_entity_id: r.zone_entity_id,
            zone_name: this._zoneName(r.zone_entity_id),
            duration_minutes: r.duration_minutes,
            schedule_name: r.schedule_name,
            schedule_id: r.schedule_id,
            color: r.color || null, // v1.18 — schedule color for pill tint
          });
        }
        // Sort each day's bucket by start time
        for (const arr of byDate.values()) {
          arr.sort((a, b) => a.start_minutes - b.start_minutes);
        }
        this._plannedRunsByDate = byDate;
        this._plannedRunsLoaded = true;
        this._scheduleRender();
      } catch (err) {
        console.warn(
          "[complete-irrigation] list_planned_runs failed (falling back to in-panel planner):",
          err?.message || err
        );
      }
    }

    _localDateKey(date) {
      // Format a JS Date as local YYYY-MM-DD (matches the keys in
      // _plannedRunsByDate). Shared between Today calendar + Zones strip.
      return (
        date.getFullYear() +
        "-" +
        String(date.getMonth() + 1).padStart(2, "0") +
        "-" +
        String(date.getDate()).padStart(2, "0")
      );
    }

    async _fetchRunHistory() {
      // Pulled lazily — only when the History tab opens, plus a refresh
      // after run_zone/stop_zone since those mutate history.
      if (!this._hass?.callWS) return;
      try {
        const res = await this._hass.callWS({
          type: "complete_irrigation/list_run_history",
        });
        this._runHistory = (res && res.records) || [];
        this._runHistoryLoaded = true;
        this._scheduleRender();
      } catch (err) {
        console.error("[complete-irrigation] list_run_history failed:", err);
      }
    }

    async _fetchHaThemes() {
      // Pull the list of HA-installed themes so the Settings tab can
      // offer them as a picker. Response shape:
      //   { themes: { "Mushroom": {primary-color: "...", ...}, ... },
      //     default_theme: "...", ... }
      if (!this._hass?.callWS) return;
      try {
        const resp = await this._hass.callWS({ type: "frontend/get_themes" });
        this._haThemes = (resp && resp.themes) || {};
        this._scheduleRender();
      } catch (err) {
        console.warn("[complete-irrigation] frontend/get_themes failed:", err?.message || err);
      }
    }

    _renderHaThemeStyle() {
      // If the user picked an HA theme, inject a <style> block whose
      // :host rule overrides our --ci-* variables (and the underlying
      // HA vars they fall back to) with the theme's values.
      //
      // v1.15 — theme keys/values are sanitized before interpolation. A
      // malicious or malformed theme value containing `}` could otherwise
      // close the `:host{...}` rule and inject arbitrary CSS, and
      // `</style>` would break out of the style element entirely. We
      // strip chars that have CSS-syntactic meaning + any quotes / angle
      // brackets so the worst a bad theme can do is produce an
      // ineffective rule.
      const name = this._haTheme;
      if (!name || !this._haThemes || !this._haThemes[name]) return "";
      const theme = this._haThemes[name];
      const safeKey = (k) =>
        typeof k === "string" && /^[a-zA-Z0-9_-]+$/.test(k) ? k : null;
      const safeVal = (v) =>
        typeof v === "string"
          ? v.replace(/[{}<>;"'\\\r\n]/g, "").trim()
          : null;
      const lines = [];
      for (const [k, v] of Object.entries(theme)) {
        const sk = safeKey(k);
        const sv = safeVal(v);
        if (!sk || !sv) continue;
        lines.push(`--${sk}: ${sv};`);
      }
      // Also remap to --ci-* directly for safety when the theme doesn't
      // define every fallback var we use.
      const map = {
        "primary-background-color": ["--ci-bg", "--ci-input-bg"],
        "card-background-color": ["--ci-card"],
        "primary-text-color": ["--ci-text"],
        "secondary-text-color": ["--ci-text-2"],
        "divider-color": ["--ci-border"],
        "primary-color": ["--ci-accent"],
        "secondary-background-color": ["--ci-hover"],
      };
      for (const [haKey, ciKeys] of Object.entries(map)) {
        const sv = safeVal(theme[haKey]);
        if (!sv) continue;
        for (const ciKey of ciKeys) {
          lines.push(`${ciKey}: ${sv};`);
        }
      }
      if (lines.length === 0) return "";
      return `<style>:host{${lines.join("")}}</style>`;
    }

    async _fetchActiveRuns() {
      // Hydrate _localRuns from the server-side ManualRunTracker so a
      // page reload mid-run, or a run started outside this panel (via
      // service call, schedule, etc.), still shows the countdown.
      if (!this._hass || !this._hass.callWS) return;
      try {
        const resp = await this._hass.callWS({
          type: "complete_irrigation/get_active_runs",
        });
        const runs = resp?.runs || [];
        for (const r of runs) {
          const deadlineMs = new Date(r.deadline).getTime();
          if (Number.isFinite(deadlineMs) && deadlineMs > Date.now()) {
            this._localRuns[r.entity_id] = deadlineMs;
            if (Number.isFinite(r.duration_minutes)) {
              this._localRunDurations[r.entity_id] = r.duration_minutes;
            }
          }
        }
        // v1.30 — rebuild the active-session map fresh each fetch (a cleared
        // session disappears; entries also self-expire at their deadline).
        const sessions = {};
        for (const s of resp?.sessions || []) {
          const dl = new Date(s.deadline).getTime();
          if (Number.isFinite(dl) && dl > Date.now()) sessions[s.entity_id] = dl;
        }
        this._activeSessions = sessions;
        if (
          Object.keys(this._localRuns).length > 0 &&
          !this._countdownTimer
        ) {
          this._countdownTimer = setInterval(() => this._tickCountdowns(), 1000);
        }
        this._scheduleRender();
      } catch (err) {
        // Pre-v1.13.2 backends don't have this command — no-op, fall
        // back to the local-only countdown behavior.
        console.warn("[complete-irrigation] get_active_runs not available:", err);
      }
    }

    async _runZone(entityId, minutes) {
      if (!this._hass?.callService) return;
      try {
        await this._hass.callService("complete_irrigation", "run_zone", {
          entity_id: entityId,
          minutes,
        });
        this._startLocalCountdown(entityId, minutes);
        // Refresh history if user is currently viewing it so the new
        // run shows up at the top of the list right away.
        if (this._currentSection === "history") this._fetchRunHistory();
      } catch (err) {
        alert("Failed to start zone: " + (err?.message || err));
      }
    }

    async _stopZone(entityId) {
      if (!this._hass?.callService) return;
      try {
        await this._hass.callService("complete_irrigation", "stop_zone", {
          entity_id: entityId,
        });
        this._stopLocalCountdown(entityId);
        if (this._currentSection === "history") this._fetchRunHistory();
      } catch (err) {
        alert("Failed to stop zone: " + (err?.message || err));
      }
    }

    async _clearRunHistory() {
      if (!this._hass?.callService) return;
      try {
        await this._hass.callService("complete_irrigation", "clear_run_history", {});
        this._historyExpanded.clear();
        await this._fetchRunHistory();
      } catch (err) {
        alert("Failed to clear run history: " + (err?.message || err));
      }
    }

    async _saveSchedule() {
      // Make sure duration_minutes + start_time reflect the latest h/m
      // inputs (covers the case where save was clicked before any input
      // event fired).
      this._syncDurationFromForm();
      this._syncStartTimeFromForm();
      this._syncIntervalEndTimeFromForm();
      const e = this._scheduleEditor;
      const minutes = parseInt(e.duration_minutes, 10);
      const mode =
        e.mode === "interval"
          ? "interval"
          : e.mode === "interval_hours"
          ? "interval_hours"
          : "weekdays";
      if (!e.name || !e.name.trim()) return alert("Schedule name is required.");
      if (!e.zone_entity_id) return alert("Pick a zone.");
      if (!minutes || minutes < 1 || minutes > MAX_SCHEDULE_MINUTES) {
        const maxH = Math.floor(MAX_SCHEDULE_MINUTES / 60);
        return alert(`Duration must be at least 1 minute and no more than ${maxH} hours.`);
      }
      if (mode === "weekdays" && !e.weekdays.length)
        return alert("Pick at least one weekday.");
      if (mode === "interval") {
        const days = parseInt(e.interval_days, 10);
        if (!days || days < 1 || days > 365)
          return alert("Interval must be 1–365 days.");
        if (!e.interval_anchor) return alert("Pick a first-run date.");
      }
      if (mode === "interval_hours") {
        const hrs = parseInt(e.interval_hours, 10);
        if (!hrs || hrs < 1 || hrs > 72)
          return alert("Interval must be 1–72 hours.");
        if (!e.interval_anchor) return alert("Pick a first-run date.");
        // Optional daily-window cap — must be strictly after start_time.
        if (e.interval_end_time && e.interval_end_time.trim()) {
          if (e.interval_end_time <= e.start_time) {
            return alert(
              `Stop-after time (${e.interval_end_time}) must be later than start time (${e.start_time}).`
            );
          }
        }
      }

      // Validate annual-repeat preconditions client-side for a friendlier
      // error than the server-side voluptuous failure.
      if (e.repeat_annually && (!e.start_date || !e.end_date)) {
        return alert(
          "Repeat every year needs both a Start date and an End date."
        );
      }
      if (
        e.repeat_annually &&
        e.start_date &&
        e.end_date &&
        e.start_date.slice(5) > e.end_date.slice(5)
      ) {
        return alert(
          "For yearly repeat, Start date's month/day must be on or before End date's month/day."
        );
      }

      const payload = {
        name: e.name.trim(),
        zone_entity_id: e.zone_entity_id,
        start_time: e.start_time,
        duration_minutes: minutes,
        enabled: e.enabled,
        mode,
        // Active period. Empty string → null (no bound).
        start_date: e.start_date || null,
        end_date: e.end_date || null,
        repeat_annually: !!e.repeat_annually,
        // v1.19.0 — per-schedule weather-gate opt-outs
        ignore_wind: !!e.ignore_wind,
        ignore_hot_weather: !!e.ignore_hot_weather,
        ignore_rain_lockout: !!e.ignore_rain_lockout,
        // v1.18 — color ("" → null clears it on the server)
        color: e.color ? e.color : null,
      };
      if (mode === "weekdays") {
        payload.weekdays = e.weekdays;
      } else if (mode === "interval") {
        payload.weekdays = [];
        payload.interval_days = parseInt(e.interval_days, 10);
        payload.interval_anchor = e.interval_anchor;
      } else {
        // interval_hours
        payload.weekdays = [];
        payload.interval_hours = parseInt(e.interval_hours, 10);
        payload.interval_anchor = e.interval_anchor;
        // Optional daily-window cap. Empty string → omit (legacy mode).
        if (e.interval_end_time && e.interval_end_time.trim()) {
          payload.interval_end_time = e.interval_end_time;
        } else {
          payload.interval_end_time = null;  // explicit clear on edit
        }
      }

      // Multi-zone: always send zone_steps (full list including primary).
      // When extra_steps is empty we explicitly send [] to clear any
      // previously-stored extras on an edit.
      const extras = (e.extra_steps || [])
        .map((s) => ({
          zone_entity_id: s.zone_entity_id,
          duration_minutes: parseInt(s.duration_minutes, 10) || 0,
        }))
        .filter((s) => s.zone_entity_id && s.duration_minutes > 0);
      if (extras.length > 0) {
        payload.zone_steps = [
          { zone_entity_id: e.zone_entity_id, duration_minutes: minutes },
          ...extras,
        ];
      } else {
        payload.zone_steps = [];
      }

      try {
        if (e.id) {
          await this._hass.callService("complete_irrigation", "update_schedule", {
            schedule_id: e.id,
            ...payload,
          });
        } else {
          await this._hass.callService("complete_irrigation", "add_schedule", payload);
        }
        this._closeAllModals();
        await this._fetchSchedules();
      } catch (err) {
        alert("Failed to save schedule: " + (err?.message || err));
      }
    }

    async _deleteSchedule(scheduleId) {
      if (!confirm("Delete this schedule?")) return;
      try {
        await this._hass.callService("complete_irrigation", "delete_schedule", {
          schedule_id: scheduleId,
        });
        await this._fetchSchedules();
      } catch (err) {
        alert("Failed to delete: " + (err?.message || err));
      }
    }

    // ── v2 Yard: plant CRUD + ETo ──────────────────────────────────
    async _savePlant() {
      const e = this._plantEditor;
      if (!e) return;
      const name = (e.name || "").trim();
      const area = parseFloat(e.canopy_area_sqft);
      const zone = e.zone_entity_id;
      if (!name || !zone || !(area > 0)) {
        alert("Enter a name, a positive canopy area (ft²), and pick a zone.");
        return;
      }
      const payload = {
        name,
        wucols_category: e.wucols_category,
        canopy_area_sqft: area,
        zone_entity_id: zone,
        // v1.35 — optional species (backend caps at 120 chars; the input's
        // maxlength matches, the slice is belt-and-suspenders).
        species: (e.species || "").trim().slice(0, 120),
      };
      try {
        if (e.id) {
          // v1.35 — light range rides along on an EDIT only (the form hides
          // the lux fields on add: add_plant takes none, so a range there
          // would be silently discarded). Validate BEFORE any call so a bad
          // range can't half-apply — and can never block an add.
          const luxLowRaw = String(e.lux_low == null ? "" : e.lux_low).trim();
          const luxHighRaw = String(e.lux_high == null ? "" : e.lux_high).trim();
          const luxLow = parseInt(luxLowRaw, 10);
          const luxHigh = parseInt(luxHighRaw, 10);
          const bothSet =
            luxLowRaw !== "" &&
            luxHighRaw !== "" &&
            Number.isFinite(luxLow) &&
            Number.isFinite(luxHigh);
          const bothEmpty = luxLowRaw === "" && luxHighRaw === "";
          if (!bothSet && !bothEmpty) {
            // Half-filled pair would be a silent no-op — say so and keep
            // the editor open.
            alert("Enter both Lux low and Lux high, or clear both to remove the range.");
            return;
          }
          if (bothSet && luxLow >= luxHigh) {
            alert("Lux low must be less than lux high.");
            return;
          }
          await this._hass.callService("complete_irrigation", "update_plant", {
            plant_id: e.id,
            ...payload,
          });
          if (bothSet) {
            await this._hass.callService(
              "complete_irrigation",
              "set_plant_light_range",
              { plant_id: e.id, lux_low: luxLow, lux_high: luxHigh }
            );
          } else if (bothEmpty && e._hadLightRange) {
            await this._hass.callService(
              "complete_irrigation",
              "set_plant_light_range",
              { plant_id: e.id, clear: true }
            );
          }
        } else {
          await this._hass.callService("complete_irrigation", "add_plant", payload);
        }
        this._plantEditor = null;
        await this._fetchYard();
      } catch (err) {
        alert("Failed to save plant: " + (err?.message || err));
      }
    }

    async _deletePlant(plantId, name) {
      if (!confirm(`Delete plant "${name || plantId}"?`)) return;
      try {
        await this._hass.callService("complete_irrigation", "delete_plant", {
          plant_id: plantId,
        });
        await this._fetchYard();
      } catch (err) {
        alert("Failed to delete plant: " + (err?.message || err));
      }
    }

    // ── v1.35 light surveys ────────────────────────────────────────
    async _startLightSurvey(plantId) {
      // Kick off an illuminance survey; the backend samples the sensor for
      // N minutes and stores a verdict against the plant's light range.
      if (!plantId) return;
      const e = this._plantEditor || {};
      const sensor = (e.light_survey_sensor || "").trim();
      const minutes = parseInt(e.light_survey_minutes, 10);
      if (!sensor) {
        alert("Enter the illuminance sensor entity id (e.g. sensor.back_yard_illuminance).");
        return;
      }
      if (!(minutes >= 1) || minutes > 240) {
        alert("Survey length must be between 1 and 240 minutes.");
        return;
      }
      try {
        await this._hass.callService("complete_irrigation", "start_light_survey", {
          plant_id: plantId,
          sensor_entity_id: sensor,
          minutes,
        });
        // Refreshes active_light_surveys → the controls flip to "Surveying…".
        await this._fetchYard();
      } catch (err) {
        alert("Failed to start the light survey: " + (err?.message || err));
      }
    }

    async _cancelLightSurvey(plantId) {
      if (!plantId) return;
      try {
        await this._hass.callService("complete_irrigation", "cancel_light_survey", {
          plant_id: plantId,
        });
        await this._fetchYard();
      } catch (err) {
        alert("Failed to cancel the survey: " + (err?.message || err));
      }
    }

    // ── v1.35 care tasks ───────────────────────────────────────────
    async _addCareTask() {
      // Read the rendered controls (selects show their pick even when the
      // user never fired a change event); the draft only preserves typing.
      const root = this.shadowRoot;
      const kind = root?.querySelector('select[name="care_kind"]')?.value || "fertilize";
      const label = (root?.querySelector('input[name="care_label"]')?.value || "").trim();
      const interval = parseInt(
        root?.querySelector('input[name="care_interval"]')?.value || "0",
        10
      );
      const subject = root?.querySelector('select[name="care_subject"]')?.value || "";
      if (!(interval >= 1)) {
        alert("Enter a repeat interval of at least 1 day.");
        return;
      }
      if (!subject) {
        alert("Pick a plant or zone for this task.");
        return;
      }
      if (kind === "custom" && !label) {
        alert("A custom task needs a label.");
        return;
      }
      const payload = { kind, interval_days: interval };
      if (label) payload.label = label;
      if (subject.startsWith("plant:")) payload.plant_id = subject.slice(6);
      else payload.zone_entity_id = subject.slice(5);
      try {
        await this._hass.callService("complete_irrigation", "add_care_task", payload);
        this._careDraft.care_label = ""; // fresh label for the next add
        await this._fetchCareTasks();
      } catch (err) {
        alert("Failed to add the care task: " + (err?.message || err));
      }
    }

    async _completeCareTask(taskId) {
      if (!taskId) return;
      try {
        await this._hass.callService("complete_irrigation", "complete_care_task", {
          task_id: taskId,
        });
        await this._fetchCareTasks();
      } catch (err) {
        alert("Failed to complete the task: " + (err?.message || err));
      }
    }

    async _deleteCareTask(taskId, name) {
      if (!taskId) return;
      if (!confirm(`Delete care task "${name || taskId}"?`)) return;
      try {
        await this._hass.callService("complete_irrigation", "delete_care_task", {
          task_id: taskId,
        });
        await this._fetchCareTasks();
      } catch (err) {
        alert("Failed to delete the task: " + (err?.message || err));
      }
    }

    // ── v1.35 watering diagnosis ───────────────────────────────────
    async _diagnoseZone(entityId) {
      // Toggle: a second click on 🩺 collapses the open panel.
      if (!entityId || !this._hass?.callWS) return;
      if (this._zoneDiagnosis[entityId]) {
        delete this._zoneDiagnosis[entityId];
        return this._renderNow();
      }
      this._zoneDiagnosis[entityId] = { loading: true };
      this._renderNow();
      try {
        const res = await this._hass.callWS({
          type: "complete_irrigation/watering_diagnosis",
          zone_entity_id: entityId,
        });
        this._zoneDiagnosis[entityId] = (res && res.diagnosis) || {
          status: "unknown",
          signs: [],
          confirm: [],
          suggestions: [],
        };
      } catch (err) {
        delete this._zoneDiagnosis[entityId];
        console.error("[complete-irrigation] watering_diagnosis failed:", err);
        alert("Failed to run the diagnosis: " + (err?.message || err));
      }
      this._renderNow();
    }

    async _applyEto() {
      const input = this.shadowRoot.querySelector('input[name="eto_in_week"]');
      const val = input ? parseFloat(input.value) : NaN;
      if (!(val > 0)) {
        alert("Enter a positive reference ET value (inches/week).");
        return;
      }
      try {
        await this._hass.callService("complete_irrigation", "set_weather_config", {
          eto_in_week: val,
        });
        await this._fetchYard();
      } catch (err) {
        alert("Failed to set ET: " + (err?.message || err));
      }
    }

    async _toggleAutoEto(checked) {
      // v1.28 — flip auto ETo (FAO-56 from the weather forecast) on/off. The
      // backend awaits its immediate refresh before returning, so the refetch
      // right after sees the fresh figure with no timing race. We hold an
      // optimistic pending state so a background re-render can't visually snap
      // the checkbox back to the old value while the call is in flight.
      this._pendingAutoEto = !!checked;
      this._renderNow();
      try {
        await this._hass.callService("complete_irrigation", "set_weather_config", {
          eto_auto: !!checked,
        });
      } catch (err) {
        alert("Failed to toggle auto ET: " + (err?.message || err));
      } finally {
        this._pendingAutoEto = null;
        await this._fetchYard();
      }
    }

    async _setupYardMap() {
      // v1.30 — fetch + cache the aerial backdrop (centered on the HA location).
      if (this._mapBusy) return;
      this._mapBusy = true;
      this._renderNow();
      try {
        await this._hass.callService("complete_irrigation", "set_yard_map", {});
      } catch (err) {
        alert("Failed to fetch the aerial image: " + (err?.message || err));
      } finally {
        this._mapBusy = false;
        await this._fetchYard();
      }
    }

    async _placePlant(plantId) {
      // v1.30 — drop an unplaced plant at the map center so it becomes a
      // draggable marker; the user then drags it to the right spot.
      if (!plantId) return;
      try {
        await this._hass.callService("complete_irrigation", "update_plant", {
          plant_id: plantId,
          map_x: 0.5,
          map_y: 0.5,
        });
        await this._fetchYard();
      } catch (err) {
        alert("Failed to place plant: " + (err?.message || err));
      }
    }

    _onMapPointerDown(e) {
      // v1.30 — begin dragging a plant marker. We mutate the live element during
      // the drag (no re-render) and persist on release.
      const marker = e.target?.closest?.('[data-action="map-marker"]');
      if (!marker) return;
      const wrap = marker.closest(".yard-map-wrap");
      if (!wrap) return;
      e.preventDefault();
      const rect = wrap.getBoundingClientRect();
      this._mapDrag = { plantId: marker.dataset.plantId, el: marker, rect, x: null, y: null };
      marker.classList.add("dragging");
      try {
        marker.setPointerCapture(e.pointerId);
      } catch (_e) {
        /* not all targets support capture; window listeners cover it */
      }
    }

    _onMapPointerMove(e) {
      const d = this._mapDrag;
      if (!d) return;
      const x = Math.min(1, Math.max(0, (e.clientX - d.rect.left) / d.rect.width));
      const y = Math.min(1, Math.max(0, (e.clientY - d.rect.top) / d.rect.height));
      d.x = x;
      d.y = y;
      d.el.style.left = (x * 100).toFixed(3) + "%";
      d.el.style.top = (y * 100).toFixed(3) + "%";
    }

    async _onMapPointerUp() {
      const d = this._mapDrag;
      this._mapDrag = null;
      if (!d) return;
      d.el.classList.remove("dragging");
      if (d.x == null || d.y == null) return; // a tap with no movement — leave as-is
      try {
        await this._hass.callService("complete_irrigation", "update_plant", {
          plant_id: d.plantId,
          map_x: d.x,
          map_y: d.y,
        });
        await this._fetchYard();
      } catch (err) {
        alert("Failed to save marker position: " + (err?.message || err));
        await this._fetchYard();
      }
    }

    async _runSchedule(scheduleId, scheduleName) {
      // v1.19.0 — "Run" button on each schedule row. Confirms before
      // firing since this triggers physical irrigation hardware. The
      // backend service handles multi-zone chaining (staggered
      // run_zone calls with the configured inter-zone buffer).
      if (!this._hass?.callService) return;
      const sched = this._schedules?.find((s) => s.id === scheduleId);
      let confirmMsg;
      if (sched && Array.isArray(sched.zone_steps) && sched.zone_steps.length > 1) {
        const totalMin = sched.zone_steps.reduce(
          (sum, s) => sum + (parseInt(s.duration_minutes, 10) || 0),
          0
        );
        confirmMsg =
          `Run "${scheduleName}" now?\n\n` +
          `${sched.zone_steps.length} zones, ~${totalMin} min total (plus inter-zone buffers).\n` +
          `Weather gates (moisture / wind / hot-weather / rain lockout) are bypassed.`;
      } else if (sched) {
        confirmMsg =
          `Run "${scheduleName}" now?\n\n` +
          `${sched.duration_minutes} min on the configured zone.\n` +
          `Weather gates (moisture / wind / hot-weather / rain lockout) are bypassed.`;
      } else {
        confirmMsg = `Run "${scheduleName}" now?`;
      }
      if (!confirm(confirmMsg)) return;
      try {
        await this._hass.callService("complete_irrigation", "run_schedule", {
          schedule_id: scheduleId,
        });
        // Refresh history if user is on the History tab so the new run
        // shows up immediately as soon as services.py records the start.
        if (this._currentSection === "history") this._fetchRunHistory();
      } catch (err) {
        alert("Failed to run schedule: " + (err?.message || err));
      }
    }

    async _toggleSchedule(scheduleId, enabled) {
      try {
        await this._hass.callService("complete_irrigation", "set_schedule_enabled", {
          schedule_id: scheduleId,
          enabled: !enabled, // flip
        });
        await this._fetchSchedules();
      } catch (err) {
        alert("Failed to toggle: " + (err?.message || err));
      }
    }

    // ── Data helpers ───────────────────────────────────────────────
    _orderedZoneIds() {
      // Resolve the rendering order for zones. config.zone_order (if set)
      // wins; any zone present in entry.data.zones but not yet in the
      // order list is appended at the end. Any stale entries in the
      // saved order that no longer exist as configured zones are dropped.
      const configured = this._panel?.config?.zones || [];
      const configuredSet = new Set(configured);
      const saved = (this._config && this._config.zone_order) || [];
      const out = [];
      const seen = new Set();
      for (const eid of saved) {
        if (configuredSet.has(eid) && !seen.has(eid)) {
          out.push(eid);
          seen.add(eid);
        }
      }
      for (const eid of configured) {
        if (!seen.has(eid)) {
          out.push(eid);
          seen.add(eid);
        }
      }
      return out;
    }

    _zones() {
      const ids = this._orderedZoneIds();
      return ids.map((entityId) => {
        const state = this._hass?.states?.[entityId];
        const friendly =
          state?.attributes?.friendly_name ||
          entityId.replace(/^switch\./, "").replace(/_/g, " ");
        return {
          entityId,
          name: friendly,
          on: state?.state === "on",
          available: !!state,
        };
      });
    }

    _zoneName(entityId) {
      const state = this._hass?.states?.[entityId];
      return (
        state?.attributes?.friendly_name ||
        entityId.replace(/^switch\./, "").replace(/_/g, " ")
      );
    }

    // ── Rendering ──────────────────────────────────────────────────
    _effectiveTheme() {
      // "auto" follows HA / OS preference; "light" or "dark" are explicit
      if (this._theme === "dark") return "dark";
      if (this._theme === "light") return "light";
      try {
        return window.matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark"
          : "light";
      } catch (_) {
        return "light";
      }
    }

    _cycleTheme() {
      // light → dark → auto → light. Three positions so users can opt
      // back into "follow HA" without clearing browser data.
      this._theme =
        this._theme === "light"
          ? "dark"
          : this._theme === "dark"
          ? "auto"
          : "light";
      try {
        localStorage.setItem(THEME_STORAGE_KEY, this._theme);
      } catch (_) {}
      this._renderNow();
    }

    _render() {
      const sidebarClass = this._collapsed ? "sidebar collapsed" : "sidebar";
      // Reflect the effective theme on the host so CSS variables in
      // _styles() can switch via [data-theme="dark"] selectors.
      this.setAttribute("data-theme", this._effectiveTheme());

      const navItems = SECTIONS.map(
        (s) =>
          `<button class="sidebar-item ${
            s.id === this._currentSection ? "active" : ""
          }" data-section="${s.id}" title="${escapeAttr(s.label)}">` +
          `<span class="sidebar-icon">${s.icon}</span>` +
          `<span class="sidebar-label">${escapeHtml(s.label)}</span>` +
          `</button>`
      ).join("");

      this.shadowRoot.innerHTML =
        `<style>${this._styles()}</style>` +
        this._renderHaThemeStyle() +
        `<div class="root">` +
        `<aside class="${sidebarClass}">` +
        `<div class="sidebar-header">` +
        `<button class="collapse-btn" title="Toggle sidebar"><span>${
          this._collapsed ? "›" : "‹"
        }</span></button>` +
        `<span class="brand">💧 Irrigation</span>` +
        `</div>` +
        `<nav>${navItems}</nav>` +
        `</aside>` +
        `<main>${this._renderSection()}</main>` +
        `</div>` +
        (this._runModalOpen ? this._renderRunModal() : "") +
        (this._scheduleModalOpen ? this._renderScheduleModal() : "") +
        (this._sensorModalOpen ? this._renderSensorModal() : "") +
        (this._bannerModalOpen ? this._renderBannerSettingsModal() : "") +
        (this._establishmentModalOpen ? this._renderEstablishmentModal() : "");
    }

    _renderSection() {
      if (this._currentSection === "today") return this._renderToday();
      if (this._currentSection === "schedules") return this._renderSchedules();
      if (this._currentSection === "zones") return this._renderZones();
      if (this._currentSection === "yard") return this._renderYard();
      if (this._currentSection === "history") return this._renderHistory();
      if (this._currentSection === "sensors") return this._renderSensors();
      if (this._currentSection === "weather") return this._renderWeather();
      if (this._currentSection === "notifications") return this._renderNotifications();
      if (this._currentSection === "settings") return this._renderSettings();
      const section = SECTIONS.find((s) => s.id === this._currentSection) || {
        icon: "",
        label: "Section",
      };
      return (
        `<div class="placeholder">` +
        `<h2>${section.icon} ${escapeHtml(section.label)}</h2>` +
        `<p>Quick configuration UI lands in v1.2. For now, use Developer Tools → Services:</p>` +
        `<ul>` +
        (this._currentSection === "weather"
          ? `<li><code>complete_irrigation.set_weather_config</code> — bind rain sensor, hot weather boost</li>`
          : "") +
        (this._currentSection === "sensors"
          ? `<li><code>complete_irrigation.set_zone_moisture</code> — bind moisture sensor(s) per zone</li>`
          : "") +
        (this._currentSection === "notifications"
          ? `<li><code>complete_irrigation.set_notification_config</code> — notify target, quiet hours</li>` +
            `<li><code>complete_irrigation.test_notification</code> — verify routing</li>`
          : "") +
        (this._currentSection === "zones"
          ? `<li>Zones are configured at integration setup. Re-add via Settings → Devices &amp; Services to change.</li>`
          : "") +
        `</ul>` +
        `</div>`
      );
    }

    _renderNotifications() {
      const n = (this._config && this._config.notifications) || {};
      if (this._notifyDraft === null) this._hydrateNotifyDraft();
      const draft = this._notifyDraft || [];
      const qStart = n.quiet_hours_start || "22:00";
      const qEnd = n.quiet_hours_end || "07:00";
      const enabled = n.enabled !== false; // default true
      const lowMoistureAlerts = n.low_moisture_alerts !== false; // default true
      const notifyOnMissed = n.notify_on_missed !== false; // default true (v1.17)
      const notifyOnAborted = n.notify_on_aborted !== false; // default true (v1.19.0)
      // v1.19.0 — render the info bubble with a custom popover instead
      // of the native `title` attribute. The native tooltip is delayed
      // ~1.5s on desktop AND silently does NOTHING on touch devices.
      // The custom popover shows immediately on hover, on tap (touch
      // toggles via the .help-tip-open class set by _onClick), and on
      // keyboard focus. tabindex+role makes it screen-reader-aware.
      const tip = (text) =>
        `<span class="help-tip" role="button" tabindex="0" aria-label="${escapeAttr(text)}">` +
        `ⓘ<span class="help-tip-popup">${escapeHtml(text)}</span>` +
        `</span>`;

      // Build the dropdown of available notify.* services from HA. We
      // include any draft value that's NOT in the live list so users
      // can still see + remove a stale target whose integration was
      // unloaded.
      const notifyServices = Object.keys(
        (this._hass && this._hass.services && this._hass.services.notify) || {}
      )
        .map((s) => "notify." + s)
        .sort();
      const allOptions = new Set(notifyServices);
      for (const t of draft) {
        if (t) allOptions.add(t);
      }
      const optionList = Array.from(allOptions).sort();

      // One row per target — select dropdown + remove button.
      const rows = draft
        .map((target, idx) => {
          const isMissing = target && !notifyServices.includes(target);
          const optHtml = optionList
            .map(
              (opt) =>
                `<option value="${escapeAttr(opt)}"${
                  opt === target ? " selected" : ""
                }>${escapeHtml(opt)}${
                  isMissing && opt === target ? " (not loaded)" : ""
                }</option>`
            )
            .join("");
          return (
            `<div class="notify-target-row">` +
            `<select class="notify-target-select" data-action="notify-target-change" data-idx="${idx}">` +
            `<option value=""${target ? "" : " selected"}>— Pick a notify service —</option>` +
            optHtml +
            `</select>` +
            `<button type="button" class="btn btn-icon notify-target-remove" data-action="notify-target-remove" data-idx="${idx}" title="Remove this target">✕</button>` +
            `</div>`
          );
        })
        .join("");

      const noServicesHint =
        notifyServices.length === 0
          ? `<p class="section-hint" style="color:var(--ci-text-2)">No <code>notify.*</code> services found in this HA instance yet. Install the Home Assistant Companion app on your phone (or another notify integration) and they'll appear here.</p>`
          : "";

      return (
        `<header class="page-header"><h2>Notifications</h2>` +
        `<span class="version-pill">${PANEL_VERSION}</span></header>` +
        `<form class="weather-form" data-form="notifications">` +
        `<label class="enabled-check"><input type="checkbox" name="enabled"${
          enabled ? " checked" : ""
        } /> Notifications enabled ${tip("Master switch. Turn off to silence all push notifications without losing your config.")}</label>` +
        `<label>Notify targets ${tip("Pick one or more HA notify services. Every notification this integration sends will fan out to all of them.")}</label>` +
        `<div class="notify-target-list">${rows}</div>` +
        `<div class="notify-target-actions">` +
        `<button type="button" class="btn btn-small" data-action="notify-target-add">+ Add target</button>` +
        `</div>` +
        noServicesHint +
        `<h3 class="section-title">Quiet hours</h3>` +
        `<p class="section-hint">Non-urgent notifications received in this window are bundled into a single morning summary.</p>` +
        `<div class="row-2">` +
        `<div><label>Start ${tip("24h, e.g. 22:00")}</label><input name="quiet_hours_start" type="time" value="${escapeAttr(qStart)}" /></div>` +
        `<div><label>End ${tip("24h, e.g. 07:00 — morning summary fires at this time")}</label><input name="quiet_hours_end" type="time" value="${escapeAttr(qEnd)}" /></div>` +
        `</div>` +
        `<h3 class="section-title">Missed-run recovery</h3>` +
        `<label class="enabled-check"><input type="checkbox" name="notify_on_missed"${
          notifyOnMissed ? " checked" : ""
        } /> Notify when a scheduled run is skipped ${tip("Whenever the system drops a scheduled run (conflict resolver pushes it past its 2h deferral cap, a moisture/wind/rain gate skips it, or HA was down at the firing minute), a notification with a 'Run now' action button is sent. Tap the button to run the zone with its original planned duration. Only works on the Home Assistant Companion mobile app — other notify targets get plain text.")}</label>` +
        `<label class="enabled-check"><input type="checkbox" name="notify_on_aborted"${
          notifyOnAborted ? " checked" : ""
        } /> Notify when a scheduled run is cut short ${tip("When something outside this integration turns the zone switch off mid-run (controller safety timer, automation, manual toggle in HA, etc.) and the run ran less than 90% of its planned duration, send a notification with 'Run remainder' + 'Open Logbook' buttons. The Logbook button takes you straight to HA's audit trail filtered to that switch so you can see who/what turned it off.")}</label>` +
        `<h3 class="section-title">Daily low-moisture summary</h3>` +
        `<label class="enabled-check"><input type="checkbox" name="low_moisture_alerts"${
          lowMoistureAlerts ? " checked" : ""
        } /> Send a daily summary when any zone sensor is below its minimum ${tip("Fires once per day at quiet-hours-end. Lists every zone whose moisture sensor has dropped below the configured min%.")}</label>` +
        `<div class="modal-actions">` +
        `<button type="button" class="btn btn-secondary" data-action="test-notification">Send test</button>` +
        `<button type="submit" class="btn btn-primary">Save</button>` +
        `</div>` +
        `</form>`
      );
    }

    async _saveNotificationConfig(form) {
      const data = new FormData(form);
      // Targets come from the row-based editor draft, not a textarea.
      // Strip empty / whitespace-only slots and reject any that aren't
      // notify.<service> (the backend will also reject, but the alert
      // is friendlier here).
      const draft = Array.isArray(this._notifyDraft) ? this._notifyDraft : [];
      const targets = draft
        .map((t) => (typeof t === "string" ? t.trim() : ""))
        .filter(Boolean);
      const bad = targets.filter((t) => {
        const parts = t.split(".", 2);
        return parts.length !== 2 || parts[0] !== "notify" || !parts[1];
      });
      if (bad.length > 0) {
        return alert(
          `These targets aren't valid notify services and won't be saved:\n` +
            bad.map((s) => `  • ${s}`).join("\n") +
            `\n\nEach target must be of the form notify.<service>.`
        );
      }
      const payload = {
        // Send both for compat: notify_target = first entry (legacy
        // consumers), notify_targets = the full list (the dispatcher's
        // preferred path).
        notify_target: targets[0] || "",
        notify_targets: targets,
        quiet_hours_start: data.get("quiet_hours_start") || "22:00",
        quiet_hours_end: data.get("quiet_hours_end") || "07:00",
        enabled: form.querySelector('input[name="enabled"]').checked,
      };
      // low_moisture_alerts + notify_on_missed are stored alongside
      // the notification config but consumed by the coordinator, not
      // the dispatcher itself. Sent as a second service call so the
      // notify-targets validation above doesn't reject the whole
      // payload if those fields are missing.
      const lowToggle = form.querySelector('input[name="low_moisture_alerts"]');
      const missedToggle = form.querySelector('input[name="notify_on_missed"]');
      const abortedToggle = form.querySelector('input[name="notify_on_aborted"]');
      try {
        await this._hass.callService(
          "complete_irrigation",
          "set_notification_config",
          payload
        );
        const extras = {};
        if (lowToggle) extras.low_moisture_alerts = lowToggle.checked;
        if (missedToggle) extras.notify_on_missed = missedToggle.checked;
        if (abortedToggle) extras.notify_on_aborted = abortedToggle.checked;
        if (Object.keys(extras).length > 0) {
          await this._hass.callService(
            "complete_irrigation",
            "set_notification_config",
            extras
          );
        }
        await this._fetchConfig();
        // Invalidate the draft so the next render re-hydrates from the
        // freshly-loaded config (catches server-side filtering / dedupe).
        this._notifyDraft = null;
        alert("Notification config saved.");
      } catch (err) {
        alert("Failed to save: " + (err?.message || err));
      }
    }

    async _testNotification() {
      try {
        await this._hass.callService(
          "complete_irrigation",
          "test_notification",
          { message: "Hello from Complete Irrigation 🌱" }
        );
        alert(
          "Test sent. If you don't see it, check your notify target in HA Settings → Devices & Services."
        );
      } catch (err) {
        alert("Test failed: " + (err?.message || err));
      }
    }

    _renderSettings() {
      const c = this._config || {};
      const themeLabel =
        this._theme === "dark" ? "Dark" : this._theme === "light" ? "Light" : "Auto (follow HA)";
      // v1.19.0 — render the info bubble with a custom popover instead
      // of the native `title` attribute. The native tooltip is delayed
      // ~1.5s on desktop AND silently does NOTHING on touch devices.
      // The custom popover shows immediately on hover, on tap (touch
      // toggles via the .help-tip-open class set by _onClick), and on
      // keyboard focus. tabindex+role makes it screen-reader-aware.
      const tip = (text) =>
        `<span class="help-tip" role="button" tabindex="0" aria-label="${escapeAttr(text)}">` +
        `ⓘ<span class="help-tip-popup">${escapeHtml(text)}</span>` +
        `</span>`;
      const icalUrl = "/api/complete_irrigation/calendar.ics";
      const repoUrl = "https://github.com/HL-Apprentice/ha-complete-irrigation";

      const policy = c.conflict_policy || "defer_new";
      const zoneBuffer = c.zone_buffer_seconds != null ? c.zone_buffer_seconds : 30;
      const snoozedUntil = c.weekly_reminder_snoozed_until || "";
      const adminOnlyServices = !!c.admin_only_services;
      const policyOpt = (val, label) =>
        `<option value="${val}"${policy === val ? " selected" : ""}>${label}</option>`;

      return (
        `<header class="page-header"><h2>Settings</h2>` +
        `<span class="version-pill">${PANEL_VERSION}</span></header>` +
        `<section class="settings-card">` +
        `<h3 class="section-title">Theme ${tip("Cycle Light/Dark/Auto with the ☀️/🌙 button on Today, or pick one of your HA-installed themes below.")}</h3>` +
        `<p class="section-hint">Light/Dark/Auto: <strong>${escapeHtml(themeLabel)}</strong>.</p>` +
        `<form class="weather-form" data-form="ha-theme" style="background:transparent;border:none;padding:0;max-width:none">` +
        `<label>HA theme override</label>` +
        `<select name="ha_theme">` +
        `<option value=""${this._haTheme ? "" : " selected"}>— None (use Light/Dark above) —</option>` +
        Object.keys(this._haThemes || {})
          .sort()
          .map(
            (n) =>
              `<option value="${escapeAttr(n)}"${
                n === this._haTheme ? " selected" : ""
              }>${escapeHtml(n)}</option>`
          )
          .join("") +
        `</select>` +
        `<p class="section-hint" style="margin-top:6px">Picks from themes installed in your HA (Settings → Themes). Applies to this panel; HA's main UI is unaffected.</p>` +
        `<div class="modal-actions"><button type="submit" class="btn btn-primary">Apply theme</button></div>` +
        `</form>` +
        `</section>` +
        `<section class="settings-card">` +
        `<h3 class="section-title">Schedule conflicts ${tip("When two schedules' run windows overlap, this picks how the coordinator resolves them. Applies to all schedules.")}</h3>` +
        `<form class="weather-form" data-form="conflict-policy" style="background:transparent;border:none;padding:0;max-width:none">` +
        `<label>Policy</label>` +
        `<select name="policy">` +
        policyOpt("defer_new", "Defer the new one (skip overlapping new run) — safest") +
        policyOpt("shift_existing", "Shift existing earlier to make room") +
        policyOpt("split_difference", "Split the difference (both move equally apart)") +
        `</select>` +
        `<div class="modal-actions"><button type="submit" class="btn btn-primary">Save policy</button></div>` +
        `</form>` +
        `</section>` +
        // PRD #38 — configurable inter-zone buffer for multi-zone schedules
        `<section class="settings-card">` +
        `<h3 class="section-title">Schedule timing ${tip("Inter-zone valve-settle buffer for multi-zone schedules. Default 30s lets the previous valve close fully before the next opens.")}</h3>` +
        `<form class="weather-form" data-form="zone-buffer" style="background:transparent;border:none;padding:0;max-width:none">` +
        `<label>Inter-zone buffer (seconds)</label>` +
        `<input name="zone_buffer_seconds" type="number" min="0" max="600" step="1" value="${zoneBuffer}" />` +
        `<div class="modal-actions"><button type="submit" class="btn btn-primary">Save buffer</button></div>` +
        `</form>` +
        `</section>` +
        // PRD #81 — snooze the Sunday weekly reminder
        `<section class="settings-card">` +
        `<h3 class="section-title">Weekly reminder ${tip("Fires every Sunday at 8 AM with a per-zone summary. Snooze for 30 days if you're on vacation.")}</h3>` +
        (snoozedUntil
          ? `<p class="section-hint">Snoozed until <strong>${escapeHtml(snoozedUntil)}</strong>. <button class="btn btn-small" data-action="weekly-unsnooze" type="button">Resume now</button></p>`
          : `<p class="section-hint">Currently active.</p>`) +
        `<div class="modal-actions">` +
        `<button class="btn btn-secondary" type="button" data-action="weekly-snooze-30">Snooze 30 days</button>` +
        `</div>` +
        `</section>` +
        // v1.19.0 — admin-only services. Opt-in security hardening
        // for setups with non-admin HA users. The panel itself is
        // already admin-only (v1.15.0 S3) but the underlying services
        // default to "any authenticated user". Flipping this on makes
        // run_zone / stop_zone / schedule CRUD all admin-only too.
        `<section class="settings-card">` +
        `<h3 class="section-title">Security ${tip("Lock down hardware-actuating + data-mutating services to admin users only. Useful when you have non-admin HA accounts (kids, guests, dashboards) that you don't want triggering irrigation runs or editing schedules via Developer Tools.")}</h3>` +
        `<form class="weather-form" data-form="admin-only-services" style="background:transparent;border:none;padding:0;max-width:none">` +
        `<label class="enabled-check"><input type="checkbox" name="admin_only_services"${
          adminOnlyServices ? " checked" : ""
        } /> Restrict services to admin users ${tip("When ON: only HA admin accounts can call run_zone, stop_zone, add/update/delete schedules, edit weather/moisture config, change conflict policy, or test notifications via service calls. Non-admin user calls (from the panel by non-admins — although they can't see the panel — or from Developer Tools / scripts running under non-admin contexts) are rejected with a warning in the HA log. System-initiated calls (e.g. the 'Run now' button on missed-run notifications) always pass through. Default OFF for back-compat with existing scripts that run under non-admin contexts.")}</label>` +
        `<div class="modal-actions"><button type="submit" class="btn btn-primary">Save security setting</button></div>` +
        `</form>` +
        `</section>` +
        `<section class="settings-card">` +
        `<h3 class="section-title">Manual run default ${tip("How many minutes the Run Now popup prefills with. You can always override per-run.")}</h3>` +
        `<form class="weather-form" data-form="manual-default" style="background:transparent;border:none;padding:0;max-width:none">` +
        `<label>Default duration (minutes)</label>` +
        `<input name="manual_default" type="number" min="1" max="${MAX_MANUAL_MINUTES}" step="1" value="${this._userManualDefault()}" />` +
        `<div class="modal-actions"><button type="submit" class="btn btn-primary">Save default</button></div>` +
        `</form>` +
        `</section>` +
        `<section class="settings-card">` +
        `<h3 class="section-title">Calendar feed</h3>` +
        `<p class="section-hint">Subscribe from your phone's calendar app to see the next 30 days of planned runs.</p>` +
        `<div class="copy-row"><code>${escapeHtml(icalUrl)}</code><button class="btn btn-small" data-action="copy-ical">Copy</button></div>` +
        `</section>` +
        `<section class="settings-card">` +
        `<h3 class="section-title">About</h3>` +
        `<table class="settings-table">` +
        `<tr><td>Version</td><td><strong>${PANEL_VERSION}</strong></td></tr>` +
        `<tr><td>Repository</td><td><a href="${escapeAttr(repoUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(repoUrl)}</a></td></tr>` +
        `<tr><td>Zones configured</td><td>${(this._panel?.config?.zones || []).length}</td></tr>` +
        `<tr><td>Schedules</td><td>${(this._schedules || []).length}</td></tr>` +
        `</table>` +
        `<p class="section-hint" style="margin-top:12px">All configuration is also reachable via Developer Tools → Services — useful for advanced automations.</p>` +
        `</section>`
      );
    }

    async _saveConflictPolicy(form) {
      const policy = form.querySelector('select[name="policy"]')?.value;
      if (!policy) return;
      try {
        await this._hass.callService("complete_irrigation", "set_conflict_policy", {
          policy,
        });
        await this._fetchConfig();
        alert("Conflict policy saved.");
      } catch (err) {
        alert("Failed to save policy: " + (err?.message || err));
      }
    }

    async _saveZoneBuffer(form) {
      const seconds = parseInt(
        form.querySelector('input[name="zone_buffer_seconds"]')?.value,
        10
      );
      if (!Number.isFinite(seconds) || seconds < 0 || seconds > 600) {
        return alert("Buffer must be 0–600 seconds.");
      }
      try {
        await this._hass.callService(
          "complete_irrigation",
          "set_general_config",
          { zone_buffer_seconds: seconds }
        );
        await this._fetchConfig();
        alert(`Inter-zone buffer saved: ${seconds}s.`);
      } catch (err) {
        alert("Failed to save: " + (err?.message || err));
      }
    }

    async _saveAdminOnlyServices(form) {
      // v1.19.0 — opt-in gate on hardware/CRUD service calls. Default
      // off; flipping on requires admin context for run_zone / stop_zone
      // / schedule CRUD / etc. See coordinator-side _require_admin_if_configured.
      const checked = !!form.querySelector('input[name="admin_only_services"]')?.checked;
      try {
        await this._hass.callService("complete_irrigation", "set_general_config", {
          admin_only_services: checked,
        });
        await this._fetchConfig();
        alert(
          checked
            ? "Service-level admin gate enabled. Non-admin user calls to run_zone, stop_zone, schedule CRUD, etc. will now be rejected with a warning in the HA log."
            : "Service-level admin gate disabled. Any authenticated HA user can now call these services."
        );
      } catch (err) {
        alert("Failed to save: " + (err?.message || err));
      }
    }

    async _weeklySnooze(days) {
      // 0 days = resume (clear the snooze). Otherwise snooze N days from today.
      let payload;
      if (days <= 0) {
        payload = { weekly_reminder_snoozed_until: null };
      } else {
        const target = new Date();
        target.setDate(target.getDate() + days);
        // Format as YYYY-MM-DD without timezone shenanigans
        const iso =
          target.getFullYear() +
          "-" +
          String(target.getMonth() + 1).padStart(2, "0") +
          "-" +
          String(target.getDate()).padStart(2, "0");
        payload = { weekly_reminder_snoozed_until: iso };
      }
      try {
        await this._hass.callService(
          "complete_irrigation",
          "set_general_config",
          payload
        );
        await this._fetchConfig();
      } catch (err) {
        alert("Failed: " + (err?.message || err));
      }
    }

    _saveHaTheme(form) {
      const name = form.querySelector('select[name="ha_theme"]')?.value || "";
      this._haTheme = name;
      try {
        localStorage.setItem(HA_THEME_STORAGE_KEY, name);
      } catch (_) {}
      this._renderNow();
    }

    _userManualDefault() {
      try {
        const v = parseInt(localStorage.getItem(MANUAL_DEFAULT_STORAGE_KEY), 10);
        if (Number.isFinite(v) && v >= 1 && v <= MAX_MANUAL_MINUTES) return v;
      } catch (_) {}
      return DEFAULT_MANUAL_MINUTES;
    }

    _saveManualDefault(form) {
      const v = parseInt(form.querySelector('input[name="manual_default"]')?.value, 10);
      if (!Number.isFinite(v) || v < 1 || v > MAX_MANUAL_MINUTES) {
        return alert(`Manual run default must be 1-${MAX_MANUAL_MINUTES} min.`);
      }
      try {
        localStorage.setItem(MANUAL_DEFAULT_STORAGE_KEY, String(v));
      } catch (_) {}
      alert(`Manual run default saved: ${v} minutes.`);
    }

    async _copyICalUrl() {
      // Best-effort: copy to clipboard, fall back to alert with the URL.
      const url = window.location.origin + "/api/complete_irrigation/calendar.ics";
      try {
        await navigator.clipboard.writeText(url);
        alert("iCal feed URL copied:\n" + url);
      } catch (_) {
        prompt("Copy this URL:", url);
      }
    }

    _renderToday() {
      const allZones = this._zones();
      // Hidden zones are toggled from the Zones tab only — Today simply
      // filters them out of view. (PRD #4 — remove the hide/show
      // controls from Today entirely; this section title hints at the
      // workflow so users know where to find them.)
      const visibleZones = allZones.filter((z) => !this._hiddenZones.has(z.entityId));
      const hiddenCount = allZones.length - visibleZones.length;

      const themeBtn =
        `<button class="btn-icon theme-toggle" data-action="toggle-theme" title="Toggle light / dark">${
          this._effectiveTheme() === "dark" ? "☀️" : "🌙"
        }</button>`;
      return (
        `<header class="page-header"><h2>Today</h2>` +
        `<div class="page-header-right">${themeBtn}` +
        `<span class="version-pill">${PANEL_VERSION}</span></div></header>` +
        this._renderRainLockoutBanner() +
        this._renderMissedRunsBanner() +
        this._renderWeatherBanner() +
        this._renderDailyPlanCard() +
        `<section>` +
        `<div class="section-title-row">` +
        `<h3 class="section-title">Zones (${visibleZones.length})</h3>` +
        (hiddenCount > 0
          ? `<span class="section-hint" style="margin:0">${hiddenCount} zone(s) hidden — manage in the Zones tab.</span>`
          : "") +
        `</div>` +
        (visibleZones.length === 0
          ? this._renderEmpty()
          : `<div class="zone-grid">${visibleZones
              .map((z) => this._renderZoneTile(z))
              .join("")}</div>`) +
        `</section>` +
        this._renderDayCalendar()
      );
    }

    _renderMissedRunsBanner() {
      // v1.17 — surface today's skipped runs on the Today screen so
      // silent drops aren't hidden behind the History tab. Clicking the
      // banner navigates to the History tab filtered to today's skips.
      if (!Array.isArray(this._runHistory) || this._runHistory.length === 0) {
        return "";
      }
      const now = new Date();
      const todayStart = new Date(now);
      todayStart.setHours(0, 0, 0, 0);
      const startMs = todayStart.getTime();
      let count = 0;
      for (const r of this._runHistory) {
        if (r.status !== "skipped") continue;
        const ts = Date.parse(r.started_at);
        if (!isFinite(ts) || ts < startMs) continue;
        count++;
      }
      if (count === 0) return "";
      const label = count === 1 ? "1 run skipped today" : `${count} runs skipped today`;
      return (
        `<div class="missed-runs-banner" data-action="go-to-history-skipped" ` +
        `title="View today's skipped runs in the History tab" role="button" tabindex="0">` +
        `<span style="font-size:18px">⚠️</span>` +
        `<div style="flex:1">` +
        `<div style="font-weight:600">${label}</div>` +
        `<div style="font-size:12px;opacity:0.85">Tap to view details. If you enabled missed-run notifications, you should have received a "Run now?" alert on your phone.</div>` +
        `</div>` +
        `<span style="opacity:0.7">→</span>` +
        `</div>`
      );
    }

    _renderRainLockoutBanner() {
      const until = this._config?.lockout_until;
      if (!until) return "";
      const dt = new Date(until);
      if (isNaN(dt.getTime())) return "";
      if (dt < new Date()) return "";  // lockout already expired
      const timeStr = dt.toLocaleString(undefined, {
        weekday: "short",
        hour: "numeric",
        minute: "2-digit",
      });
      return (
        `<div class="rain-lockout-banner">` +
        `<span style="font-size:18px">🌧️</span>` +
        `<div style="flex:1">` +
        `<div style="font-weight:600">Rain lockout active</div>` +
        `<div style="font-size:12px;opacity:0.85">All watering paused until ${escapeHtml(timeStr)}</div>` +
        `</div>` +
        `</div>`
      );
    }

    _readSensor(entity_id) {
      if (!entity_id) return null;
      const state = this._hass?.states?.[entity_id];
      if (!state || state.state === "unknown" || state.state === "unavailable") return null;
      return state;
    }

    _buildBannerCells() {
      // Build a keyed map of all available banner cells from current
      // hass state + config. Returns {key: {icon, label, value}}.
      const out = {};
      const detected = this._autoDetectWeatherSensors();
      const sunState = this._readSensor("sun.sun");
      const weatherEntity = this._findWeatherEntity();

      if (weatherEntity) {
        const cond = WEATHER_CONDITION_MAP[weatherEntity.state] || {
          icon: "🌤️",
          label: weatherEntity.state || "Unknown",
        };
        out.condition = { icon: cond.icon, label: "Condition", value: cond.label };
      }

      const tempState =
        this._readSensor(this._config?.temperature_sensor) || detected.temperature;
      if (tempState) {
        const unit = tempState.attributes?.unit_of_measurement || "°";
        out.temp = { icon: "🌡️", label: "Temp", value: `${tempState.state}${unit}` };
      } else if (weatherEntity?.attributes?.temperature != null) {
        const unit = weatherEntity.attributes.temperature_unit || "°";
        out.temp = {
          icon: "🌡️",
          label: "Temp",
          value: `${weatherEntity.attributes.temperature}${unit}`,
        };
      }

      if (detected.feels_like) {
        const unit = detected.feels_like.attributes?.unit_of_measurement || "°";
        out.feels_like = {
          icon: "🤚",
          label: "Feels like",
          value: `${detected.feels_like.state}${unit}`,
        };
      }
      if (detected.humidity) {
        out.humidity = { icon: "💧", label: "Humidity", value: `${detected.humidity.state}%` };
      }
      // Heat index (NWS Rothfusz). Only meaningful at T ≥ 80°F + RH ≥ 40%.
      // Uses Fahrenheit because that's the dominant unit in the US/irrigation
      // context and what the rest of the integration assumes.
      const tempF = (() => {
        const t = tempState;
        if (!t) return weatherEntity?.attributes?.temperature ?? null;
        const unit = t.attributes?.unit_of_measurement || "";
        const raw = parseFloat(t.state);
        if (Number.isNaN(raw)) return null;
        if (unit === "°C" || unit === "C") return raw * 9 / 5 + 32;
        return raw;
      })();
      const humPct = detected.humidity ? parseFloat(detected.humidity.state) : NaN;
      if (
        Number.isFinite(tempF) &&
        Number.isFinite(humPct) &&
        tempF >= 80 &&
        humPct >= 40
      ) {
        const T = tempF;
        const R = humPct;
        const hi =
          -42.379 +
          2.04901523 * T +
          10.14333127 * R -
          0.22475541 * T * R -
          0.00683783 * T * T -
          0.05481717 * R * R +
          0.00122874 * T * T * R +
          0.00085282 * T * R * R -
          0.00000199 * T * T * R * R;
        out.heat_index = {
          icon: "🥵",
          label: "Feels (heat idx)",
          value: `${hi.toFixed(0)}°F`,
        };
      }
      if (detected.dew_point) {
        const unit = detected.dew_point.attributes?.unit_of_measurement || "°";
        out.dew_point = {
          icon: "🌫️",
          label: "Dew pt",
          value: `${detected.dew_point.state}${unit}`,
        };
      }

      // Rain — supports either legacy single `rain_sensor` or the new
      // multi-sensor `rain_sensors` array. Each bound sensor becomes
      // its own cell so the user sees all readings.
      const rainList =
        (this._config?.rain_sensors && this._config.rain_sensors.length
          ? this._config.rain_sensors
          : this._config?.rain_sensor
          ? [this._config.rain_sensor]
          : []) || [];
      const rainStates = rainList
        .map((eid) => this._readSensor(eid))
        .filter(Boolean);
      if (rainStates.length === 0 && detected.rain) rainStates.push(detected.rain);
      rainStates.forEach((s, i) => {
        const unit = s.attributes?.unit_of_measurement || "in";
        const friendly = s.attributes?.friendly_name || s.entity_id;
        out[`rain_${i}`] = {
          icon: "☔",
          label: rainStates.length > 1 ? friendly : "Rain",
          value: `${s.state} ${unit}`,
        };
      });

      if (detected.wind_speed) {
        const unit = detected.wind_speed.attributes?.unit_of_measurement || "mph";
        // Round to 1 decimal — Tempest etc. report 8-significant-digit
        // floats which are visually noisy on the banner.
        const fmt = (s) => {
          const n = parseFloat(s);
          return Number.isFinite(n) ? n.toFixed(1) : s;
        };
        let val = `${fmt(detected.wind_speed.state)} ${unit}`;
        if (detected.wind_gust)
          val += ` (gust ${fmt(detected.wind_gust.state)})`;
        out.wind = { icon: "💨", label: "Wind", value: val };
      }
      if (detected.uv) out.uv = { icon: "🔆", label: "UV index", value: detected.uv.state };
      if (detected.solar) {
        const unit = detected.solar.attributes?.unit_of_measurement || "W/m²";
        out.solar = { icon: "☀️", label: "Solar", value: `${detected.solar.state} ${unit}` };
      }
      if (detected.pressure) {
        const unit = detected.pressure.attributes?.unit_of_measurement || "";
        out.pressure = {
          icon: "📊",
          label: "Pressure",
          value: `${detected.pressure.state} ${unit}`,
        };
      }
      if (sunState) {
        const riseNext = sunState.attributes?.next_rising;
        const setNext = sunState.attributes?.next_setting;
        if (riseNext) {
          const dt = new Date(riseNext);
          out.sunrise = {
            icon: "🌄",
            label: "Sunrise",
            value: dt.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }),
          };
        }
        if (setNext) {
          const dt = new Date(setNext);
          out.sunset = {
            icon: "🌅",
            label: "Sunset",
            value: dt.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }),
          };
        }
      }
      const hotF = this._config?.hot_threshold_f;
      const boost = this._config?.boost_percent;
      if (hotF && boost) {
        out.hot_boost = {
          icon: "🔥",
          label: "Hot boost",
          value: `>${hotF}°F = +${boost}%`,
        };
      }
      return out;
    }

    _bannerOrderedKeys(available) {
      // Resolve which keys to render and in what order. User layout (if
      // any) is respected first; any newly-available keys not in their
      // saved layout are appended at the end so they're discoverable.
      const layout = this._bannerLayout || { visible: {}, order: [] };
      const visible = layout.visible || {};
      const order = layout.order || [];
      const seen = new Set();
      const resolved = [];
      for (const key of order) {
        if (available[key] && visible[key] !== false) {
          resolved.push(key);
          seen.add(key);
        }
      }
      // Append newly-detected keys not in the saved order
      for (const key of Object.keys(available)) {
        if (!seen.has(key) && visible[key] !== false) {
          resolved.push(key);
        }
      }
      return resolved;
    }

    _renderWeatherBanner() {
      const cellMap = this._buildBannerCells();
      const order = this._bannerOrderedKeys(cellMap);
      const gear =
        `<button class="btn-icon banner-gear" data-action="open-banner-settings" title="Customize what shows here">⚙️</button>`;

      if (order.length === 0) {
        // Empty state
        return (
          `<div class="weather-banner weather-banner-empty">` +
          `<span style="font-size:20px">🌤️</span>` +
          `<div style="flex:1">` +
          `<div style="font-weight:600;font-size:13px">No weather data found yet</div>` +
          `<div style="font-size:12px;color:var(--ci-text-2)">` +
          `Install a weather integration or bind sensors in the Weather tab.` +
          `</div></div>` +
          gear +
          `</div>`
        );
      }
      const cellsHtml = order
        .map((k) => this._weatherCell(cellMap[k].icon, cellMap[k].label, cellMap[k].value))
        .join("");
      return `<div class="weather-banner">${cellsHtml}${gear}</div>`;
    }

    _renderBannerSettingsModal() {
      // Show every cell that *could* be displayed (visible + hidden),
      // each row a checkbox + up/down arrows. Save persists to localStorage.
      const cellMap = this._buildBannerCells();
      const layout = this._bannerLayout || { visible: {}, order: [] };
      // Compose a stable list: existing order first, then unseen keys
      const seen = new Set();
      const list = [];
      for (const k of layout.order || []) {
        if (cellMap[k]) {
          list.push(k);
          seen.add(k);
        }
      }
      for (const k of Object.keys(cellMap)) {
        if (!seen.has(k)) list.push(k);
      }

      const rows = list
        .map(
          (k, i) =>
            `<div class="banner-row" data-key="${escapeAttr(k)}">` +
            `<label class="banner-row-check"><input type="checkbox" name="banner_visible" value="${escapeAttr(k)}"${
              layout.visible?.[k] === false ? "" : " checked"
            } /> <span>${escapeHtml(cellMap[k].icon)} ${escapeHtml(cellMap[k].label)}</span></label>` +
            `<div class="banner-row-arrows">` +
            `<button type="button" class="btn-icon" data-action="banner-up" data-key="${escapeAttr(k)}"${
              i === 0 ? " disabled" : ""
            }>▲</button>` +
            `<button type="button" class="btn-icon" data-action="banner-down" data-key="${escapeAttr(k)}"${
              i === list.length - 1 ? " disabled" : ""
            }>▼</button>` +
            `</div></div>`
        )
        .join("");

      return (
        `<div class="modal-backdrop"></div>` +
        `<div class="modal modal-wide" role="dialog" aria-modal="true">` +
        `<form class="modal-form banner-settings-form">` +
        `<h3>Customize weather banner</h3>` +
        `<p class="section-hint" style="margin:0 0 12px">Toggle cells and reorder with ▲▼. Changes save when you hit Done.</p>` +
        `<div class="banner-list">${rows}</div>` +
        `<div class="modal-actions">` +
        `<button type="button" class="btn btn-secondary modal-cancel">Cancel</button>` +
        `<button type="submit" class="btn btn-primary">Done</button>` +
        `</div>` +
        `</form>` +
        `</div>`
      );
    }

    _saveBannerLayout(form) {
      // Read the on-screen order + the on-screen checkbox state and
      // commit it to localStorage. Order is derived from current DOM
      // (after any ▲▼ presses), not from form data.
      const rows = form.querySelectorAll(".banner-row");
      const order = [];
      const visible = {};
      rows.forEach((r) => {
        const key = r.dataset.key;
        order.push(key);
        visible[key] = r.querySelector('input[type="checkbox"]').checked;
      });
      this._bannerLayout = { order, visible };
      try {
        localStorage.setItem(
          BANNER_LAYOUT_STORAGE_KEY,
          JSON.stringify(this._bannerLayout)
        );
      } catch (_) {}
      this._closeAllModals();
    }

    _bannerReorder(key, direction) {
      // Move `key` up (-1) or down (+1) in the current modal DOM.
      const form = this.shadowRoot.querySelector(".banner-settings-form");
      if (!form) return;
      const rows = Array.from(form.querySelectorAll(".banner-row"));
      const idx = rows.findIndex((r) => r.dataset.key === key);
      if (idx === -1) return;
      const target = idx + direction;
      if (target < 0 || target >= rows.length) return;
      const list = form.querySelector(".banner-list");
      if (direction < 0) list.insertBefore(rows[idx], rows[target]);
      else list.insertBefore(rows[target], rows[idx]);
      // Update disabled state on first/last arrows
      const newRows = Array.from(list.querySelectorAll(".banner-row"));
      newRows.forEach((r, i) => {
        const up = r.querySelector('[data-action="banner-up"]');
        const down = r.querySelector('[data-action="banner-down"]');
        if (up) up.disabled = i === 0;
        if (down) down.disabled = i === newRows.length - 1;
      });
    }

    async _fetchForecast(entityId) {
      // HA 2024+ deprecated weather.* attributes.forecast in favor of the
      // weather.get_forecasts service, which we invoke via call_service
      // with return_response: true. Response shape:
      //   { response: { "weather.xxx": { forecast: [...] } } }
      if (!this._hass?.callWS) return;
      try {
        const resp = await this._hass.callWS({
          type: "call_service",
          domain: "weather",
          service: "get_forecasts",
          service_data: { type: "daily" },
          target: { entity_id: entityId },
          return_response: true,
        });
        const days = resp?.response?.[entityId]?.forecast;
        if (Array.isArray(days)) {
          this._forecastCache[entityId] = days;
          this._renderNow();
        }
      } catch (err) {
        // Some entities don't support the daily forecast call — silent fallback.
        console.warn("[complete-irrigation] get_forecasts failed:", err?.message || err);
      }
    }

    _findWeatherEntity() {
      // Return the first weather.* entity that has a real condition state.
      // HA's weather component is the easiest way to get a current
      // condition string + a forecast attribute.
      if (!this._hass?.states) return null;
      for (const eid of Object.keys(this._hass.states)) {
        if (!eid.startsWith("weather.")) continue;
        const s = this._hass.states[eid];
        if (s && s.state && s.state !== "unknown" && s.state !== "unavailable") {
          return s;
        }
      }
      return null;
    }

    _autoDetectWeatherSensors() {
      const found = {};
      if (!this._hass?.states) return found;

      // Pattern keys to match in entity_ids (any case)
      const patterns = {
        temperature: [/^sensor\..*tempest.*temperature$/i, /^sensor\..*weatherflow.*temperature$/i, /^sensor\.outdoor_?temperature$/i, /^sensor\.outside_?temperature$/i],
        feels_like: [/^sensor\..*tempest.*feels_like$/i, /^sensor\..*weatherflow.*feels_like$/i, /^sensor\..*feels_like$/i],
        humidity: [/^sensor\..*tempest.*humidity$/i, /^sensor\..*weatherflow.*humidity$/i, /^sensor\.outdoor_?humidity$/i],
        dew_point: [/^sensor\..*tempest.*dew_?point$/i, /^sensor\..*dew_?point$/i],
        rain: [/^sensor\..*tempest.*rain_today$/i, /^sensor\..*precipitation_today$/i, /^sensor\..*rain_today$/i],
        wind_speed: [/^sensor\..*tempest.*wind_avg$/i, /^sensor\..*tempest.*wind_speed$/i, /^sensor\..*wind_speed$/i],
        wind_gust: [/^sensor\..*tempest.*wind_gust$/i, /^sensor\..*wind_gust$/i],
        uv: [/^sensor\..*tempest.*uv$/i, /^sensor\..*tempest.*uv_index$/i, /^sensor\..*uv_?index$/i],
        solar: [/^sensor\..*tempest.*solar_radiation$/i, /^sensor\..*solar_radiation$/i],
        pressure: [/^sensor\..*tempest.*pressure$/i, /^sensor\.barometric_?pressure$/i],
      };

      const ids = Object.keys(this._hass.states);
      for (const [key, regexList] of Object.entries(patterns)) {
        for (const eid of ids) {
          if (regexList.some((r) => r.test(eid))) {
            const state = this._hass.states[eid];
            if (state && state.state !== "unknown" && state.state !== "unavailable") {
              found[key] = state;
              break;
            }
          }
        }
      }
      return found;
    }

    _weatherCell(icon, label, value) {
      return (
        `<div class="weather-cell">` +
        `<span class="weather-cell-icon">${icon}</span>` +
        `<div class="weather-cell-body">` +
        `<div class="weather-cell-label">${escapeHtml(label)}</div>` +
        `<div class="weather-cell-value">${escapeHtml(value)}</div>` +
        `</div></div>`
      );
    }

    _renderEmpty() {
      return `<div class="empty"><p>No zones configured. Re-run setup from Settings → Devices & services.</p></div>`;
    }

    _groupHistoryRows(records) {
      // v1.30 — collapse a chunked run's consecutive "Base (block i/n)" records
      // into one session group. Records are newest-first, so a session reads as
      // n/n … 1/n (strictly descending index); a non-descending index or a
      // different (zone, base, n) starts a new session.
      const BLOCK_RE = /^(.*) \(block (\d+)\/(\d+)\)$/;
      const out = [];
      let group = null;
      const close = () => {
        if (group) {
          out.push(group);
          group = null;
        }
      };
      for (const r of records) {
        const m = (r.schedule_name || "").match(BLOCK_RE);
        if (!m) {
          close();
          out.push(r);
          continue;
        }
        const base = m[1];
        const idx = parseInt(m[2], 10);
        const n = parseInt(m[3], 10);
        const startMs = Date.parse(r.started_at);
        // A block joins the current group only if it's the same (zone, base, n),
        // its index is strictly lower (newest-first => descending within a
        // session), AND it started within ~2h of the group's earliest block.
        // The index check alone splits adjacent sessions (1 then n breaks
        // descending), but a status/zone filter can drop the boundary block and
        // leave two sessions' blocks adjacent with still-descending indices — the
        // time guard catches that (real consecutive blocks are <~1h apart).
        const closeInTime =
          group &&
          isFinite(startMs) &&
          isFinite(group.minStartMs) &&
          group.minStartMs - startMs <= 2 * 60 * 60 * 1000;
        if (
          group &&
          group.zone_entity_id === r.zone_entity_id &&
          group.base === base &&
          group.n === n &&
          idx < group.minIdx &&
          closeInTime
        ) {
          group.blocks.push(r);
          group.minIdx = idx;
          if (isFinite(startMs)) group.minStartMs = Math.min(group.minStartMs, startMs);
        } else {
          close();
          group = {
            session: true,
            base,
            n,
            minIdx: idx,
            minStartMs: isFinite(startMs) ? startMs : Infinity,
            zone_entity_id: r.zone_entity_id,
            zone_name: r.zone_name,
            schedule_id: r.schedule_id,
            blocks: [r],
          };
        }
      }
      close();
      return out;
    }

    _meaningfulTriggerKeys(triggers) {
      // v1.30 — keys of triggers that actually changed/gated the run. Hides the
      // coordinator's no-op breadcrumbs (a gate that was disabled, inapplicable,
      // or evaluated-but-took-no-action) so History isn't cluttered with
      // "moisture, wind, hot_weather" on every row.
      if (!triggers || typeof triggers !== "object") return [];
      const keep = [];
      for (const key of Object.keys(triggers)) {
        const v = triggers[key] || {};
        if (v.disabled_by_config || v.all_sensors_excluded || v.ignored_by_schedule) continue;
        if (key === "wind" && !v.deferred) continue; // evaluated but didn't defer
        if (key === "hot_weather" && !v.boost_applied) continue; // evaluated but no boost
        if (key === "moisture") {
          const acted = v.decision === "skip" || v.decision === "skip_no_reading" || v.adjusted_minutes != null;
          if (!acted) continue; // passed/ok with no adjustment — no effect
        }
        keep.push(key);
      }
      return keep;
    }

    _renderZoneTile(zone) {
      const isHidden = this._hiddenZones.has(zone.entityId);
      const countdown = this._localRuns[zone.entityId];
      const remainingMs = countdown ? Math.max(0, countdown - Date.now()) : 0;
      const isCountingDown = remainingMs > 0 && zone.on;
      const totalMinutes = this._localRunDurations[zone.entityId];

      // Countdown text is wrapped in <span data-countdown-for> so the
      // 1Hz tick can update its textContent without rebuilding any DOM
      // (avoids the v1.3.0 flicker + shifted-click-target regression).
      const cdSpan = isCountingDown
        ? `<span data-countdown-for="${escapeAttr(zone.entityId)}">${_formatRemaining(remainingMs)}</span>`
        : "";
      // "of 10 min" suffix when we know the original run length
      const totalLabel = isCountingDown && totalMinutes
        ? ` of ${totalMinutes} min`
        : "";

      // v1.30 — the raw switch reads OFF during a chunked run's inter-block gaps
      // and during Rachio state-poll lag, which made the card say "Idle" while a
      // run was actually in progress. Use the active SESSION (whole-run, spans
      // gaps) as the truth — NOT the schedule projection, which would also light
      // up for a rain/moisture/wind-GATED run that never fired.
      const sess = this._activeSessions[zone.entityId];
      const sessionActive = sess != null && sess > Date.now();
      const runningOffSwitch = !zone.on && zone.available && sessionActive;
      const showRunning = zone.on || runningOffSwitch;

      let statusClass, statusLabel;
      if (!zone.available) {
        statusClass = "unavailable";
        statusLabel = "Unavailable";
      } else if (zone.on) {
        statusClass = "running";
        statusLabel = isCountingDown
          ? `Running — ${cdSpan} left${totalLabel}`
          : "Running";
      } else if (runningOffSwitch) {
        statusClass = "running";
        statusLabel = "Running"; // active session — switch off between blocks
      } else {
        statusClass = "idle";
        statusLabel = "Idle";
      }

      const action = showRunning
        ? `<button class="btn btn-stop" data-action="stop" data-entity-id="${escapeAttr(
            zone.entityId
          )}">⏹ Stop${isCountingDown ? " (" + cdSpan + ")" : ""}</button>`
        : `<button class="btn btn-run" data-action="run-now" data-entity-id="${escapeAttr(
            zone.entityId
          )}" data-zone-name="${escapeAttr(zone.name)}"${
            zone.available ? "" : " disabled"
          }>▶ Run Now</button>`;

      // v1.19.0 — moisture min/avg/max at a glance (Today screen).
      const cfg = this._config?.zones?.[zone.entityId] || {};
      const moistureStats = this._renderZoneMoistureStats(cfg);

      // PRD #4 — hide/show is managed in the Zones tab only.
      return (
        `<article class="zone-tile${isHidden ? " zone-hidden" : ""}">` +
        `<header>` +
        `<span class="status-dot ${statusClass}"></span>` +
        `<h4>${escapeHtml(zone.name)}</h4>` +
        `</header>` +
        `<div class="status-text">${statusLabel}</div>` +
        moistureStats +
        `<div class="zone-actions">${action}</div>` +
        `</article>`
      );
    }

    _renderZoneMoistureStats(zoneCfg) {
      // v1.19.0 — compact moisture summary for the Today zone tile.
      // Shows avg / min / max of the live readings across the zone's
      // bound moisture sensors. Single-sensor zones show just the
      // value (min = avg = max). No sensors → nothing rendered.
      const moistures = this._readPercentSensors(zoneCfg.moisture_entities || []);
      if (moistures.length === 0) return "";
      // v1.19 — per-sensor analysis opt-out: stats reflect what the
      // gate / auto-soak actually act on (in-analysis sensors only);
      // excluded sensors stay visible in the tooltip, marked.
      const excludedSet = new Set(zoneCfg.moisture_excluded || []);
      const inAnalysis = moistures.filter((m) => !excludedSet.has(m.entity_id));
      const statsSource = inAnalysis.length > 0 ? inAnalysis : moistures;
      const allExcluded = inAnalysis.length === 0;
      const vals = statsSource.map((m) => m.value);
      const min = Math.min(...vals);
      const max = Math.max(...vals);
      const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
      // v1.19.0 — when the moisture gate is disabled for this zone the
      // readings are display-only: never flag "low" (the gate won't act
      // on it) and say so in the line + tooltip.
      const gateOff = !!zoneCfg.moisture_disabled;
      // Low when the gate would consider this dry (avg below configured min).
      const minPct = zoneCfg.min_pct;
      const low = !gateOff && !allExcluded && minPct != null && avg < minPct;
      const cls = `zone-moisture${low ? " zone-moisture-low" : ""}`;
      const tip =
        moistures
          .map(
            (m) =>
              `${m.friendly}: ${m.value.toFixed(1)}%` +
              (excludedSet.has(m.entity_id) ? " (excluded from analysis)" : "")
          )
          .join("\n") +
        (gateOff ? "\n\n(display only — moisture is ignored for watering decisions)" : "") +
        (allExcluded && !gateOff
          ? "\n\n(all sensors excluded from analysis — readings shown but not acted on)"
          : "");
      const offBadge = gateOff
        ? `<span class="zone-moisture-band"> · gate off</span>`
        : allExcluded
        ? `<span class="zone-moisture-band"> · not used</span>`
        : "";

      if (statsSource.length === 1) {
        return (
          `<div class="${cls}" title="${escapeAttr(tip)}">` +
          `💧 ${avg.toFixed(0)}%` +
          (!gateOff && !allExcluded && minPct != null
            ? `<span class="zone-moisture-band"> (min ${minPct}%)</span>`
            : "") +
          offBadge +
          `</div>`
        );
      }
      return (
        `<div class="${cls}" title="${escapeAttr(tip)}">` +
        `💧 <strong>${avg.toFixed(0)}%</strong> avg` +
        `<span class="zone-moisture-band"> · ${min.toFixed(0)} min · ${max.toFixed(0)} max` +
        ` · ${statsSource.length} sensors</span>` +
        offBadge +
        `</div>`
      );
    }

    // ── Zones tab ──────────────────────────────────────────────────
    _renderZones() {
      const zones = this._zones();
      if (zones.length === 0) {
        return (
          `<header class="page-header"><h2>Zones</h2>` +
          `<span class="version-pill">${PANEL_VERSION}</span></header>` +
          `<div class="empty"><p>No zones configured. Add them via Settings → Devices &amp; Services.</p></div>`
        );
      }
      const rows = zones
        .map((z, i) => this._renderZoneRow(z, i, zones.length))
        .join("");
      return (
        `<header class="page-header"><h2>Zones</h2>` +
        `<span class="version-pill">${PANEL_VERSION}</span></header>` +
        `<p class="section-hint">Hidden zones still run on schedule — they're just hidden from the Today view.</p>` +
        `<div class="zones-list">${rows}</div>`
      );
    }

    _renderZoneRow(zone, index = 0, total = 1) {
      const isHidden = this._hiddenZones.has(zone.entityId);
      const dayStrip = this._renderZone7DayStrip(zone.entityId);
      const statusClass = !zone.available
        ? "unavailable"
        : zone.on
        ? "running"
        : "idle";
      const statusLabel = !zone.available
        ? "Unavailable"
        : zone.on
        ? "Running"
        : "Idle";
      const hideBtn = isHidden
        ? `<button class="btn btn-small" data-action="show-zone" data-entity-id="${escapeAttr(zone.entityId)}">👁️ Show in Today</button>`
        : `<button class="btn btn-small" data-action="hide-zone" data-entity-id="${escapeAttr(zone.entityId)}">🚫 Hide from Today</button>`;
      const grassBtn =
        `<button class="btn btn-small" data-action="open-establishment" data-entity-id="${escapeAttr(zone.entityId)}" data-zone-name="${escapeAttr(zone.name)}">🌱 New Planting</button>`;
      // v1.35 — advisory watering diagnosis. Toggles the panel under the row.
      const diagBtn =
        `<button class="btn btn-small" data-action="zone-diagnose" data-entity-id="${escapeAttr(zone.entityId)}">🩺 Diagnose</button>`;
      // Reorder controls. ↑ disabled on first row, ↓ disabled on last.
      // They apply to both Today and Zones (shared zone_order config).
      const upBtn =
        `<button class="btn-icon zone-reorder" data-action="zone-move-up" data-entity-id="${escapeAttr(zone.entityId)}" title="Move up"${index === 0 ? " disabled" : ""}>▲</button>`;
      const downBtn =
        `<button class="btn-icon zone-reorder" data-action="zone-move-down" data-entity-id="${escapeAttr(zone.entityId)}" title="Move down"${index >= total - 1 ? " disabled" : ""}>▼</button>`;

      // Climate chips (temp / humidity / moisture) — show whatever the
      // user has bound to this zone. Each chip shows the averaged value
      // across the bound sensors of that kind.
      const cfg = this._config?.zones?.[zone.entityId] || {};
      const climateChips = this._renderZoneClimateChips(cfg);

      return (
        `<article class="zone-row${isHidden ? " zone-row-hidden" : ""}">` +
        `<div class="zone-row-main">` +
        `<span class="status-dot ${statusClass}"></span>` +
        `<div class="zone-row-text">` +
        `<div class="zone-row-name">${escapeHtml(zone.name)}${isHidden ? ' <span class="zone-row-badge">HIDDEN</span>' : ""}</div>` +
        `<div class="zone-row-meta">${escapeHtml(zone.entityId)} • ${statusLabel}</div>` +
        (climateChips ? `<div class="zone-row-climate">${climateChips}</div>` : "") +
        `</div></div>` +
        `<div class="zone-row-strip">${dayStrip}</div>` +
        `<div class="zone-row-actions">` +
        `<div class="zone-reorder-group">${upBtn}${downBtn}</div>` +
        grassBtn +
        diagBtn +
        hideBtn +
        `</div>` +
        this._renderZoneDiagnosis(zone.entityId) +
        `</article>`
      );
    }

    _renderZoneDiagnosis(entityId) {
      // v1.35 — advisory over/under-watering read-out, expanded under the
      // zone row after a 🩺 Diagnose click. Purely informational.
      const d = this._zoneDiagnosis[entityId];
      if (!d) return "";
      if (d.loading) {
        return `<div class="zone-diag"><span class="zone-diag-loading">Running diagnosis…</span></div>`;
      }
      const meta = {
        ok: { cls: "ok", label: "No watering issues detected" },
        possible_overwatering: { cls: "warn", label: "Possible overwatering" },
        possible_underwatering: { cls: "warn", label: "Possible underwatering" },
      };
      const m = meta[d.status] || { cls: "none", label: "Not enough data to diagnose" };
      const list = (title, arr) =>
        Array.isArray(arr) && arr.length
          ? `<div class="zone-diag-block"><span class="zone-diag-sub">${title}</span><ul>` +
            arr.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("") +
            `</ul></div>`
          : "";
      return (
        `<div class="zone-diag">` +
        `<div class="zone-diag-head zone-diag-${m.cls}">${escapeHtml(m.label)}</div>` +
        list("Signs", d.signs) +
        list("How to confirm", d.confirm) +
        list("Suggestions", d.suggestions) +
        `<span class="zone-diag-foot">Advisory only — no schedule was changed.</span>` +
        `</div>`
      );
    }

    _renderZoneClimateChips(zoneCfg) {
      const chips = [];
      const moistureIds = zoneCfg.moisture_entities || [];
      const moistures = this._readPercentSensors(moistureIds);
      if (moistures.length > 0) {
        // v1.19 — combine from in-analysis sensors only; excluded ones
        // stay in the tooltip, marked.
        const exSet = new Set(zoneCfg.moisture_excluded || []);
        const inAnalysis = moistures.filter((m) => !exSet.has(m.entity_id));
        const source = inAnalysis.length > 0 ? inAnalysis : moistures;
        const combined = this._combineReadings(source, zoneCfg.combine_mode);
        const minPct = zoneCfg.min_pct;
        const low =
          inAnalysis.length > 0 && minPct != null && combined !== null && combined < minPct;
        chips.push(
          `<span class="zone-chip${low ? " zone-chip-low" : ""}" title="${escapeAttr(
            moistures
              .map(
                (m) =>
                  `${m.friendly}: ${m.value.toFixed(1)}%` +
                  (exSet.has(m.entity_id) ? " (excluded from analysis)" : "")
              )
              .join("\n")
          )}">💧 ${combined.toFixed(0)}%${source.length > 1 ? ` (${source.length})` : ""}</span>`
        );
      }

      // v1.15 — if the user hasn't explicitly bound temp/humidity but
      // their bound moisture sensors have sibling entities on the same
      // device (typical for multi-purpose soil sensors that report
      // moisture + temperature + humidity), fall back to those for
      // display. Marked with "(auto)" in the hover tooltip so the
      // distinction is visible.
      let tempEids = zoneCfg.temperature_entities || [];
      let tempAuto = false;
      if (tempEids.length === 0 && moistureIds.length > 0) {
        tempEids = this._findSiblingSensors(moistureIds, "temperature");
        tempAuto = tempEids.length > 0;
      }
      if (tempEids.length > 0) {
        const readings = tempEids
          .map((eid) => this._readSensor(eid))
          .filter(Boolean)
          .map((s) => ({
            value: parseFloat(s.state),
            unit: s.attributes?.unit_of_measurement || "°",
            friendly: s.attributes?.friendly_name || s.entity_id,
          }))
          .filter((r) => !Number.isNaN(r.value));
        if (readings.length > 0) {
          const avg = readings.reduce((a, b) => a + b.value, 0) / readings.length;
          const unit = readings[0].unit;
          const tipLines = readings
            .map((r) => `${r.friendly}: ${r.value.toFixed(1)}${unit}`)
            .concat(
              tempAuto
                ? ["", "(auto-detected from moisture sensor device)"]
                : []
            )
            .join("\n");
          chips.push(
            `<span class="zone-chip${tempAuto ? " zone-chip-auto" : ""}" title="${escapeAttr(tipLines)}">🌡️ ${avg.toFixed(0)}${unit}${readings.length > 1 ? ` (${readings.length})` : ""}</span>`
          );
        }
      }

      // Same fallback for humidity
      let humEids = zoneCfg.humidity_entities || [];
      let humAuto = false;
      if (humEids.length === 0 && moistureIds.length > 0) {
        humEids = this._findSiblingSensors(moistureIds, "humidity");
        humAuto = humEids.length > 0;
      }
      if (humEids.length > 0) {
        const reads = humEids
          .map((eid) => this._readSensor(eid))
          .filter(Boolean)
          .map((s) => ({
            value: parseFloat(s.state),
            friendly: s.attributes?.friendly_name || s.entity_id,
          }))
          .filter((r) => !Number.isNaN(r.value));
        if (reads.length > 0) {
          const avg = reads.reduce((a, b) => a + b.value, 0) / reads.length;
          const tipLines = reads
            .map((r) => `${r.friendly}: ${r.value.toFixed(1)}%`)
            .concat(
              humAuto
                ? ["", "(auto-detected from moisture sensor device)"]
                : []
            )
            .join("\n");
          chips.push(
            `<span class="zone-chip${humAuto ? " zone-chip-auto" : ""}" title="${escapeAttr(tipLines)}">💨 ${avg.toFixed(0)}%${reads.length > 1 ? ` (${reads.length})` : ""}</span>`
          );
        }
      }
      return chips.join("");
    }

    _findSiblingSensors(moistureIds, kind) {
      // Given a list of moisture entity_ids and a target kind
      // ("temperature" | "humidity"), return live entity_ids that look
      // like siblings on the same device. Pure name-substitution: we
      // swap the word "moisture" for "temperature"/"humidity" (and a
      // few common variants) and check if the resulting entity exists
      // in HA's state. No device-registry lookup required.
      if (!this._hass?.states) return [];
      const wordMap = {
        temperature: ["temperature", "temp"],
        humidity: ["humidity", "humid"],
      };
      const targets = wordMap[kind] || [];
      const found = new Set();
      for (const moistureId of moistureIds) {
        if (typeof moistureId !== "string" || !/moisture/i.test(moistureId)) continue;
        for (const word of targets) {
          // Replace "moisture" (case-insensitive) with the target word
          const candidate = moistureId.replace(/moisture/gi, word);
          if (candidate === moistureId) continue;
          if (this._hass.states[candidate]) {
            found.add(candidate);
          }
        }
      }
      return Array.from(found);
    }

    _renderZone7DayStrip(entityId) {
      // Next 7 days starting today. For each day, list any schedule
      // firings on this zone in HH:MM form. Hover shows full details.
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      const cells = [];
      for (let i = 0; i < 7; i++) {
        const day = new Date(today.getTime() + i * 86400000);
        const fires = this._schedulesFiringOn(entityId, day);
        const dayLabel = WEEKDAY_LABELS[(day.getDay() + 6) % 7]; // shift Sun=0 -> Mon=0
        const dateLabel = day.getDate();
        const hasFires = fires.length > 0;
        const tooltip = hasFires
          ? fires.map((f) => `${f.start_time} — ${f.name}`).join("\n")
          : "No runs scheduled";
        const dots = hasFires
          ? `<div class="zone-day-dots">${fires
              .slice(0, 3)
              .map(() => '<span class="zone-day-dot"></span>')
              .join("")}${fires.length > 3 ? `<span class="zone-day-more">+${fires.length - 3}</span>` : ""}</div>`
          : `<div class="zone-day-dots zone-day-empty">·</div>`;
        cells.push(
          `<div class="zone-day${hasFires ? " zone-day-on" : ""}${i === 0 ? " zone-day-today" : ""}" title="${escapeAttr(tooltip)}">` +
            `<div class="zone-day-label">${dayLabel}</div>` +
            `<div class="zone-day-date">${dateLabel}</div>` +
            dots +
            `</div>`
        );
      }
      return cells.join("");
    }

    _schedulesFiringOn(zoneEntityId, dayDate) {
      // v1.16 — derived from the cached server-side planner output
      // instead of the in-JS reimplementation (which ignored start_date,
      // repeat_annually, configurable zone_buffer, conflict resolution).
      const runs = this._runsForDay(dayDate);
      const seen = new Set();
      const result = [];
      for (const r of runs) {
        if (r.zone_entity_id !== zoneEntityId) continue;
        // Dedupe per-schedule per-day so the strip doesn't show one row
        // per cycle for hourly-interval schedules.
        const key = `${r.schedule_id}@${r.start_minutes}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const hh = String(Math.floor(r.start_minutes / 60)).padStart(2, "0");
        const mm = String(r.start_minutes % 60).padStart(2, "0");
        result.push({ start_time: `${hh}:${mm}`, name: r.schedule_name });
      }
      return result;
    }

    _runsForDay(day) {
      // v1.16 — reads from the server-resolved planner output that's
      // cached in this._plannedRunsByDate, keyed by local YYYY-MM-DD.
      // Returns an empty array until the first WS response arrives; the
      // _scheduleRender callback inside _fetchPlannedRuns will trigger
      // a fresh render then.
      const key = this._localDateKey(day);
      return (this._plannedRunsByDate.get(key) || []).slice();
    }

    _renderDayCalendar() {
      // Vertical day calendar — 2-day window so users see today + tomorrow
      // at a glance. ← / → shift the window by one day; "Today" snaps back.
      // Layout: two columns on desktop, stacked on mobile (CSS handles).
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      const offset = this._calendarDayOffset || 0;
      const dayA = new Date(today.getTime() + offset * 86400000);
      const dayB = new Date(today.getTime() + (offset + 1) * 86400000);

      const navBar =
        `<div class="day-cal-nav">` +
        `<button class="btn btn-small" data-action="day-cal-prev" title="Previous day">←</button>` +
        `<span class="day-cal-label">${escapeHtml(this._dayLabel(dayA, offset))} <span class="day-cal-arrow">→</span> ${escapeHtml(this._dayLabel(dayB, offset + 1))}</span>` +
        `<button class="btn btn-small" data-action="day-cal-next" title="Next day">→</button>` +
        (offset !== 0
          ? `<button class="btn btn-small" data-action="day-cal-today">Today</button>`
          : "") +
        `</div>`;

      return (
        `<section class="day-cal">` +
        navBar +
        `<div class="day-cal-cols">` +
        this._renderDayColumn(dayA, offset) +
        this._renderDayColumn(dayB, offset + 1) +
        `</div>` +
        `</section>`
      );
    }

    _dayLabel(date, offset) {
      const fmtDate = (d) =>
        d.toLocaleDateString(undefined, {
          weekday: "long",
          month: "short",
          day: "numeric",
        });
      if (offset === 0) return `Today — ${fmtDate(date)}`;
      if (offset === 1) return `Tomorrow — ${fmtDate(date)}`;
      if (offset === -1) return `Yesterday — ${fmtDate(date)}`;
      return fmtDate(date);
    }

    _renderDayColumn(selected, offset) {
      // One 24-hour vertical grid for a single day. Used twice by
      // _renderDayCalendar to show a 2-day window.
      const runs = this._runsForDay(selected);
      const fmtTime = fmtTimeOfDay;  // module-level shared helper (v1.16)

      const now = new Date();
      const nowMin = now.getHours() * 60 + now.getMinutes();
      const isToday = offset === 0;

      // 24-hour time grid markers — hour line solid (top border on each
      // .day-cal-hour), half-hour line at 50% opacity via ::after pseudo.
      const hourLabels = [];
      for (let h = 0; h < 24; h++) {
        const ampm = h >= 12 ? "PM" : "AM";
        const h12 = h % 12 || 12;
        hourLabels.push(
          `<div class="day-cal-hour" style="top:${h * 60}px"><span class="day-cal-hour-label">${h12} ${ampm}</span></div>`
        );
      }
      const hours = hourLabels.join("");

      // Pills — absolutely positioned, 1px per minute, min 18px tall.
      const pills = runs
        .map((r) => {
          const top = r.start_minutes;
          const height = Math.max(18, r.duration_minutes);
          let cls = "day-cal-pill";
          let status = "";
          if (isToday) {
            const inWindow =
              r.start_minutes <= nowMin && nowMin < r.start_minutes + r.duration_minutes;
            // v1.30 — only claim "Running now" when an active SESSION confirms the
            // zone is actually watering (same truth the Today card uses). A run
            // gated off at fire time (rain/moisture/wind) sits in its planned
            // window but never created a session, so it shows "Scheduled", not
            // "Running now" — keeping the card and calendar in agreement.
            const sess = this._activeSessions[r.zone_entity_id];
            const sessionActive = sess != null && sess > Date.now();
            if (r.start_minutes + r.duration_minutes < nowMin) {
              cls += " past";
              status = " · Past";
            } else if (inWindow && sessionActive) {
              cls += " live";
              status = " · Running now";
            } else if (inWindow) {
              status = " · Scheduled";
            }
          }
          const endMin = r.start_minutes + r.duration_minutes;
          // Hover-card payload: split into structured data-attrs so the
          // tooltip element can render rich, multi-line info instantly
          // (native title="..." takes ~1s and can't be styled).
          const hoverTitle = `${r.schedule_name} → ${r.zone_name}`;
          const hoverWhen = `${fmtTime(r.start_minutes)} – ${fmtTime(endMin)} (${r.duration_minutes} min)`;
          const hoverHint = "Click to edit schedule";
          // v1.18 — tint by schedule color (skip when past/live so those
          // status colors stay readable). Inline style overrides the
          // default .day-cal-pill accent background.
          const colorStyle =
            r.color && !cls.includes("past") && !cls.includes("live")
              ? `;background:${escapeAttr(r.color)}` // escape to match the file's convention (can't break the style attr)
              : "";
          return (
            `<div class="${cls}" style="top:${top}px;height:${height}px${colorStyle}" ` +
            `data-action="open-schedule-edit" data-schedule-id="${escapeAttr(r.schedule_id)}" ` +
            `data-hover-title="${escapeAttr(hoverTitle)}" ` +
            `data-hover-when="${escapeAttr(hoverWhen)}" ` +
            `data-hover-hint="${escapeAttr(hoverHint)}${status ? " · " + status.replace(/^ · /, "") : ""}" ` +
            // keep native title as accessibility fallback
            `title="${escapeAttr(hoverTitle + "\n" + hoverWhen + "\n" + hoverHint)}">` +
            `<div class="day-cal-pill-time">${fmtTime(r.start_minutes)}</div>` +
            `<div class="day-cal-pill-zone">${escapeHtml(r.zone_name)}</div>` +
            `<div class="day-cal-pill-meta">${escapeHtml(r.schedule_name)} · ${r.duration_minutes}m${status}</div>` +
            `</div>`
          );
        })
        .join("");

      // v1.19.0 — more visible now-line: 3px red line + a labeled chip
      // pinned to the left edge showing the current time. The chip is
      // a child of the line so positioning is automatic. Pulses every
      // 2s so the eye catches it even on a dense calendar.
      const nowMarker = isToday
        ? `<div class="day-cal-now" style="top:${nowMin}px" title="Now: ${fmtTime(nowMin)}">` +
          `<span class="day-cal-now-label">${fmtTime(nowMin)}</span>` +
          `</div>`
        : "";

      const emptyHint =
        runs.length === 0
          ? `<div class="day-cal-empty-hint">No runs scheduled.</div>`
          : "";

      const colHead =
        `<div class="day-cal-col-head">` +
        `<span class="day-cal-col-title">${escapeHtml(this._dayLabel(selected, offset))}</span>` +
        `<span class="day-cal-col-count">${runs.length} run${runs.length === 1 ? "" : "s"}</span>` +
        `</div>`;

      return (
        `<div class="day-cal-col">` +
        colHead +
        `<div class="day-cal-grid">` +
        `<div class="day-cal-hours">${hours}</div>` +
        `<div class="day-cal-pills">${nowMarker}${pills}${emptyHint}</div>` +
        `</div>` +
        `</div>`
      );
    }

    // ── History tab ────────────────────────────────────────────────
    _renderHistory() {
      const all = Array.isArray(this._runHistory) ? this._runHistory : [];
      const f = this._historyFilters;
      const now = new Date();
      const cutoff = f.days > 0 ? now.getTime() - f.days * 86400000 : 0;

      // Distinct zones + schedules for the filter dropdowns. Built from the
      // record set so filters auto-prune as old records age out.
      const zoneSet = new Map();
      const scheduleSet = new Map();
      for (const r of all) {
        if (r.zone_entity_id && !zoneSet.has(r.zone_entity_id)) {
          zoneSet.set(r.zone_entity_id, r.zone_name || r.zone_entity_id);
        }
        if (r.schedule_id && !scheduleSet.has(r.schedule_id)) {
          scheduleSet.set(r.schedule_id, r.schedule_name || r.schedule_id);
        }
      }
      const zoneOptions = Array.from(zoneSet.entries())
        .sort((a, b) => a[1].localeCompare(b[1]))
        .map(([id, name]) =>
          `<option value="${escapeAttr(id)}" ${f.zone === id ? "selected" : ""}>${escapeHtml(name)}</option>`
        )
        .join("");
      const scheduleOptions = Array.from(scheduleSet.entries())
        .sort((a, b) => a[1].localeCompare(b[1]))
        .map(([id, name]) =>
          `<option value="${escapeAttr(id)}" ${f.schedule === id ? "selected" : ""}>${escapeHtml(name)}</option>`
        )
        .join("");

      // Apply filters
      const filtered = all.filter((r) => {
        if (f.zone && r.zone_entity_id !== f.zone) return false;
        if (f.schedule && r.schedule_id !== f.schedule) return false;
        if (f.status && r.status !== f.status) return false;
        if (cutoff > 0) {
          const started = Date.parse(r.started_at);
          if (isFinite(started) && started < cutoff) return false;
        }
        return true;
      });

      const statusBadge = (s) => {
        const cls = `history-status history-status-${s}`;
        return `<span class="${cls}">${s}</span>`;
      };

      const fmtRow = (r) => {
        const started = new Date(r.started_at);
        const dateStr = started.toLocaleString(undefined, {
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
        });
        const sched = r.schedule_name
          ? escapeHtml(r.schedule_name)
          : `<span class="history-dim">Manual</span>`;
        const requested = r.requested_minutes;
        const actual = r.actual_minutes;
        let durationCell;
        if (r.status === "skipped") {
          durationCell = `<span class="history-dim">${requested} min · skipped</span>`;
        } else if (actual === null || actual === undefined) {
          durationCell = `<span class="history-dim">${requested} min planned</span>`;
        } else if (actual === requested) {
          durationCell = `${actual} min`;
        } else {
          durationCell = `${actual} / ${requested} min`;
        }
        const reason = r.reason
          ? `<span class="history-reason">${escapeHtml(r.reason)}</span>`
          : "";
        // v1.30 — only surface triggers that actually AFFECTED this run. The
        // coordinator writes breadcrumbs for disabled / no-effect gates (e.g.
        // {wind:{deferred:false}}, {moisture:{disabled_by_config:true}}) which
        // cluttered every row with "moisture, wind, hot_weather". Hide those;
        // the full blob is still available on expand.
        const meaningfulKeys = this._meaningfulTriggerKeys(r.triggers);
        const hasMeaningful = meaningfulKeys.length > 0;
        const expanded = this._historyExpanded.has(r.id);
        const triggerCell = hasMeaningful
          ? `<button class="btn btn-small history-trigger-toggle" data-action="history-toggle-triggers" data-record-id="${escapeAttr(r.id)}">${expanded ? "▾" : "▸"} ${meaningfulKeys.map(escapeHtml).join(", ")}</button>`
          : `<span class="history-dim">—</span>`;
        const expandedBlock =
          expanded && hasMeaningful
            ? `<tr class="history-expanded-row"><td colspan="6"><pre class="history-triggers">${escapeHtml(JSON.stringify(r.triggers, null, 2))}</pre></td></tr>`
            : "";
        return (
          `<tr class="history-row history-row-${r.status}">` +
          `<td class="history-when">${escapeHtml(dateStr)}</td>` +
          `<td class="history-zone">${escapeHtml(r.zone_name || r.zone_entity_id)}</td>` +
          `<td class="history-schedule">${sched}</td>` +
          `<td class="history-duration">${durationCell}</td>` +
          `<td class="history-status-cell">${statusBadge(r.status)}${reason ? "<br>" + reason : ""}</td>` +
          `<td class="history-triggers-cell">${triggerCell}</td>` +
          `</tr>` +
          expandedBlock
        );
      };

      // v1.30 — collapse a chunked run's "(block i/n)" records into ONE session
      // row with a progress bar (i of n blocks done), instead of N short rows.
      const fmtSessionRow = (g) => {
        const blocks = g.blocks;
        const starts = blocks.map((b) => Date.parse(b.started_at)).filter(isFinite);
        const startedAt = starts.length
          ? new Date(Math.min(...starts))
          : new Date(blocks[blocks.length - 1].started_at);
        const dateStr = startedAt.toLocaleString(undefined, {
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
        });
        const n = g.n;
        const completed = blocks.filter((b) => b.status === "completed").length;
        const runningAny = blocks.some((b) => b.status === "running");
        const abortedAny = blocks.some((b) => b.status === "aborted");
        const delivered = blocks.reduce((s, b) => s + (b.actual_minutes || 0), 0);
        const pct = Math.round((completed / Math.max(1, n)) * 100);
        // Only "running" when a block actually IS running. A status filter can
        // drop a session's running block, leaving completed<n with none running —
        // that must NOT default to a permanent false "Running" badge.
        const sessStatus = runningAny
          ? "running"
          : abortedAny
            ? "aborted"
            : "completed";
        const meaningful = Array.from(
          new Set(blocks.flatMap((b) => this._meaningfulTriggerKeys(b.triggers)))
        );
        const triggerCell = meaningful.length
          ? `<span class="history-dim">${meaningful.map(escapeHtml).join(", ")}</span>`
          : `<span class="history-dim">—</span>`;
        return (
          `<tr class="history-row history-row-${sessStatus}">` +
          `<td class="history-when">${escapeHtml(dateStr)}</td>` +
          `<td class="history-zone">${escapeHtml(g.zone_name || g.zone_entity_id)}</td>` +
          `<td class="history-schedule">${escapeHtml(g.base)} <span class="history-dim">(${n} blocks)</span></td>` +
          `<td class="history-duration">${delivered} min</td>` +
          `<td class="history-status-cell">${statusBadge(sessStatus)}` +
          `<div class="history-block-progress" title="${completed} of ${n} blocks completed">` +
          `<div class="history-block-bar" style="width:${pct}%"></div></div>` +
          `<span class="history-block-count">${completed}/${n} blocks</span></td>` +
          `<td class="history-triggers-cell">${triggerCell}</td>` +
          `</tr>`
        );
      };

      const grouped = this._groupHistoryRows(filtered);
      const rowsHtml = grouped.length
        ? grouped.map((g) => (g.session ? fmtSessionRow(g) : fmtRow(g))).join("")
        : `<tr><td colspan="6" class="history-empty">No runs match these filters.</td></tr>`;

      const loadingNote = !this._runHistoryLoaded
        ? `<p class="history-loading">Loading…</p>`
        : "";

      return (
        `<header class="page-header"><h2>Run history</h2>` +
        `<span class="version-pill">${PANEL_VERSION}</span></header>` +
        `<div class="history-toolbar">` +
        `<label>Zone <select data-action="history-filter-zone"><option value="">All zones</option>${zoneOptions}</select></label>` +
        `<label>Schedule <select data-action="history-filter-schedule"><option value="">All schedules</option>${scheduleOptions}</select></label>` +
        `<label>Status <select data-action="history-filter-status">` +
        ["", "completed", "skipped", "aborted", "running"]
          .map(
            (s) =>
              `<option value="${s}" ${f.status === s ? "selected" : ""}>${s || "All"}</option>`
          )
          .join("") +
        `</select></label>` +
        `<label>Range <select data-action="history-filter-days">` +
        [
          { v: 1, l: "Last 24 h" },
          { v: 7, l: "Last 7 days" },
          { v: 30, l: "Last 30 days" },
          { v: 90, l: "Last 90 days" },
          { v: 0, l: "All" },
        ]
          .map(
            ({ v, l }) =>
              `<option value="${v}" ${f.days === v ? "selected" : ""}>${l}</option>`
          )
          .join("") +
        `</select></label>` +
        `<button class="btn btn-small" data-action="history-refresh">Refresh</button>` +
        `<button class="btn btn-small btn-stop" data-action="history-clear">Clear all</button>` +
        `</div>` +
        loadingNote +
        `<p class="history-summary">${filtered.length} of ${all.length} record${all.length === 1 ? "" : "s"}</p>` +
        `<div class="history-table-wrap">` +
        `<table class="history-table">` +
        `<thead><tr>` +
        `<th>When</th><th>Zone</th><th>Schedule</th><th>Duration</th><th>Status</th><th>Triggers</th>` +
        `</tr></thead>` +
        `<tbody>${rowsHtml}</tbody>` +
        `</table>` +
        `</div>`
      );
    }

    // ── Sensors tab ────────────────────────────────────────────────
    _renderSensors() {
      const zones = this._zones();
      if (zones.length === 0) {
        return (
          `<header class="page-header"><h2>Sensors</h2>` +
          `<span class="version-pill">${PANEL_VERSION}</span></header>` +
          `<div class="empty"><p>No zones configured.</p></div>`
        );
      }
      const cards = zones
        .map((z) => this._renderSensorZoneCard(z))
        .join("");
      return (
        `<header class="page-header"><h2>Sensors</h2>` +
        `<span class="version-pill">${PANEL_VERSION}</span></header>` +
        `<p class="section-hint">Bind soil-moisture sensors to a zone so runtimes auto-adjust based on actual moisture. You can attach one sensor or several (combined as average, lowest, highest, or just the primary).</p>` +
        `<div class="sensor-zone-list">${cards}</div>`
      );
    }

    _readPercentSensors(entity_ids) {
      // Helper: read a list of % sensor entities, returning
      // [{entity_id, friendly, value: number}, ...] for the live ones.
      const out = [];
      for (const eid of entity_ids || []) {
        const s = this._readSensor(eid);
        if (!s) continue;
        const val = parseFloat(s.state);
        if (Number.isNaN(val)) continue;
        out.push({
          entity_id: eid,
          friendly: s.attributes?.friendly_name || eid,
          value: val,
          unit: s.attributes?.unit_of_measurement || "%",
        });
      }
      return out;
    }

    _combineReadings(readings, combine) {
      // Apply combine_mode to a list of {value} readings, return number or null.
      if (!readings.length) return null;
      const vals = readings.map((r) => r.value);
      switch (combine) {
        case "lowest":
          return Math.min(...vals);
        case "highest":
          return Math.max(...vals);
        case "primary":
          return vals[0];
        default:
          return vals.reduce((a, b) => a + b, 0) / vals.length;
      }
    }

    _renderSensorZoneCard(zone) {
      const zoneCfg = this._config?.zones?.[zone.entityId] || {};
      const bound = zoneCfg.moisture_entities || [];
      const combine = zoneCfg.combine_mode;
      const category = zoneCfg.category;
      const minPct = zoneCfg.min_pct;
      const targetPct = zoneCfg.target_pct;
      const maxPct = zoneCfg.max_pct;

      const readings = this._readPercentSensors(bound);
      const combined = this._combineReadings(readings, combine);

      // Header live readout = combined value
      const headerReadout =
        combined !== null
          ? `<span class="sensor-live${minPct != null && combined < minPct ? " sensor-low" : ""}">${combined.toFixed(1)}%</span>`
          : "";

      // Per-sensor breakdown — one row per bound sensor + a separate
      // "Combined" row showing the value used for irrigation decisions.
      let breakdown = "";
      if (bound.length > 0) {
        const sensorRows = bound
          .map((eid) => {
            const r = readings.find((x) => x.entity_id === eid);
            const friendly = r?.friendly || this._hass?.states?.[eid]?.attributes?.friendly_name || eid;
            const valHtml =
              r != null
                ? `<span class="sensor-reading${minPct != null && r.value < minPct ? " sensor-low" : ""}">${r.value.toFixed(1)}%</span>`
                : `<span class="sensor-reading sensor-unavailable">—</span>`;
            // PRD #23/#85 — deep-link to HA's developer-state page so
            // users can open the entity's full details + recalibrate
            // without leaving the panel context. target="_top" navigates
            // the parent window out of the iframe sandbox.
            const deepLink =
              `/developer-tools/state?entity_id=${encodeURIComponent(eid)}`;
            return (
              `<div class="sensor-bound-row sensor-reading-row">` +
              `<a class="sensor-label sensor-link" target="_top" href="${deepLink}" title="${escapeAttr("Open " + eid + " in HA")}">${escapeHtml(friendly)}</a>` +
              valHtml +
              `</div>`
            );
          })
          .join("");
        const combinedRow =
          bound.length > 1 && combined !== null
            ? `<div class="sensor-bound-row sensor-combined-row">` +
              `<span class="sensor-label"><strong>Combined (${escapeHtml(combine || "average")})</strong></span>` +
              `<span class="sensor-reading sensor-reading-combined">${combined.toFixed(1)}%</span>` +
              `</div>`
            : "";
        breakdown = sensorRows + combinedRow;
      }

      const status =
        bound.length === 0
          ? `<div class="sensor-empty">No sensors bound — runtime is fixed at the scheduled duration.</div>`
          : `<div class="sensor-bound">` +
            breakdown +
            `<div class="sensor-bound-row"><span class="sensor-label">Range</span><span>min ${minPct ?? "—"}% • target ${targetPct ?? "—"}% • max ${maxPct ?? "—"}%</span></div>` +
            (category
              ? `<div class="sensor-bound-row"><span class="sensor-label">Category</span><span>${escapeHtml(category)}</span></div>`
              : "") +
            `</div>`;

      return (
        `<article class="sensor-zone-card">` +
        `<header class="sensor-zone-head">` +
        `<div><h4>${escapeHtml(zone.name)}</h4><div class="sensor-zone-eid">${escapeHtml(zone.entityId)}</div></div>` +
        `<div class="sensor-zone-right">${headerReadout}<button class="btn btn-small" data-action="configure-sensor" data-entity-id="${escapeAttr(zone.entityId)}">${bound.length > 0 ? "Edit" : "Configure"}</button></div>` +
        `</header>` +
        status +
        `</article>`
      );
    }

    _openConfigureSensor(zoneEntityId) {
      const zoneCfg = this._config?.zones?.[zoneEntityId] || {};
      this._sensorEditor = {
        zone_entity_id: zoneEntityId,
        moisture_entities: [...(zoneCfg.moisture_entities || [])],
        combine_mode: zoneCfg.combine_mode || "",
        min_pct: zoneCfg.min_pct ?? 21,
        target_pct: zoneCfg.target_pct ?? 31,
        max_pct: zoneCfg.max_pct ?? 40,
        category: zoneCfg.category || "",
        temperature_entities: [...(zoneCfg.temperature_entities || [])],
        humidity_entities: [...(zoneCfg.humidity_entities || [])],
        require_moisture_reading: !!zoneCfg.require_moisture_reading, // v1.18
        moisture_disabled: !!zoneCfg.moisture_disabled, // v1.19.0
        moisture_excluded: [...(zoneCfg.moisture_excluded || [])], // v1.19
        auto_soak_enabled: !!zoneCfg.auto_soak_enabled, // v1.19
        soak_run_minutes: zoneCfg.soak_run_minutes ?? 10,
        soak_wait_minutes: zoneCfg.soak_wait_minutes ?? 30,
        soak_max_cycles: zoneCfg.soak_max_cycles ?? 4,
      };
      this._sensorModalOpen = true;
      this._renderNow();
    }

    _renderSensorModal() {
      const e = this._sensorEditor;
      if (!e) return "";
      const zoneName = this._zoneName(e.zone_entity_id);
      // Candidate moisture sensors: anything in sensor.* whose unit is %
      // OR name contains "moisture". Falls back to listing every sensor.* if none match.
      const allSensors = this._hass?.states ? Object.values(this._hass.states) : [];
      const moistureCandidates = allSensors
        .filter((s) => s.entity_id.startsWith("sensor."))
        .filter((s) => {
          const unit = s.attributes?.unit_of_measurement;
          if (unit === "%") return true;
          if (/moisture|wet|damp/i.test(s.entity_id)) return true;
          return false;
        })
        .map((s) => s.entity_id)
        .sort();

      // v1.19.0 — render the info bubble with a custom popover instead
      // of the native `title` attribute. The native tooltip is delayed
      // ~1.5s on desktop AND silently does NOTHING on touch devices.
      // The custom popover shows immediately on hover, on tap (touch
      // toggles via the .help-tip-open class set by _onClick), and on
      // keyboard focus. tabindex+role makes it screen-reader-aware.
      const tip = (text) =>
        `<span class="help-tip" role="button" tabindex="0" aria-label="${escapeAttr(text)}">` +
        `ⓘ<span class="help-tip-popup">${escapeHtml(text)}</span>` +
        `</span>`;

      const moistureList = moistureCandidates.length > 0
        ? moistureCandidates
        : allSensors.map((s) => s.entity_id).filter((id) => id.startsWith("sensor.")).sort();
      const sensorChecks = this._renderSensorCheckRows(
        moistureList,
        e.moisture_entities,
        "moisture_entity",
        e.moisture_excluded || [], // v1.19 — enables the "in avg" toggle
      );

      return (
        `<div class="modal-backdrop"></div>` +
        `<div class="modal modal-wide" role="dialog" aria-modal="true">` +
        `<form class="modal-form sensor-form">` +
        `<h3>Moisture sensors for ${escapeHtml(zoneName)}</h3>` +
        `<label>Sensors ${tip("Pick one or more soil-moisture sensors. If you pick multiple, choose how to combine their readings below.")}</label>` +
        this._renderSensorPickerWithSearch(
          sensorChecks,
          "moisture",
          'No sensors found in HA. Add a moisture sensor first.',
        ) +
        `<label>Combine mode ${tip("How to combine multiple sensor readings. Required when you have more than one sensor.")}</label>` +
        `<select name="combine_mode" required>` +
        `<option value=""${e.combine_mode ? "" : " selected"} disabled>— Pick one —</option>` +
        `<option value="average"${e.combine_mode === "average" ? " selected" : ""}>Average (mean of all sensors)</option>` +
        `<option value="lowest"${e.combine_mode === "lowest" ? " selected" : ""}>Lowest (most dry sensor wins — conservative)</option>` +
        `<option value="highest"${e.combine_mode === "highest" ? " selected" : ""}>Highest (wettest sensor wins — saves water)</option>` +
        `<option value="primary"${e.combine_mode === "primary" ? " selected" : ""}>Primary (just use first sensor)</option>` +
        `</select>` +
        `<div class="row-3">` +
        `<div><label>Min % ${tip("Below this — urgent boost.")}</label><input name="min_pct" type="number" min="0" max="100" step="1" value="${e.min_pct}" /></div>` +
        `<div><label>Target % ${tip("Aim for this moisture.")}</label><input name="target_pct" type="number" min="0" max="100" step="1" value="${e.target_pct}" /></div>` +
        `<div><label>Max % ${tip("Above this — skip the run.")}</label><input name="max_pct" type="number" min="0" max="100" step="1" value="${e.max_pct}" /></div>` +
        `</div>` +
        // v1.18 — fail-closed toggle
        `<label class="enabled-check"><input type="checkbox" name="require_moisture_reading"${
          e.require_moisture_reading ? " checked" : ""
        } /> Skip run if no moisture reading ${tip("When ON: if every moisture sensor for this zone is offline / unavailable at run time, the scheduled run is SKIPPED instead of watering blind (fail-closed). When OFF (default): the run proceeds normally if sensors are dark (fail-open). Note: individual offline sensors are always excluded from the combined reading — this only governs what happens when NONE are reporting.")}</label>` +
        // v1.19.0 — ignore moisture for watering decisions
        `<label class="enabled-check"><input type="checkbox" name="moisture_disabled"${
          e.moisture_disabled ? " checked" : ""
        } /> Ignore moisture for watering decisions ${tip("When ON: this zone's moisture readings are display-only. Schedules run at their full configured duration — no saturated-skip, no runtime boost or reduction, and 'Skip run if no moisture reading' is ignored too. The sensors stay bound, so the Zones tab chips and Today tile still show live readings. Useful when a sensor is misbehaving or you want fixed watering times for a while without unbinding everything.")}</label>` +
        // v1.19 — auto-soak recovery
        `<h3 class="section-title">Auto-soak recovery ${tip("Closed-loop low-moisture fix: when this zone's moisture (from the sensors marked 'in avg') drops below Min %, run for the set minutes, wait for the water to soak in, re-read the sensors, and repeat until moisture is back above Min % — or the cycle cap is hit, in which case you're notified and the zone won't retry for 6 hours (so a stuck-low sensor can't water all day). Paused during rain lockout. Disabled while 'Ignore moisture' is on.")}</h3>` +
        `<label class="enabled-check"><input type="checkbox" name="auto_soak_enabled"${
          e.auto_soak_enabled ? " checked" : ""
        } /> Water automatically when below Min %</label>` +
        `<div class="row-3">` +
        `<div><label>Run (min) ${tip("Length of each watering burst. Short bursts + soak pauses absorb better than one long run.")}</label><input name="soak_run_minutes" type="number" min="1" max="60" step="1" value="${e.soak_run_minutes}" /></div>` +
        `<div><label>Soak wait (min) ${tip("Pause between bursts so water percolates down to the sensor depth before re-reading. 30 min suits most soils; clay needs longer.")}</label><input name="soak_wait_minutes" type="number" min="5" max="240" step="1" value="${e.soak_wait_minutes}" /></div>` +
        `<div><label>Max cycles ${tip("Safety cap. If moisture is still below Min % after this many run/soak rounds, stop, notify you, and wait 6 hours before trying again.")}</label><input name="soak_max_cycles" type="number" min="1" max="10" step="1" value="${e.soak_max_cycles}" /></div>` +
        `</div>` +
        `<label>Plant category ${tip("Pick a category to see typical moisture ranges. The min/target/max above stay independent — you can change them after picking.")}</label>` +
        `<select name="category">` +
        `<option value=""${e.category ? "" : " selected"}>—</option>` +
        ["lawn", "vegetable_garden", "bushes", "citrus", "trees", "custom"]
          .map(
            (c) =>
              `<option value="${c}"${e.category === c ? " selected" : ""}>${c}</option>`
          )
          .join("") +
        `</select>` +
        `<p class="section-hint" data-category-info>${
          e.category && CATEGORY_INFO[e.category] ? escapeHtml(CATEGORY_INFO[e.category]) : "Pick a category for a typical moisture range."
        }</p>` +
        // Climate sensors (optional, display-only on Zones tab)
        `<h3 class="section-title">Climate sensors (optional, display-only)</h3>` +
        `<label>Temperature sensors ${tip("One or more temperature sensors near this zone. Multiple sensors are averaged.")}</label>` +
        this._renderClimateChecks(allSensors, e.temperature_entities, "temperature_entity", "temp") +
        `<label>Humidity sensors ${tip("One or more humidity sensors near this zone. Multiple sensors are averaged.")}</label>` +
        this._renderClimateChecks(allSensors, e.humidity_entities, "humidity_entity", "humidity") +
        `<div class="modal-actions">` +
        `<button type="button" class="btn btn-secondary modal-cancel">Cancel</button>` +
        `<button type="submit" class="btn btn-primary">Save</button>` +
        `</div>` +
        `</form>` +
        `</div>`
      );
    }

    _renderClimateChecks(allSensors, selected, inputName, kind) {
      // Build a checkbox list of candidate temperature OR humidity sensors.
      // Filters by unit (°F/°C for temp, % for humidity) + keyword in name.
      const tempUnits = ["°F", "°C", "F", "C"];
      const matches = allSensors
        .filter((s) => s.entity_id.startsWith("sensor."))
        .filter((s) => {
          const unit = (s.attributes?.unit_of_measurement || "").trim();
          if (kind === "temp") {
            if (tempUnits.includes(unit)) return true;
            return /temperature|temp\b/i.test(s.entity_id);
          }
          // humidity
          if (unit === "%") return /humidity|humid/i.test(s.entity_id);
          return /humidity/i.test(s.entity_id);
        })
        .map((s) => s.entity_id)
        .sort();
      const rows = this._renderSensorCheckRows(matches, selected, inputName);
      return this._renderSensorPickerWithSearch(
        rows,
        kind,
        "No matching sensors found in HA.",
      );
    }

    _syncMoistureUseToggle(row, eid, selected) {
      // v1.19 — surgical add/remove of the "in avg" control when the
      // user (de)selects a moisture sensor, so the modal doesn't need a
      // full re-render (which would wipe the search filter + focus).
      if (!row) return;
      const existing = row.querySelector(".sensor-pick-use");
      if (!selected) {
        if (existing) existing.remove();
        // Deselected sensors can't stay excluded — keep state clean.
        const set = new Set(this._sensorEditor.moisture_excluded || []);
        set.delete(eid);
        this._sensorEditor.moisture_excluded = Array.from(set);
        return;
      }
      if (existing) return;
      const label = document.createElement("label");
      label.className = "sensor-pick-use";
      label.title =
        "Checked: this sensor's reading counts in the combine/average used " +
        "for watering decisions. Unchecked: display-only — still shown on " +
        "chips and tiles, but ignored by the moisture gate and auto-soak.";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.name = "moisture_exclude";
      cb.value = eid;
      cb.checked = true; // newly selected sensors default to in-analysis
      label.appendChild(cb);
      label.appendChild(document.createTextNode(" in avg"));
      row.appendChild(label);
    }

    _renderSensorCheckRows(entityIds, selected, inputName, excludedList) {
      // v1.19.0 — extracted helper so the moisture + climate paths share
      // one row-rendering shape. Adds `data-search-text` per row holding
      // a lowercased "friendly + entity_id" blob the search input can
      // filter against without re-rendering the whole modal.
      //
      // v1.19 — when `excludedList` is provided (moisture list only),
      // each SELECTED row also gets an "in avg" mini-checkbox. Unchecked
      // = the sensor's reading is shown everywhere but excluded from the
      // combine/average the gate and auto-soak act on. The row becomes a
      // <div> with the main checkbox in its own <label> so the two
      // controls don't activate each other.
      const withExclude = Array.isArray(excludedList);
      return entityIds
        .map((eid) => {
          const friendly = this._hass.states[eid]?.attributes?.friendly_name || eid;
          const checked = selected.includes(eid);
          const searchText = `${friendly} ${eid}`.toLowerCase();
          const body =
            `<input type="checkbox" name="${inputName}" value="${escapeAttr(eid)}"${
              checked ? " checked" : ""
            } />` +
            `<span><strong>${escapeHtml(friendly)}</strong><br />` +
            `<code>${escapeHtml(eid)}</code></span>`;
          if (!withExclude) {
            return `<label class="sensor-pick" data-search-text="${escapeAttr(searchText)}">${body}</label>`;
          }
          const inAnalysis = !excludedList.includes(eid);
          const useToggle = checked
            ? `<label class="sensor-pick-use" title="Checked: this sensor's reading counts in the combine/average used for watering decisions. Unchecked: display-only — still shown on chips and tiles, but ignored by the moisture gate and auto-soak. For sensors that read consistently wrong.">` +
              `<input type="checkbox" name="moisture_exclude" value="${escapeAttr(eid)}"${
                inAnalysis ? " checked" : ""
              } /> in avg</label>`
            : "";
          return (
            `<div class="sensor-pick" data-search-text="${escapeAttr(searchText)}">` +
            `<label class="sensor-pick-main">${body}</label>` +
            useToggle +
            `</div>`
          );
        })
        .join("");
    }

    _renderSensorPickerWithSearch(rowsHtml, kindKey, emptyMessage) {
      // v1.19.0 — wrap a sensor checklist with a search input. The
      // input's data-action="filter-sensor-list" is caught by _onInput;
      // it walks the sibling .sensor-pick-list and hides any row whose
      // data-search-text doesn't contain the lowercased query. Pure
      // client-side; no re-render needed (preserves checkbox state +
      // cursor focus while typing).
      if (!rowsHtml) {
        return `<div class="empty" style="margin-bottom:8px">${escapeHtml(emptyMessage)}</div>`;
      }
      return (
        `<div class="sensor-picker" data-kind="${escapeAttr(kindKey)}">` +
        `<input type="search" class="sensor-pick-search" ` +
        `data-action="filter-sensor-list" data-kind="${escapeAttr(kindKey)}" ` +
        `placeholder="Filter sensors — type a name or entity id" ` +
        `aria-label="Filter ${escapeAttr(kindKey)} sensors" />` +
        `<div class="sensor-pick-list">${rowsHtml}` +
        `<div class="sensor-pick-no-match" hidden>No sensors match your filter.</div>` +
        `</div>` +
        `</div>`
      );
    }

    async _saveSensorConfig() {
      const e = this._sensorEditor;
      if (!e) return;
      if (e.moisture_entities.length === 0)
        return alert("Pick at least one moisture sensor.");
      if (e.moisture_entities.length > 1 && !e.combine_mode)
        return alert("Pick a combine mode for multiple sensors.");
      if (!e.combine_mode) {
        // Single sensor — default to 'primary' (just use it as-is)
        e.combine_mode = "primary";
      }
      // v1.19 — prune exclusions to currently-selected sensors and
      // sanity-check: at least one selected sensor must stay in the
      // analysis (excluding ALL of them = use the per-zone "ignore
      // moisture" toggle instead, which says what it means).
      const excluded = (e.moisture_excluded || []).filter((eid) =>
        e.moisture_entities.includes(eid)
      );
      if (excluded.length >= e.moisture_entities.length && !e.moisture_disabled) {
        return alert(
          'Every selected sensor is excluded from the analysis ("in avg" unchecked).\n\n' +
            "Keep at least one sensor in the average, or use the " +
            '"Ignore moisture for watering decisions" toggle below instead.'
        );
      }
      // v1.19 — soak field validation
      const soakRun = parseInt(e.soak_run_minutes, 10);
      const soakWait = parseInt(e.soak_wait_minutes, 10);
      const soakCycles = parseInt(e.soak_max_cycles, 10);
      if (e.auto_soak_enabled) {
        if (!soakRun || soakRun < 1 || soakRun > 60)
          return alert("Auto-soak run time must be 1–60 minutes.");
        if (!soakWait || soakWait < 5 || soakWait > 240)
          return alert("Auto-soak wait time must be 5–240 minutes.");
        if (!soakCycles || soakCycles < 1 || soakCycles > 10)
          return alert("Auto-soak max cycles must be 1–10.");
      }
      try {
        await this._hass.callService("complete_irrigation", "set_zone_moisture", {
          zone_entity_id: e.zone_entity_id,
          moisture_entities: e.moisture_entities,
          combine_mode: e.combine_mode,
          min_pct: parseInt(e.min_pct, 10),
          target_pct: parseInt(e.target_pct, 10),
          max_pct: parseInt(e.max_pct, 10),
          ...(e.category ? { category: e.category } : {}),
          temperature_entities: e.temperature_entities,
          humidity_entities: e.humidity_entities,
          require_moisture_reading: !!e.require_moisture_reading, // v1.18
          moisture_disabled: !!e.moisture_disabled, // v1.19.0
          moisture_excluded: excluded, // v1.19
          auto_soak_enabled: !!e.auto_soak_enabled, // v1.19
          ...(Number.isFinite(soakRun) ? { soak_run_minutes: soakRun } : {}),
          ...(Number.isFinite(soakWait) ? { soak_wait_minutes: soakWait } : {}),
          ...(Number.isFinite(soakCycles) ? { soak_max_cycles: soakCycles } : {}),
        });
        this._closeAllModals();
        await this._fetchConfig();
      } catch (err) {
        alert("Failed to save sensor config: " + (err?.message || err));
      }
    }

    // ── New grass establishment modal ──────────────────────────────
    _openEstablishmentModal(entityId, zoneName) {
      this._establishmentEditor = {
        zone_entity_id: entityId,
        zone_name: zoneName || entityId,
        cycles_per_day: 3,
        minutes_per_cycle: 10,
        days: 12,
        start_hour: 6,
      };
      this._establishmentModalOpen = true;
      this._renderNow();
    }

    _renderEstablishmentModal() {
      const e = this._establishmentEditor;
      if (!e) return "";
      // v1.19.0 — render the info bubble with a custom popover instead
      // of the native `title` attribute. The native tooltip is delayed
      // ~1.5s on desktop AND silently does NOTHING on touch devices.
      // The custom popover shows immediately on hover, on tap (touch
      // toggles via the .help-tip-open class set by _onClick), and on
      // keyboard focus. tabindex+role makes it screen-reader-aware.
      const tip = (text) =>
        `<span class="help-tip" role="button" tabindex="0" aria-label="${escapeAttr(text)}">` +
        `ⓘ<span class="help-tip-popup">${escapeHtml(text)}</span>` +
        `</span>`;
      return (
        `<div class="modal-backdrop"></div>` +
        `<div class="modal modal-wide" role="dialog" aria-modal="true">` +
        `<form class="modal-form establishment-form">` +
        `<h3>🌱 New planting establishment for ${escapeHtml(e.zone_name)}</h3>` +
        `<p class="section-hint">Runs multiple short cycles per day for N days to keep newly-planted grass seed, shrubs, trees, or other plantings consistently moist. The zone's normal schedule is paused while establishment is active; moisture min/max thresholds are bypassed (the soil is intentionally kept wet).</p>` +
        `<div class="row-2">` +
        `<div><label>Cycles per day ${tip("How many short waterings each day. 3 is a good default for cool-season grass.")}</label><input name="cycles_per_day" type="number" min="1" max="8" step="1" value="${e.cycles_per_day}" required /></div>` +
        `<div><label>Minutes per cycle ${tip("Length of each cycle. Keep short to avoid runoff on bare soil.")}</label><input name="minutes_per_cycle" type="number" min="1" max="60" step="1" value="${e.minutes_per_cycle}" required /></div>` +
        `</div>` +
        `<div class="row-2">` +
        `<div><label>Total days ${tip("Establishment window. 12-14 days is typical until germination.")}</label><input name="days" type="number" min="1" max="60" step="1" value="${e.days}" required /></div>` +
        `<div><label>First cycle hour (0-23) ${tip("When the first cycle of the day fires. Subsequent cycles spread evenly through the daylight hours.")}</label><input name="start_hour" type="number" min="0" max="23" step="1" value="${e.start_hour}" required /></div>` +
        `</div>` +
        `<div class="modal-actions">` +
        `<button type="button" class="btn btn-secondary modal-cancel">Cancel</button>` +
        `<button type="submit" class="btn btn-primary">Start establishment</button>` +
        `</div>` +
        `</form>` +
        `</div>`
      );
    }

    async _saveEstablishment(form) {
      const e = this._establishmentEditor;
      if (!e) return;
      const data = new FormData(form);
      const payload = {
        zone_entity_id: e.zone_entity_id,
        cycles_per_day: parseInt(data.get("cycles_per_day"), 10),
        minutes_per_cycle: parseInt(data.get("minutes_per_cycle"), 10),
        days: parseInt(data.get("days"), 10),
        start_hour: parseInt(data.get("start_hour"), 10),
      };
      try {
        await this._hass.callService(
          "complete_irrigation",
          "start_establishment",
          payload
        );
        this._closeAllModals();
        await this._fetchSchedules();
        alert(`Establishment started for ${e.zone_name}.`);
      } catch (err) {
        alert("Failed to start establishment: " + (err?.message || err));
      }
    }

    // ── Weather tab ────────────────────────────────────────────────
    _renderWeather() {
      const c = this._config || {};
      const rainSensors =
        c.rain_sensors && c.rain_sensors.length
          ? c.rain_sensors
          : c.rain_sensor
          ? [c.rain_sensor]
          : [];
      const tempSensor = c.temperature_sensor || "";
      const windSensor = c.wind_sensor || "";
      const windMph = c.wind_defer_mph ?? 0;
      const hotF = c.hot_threshold_f ?? 100;
      const boost = c.boost_percent ?? 25;

      const allSensors = this._hass?.states
        ? Object.values(this._hass.states)
            .filter((s) => s.entity_id.startsWith("sensor."))
            .map((s) => s.entity_id)
            .sort()
        : [];

      // v1.19.0 — render the info bubble with a custom popover instead
      // of the native `title` attribute. The native tooltip is delayed
      // ~1.5s on desktop AND silently does NOTHING on touch devices.
      // The custom popover shows immediately on hover, on tap (touch
      // toggles via the .help-tip-open class set by _onClick), and on
      // keyboard focus. tabindex+role makes it screen-reader-aware.
      const tip = (text) =>
        `<span class="help-tip" role="button" tabindex="0" aria-label="${escapeAttr(text)}">` +
        `ⓘ<span class="help-tip-popup">${escapeHtml(text)}</span>` +
        `</span>`;

      // Multi-pick rain sensor checkbox list (rain-named sensors first).
      const rainCandidates = allSensors.filter((eid) => /rain|precip/i.test(eid));
      const otherSensors = allSensors.filter((eid) => !/rain|precip/i.test(eid));
      const rainChecks = (rainCandidates.length > 0 ? rainCandidates : otherSensors)
        .map((eid) => {
          const friendly = this._hass.states[eid]?.attributes?.friendly_name || eid;
          const s = this._hass.states[eid];
          const liveVal =
            s && s.state !== "unknown" && s.state !== "unavailable"
              ? ` <span class="rain-live">${escapeHtml(s.state)} ${escapeHtml(s.attributes?.unit_of_measurement || "")}</span>`
              : "";
          const idx = rainSensors.indexOf(eid);
          const primary = idx === 0;
          return (
            `<label class="rain-pick"><input type="checkbox" name="rain_sensor_pick" value="${escapeAttr(eid)}"${
              idx >= 0 ? " checked" : ""
            } /> <span><strong>${escapeHtml(friendly)}</strong>${primary ? ' <span class="rain-primary-tag">primary</span>' : ""}<br /><code>${escapeHtml(eid)}</code>${liveVal}</span></label>`
          );
        })
        .join("");

      // Single-pick temperature sensor (still scalar in coordinator logic)
      const tempOptHtml = allSensors
        .map(
          (eid) =>
            `<option value="${escapeAttr(eid)}"${
              eid === tempSensor ? " selected" : ""
            }>${escapeHtml(eid)}</option>`
        )
        .join("");

      const weather = this._findWeatherEntity();
      const forecastHtml = this._renderForecast(weather);

      const lockoutHtml = c.lockout_until
        ? `<div class="rain-lockout-banner"><span>🌧️</span><span>Rain lockout active until ${escapeHtml(
            new Date(c.lockout_until).toLocaleString()
          )}</span><button class="btn btn-small" data-action="clear-rain-lockout">Clear now</button></div>`
        : "";

      return (
        `<header class="page-header"><h2>Weather</h2>` +
        `<span class="version-pill">${PANEL_VERSION}</span></header>` +
        lockoutHtml +
        forecastHtml +
        `<form class="weather-form" data-form="weather">` +
        `<h3 class="section-title">Rain lockout</h3>` +
        `<label>Rain sensors ${tip("Pick one or more rainfall sensors (accumulation today / yesterday / duration / intensity, etc.). The first checked sensor is used for the lockout calc; the others show on the Today banner. Check the boxes in your preferred priority order.")}</label>` +
        `<div class="rain-pick-list">${rainChecks || '<div class="empty">No sensors found in HA.</div>'}</div>` +
        `<h3 class="section-title">Hot weather boost</h3>` +
        `<label>Temperature sensor ${tip("Sensor reporting outdoor temp in °F. Hot days trigger a runtime boost.")}</label>` +
        `<select name="temperature_sensor"><option value="">— None —</option>${tempOptHtml}</select>` +
        `<div class="row-2">` +
        `<div><label>Hot threshold (°F) ${tip("Boost runtime when temp meets or exceeds this.")}</label><input name="hot_threshold_f" type="number" min="50" max="130" step="1" value="${hotF}" /></div>` +
        `<div><label>Boost (%) ${tip("Increase runtime by this percent on hot days.")}</label><input name="boost_percent" type="number" min="0" max="100" step="1" value="${boost}" /></div>` +
        `</div>` +
        // PRD #52 — wind defer
        `<h3 class="section-title">Wind defer</h3>` +
        `<label>Wind sensor (optional) ${tip("Sensor reporting current wind speed in mph. If omitted, falls back to any weather.* entity's wind_speed attribute.")}</label>` +
        `<select name="wind_sensor"><option value="">— None (auto from weather.*) —</option>${allSensors
          .filter((eid) => /wind|gust/i.test(eid))
          .concat(allSensors.filter((eid) => !/wind|gust/i.test(eid)))
          .map(
            (eid) =>
              `<option value="${escapeAttr(eid)}"${
                eid === windSensor ? " selected" : ""
              }>${escapeHtml(eid)}</option>`
          )
          .join("")}</select>` +
        `<label>Wind defer threshold (mph) ${tip("Skip scheduled runs when current wind meets or exceeds this. 0 disables wind defer.")}</label>` +
        `<input name="wind_defer_mph" type="number" min="0" max="80" step="1" value="${windMph}" />` +
        `<div class="modal-actions"><button type="submit" class="btn btn-primary">Save weather config</button></div>` +
        `</form>`
      );
    }

    _renderForecast(weather) {
      // Prefer the fresh forecast we fetched via service (HA 2024+ way).
      // Fall back to the legacy attributes.forecast for older HA installs.
      const cached = weather && this._forecastCache[weather.entity_id];
      const fc = (cached && cached.length ? cached : null) || weather?.attributes?.forecast;
      if (!Array.isArray(fc) || fc.length === 0) return "";
      const unitT = weather.attributes.temperature_unit || "°";
      const cells = fc
        .slice(0, 3)
        .map((day) => {
          const cond = WEATHER_CONDITION_MAP[day.condition] || {
            icon: "🌤️",
            label: day.condition || "—",
          };
          const dateLabel = day.datetime
            ? new Date(day.datetime).toLocaleDateString(undefined, {
                weekday: "short",
                month: "short",
                day: "numeric",
              })
            : "";
          return (
            `<div class="forecast-cell">` +
            `<div class="forecast-date">${escapeHtml(dateLabel)}</div>` +
            `<div class="forecast-icon">${cond.icon}</div>` +
            `<div class="forecast-label">${escapeHtml(cond.label)}</div>` +
            (day.temperature != null
              ? `<div class="forecast-temp">${day.temperature}${unitT}` +
                (day.templow != null ? ` / ${day.templow}${unitT}` : "") +
                `</div>`
              : "") +
            `</div>`
          );
        })
        .join("");
      return (
        `<section class="forecast"><h3 class="section-title">3-day forecast</h3>` +
        `<div class="forecast-row">${cells}</div></section>`
      );
    }

    async _saveWeatherConfig(form) {
      const data = new FormData(form);
      const payload = {};

      // rain_sensors: collect all checked rain checkboxes (preserves DOM order).
      const rainChecks = Array.from(
        form.querySelectorAll('input[name="rain_sensor_pick"]:checked')
      ).map((el) => el.value);
      payload.rain_sensors = rainChecks;

      const ts = data.get("temperature_sensor");
      if (ts) payload.temperature_sensor = ts;
      const ws = data.get("wind_sensor");
      if (ws) payload.wind_sensor = ws;
      const hot = parseInt(data.get("hot_threshold_f"), 10);
      const boost = parseInt(data.get("boost_percent"), 10);
      const windMph = parseFloat(data.get("wind_defer_mph"));
      if (!Number.isNaN(hot)) payload.hot_threshold_f = hot;
      if (!Number.isNaN(boost)) payload.boost_percent = boost;
      if (Number.isFinite(windMph)) payload.wind_defer_mph = windMph;
      try {
        await this._hass.callService(
          "complete_irrigation",
          "set_weather_config",
          payload
        );
        await this._fetchConfig();
        alert("Weather config saved.");
      } catch (err) {
        alert("Failed to save: " + (err?.message || err));
      }
    }

    async _clearRainLockout() {
      try {
        await this._hass.callService("complete_irrigation", "clear_rain_lockout", {});
        await this._fetchConfig();
      } catch (err) {
        alert("Failed to clear lockout: " + (err?.message || err));
      }
    }

    // ── v2 Yard tab (plant-aware irrigation) ───────────────────────
    _zoneFriendly(entityId) {
      const s = this._hass?.states?.[entityId];
      return (s && s.attributes?.friendly_name) || entityId;
    }

    _switchOptions() {
      const out = [];
      const states = this._hass?.states || {};
      for (const eid of Object.keys(states)) {
        if (!eid.startsWith("switch.")) continue;
        out.push({ id: eid, name: states[eid].attributes?.friendly_name || eid });
      }
      out.sort((a, b) => a.name.localeCompare(b.name));
      return out;
    }

    _catLabel(cat) {
      return (
        {
          very_low: "Very low",
          low: "Low",
          moderate: "Moderate",
          high: "High",
        }[cat] || cat
      );
    }

    _emitterLabel(emitters) {
      if (!emitters || !emitters.length) return "—";
      return emitters.map((e) => `${e.count}×${e.gph}`).join(" + ") + " GPH";
    }

    _fmtRuns(n) {
      if (n == null) return "—";
      const r = Math.round(n * 10) / 10;
      return (Number.isInteger(r) ? r : r.toFixed(1)) + "×";
    }

    _renderYard() {
      if (!this._yardLoaded) {
        return `<div class="placeholder"><p>Loading yard…</p></div>`;
      }
      const eff = this._yardEff != null ? Math.round(this._yardEff * 100) : 90;
      // v1.28 — auto (FAO-56 from weather) vs manual reference ET. The number
      // input edits the MANUAL fallback; the effective value (auto when fresh,
      // else manual) is what the report up top is computed against.
      const st = this._yardEtoStatus || {};
      // Optimistic: reflect the user's in-flight toggle intent until the
      // authoritative value returns, so a background re-render can't snap back.
      const autoOn = this._pendingAutoEto != null ? this._pendingAutoEto : !!st.eto_auto;
      const effVal = this._yardEto != null ? Number(this._yardEto) : null;
      const manualVal =
        st.eto_manual != null ? st.eto_manual : this._yardEto != null ? this._yardEto : "";
      const usingAuto = st.eto_source === "auto" && st.eto_auto_value != null;
      let autoAtTxt = "";
      if (st.eto_auto_at) {
        const d = new Date(st.eto_auto_at);
        if (!isNaN(d)) autoAtTxt = d.toLocaleString();
      }
      const wEnt = st.weather_entity ? escapeHtml(st.weather_entity) : "your weather entity";
      const autoNote = autoOn
        ? `<p class="muted yard-eto-auto">` +
          (usingAuto
            ? `Currently <strong>${effVal != null ? effVal.toFixed(2) : "—"} in/week</strong>, ` +
              `computed from ${wEnt}` +
              (autoAtTxt ? ` (updated ${escapeHtml(autoAtTxt)})` : "") +
              `.`
            : `Waiting on a usable forecast from ${wEnt} — using your manual value ` +
              `below until one is available.`) +
          ` Falls back to the manual value if the forecast is unavailable or stale.</p>`
        : "";
      return (
        `<div class="yard-intro">` +
        `<h2>🪴 Yard</h2>` +
        `<p class="muted">Place each plant on its zone (drip loop) and the calculator sizes ` +
        `emitters so every plant gets the right water — even when they share a loop.</p>` +
        `</div>` +
        // v1.30 — aerial yard map with draggable plant markers
        this._renderYardMap() +
        // Reference ET control
        `<div class="card yard-eto">` +
        `<label class="enabled-check"><input type="checkbox" data-action="toggle-auto-eto"${
          autoOn ? " checked" : ""
        } /> Auto reference ET from weather forecast (FAO-56)</label>` +
        autoNote +
        `<label>${autoOn ? "Manual fallback (inches / week)" : "Reference ET (inches / week)"}</label>` +
        `<div class="yard-eto-row">` +
        `<input type="number" name="eto_in_week" min="0.1" max="10" step="0.1" value="${escapeAttr(
          String(manualVal)
        )}" />` +
        `<button class="btn btn-primary" data-action="apply-eto">Apply</button>` +
        `<span class="muted">Drives every plant's weekly need (drip efficiency ${eff}%). ` +
        `${autoOn ? "Used whenever the forecast can't be read." : "Raise it in summer, lower in winter."}</span>` +
        `</div>` +
        `</div>` +
        // Add-plant button or the inline editor form
        (this._plantEditor
          ? this._renderPlantForm()
          : `<button class="btn btn-primary yard-add" data-action="add-plant">+ Add plant</button>`) +
        // Plant list
        this._renderPlantList() +
        // v1.35 — recurring care reminders (fertilize/prune/mulch/inspect)
        this._renderCareTasks() +
        // Per-loop design report
        this._renderYardReport()
      );
    }

    _renderYardMap() {
      const m = this._yardMap;
      const setupBtn = this._mapBusy
        ? `<button class="btn" disabled>Fetching aerial…</button>`
        : `<button class="btn" data-action="setup-yard-map">${
            m && m.image_path ? "Refresh aerial" : "Set up yard map"
          }</button>`;
      if (!m || !m.image_path) {
        return (
          `<div class="card yard-map-card">` +
          `<div class="yard-map-head"><strong>🗺️ Yard map</strong>${setupBtn}</div>` +
          `<p class="muted">Fetch an aerial photo of your property (centered on your Home ` +
          `Assistant location) to place plant markers on it.</p>` +
          `</div>`
        );
      }
      const plants = this._plants || [];
      const placed = plants.filter((p) => p.map_x != null && p.map_y != null);
      const unplaced = plants.filter((p) => p.map_x == null || p.map_y == null);
      const markers = placed
        .map(
          (p) =>
            `<button class="yard-map-marker" data-action="map-marker" data-plant-id="${escapeAttr(
              p.id
            )}" style="left:${(p.map_x * 100).toFixed(3)}%;top:${(p.map_y * 100).toFixed(
              3
            )}%" title="${escapeAttr(p.name)} — drag to reposition">` +
            `<span class="yard-map-dot"></span>` +
            `<span class="yard-map-label">${escapeHtml(p.name)}</span>` +
            `</button>`
        )
        .join("");
      const chips = unplaced
        .map(
          (p) =>
            `<button class="yard-chip" data-action="place-plant" data-plant-id="${escapeAttr(
              p.id
            )}">+ ${escapeHtml(p.name)}</button>`
        )
        .join("");
      return (
        `<div class="card yard-map-card">` +
        `<div class="yard-map-head"><strong>🗺️ Yard map</strong>${setupBtn}</div>` +
        `<div class="yard-map-wrap" style="aspect-ratio:${m.width} / ${m.height}">` +
        `<img class="yard-map-img" src="${escapeAttr(m.image_path)}" alt="Aerial view of the yard" draggable="false" />` +
        markers +
        `</div>` +
        (chips
          ? `<div class="yard-map-unplaced"><span class="muted">Tap to place:</span> ${chips}</div>`
          : `<p class="muted yard-map-hint">Drag a marker to reposition it.</p>`) +
        `</div>`
      );
    }

    _renderPlantForm() {
      const e = this._plantEditor;
      const cats = [
        ["very_low", "Very low"],
        ["low", "Low"],
        ["moderate", "Moderate"],
        ["high", "High"],
      ];
      const zones = this._switchOptions();
      return (
        `<form class="card plant-form">` +
        `<h3>${e.id ? "Edit plant" : "Add plant"}</h3>` +
        `<div class="yard-form-grid">` +
        `<div><label>Name</label>` +
        `<input name="name" data-action="plant-field" type="text" value="${escapeAttr(
          e.name
        )}" placeholder="e.g. Lemon tree" required /></div>` +
        // v1.35 — optional species (free text, backend caps at 120)
        `<div><label>Species</label>` +
        `<input name="species" data-action="plant-field" type="text" maxlength="120" value="${escapeAttr(
          e.species || ""
        )}" placeholder="e.g. Citrus limon" /></div>` +
        `<div><label>Water-use category</label>` +
        `<select name="wucols_category" data-action="plant-field">` +
        cats
          .map(
            ([v, l]) =>
              `<option value="${v}"${e.wucols_category === v ? " selected" : ""}>${l}</option>`
          )
          .join("") +
        `</select></div>` +
        `<div><label>Canopy area (ft²)</label>` +
        `<input name="canopy_area_sqft" data-action="plant-field" type="number" min="0.1" step="1" value="${escapeAttr(
          String(e.canopy_area_sqft)
        )}" placeholder="e.g. 100" required /></div>` +
        `<div><label>Zone / loop</label>` +
        `<select name="zone_entity_id" data-action="plant-field">` +
        `<option value="">— pick a zone —</option>` +
        zones
          .map(
            (z) =>
              `<option value="${escapeAttr(z.id)}"${
                e.zone_entity_id === z.id ? " selected" : ""
              }>${escapeHtml(z.name)}</option>`
          )
          .join("") +
        `</select></div>` +
        // v1.35 — light range (SAVED plants only: add_plant takes no lux
        // fields, so on add the pair would be silently discarded — show a
        // hint instead, like the photo/health/light sections below). The
        // preset fills the two lux inputs; both stay editable. Selected
        // preset is inferred from the current pair.
        (e.id
          ? `<div><label>Light preset</label>` +
            `<select name="light_preset" data-action="light-preset">` +
            LIGHT_PRESETS.map(([v, l]) => {
              const cur = `${String(e.lux_low == null ? "" : e.lux_low).trim()}:${String(
                e.lux_high == null ? "" : e.lux_high
              ).trim()}`;
              return `<option value="${escapeAttr(v)}"${
                v && cur === v ? " selected" : ""
              }>${escapeHtml(l)}</option>`;
            }).join("") +
            `</select></div>` +
            `<div><label>Lux low</label>` +
            `<input name="lux_low" data-action="plant-field" type="number" min="0" step="1" value="${escapeAttr(
              String(e.lux_low == null ? "" : e.lux_low)
            )}" placeholder="e.g. 3000" /></div>` +
            `<div><label>Lux high</label>` +
            `<input name="lux_high" data-action="plant-field" type="number" min="0" step="1" value="${escapeAttr(
              String(e.lux_high == null ? "" : e.lux_high)
            )}" placeholder="e.g. 10000" /></div>`
          : `<div><label>Light range</label>` +
            `<span class="light-add-hint">Save the plant first, then set its light range.</span></div>`) +
        `</div>` +
        // v1.32 — photo history (only on a saved plant; you attach photos to an id).
        (e.id ? this._renderPhotoSection(e) : "") +
        // v1.33 — last vision-health verdict (if any).
        (e.id ? this._renderHealthSection(e) : "") +
        // v1.35 — light range + illuminance surveys (saved plants only).
        (e.id ? this._renderLightSection(e) : "") +
        `<div class="yard-form-actions">` +
        `<button class="btn btn-primary" type="submit">${e.id ? "Save" : "Add plant"}</button>` +
        `<button class="btn btn-small" type="button" data-action="cancel-plant">Cancel</button>` +
        `</div>` +
        `</form>`
      );
    }

    _renderPhotoSection(e) {
      const photos = Array.isArray(e.photos) ? e.photos : [];
      const thumbs = photos.length
        ? photos
            .map(
              (p) =>
                `<a class="plant-photo-thumb" href="${escapeAttr(p.path)}" target="_blank" ` +
                `rel="noopener" title="${escapeAttr(photoLabel(p))}">` +
                `<img src="${escapeAttr(p.path)}" alt="${escapeAttr(
                  e.name
                )} photo" loading="lazy" draggable="false" />` +
                `</a>`
            )
            .join("")
        : `<p class="muted plant-photo-empty">No photos yet — add one to track this plant's health over time.</p>`;
      const busy = !!this._photoBusy;
      return (
        `<div class="plant-photos">` +
        `<label class="plant-photos-title">Photos (${photos.length})</label>` +
        `<div class="plant-photo-grid">${thumbs}</div>` +
        `<label class="btn btn-small plant-photo-add${busy ? " is-busy" : ""}">` +
        (busy ? "Uploading…" : "+ Add photo") +
        `<input type="file" accept="image/*" data-action="photo-file" data-plant-id="${escapeAttr(
          e.id
        )}"${busy ? " disabled" : ""} hidden />` +
        `</label>` +
        `<span class="muted plant-photo-hint">A photo with location data places this plant ` +
        `on the map automatically (first photo only); after that, drag the marker to adjust.</span>` +
        `</div>`
      );
    }

    _renderHealthSection(e) {
      // v1.33 — last biannual vision-health verdict (posted by the external vision
      // job; bounded by the backend rail). Advisory display only.
      const h = e.health;
      if (!h || typeof h !== "object") return "";
      const state = String(h.health_state || "unknown");
      const conf = typeof h.confidence === "number" ? Math.round(h.confidence * 100) : null;
      const when = h.assessed_at ? new Date(h.assessed_at).toLocaleDateString() : "";
      const concerns = Array.isArray(h.concerns) ? h.concerns : [];
      const care = Array.isArray(h.suggested_care) ? h.suggested_care : [];
      const li = (arr) => arr.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("");
      return (
        `<div class="plant-health">` +
        `<label class="plant-photos-title">Health check${when ? ` — ${escapeHtml(when)}` : ""}</label>` +
        `<div class="health-row">` +
        `<span class="health-badge health-${escapeAttr(state)}">${escapeHtml(state)}</span>` +
        (conf != null ? `<span class="muted health-conf">${conf}% confidence</span>` : "") +
        (h.model ? `<span class="muted health-model">${escapeHtml(String(h.model))}</span>` : "") +
        `</div>` +
        (h.changes_since_last
          ? `<p class="health-changes">${escapeHtml(String(h.changes_since_last))}</p>`
          : "") +
        (concerns.length
          ? `<div class="health-block"><span class="health-sub">Concerns</span><ul>${li(
              concerns
            )}</ul></div>`
          : "") +
        (care.length
          ? `<div class="health-block"><span class="health-sub">Suggested care</span><ul>${li(
              care
            )}</ul></div>`
          : "") +
        `<span class="muted plant-photo-hint">Biannual vision check — advisory; ` +
        `it never changes watering.</span>` +
        `</div>`
      );
    }

    _renderLightSection(e) {
      // v1.35 — the plant's light range + illuminance survey history and
      // controls. Modeled on _renderHealthSection: advisory display; the
      // range itself is edited via the Lux low/high inputs above.
      const p = (this._plants || []).find((x) => x.id === e.id) || {};
      const surveys = Array.isArray(p.light_surveys) ? p.light_surveys : [];
      const latest = surveys.length ? surveys[0] : null; // newest-first
      const hasRange = p.lux_low != null && p.lux_high != null;
      const rangeTxt = hasRange
        ? `Optimal ${escapeHtml(String(p.lux_low))}–${escapeHtml(String(p.lux_high))} lux`
        : "No range set";
      const fmtLux = (v) => escapeHtml(String(Math.round(Number(v) || 0)));
      const fmtDay = (ts) =>
        escapeHtml(ts ? new Date(ts * 1000).toLocaleDateString() : "");
      const verdictLabel = (v) =>
        escapeHtml(LIGHT_VERDICT_META[v] || String(v == null ? "" : v));
      const latestHtml = latest
        ? `<div class="light-row">` +
          `<span class="light-badge light-${escapeAttr(
            String(latest.verdict || "no_range")
          )}">${verdictLabel(latest.verdict)}</span>` +
          `<span class="light-latest">Avg ${fmtLux(latest.lux_avg)} lux · ${escapeHtml(
            String(latest.samples)
          )} readings · ${fmtDay(latest.ts)}</span>` +
          `</div>`
        : `<p class="light-empty">No surveys yet — run one to see how much light this spot actually gets.</p>`;
      const hist = surveys
        .slice(0, 5)
        .map(
          (s) =>
            `<li>${fmtDay(s.ts)} · ${fmtLux(s.lux_avg)} lux avg · ${verdictLabel(
              s.verdict
            )}</li>`
        )
        .join("");
      // Survey controls — or the in-flight status if one is already running.
      const active = (this._activeLightSurveys || {})[e.id];
      let controls;
      if (active) {
        let untilTxt = "";
        if (active.until) {
          const d = new Date(active.until);
          if (!isNaN(d)) {
            untilTxt = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
          }
        }
        controls =
          `<div class="light-survey-active">` +
          `<span class="light-surveying">Surveying… ${escapeHtml(
            String(active.samples == null ? 0 : active.samples)
          )} readings so far${untilTxt ? ` (until ${escapeHtml(untilTxt)})` : ""}</span>` +
          `<button class="btn btn-small" type="button" data-action="cancel-light-survey" data-plant-id="${escapeAttr(
            e.id
          )}">Cancel</button>` +
          `</div>`;
      } else {
        controls =
          `<div class="light-survey-controls">` +
          `<div class="light-survey-sensor"><label>Illuminance sensor</label>` +
          `<input name="light_survey_sensor" data-action="plant-field" type="text" value="${escapeAttr(
            e.light_survey_sensor || ""
          )}" placeholder="sensor.back_yard_illuminance" /></div>` +
          `<div class="light-survey-minutes"><label>Minutes</label>` +
          `<input name="light_survey_minutes" data-action="plant-field" type="number" min="1" max="240" step="1" value="${escapeAttr(
            String(e.light_survey_minutes || 10)
          )}" /></div>` +
          `<button class="btn btn-small" type="button" data-action="start-light-survey" data-plant-id="${escapeAttr(
            e.id
          )}">Start survey</button>` +
          `</div>`;
      }
      return (
        `<div class="plant-light">` +
        `<label class="plant-light-title">Light</label>` +
        `<div class="light-row"><span class="light-range">${rangeTxt}</span></div>` +
        latestHtml +
        (hist ? `<ul class="light-history">${hist}</ul>` : "") +
        controls +
        `</div>`
      );
    }

    async _addPlantPhoto(file, plantId) {
      if (!file || !plantId || this._photoBusy) return;
      this._photoBusy = true;
      this._renderNow();
      try {
        // Pull EXIF GPS from the ORIGINAL bytes first (canvas re-encoding strips it).
        let gps = null;
        try {
          gps = exifGps(await file.arrayBuffer());
        } catch (_) {
          /* no or malformed EXIF — fine, just no auto-placement */
        }
        const b64 = await downscaleToJpegB64(file, 1280, 0.82);
        if (!b64 || b64.length < 64) throw new Error("could not read the image");
        const payload = { plant_id: plantId, image_base64: b64 };
        // Auto-place ONLY when the plant is still unplaced (the locked workflow:
        // GPS places once; selection owns identity; manual drag owns position).
        const plant = (this._plants || []).find((p) => p.id === plantId);
        const unplaced = !plant || plant.map_x == null || plant.map_y == null;
        if (gps && unplaced) {
          payload.latitude = gps.lat;
          payload.longitude = gps.lon;
        }
        await this._hass.callService("complete_irrigation", "add_plant_photo", payload);
        await this._fetchYard();
        // Re-sync the open editor's gallery from the refreshed plant list.
        if (this._plantEditor && this._plantEditor.id === plantId) {
          const fresh = (this._plants || []).find((p) => p.id === plantId);
          this._plantEditor.photos = fresh && Array.isArray(fresh.photos) ? fresh.photos : [];
        }
      } catch (err) {
        alert("Failed to add photo: " + (err?.message || err));
      } finally {
        this._photoBusy = false;
        this._renderNow();
      }
    }

    _renderPlantList() {
      if (!this._plants.length) {
        return `<div class="empty">No plants yet. Add one to see its watering needs.</div>`;
      }
      const rows = this._plants
        .map(
          (p) =>
            `<tr>` +
            `<td>${escapeHtml(p.name)}</td>` +
            `<td>${escapeHtml(this._catLabel(p.wucols_category))}</td>` +
            `<td>${escapeHtml(String(p.canopy_area_sqft))} ft²</td>` +
            `<td>${escapeHtml(this._zoneFriendly(p.zone_entity_id))}</td>` +
            `<td class="yard-row-actions">` +
            `<button class="btn btn-small" data-action="edit-plant" data-plant-id="${escapeAttr(
              p.id
            )}">Edit</button>` +
            `<button class="btn btn-small btn-stop" data-action="delete-plant" data-plant-id="${escapeAttr(
              p.id
            )}" data-plant-name="${escapeAttr(p.name)}">Delete</button>` +
            `</td></tr>`
        )
        .join("");
      return (
        `<h3 class="yard-h3">Plants (${this._plants.length})</h3>` +
        `<div class="yard-table-wrap"><table class="yard-table">` +
        `<thead><tr><th>Name</th><th>Category</th><th>Area</th><th>Zone</th><th></th></tr></thead>` +
        `<tbody>${rows}</tbody></table></div>`
      );
    }

    _careSubject(t) {
      // v1.35 — a task points at either a plant (resolve its name) or a zone.
      if (t.plant_id) {
        const p = (this._plants || []).find((x) => x.id === t.plant_id);
        return p ? p.name : t.plant_id;
      }
      return t.zone_entity_id || "";
    }

    _renderCareTasks() {
      // v1.35 — recurring care reminders (fertilize/prune/mulch/inspect/custom).
      const tasks = this._careTasks || [];
      const rows = tasks
        .map((t) => {
          const due = !t.enabled
            ? `<span class="care-task-due">Paused</span>`
            : t.is_due
            ? `<span class="care-task-due due-now">Due now</span>`
            : `<span class="care-task-due">Due ${escapeHtml(
                t.next_due_ts
                  ? new Date(t.next_due_ts * 1000).toLocaleDateString()
                  : "now"
              )}</span>`;
          return (
            `<li class="care-task-row${t.enabled ? "" : " care-task-disabled"}">` +
            `<span class="care-task-name">${escapeHtml(t.display_name || "")}</span>` +
            `<span class="care-task-meta">${escapeHtml(
              this._careSubject(t)
            )} · every ${escapeHtml(String(t.interval_days))} days</span>` +
            due +
            `<span class="care-task-actions">` +
            `<button class="btn btn-small" data-action="care-task-done" data-task-id="${escapeAttr(
              t.id
            )}">✓ Done</button>` +
            `<button class="btn btn-small" data-action="care-task-delete" data-task-id="${escapeAttr(
              t.id
            )}" data-task-name="${escapeAttr(t.display_name || "")}" title="Delete">🗑</button>` +
            `</span>` +
            `</li>`
          );
        })
        .join("");
      const kinds = [
        ["fertilize", "Fertilize"],
        ["prune", "Prune"],
        ["mulch", "Mulch"],
        ["inspect", "Inspect"],
        ["custom", "Custom"],
      ];
      const draft = this._careDraft || {};
      const plantOpts = (this._plants || [])
        .map(
          (p) =>
            `<option value="plant:${escapeAttr(p.id)}"${
              draft.care_subject === `plant:${p.id}` ? " selected" : ""
            }>${escapeHtml(p.name)}</option>`
        )
        .join("");
      const zoneOpts = this._zones()
        .map(
          (z) =>
            `<option value="zone:${escapeAttr(z.entityId)}"${
              draft.care_subject === `zone:${z.entityId}` ? " selected" : ""
            }>${escapeHtml(z.name)}</option>`
        )
        .join("");
      return (
        `<h3 class="yard-h3">Care tasks</h3>` +
        `<div class="card care-tasks">` +
        (rows
          ? `<ul class="care-task-list">${rows}</ul>`
          : `<p class="care-tasks-empty">No care tasks yet — add a recurring reminder below.</p>`) +
        `<div class="care-add">` +
        `<div><label>Kind</label><select name="care_kind" data-action="care-field">` +
        kinds
          .map(
            ([v, l]) =>
              `<option value="${v}"${draft.care_kind === v ? " selected" : ""}>${l}</option>`
          )
          .join("") +
        `</select></div>` +
        `<div class="care-add-label"><label>Label</label>` +
        `<input name="care_label" data-action="care-field" type="text" maxlength="120" value="${escapeAttr(
          draft.care_label || ""
        )}" placeholder="Required for custom" /></div>` +
        `<div><label>Every (days)</label>` +
        `<input name="care_interval" data-action="care-field" type="number" min="1" step="1" value="${escapeAttr(
          String(draft.care_interval || 90)
        )}" /></div>` +
        `<div><label>Plant / zone</label><select name="care_subject" data-action="care-field">` +
        (plantOpts ? `<optgroup label="Plants">${plantOpts}</optgroup>` : "") +
        (zoneOpts ? `<optgroup label="Zones">${zoneOpts}</optgroup>` : "") +
        `</select></div>` +
        `<button class="btn btn-primary" type="button" data-action="care-task-add">Add</button>` +
        `</div>` +
        `</div>`
      );
    }

    _renderYardReport() {
      if (!this._yardReports.length) {
        return this._plants.length
          ? `<div class="empty">Add a schedule for these plants' zones to see the water report.</div>`
          : "";
      }
      return (
        `<h3 class="yard-h3">Per-loop design report</h3>` +
        this._yardReports.map((r) => this._renderLoopReportCard(r)).join("")
      );
    }

    _renderLoopReportCard(r) {
      const zoneName = this._zoneFriendly(r.zone_entity_id);
      const capStr = r.max_flow_gph != null ? ` / ${r.max_flow_gph} GPH line` : "";
      const plantRows = (r.plants || [])
        .map((p) => {
          const status = (p.status || "").toLowerCase();
          const pct =
            p.pct_off != null
              ? ` ${p.pct_off >= 0 ? "+" : ""}${Math.round(p.pct_off * 100)}%`
              : "";
          return (
            `<tr>` +
            `<td>${escapeHtml(p.name)}</td>` +
            `<td>${escapeHtml(String(p.need_gal_week))} gal/wk</td>` +
            `<td>${escapeHtml(this._emitterLabel(p.emitters))}</td>` +
            `<td>${escapeHtml(String(p.delivered_gal_week))} gal/wk</td>` +
            `<td><span class="yard-badge ${escapeAttr(status)}">${escapeHtml(
              p.status
            )}${escapeHtml(pct)}</span></td>` +
            `</tr>`
          );
        })
        .join("");
      const warnings = (r.warnings || [])
        .map((w) => `<li>⚠ ${escapeHtml(w)}</li>`)
        .join("");
      const topups = (r.topups || [])
        .map((t) => {
          const over = (t.overwater || [])
            .filter((o) => o.extra_frac > 0.05)
            .map((o) => `${escapeHtml(o.plant_name)} +${Math.round(o.extra_frac * 100)}%`)
            .join(", ");
          if (!t.feasible || t.extra_runs_per_week < 1) {
            return (
              `<li>💧 <strong>${escapeHtml(t.plant_name)}</strong> is short ` +
              `${escapeHtml(String(t.deficit_gal_week))} gal/wk and watering more often ` +
              `can't close it — give it its own loop or a longer main run.</li>`
            );
          }
          return (
            `<li>💧 Top-up <strong>${escapeHtml(t.plant_name)}</strong>: add ` +
            `${t.extra_runs_per_week}&times;/wk &times; ${t.extra_minutes} min on this loop ` +
            `to close its ${escapeHtml(String(t.deficit_gal_week))} gal/wk shortfall` +
            (over ? ` <span class="muted">(also waters ${over})</span>` : "") +
            `.</li>`
          );
        })
        .join("");
      return (
        `<div class="card yard-loop-card">` +
        `<div class="yard-loop-head">` +
        `<strong>${escapeHtml(zoneName)}</strong>` +
        `<span class="muted">${r.runtime_minutes} min · ${this._fmtRuns(
          r.runs_per_week
        )}/wk · ${escapeHtml(String(r.total_flow_gph))} GPH${escapeHtml(
          capStr
        )} · suggested ${r.suggested_runtime_minutes} min</span>` +
        `</div>` +
        (plantRows
          ? `<div class="yard-table-wrap"><table class="yard-table">` +
            `<thead><tr><th>Plant</th><th>Needs</th><th>Emitters</th><th>Gets</th><th>Status</th></tr></thead>` +
            `<tbody>${plantRows}</tbody></table></div>`
          : "") +
        (topups ? `<ul class="yard-topups">${topups}</ul>` : "") +
        (warnings ? `<ul class="yard-warnings">${warnings}</ul>` : "") +
        `</div>`
      );
    }

    _renderSchedules() {
      return (
        `<header class="page-header">` +
        `<h2>Schedules</h2>` +
        `<button class="btn btn-primary" data-action="add-schedule">+ Add Schedule</button>` +
        `</header>` +
        (this._schedules.length === 0
          ? `<div class="empty"><p>No schedules yet. Click "+ Add Schedule" to create one.</p></div>`
          : `<div class="schedule-list">${this._schedules
              .map((s) => this._renderScheduleRow(s))
              .join("")}</div>`)
      );
    }

    _renderScheduleRow(s) {
      // Recurrence label varies by mode.
      let recurrence;
      if (s.mode === "interval") {
        recurrence = `every ${s.interval_days || "?"} day${s.interval_days === 1 ? "" : "s"}`;
      } else if (s.mode === "interval_hours") {
        recurrence = `every ${s.interval_hours || "?"} hour${s.interval_hours === 1 ? "" : "s"}`;
        if (s.interval_end_time) {
          recurrence += ` (${s.start_time}–${s.interval_end_time})`;
        }
      } else {
        recurrence = (s.weekdays || []).map((d) => WEEKDAY_LABELS[d] || "?").join(" ") || "—";
      }
      // Active-period chip
      let periodLabel = "";
      if (s.start_date || s.end_date) {
        const sd = s.start_date || "—";
        const ed = s.end_date || "never";
        const tag = s.repeat_annually ? " (yearly)" : "";
        periodLabel = ` · 📅 ${sd} → ${ed}${tag}`;
      }
      // Multi-zone: show "Front Lawn + 2 more" if extra steps exist
      const extraCount =
        Array.isArray(s.zone_steps) && s.zone_steps.length > 1
          ? s.zone_steps.length - 1
          : 0;
      const zoneName =
        this._zoneName(s.zone_entity_id) +
        (extraCount > 0 ? ` + ${extraCount} more` : "");
      // Format duration as "Xh YYm" if >60 min, else "Xm"
      const dur = s.duration_minutes;
      const durLabel =
        dur >= 60
          ? `${Math.floor(dur / 60)}h ${dur % 60 ? String(dur % 60).padStart(2, "0") + "m" : ""}`.trim()
          : `${dur}m`;
      const enabledClass = s.enabled ? "enabled" : "disabled";
      // v1.18 — left-edge color stripe when a color is set.
      const stripeStyle = s.color
        ? ` style="border-left:4px solid ${escapeAttr(s.color)};padding-left:12px"`
        : "";
      return (
        `<article class="schedule-row ${enabledClass}"${stripeStyle}>` +
        `<div class="schedule-row-main">` +
        `<div class="schedule-name">${escapeHtml(s.name)}${
          s.enabled ? "" : " (disabled)"
        }</div>` +
        `<div class="schedule-meta">` +
        `${escapeHtml(zoneName)} · ${s.start_time} · ${durLabel} · ${escapeHtml(recurrence)}${escapeHtml(periodLabel)}` +
        `</div>` +
        `</div>` +
        `<div class="schedule-row-actions">` +
        // v1.19.0 — "Run" button executes the schedule's full run
        // sequence on demand. Placed leftmost in the actions group so
        // it's the most prominent control. Visually distinct via
        // .btn-schedule-run (green accent) to separate "trigger
        // hardware" from "manage config" actions.
        `<button class="btn btn-small btn-schedule-run" data-action="run-schedule" data-schedule-id="${escapeAttr(
          s.id
        )}" data-schedule-name="${escapeAttr(s.name)}" title="Run this schedule now — fires the full zone sequence with inter-zone buffer, ignoring weather gates.">▶ Run</button>` +
        `<button class="btn btn-small" data-action="toggle-schedule" data-schedule-id="${escapeAttr(
          s.id
        )}" data-enabled="${s.enabled}">${s.enabled ? "Disable" : "Enable"}</button>` +
        `<button class="btn btn-small" data-action="edit-schedule" data-schedule-id="${escapeAttr(
          s.id
        )}">Edit</button>` +
        `<button class="btn btn-small" data-action="copy-schedule" data-schedule-id="${escapeAttr(
          s.id
        )}" title="Duplicate this schedule. Opens the editor pre-filled — change the start time (or anything else) and save.">Copy</button>` +
        `<button class="btn btn-small btn-stop" data-action="delete-schedule" data-schedule-id="${escapeAttr(
          s.id
        )}">Delete</button>` +
        `</div>` +
        `</article>`
      );
    }

    _renderRunModal() {
      // User-customizable default (Settings → Manual run default), falling
      // back to the built-in DEFAULT_MANUAL_MINUTES.
      const userDefault = this._userManualDefault();
      return (
        `<div class="modal-backdrop"></div>` +
        `<div class="modal" role="dialog" aria-modal="true">` +
        `<form class="modal-form run-form">` +
        `<h3>Run ${escapeHtml(this._runModalZoneName)}</h3>` +
        `<label for="minutes-input">Duration (minutes)</label>` +
        `<input id="minutes-input" name="minutes" type="number" min="1" max="${MAX_MANUAL_MINUTES}" step="1" value="${userDefault}" autofocus />` +
        `<p class="hint">Default ${userDefault} min. Maximum ${MAX_MANUAL_MINUTES} min. Change the default in Settings.</p>` +
        `<div class="modal-actions">` +
        `<button type="button" class="btn btn-secondary modal-cancel">Cancel</button>` +
        `<button type="submit" class="btn btn-primary">Run</button>` +
        `</div>` +
        `</form>` +
        `</div>`
      );
    }

    _renderScheduleModal() {
      const e = this._scheduleEditor;
      const allZones = this._panel?.config?.zones || [];
      // Hide zones the user has hidden from the Today view, BUT keep any
      // zone that's currently selected on this schedule (either as the
      // primary or in extra_steps) — otherwise editing a pre-existing
      // schedule whose zone got hidden later would silently drop it.
      const stillInUse = new Set([
        e.zone_entity_id,
        ...(e.extra_steps || []).map((s) => s.zone_entity_id),
      ]);
      const zones = allZones.filter(
        (z) => !this._hiddenZones.has(z) || stillInUse.has(z)
      );
      const zoneOpts = zones
        .map(
          (z) =>
            `<option value="${escapeAttr(z)}"${
              z === e.zone_entity_id ? " selected" : ""
            }>${escapeHtml(this._zoneName(z))} (${escapeHtml(z)})</option>`
        )
        .join("");
      const weekdayChecks = WEEKDAY_LABELS.map(
        (label, idx) =>
          `<label class="weekday-check"><input type="checkbox" name="weekday" value="${idx}"${
            e.weekdays.includes(idx) ? " checked" : ""
          }/>${label}</label>`
      ).join("");

      // v1.19.0 — render the info bubble with a custom popover instead
      // of the native `title` attribute. The native tooltip is delayed
      // ~1.5s on desktop AND silently does NOTHING on touch devices.
      // The custom popover shows immediately on hover, on tap (touch
      // toggles via the .help-tip-open class set by _onClick), and on
      // keyboard focus. tabindex+role makes it screen-reader-aware.
      const tip = (text) =>
        `<span class="help-tip" role="button" tabindex="0" aria-label="${escapeAttr(text)}">` +
        `ⓘ<span class="help-tip-popup">${escapeHtml(text)}</span>` +
        `</span>`;

      // Split total duration_minutes for the two-field display.
      const totalMin = parseInt(e.duration_minutes, 10) || 0;
      const durH = Math.floor(totalMin / 60);
      const durM = totalMin % 60;

      const mode =
        e.mode === "interval"
          ? "interval"
          : e.mode === "interval_hours"
          ? "interval_hours"
          : "weekdays";
      let modeFields;
      if (mode === "interval") {
        modeFields =
          `<div class="row-2">` +
          `<div>` +
          `<label>Every (days) ${tip("Fires every N days from the first-run date. E.g. 5 = every 5 days.")}</label>` +
          `<input name="interval_days" type="number" min="1" max="365" step="1" value="${
            e.interval_days || 5
          }" required />` +
          `</div>` +
          `<div>` +
          `<label>First run date ${tip("The date of the first run. Subsequent runs step by the interval.")}</label>` +
          `<input name="interval_anchor" type="date" value="${escapeAttr(
            e.interval_anchor || _todayIso()
          )}" required />` +
          `</div>` +
          `</div>`;
      } else if (mode === "interval_hours") {
        // Optional daily-window cap (v1.15.0). Empty = legacy continuous.
        const endTime = (e.interval_end_time || "").trim();
        const [endH, endM] = endTime ? endTime.split(":") : ["", ""];
        modeFields =
          `<div class="row-2">` +
          `<div>` +
          `<label>Every (hours) ${tip("Fires every N hours starting at the start time on the first-run date.")}</label>` +
          `<input name="interval_hours" type="number" min="1" max="72" step="1" value="${
            e.interval_hours || 6
          }" required />` +
          `</div>` +
          `<div>` +
          `<label>First run date ${tip("Date of the first cycle.")}</label>` +
          `<input name="interval_anchor" type="date" value="${escapeAttr(
            e.interval_anchor || _todayIso()
          )}" required />` +
          `</div>` +
          `</div>` +
          // Stop firing after — optional daily window cap. When set, the
          // schedule fires every N hours from start_time each day, stops
          // when next firing would exceed this time, then resumes the
          // next day. Empty = legacy continuous-across-days behavior.
          `<label>Stop firing after (optional) ${tip("Cap on each day's firings. When set, every N hours fires from Start time until this time, then waits until the next day's Start. Leave blank to fire continuously across day boundaries.")}</label>` +
          `<div class="schedule-time-row">` +
          `<input name="interval_end_time_h" type="number" min="0" max="23" step="1" placeholder="HH" value="${escapeAttr(endH)}" />` +
          `<span>:</span>` +
          `<input name="interval_end_time_m" type="number" min="0" max="59" step="1" placeholder="MM" value="${escapeAttr(endM)}" />` +
          (endTime
            ? ` <button type="button" class="btn btn-small btn-secondary" data-action="clear-interval-end-time">Clear</button>`
            : "") +
          `</div>`;
      } else {
        modeFields =
          `<label>Weekdays ${tip("Pick the days this schedule fires. Defaults to Mon-Fri.")}</label>` +
          // Quick-pick shortcut row above the 7 day checkboxes.
          `<div class="weekday-shortcuts">` +
          `<button type="button" class="btn btn-small" data-action="weekday-preset" data-preset="all">Every day</button>` +
          `<button type="button" class="btn btn-small" data-action="weekday-preset" data-preset="weekdays">Weekdays only</button>` +
          `<button type="button" class="btn btn-small" data-action="weekday-preset" data-preset="weekends">Weekends only</button>` +
          `</div>` +
          `<div class="weekday-group">${weekdayChecks}</div>`;
      }

      return (
        `<div class="modal-backdrop"></div>` +
        `<div class="modal modal-wide" role="dialog" aria-modal="true">` +
        `<form class="modal-form schedule-form">` +
        `<h3>${e.id ? "Edit Schedule" : "New Schedule"}</h3>` +
        `<label>Name ${tip("A friendly label, e.g. 'Morning Front Lawn'. Used in notifications and the calendar.")}</label>` +
        `<input name="name" type="text" value="${escapeAttr(e.name)}" required autofocus />` +
        // v1.18 — color picker. Swatches set _scheduleEditor.color via
        // the "pick-schedule-color" action; the "None" chip clears it.
        `<label>Color ${tip("Optional. Color-codes this schedule's left-edge stripe on the Schedules tab and its pills on the day calendar — useful for telling zones/areas apart at a glance.")}</label>` +
        `<div class="color-swatches">` +
        `<button type="button" class="color-swatch color-swatch-none${e.color ? "" : " selected"}" data-action="pick-schedule-color" data-color="" title="No color">✕</button>` +
        SCHEDULE_COLORS.map(
          (c) =>
            `<button type="button" class="color-swatch${e.color === c ? " selected" : ""}" data-action="pick-schedule-color" data-color="${escapeAttr(c)}" style="background:${c}" title="${escapeAttr(c)}"></button>`
        ).join("") +
        `</div>` +
        `<label>Zone ${tip("Which switch entity this schedule controls. Comes from the zones picked at integration setup.")}</label>` +
        `<select name="zone_entity_id" required>${
          zoneOpts || `<option value="">No zones configured</option>`
        }</select>` +
        // Split start_time "HH:MM" into hour + minute for the two number
        // inputs. macOS HA app's WKWebView crashes on the native
        // <input type="time"> picker, so we render plain number boxes.
        (() => {
          const [stH, stM] = (e.start_time || "06:00")
            .split(":")
            .map((v) => parseInt(v, 10) || 0);
          return (
            `<div class="row-2 schedule-time-row">` +
            `<div>` +
            `<label>Start time ${tip("Time of day (24h, local) the run starts. Defaults to 06:00.")}</label>` +
            `<div class="duration-row">` +
            `<input name="start_time_h" type="number" min="0" max="23" step="1" value="${stH}" aria-label="Hour (0-23)" required />` +
            `<span class="duration-unit">h</span>` +
            `<input name="start_time_m" type="number" min="0" max="59" step="1" value="${stM}" aria-label="Minute (0-59)" required />` +
            `<span class="duration-unit">m</span>` +
            `</div>` +
            `</div>` +
            `<div>` +
            `<label>Duration ${tip("How long to run, up to 8 hours. Moisture sensors can adjust this up or down at runtime.")}</label>` +
            `<div class="duration-row">` +
            `<input name="duration_h" type="number" min="0" max="8" step="1" value="${durH}" aria-label="Hours" />` +
            `<span class="duration-unit">h</span>` +
            `<input name="duration_m" type="number" min="0" max="59" step="1" value="${durM}" aria-label="Minutes" />` +
            `<span class="duration-unit">m</span>` +
            `</div>` +
            // v1.25 — controller-cap (Rachio) block-delivery notice.
            ((this._scheduleEditor.duration_minutes || 0) >
            (this._config?.controller_max_run_minutes || 58)
              ? `<div class="block-notice">⚠ Longer than your controller's ${
                  this._config?.controller_max_run_minutes || 58
                }-minute per-zone limit. Rachio caps each activation, so this run is ` +
                `delivered in ${Math.ceil(
                  (this._scheduleEditor.duration_minutes || 0) /
                    (this._config?.controller_max_run_minutes || 58)
                )} blocks of up to ${
                  this._config?.controller_max_run_minutes || 58
                } min with a short gap between (off → reset → on), to comply with the ` +
                `Rachio integration.</div>`
              : "") +
            `</div>` +
            `</div>`
          );
        })() +
        `<label>Recurrence ${tip("Weekdays = fires on the days you pick. Every N days = fires once per N-day cycle (good for deep watering trees). Every N hours = fires multiple times per day, cycling across day boundaries.")}</label>` +
        `<div class="mode-group">` +
        `<label class="mode-radio"><input type="radio" name="mode" value="weekdays"${
          mode === "weekdays" ? " checked" : ""
        } /> Weekdays</label>` +
        `<label class="mode-radio"><input type="radio" name="mode" value="interval"${
          mode === "interval" ? " checked" : ""
        } /> Every N days</label>` +
        `<label class="mode-radio"><input type="radio" name="mode" value="interval_hours"${
          mode === "interval_hours" ? " checked" : ""
        } /> Every N hours</label>` +
        `</div>` +
        modeFields +
        // Active period (v1.12): optional start/end dates + annual repeat.
        `<label>Active period ${tip("Optional. Pick when the schedule should be active. Leave blank to start now / never end. 'Repeat every year' makes the date range apply seasonally each year.")}</label>` +
        `<div class="row-2">` +
        `<div>` +
        `<label>Start date <small style="color:var(--ci-text-2)">(blank = start now)</small></label>` +
        `<input name="start_date" type="date" value="${escapeAttr(e.start_date || "")}" />` +
        `</div>` +
        `<div>` +
        `<label>End date <small style="color:var(--ci-text-2)">(blank = never end)</small></label>` +
        `<input name="end_date" type="date" value="${escapeAttr(e.end_date || "")}" />` +
        `</div>` +
        `</div>` +
        `<label class="enabled-check"><input type="checkbox" name="repeat_annually"${
          e.repeat_annually ? " checked" : ""
        } />Repeat every year (same date range each year)</label>` +
        // Multi-zone: additional zones to run back-to-back after the primary.
        `<label>Additional zones (run in order) ${tip("Optional — add more zones to run after the primary one above. They fire back-to-back at run time, each waiting for the previous to finish + 30s valve buffer. Per-zone moisture saturation still skips individual zones.")}</label>` +
        `<div class="extra-steps">` +
        (e.extra_steps || [])
          .map((step, i) => this._renderExtraStepRow(step, i, zones))
          .join("") +
        `<button type="button" class="btn btn-small" data-action="add-extra-step">+ Add another zone</button>` +
        `</div>` +
        `<label class="enabled-check"><input type="checkbox" name="enabled"${
          e.enabled ? " checked" : ""
        } />Enabled ${tip("Toggle off to keep the schedule but stop it from firing. Useful while traveling.")}</label>` +
        // v1.19.0 — per-schedule weather-gate opt-outs. Useful for
        // zones where the global gates don't make sense (e.g. a bird
        // bath fill: no spray drift to defer for wind, no
        // evapotranspiration to boost for hot weather, no point
        // pausing during rain).
        `<h3 class="section-title">Ignore weather gates ${tip("Each toggle turns OFF a global gate for this schedule only. Other schedules still honor the gates normally. Default off (gates apply).")}</h3>` +
        `<label class="enabled-check"><input type="checkbox" name="ignore_wind"${
          e.ignore_wind ? " checked" : ""
        } />Ignore wind defer ${tip("Skip the global wind-speed check. Useful for zones with no spray drift concern (e.g. drip irrigation, a bird bath fill).")}</label>` +
        `<label class="enabled-check"><input type="checkbox" name="ignore_hot_weather"${
          e.ignore_hot_weather ? " checked" : ""
        } />Ignore hot-weather boost ${tip("Skip the hot-weather runtime boost. Useful for fixed-volume zones (bird bath, fountain top-off) where extra water doesn't help.")}</label>` +
        `<label class="enabled-check"><input type="checkbox" name="ignore_rain_lockout"${
          e.ignore_rain_lockout ? " checked" : ""
        } />Ignore rain lockout ${tip("Schedule still fires during a rain-lockout period. Useful for fills/top-offs that the rain doesn't replace.")}</label>` +
        `<div class="modal-actions">` +
        `<button type="button" class="btn btn-secondary modal-cancel">Cancel</button>` +
        `<button type="submit" class="btn btn-primary">${
          e.id ? "Update" : "Create"
        }</button>` +
        `</div>` +
        `</form>` +
        `</div>`
      );
    }

    _renderExtraStepRow(step, idx, zones) {
      // One row per extra step: zone picker + duration h/m + remove button.
      // v1.16 — build <option> markup directly from the zone list, avoiding
      // the prior regex-mutate-of-rendered-HTML hack that broke if a zone
      // entity_id contained `=" selected` substrings.
      const totalMin = parseInt(step.duration_minutes, 10) || 0;
      const h = Math.floor(totalMin / 60);
      const m = totalMin % 60;
      const opts = (zones || [])
        .map(
          (z) =>
            `<option value="${escapeAttr(z)}"${
              z === step.zone_entity_id ? " selected" : ""
            }>${escapeHtml(this._zoneName(z))} (${escapeHtml(z)})</option>`
        )
        .join("");
      return (
        `<div class="extra-step-row" data-step-idx="${idx}">` +
        `<select name="extra_zone" data-step-idx="${idx}" required>${opts || '<option value="">No zones</option>'}</select>` +
        `<div class="extra-step-dur">` +
        `<input name="extra_dur_h" data-step-idx="${idx}" type="number" min="0" max="8" step="1" value="${h}" aria-label="Hours" />` +
        `<span class="duration-unit">h</span>` +
        `<input name="extra_dur_m" data-step-idx="${idx}" type="number" min="0" max="59" step="1" value="${m}" aria-label="Minutes" />` +
        `<span class="duration-unit">m</span>` +
        `</div>` +
        `<button type="button" class="btn-icon" data-action="remove-extra-step" data-step-idx="${idx}" title="Remove">✕</button>` +
        `</div>`
      );
    }

    _styles() {
      return (
        // Default (light) palette uses HA's CSS variables, which already
        // follow the user's HA theme. When [data-theme="dark"] is set
        // on the host we override the palette to a known dark scheme so
        // the panel reads well even if HA itself is light.
        // Note: spaces after commas in the var() fallbacks here are
        // intentional — they keep the bulk replaces of consumer rules
        // (which use the spaceless form) from recursively pointing
        // --ci-text → var(--ci-text). Don't reformat this line.
        `:host{display:block;height:100%;background:var(--ci-bg);color:var(--ci-text);font-family:var(--paper-font-body1_-_font-family, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif);font-size:14px;--ci-bg:var(--primary-background-color, #fafafa);--ci-card:var(--card-background-color, #fff);--ci-text:var(--primary-text-color, #212121);--ci-text-2:var(--secondary-text-color, #727272);--ci-border:var(--divider-color, rgba(0,0,0,0.12));--ci-input-bg:var(--primary-background-color, #fff);--ci-hover:var(--primary-background-color, #f6f6f6);--ci-accent:var(--primary-color, #03a9f4)}` +
        `:host([data-theme="dark"]){--ci-bg:#121417;--ci-card:#1d2024;--ci-text:#e6e8eb;--ci-text-2:#9aa0a6;--ci-border:rgba(255,255,255,0.08);--ci-input-bg:#262a2f;--ci-hover:#2a2e34;--ci-accent:#4fc3f7;background:#121417;color:#e6e8eb}` +
        // Apply the resolved palette across the panel
        `:host([data-theme="dark"]) .sidebar{background:var(--ci-card);border-right-color:var(--ci-border)}` +
        `:host([data-theme="dark"]) .sidebar-item{color:var(--ci-text-2)}` +
        `:host([data-theme="dark"]) .sidebar-item:hover{background:var(--ci-hover);color:var(--ci-text)}` +
        `:host([data-theme="dark"]) .sidebar-item.active{color:var(--ci-accent);background:rgba(79,195,247,0.08);border-left-color:var(--ci-accent)}` +
        `:host([data-theme="dark"]) .zone-tile,:host([data-theme="dark"]) .zone-row,:host([data-theme="dark"]) .schedule-row,:host([data-theme="dark"]) .weather-banner,:host([data-theme="dark"]) .sensor-zone-card,:host([data-theme="dark"]) .weather-form,:host([data-theme="dark"]) .modal,:host([data-theme="dark"]) .forecast-cell,:host([data-theme="dark"]) .placeholder,:host([data-theme="dark"]) .empty{background:var(--ci-card);border-color:var(--ci-border);color:var(--ci-text)}` +
        `:host([data-theme="dark"]) .modal input,:host([data-theme="dark"]) .modal select,:host([data-theme="dark"]) .weather-form input,:host([data-theme="dark"]) .weather-form select{background:var(--ci-input-bg);color:var(--ci-text);border-color:var(--ci-border)}` +
        `:host([data-theme="dark"]) .btn{background:var(--ci-card);color:var(--ci-text);border-color:var(--ci-border)}` +
        `:host([data-theme="dark"]) .btn:hover{background:var(--ci-hover)}` +
        `:host([data-theme="dark"]) .zone-day{background:#262a2f;border-color:var(--ci-border)}` +
        `:host([data-theme="dark"]) .zone-day-on{background:rgba(79,195,247,0.12);border-color:var(--ci-accent)}` +
        `:host([data-theme="dark"]) .version-pill{background:var(--ci-card);color:var(--ci-text-2);border-color:var(--ci-border)}` +
        `:host([data-theme="dark"]) .help-tip{background:#262a2f;color:var(--ci-text-2)}` +
        `:host([data-theme="dark"]) code{background:#262a2f}` +
        // Layout
        `.page-header-right{display:flex;align-items:center;gap:8px}` +
        `.theme-toggle{font-size:18px;line-height:1;opacity:1}` +
        // Banner gear + settings modal
        `.weather-banner{position:relative}` +
        `.banner-gear{position:absolute;top:8px;right:8px;font-size:14px;opacity:0.6}` +
        `.banner-gear:hover{opacity:1}` +
        `.banner-list{max-height:50vh;overflow-y:auto;border:1px solid var(--ci-border);border-radius:6px;padding:4px}` +
        `.banner-row{display:flex;align-items:center;justify-content:space-between;padding:8px 10px;border-bottom:1px solid var(--ci-border);gap:8px}` +
        `.banner-row:last-child{border-bottom:none}` +
        `.banner-row-check{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ci-text);flex:1;cursor:pointer}` +
        `.banner-row-arrows{display:flex;gap:2px}` +
        `.banner-row-arrows .btn-icon{font-size:12px;padding:4px 8px}` +
        `.banner-row-arrows .btn-icon[disabled]{opacity:0.3;cursor:not-allowed}` +
        `*{box-sizing:border-box}` +
        // Fixed-height grid: viewport is 100vh, sidebar stays put while
        // <main> scrolls within its own grid cell. min-height:0 on grid
        // children unlocks shrink-to-fit so the cell can be exactly
        // viewport-tall and let overflow:auto take over inside.
        `.root{display:grid;grid-template-columns:auto 1fr;height:100vh}` +
        `.sidebar{width:220px;background:var(--ci-card);border-right:1px solid var(--ci-border);display:flex;flex-direction:column;transition:width 0.18s ease;height:100vh;overflow-y:auto}` +
        `.sidebar.collapsed{width:60px}` +
        `.sidebar-header{display:flex;align-items:center;padding:12px;border-bottom:1px solid var(--ci-border);gap:8px}` +
        `.collapse-btn{background:transparent;border:1px solid var(--ci-border);border-radius:6px;width:28px;height:28px;cursor:pointer;color:inherit;font-size:16px;line-height:1;display:flex;align-items:center;justify-content:center;flex-shrink:0}` +
        `.brand{font-weight:600;white-space:nowrap;overflow:hidden}` +
        `.sidebar.collapsed .brand,.sidebar.collapsed .sidebar-label{display:none}` +
        `nav{display:flex;flex-direction:column;padding:8px 0;gap:2px}` +
        `.sidebar-item{display:flex;align-items:center;gap:12px;padding:10px 14px;background:transparent;border:none;border-left:3px solid transparent;color:var(--ci-text-2);font-size:14px;text-align:left;cursor:pointer;font-family:inherit}` +
        `.sidebar-item:hover{background:var(--ci-hover);color:var(--ci-text)}` +
        `.sidebar-item.active{color:var(--ci-accent);background:var(--ci-hover);border-left-color:var(--ci-accent);font-weight:500}` +
        `.sidebar-icon{width:24px;text-align:center;font-size:16px;flex-shrink:0}` +
        `main{padding:24px;overflow:auto;min-width:0;min-height:0}` +
        `.page-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;gap:12px}` +
        `.page-header h2{margin:0;font-size:22px;font-weight:600}` +
        `.version-pill{background:var(--ci-card);border:1px solid var(--ci-border);padding:4px 10px;border-radius:999px;font-size:11px;color:var(--ci-text-2)}` +
        `.section-title{font-size:13px;text-transform:uppercase;letter-spacing:0.05em;color:var(--ci-text-2);margin:16px 0 8px}` +
        // Zone tiles
        `.zone-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}` +
        `.zone-tile{background:var(--ci-card);border:1px solid var(--ci-border);border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:6px}` +
        `.zone-tile header{display:flex;align-items:center;gap:10px}` +
        `.zone-tile h4{margin:0;font-size:15px;font-weight:600}` +
        // v1.19.0 — moisture stats line on the Today zone tile
        `.zone-moisture{font-size:13px;color:var(--ci-text);display:flex;align-items:baseline;gap:4px;flex-wrap:wrap;cursor:help}` +
        `.zone-moisture strong{font-weight:700}` +
        `.zone-moisture-band{font-size:11px;color:var(--ci-text-2)}` +
        `.zone-moisture-low{color:#db4437}` +
        `.zone-moisture-low .zone-moisture-band{color:#db4437;opacity:0.8}` +
        `.status-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}` +
        `.status-dot.idle{background:#bdbdbd}` +
        `.status-dot.running{background:#43a047;box-shadow:0 0 0 4px rgba(67,160,71,0.2)}` +
        `.status-dot.unavailable{background:#db4437}` +
        `.entity-id{font-size:11px;color:var(--ci-text-2);font-family:var(--ha-font-family-code,monospace);word-break:break-all}` +
        `.status-text{font-size:12px;color:var(--ci-text-2)}` +
        `.zone-actions{margin-top:8px}` +
        // Schedule list
        `.schedule-list{display:flex;flex-direction:column;gap:8px}` +
        `.schedule-row{background:var(--ci-card);border:1px solid var(--ci-border);border-radius:10px;padding:12px 14px;display:flex;align-items:center;justify-content:space-between;gap:12px}` +
        `.schedule-row.disabled{opacity:0.55}` +
        `.schedule-row-main{flex:1;min-width:0}` +
        `.schedule-name{font-weight:600;font-size:15px}` +
        `.schedule-meta{color:var(--ci-text-2);font-size:12px;margin-top:4px}` +
        `.schedule-row-actions{display:flex;gap:6px;flex-shrink:0}` +
        // Buttons
        `.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:8px 14px;border-radius:8px;border:1px solid var(--ci-border);background:var(--ci-card);color:var(--ci-text);font-family:inherit;font-size:13px;font-weight:500;cursor:pointer}` +
        `.btn:hover{background:var(--ci-hover)}` +
        `.btn:disabled{opacity:0.5;cursor:not-allowed}` +
        `.btn-run,.btn-stop,.btn-primary{color:#fff}` +
        `.btn-run{background:var(--ci-accent);border-color:var(--ci-accent);width:100%}` +
        `.btn-stop{background:#db4437;border-color:#db4437;width:100%}` +
        `.btn-primary{background:var(--ci-accent);border-color:var(--ci-accent)}` +
        // Keep colored buttons their color on hover — the generic
        // .btn:hover above would otherwise override their background.
        `.btn-run:hover{background:var(--ci-accent);filter:brightness(1.08)}` +
        `.btn-stop:hover{background:#db4437;filter:brightness(1.08)}` +
        `.btn-primary:hover{background:var(--ci-accent);filter:brightness(1.08)}` +
        `.btn-secondary{background:transparent}` +
        // v1.19.0 — Run-schedule button on each schedule row. Green
        // accent separates "trigger hardware" from neutral config
        // actions (Edit / Copy / Disable). NOT width:100% — sits in
        // the action row alongside the others.
        `.btn-schedule-run{background:#43a047;border-color:#43a047;color:#fff;font-weight:600}` +
        `.btn-schedule-run:hover{background:#43a047;filter:brightness(1.08)}` +
        // v1.18 — schedule color picker swatches in the editor.
        `.color-swatches{display:flex;flex-wrap:wrap;gap:8px;margin:4px 0 10px}` +
        `.color-swatch{width:28px;height:28px;border-radius:50%;border:2px solid var(--ci-border);cursor:pointer;padding:0;position:relative;transition:transform 0.1s}` +
        `.color-swatch:hover{transform:scale(1.12)}` +
        `.color-swatch.selected{border-color:var(--ci-text);box-shadow:0 0 0 2px var(--ci-accent)}` +
        `.color-swatch-none{background:var(--ci-hover);color:var(--ci-text-2);font-size:13px;display:inline-flex;align-items:center;justify-content:center}` +
        `.btn-small{padding:6px 10px;font-size:12px}` +
        `.empty{background:var(--ci-card);border:1px dashed var(--ci-border);border-radius:12px;padding:24px;text-align:center;color:var(--ci-text-2)}` +
        // v2 — Yard tab (plant-aware irrigation)
        `.card{background:var(--ci-card);border:1px solid var(--ci-border);border-radius:12px;padding:16px;margin-bottom:14px}` +
        `.muted{color:var(--ci-text-2);font-size:12px}` +
        `.yard-intro h2{margin:0 0 4px}` +
        `.yard-intro p{margin:0 0 14px;max-width:60ch}` +
        `.yard-eto label{display:block;font-size:12px;color:var(--ci-text-2);margin-bottom:6px}` +
        `.yard-eto-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}` +
        `.yard-eto-row input{width:90px;padding:8px 10px;border:1px solid var(--ci-border);border-radius:6px;background:var(--ci-input-bg);color:inherit;font:inherit;box-sizing:border-box}` +
        `.yard-add{margin-bottom:14px}` +
        `.plant-form h3{margin:0 0 12px;font-size:15px}` +
        `.plant-form label{display:block;font-size:12px;color:var(--ci-text-2);margin-bottom:4px}` +
        `.plant-form input,.plant-form select{width:100%;padding:8px 10px;border:1px solid var(--ci-border);border-radius:6px;background:var(--ci-input-bg);color:inherit;font:inherit;box-sizing:border-box}` +
        `.yard-form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}` +
        `.yard-form-actions{display:flex;gap:8px;margin-top:14px}` +
        // v1.32 — per-plant photo gallery in the edit form
        `.plant-photos{margin-top:14px;border-top:1px solid var(--ci-border);padding-top:12px}` +
        `.plant-photos-title{display:block;font-size:12px;color:var(--ci-text-2);margin-bottom:8px}` +
        `.plant-photo-grid{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}` +
        `.plant-photo-thumb{display:block;width:72px;height:72px;border-radius:8px;overflow:hidden;border:1px solid var(--ci-border);background:var(--ci-hover)}` +
        `.plant-photo-thumb img{width:100%;height:100%;object-fit:cover;display:block}` +
        `.plant-photo-empty{margin:0 0 10px;font-size:12px}` +
        `.plant-photo-add{cursor:pointer;display:inline-block}` +
        `.plant-photo-add.is-busy{opacity:0.6;cursor:default}` +
        `.plant-photo-hint{display:block;margin-top:8px;font-size:11px}` +
        // v1.33 — vision-health verdict card
        `.plant-health{margin-top:14px;border-top:1px solid var(--ci-border);padding-top:12px}` +
        `.health-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}` +
        `.health-badge{font-size:12px;font-weight:600;padding:2px 8px;border-radius:10px;text-transform:capitalize;background:var(--ci-hover);color:var(--ci-text)}` +
        `.health-thriving,.health-healthy{background:#1f7a3f;color:#fff}` +
        `.health-stressed{background:#b8860b;color:#fff}` +
        `.health-declining{background:#a3312a;color:#fff}` +
        `.health-conf,.health-model{font-size:12px}` +
        `.health-changes{margin:4px 0;font-size:13px}` +
        `.health-block{margin:6px 0}` +
        `.health-sub{display:block;font-size:11px;color:var(--ci-text-2);text-transform:uppercase;letter-spacing:0.03em}` +
        `.health-block ul{margin:2px 0 0;padding-left:18px;font-size:13px}` +
        // v1.35 — plant light range + illuminance survey section
        `.light-add-hint{display:block;font-size:13px;color:var(--ci-text-2);padding:8px 0}` +
        `.plant-light{margin-top:14px;border-top:1px solid var(--ci-border);padding-top:12px}` +
        `.plant-light-title{display:block;font-size:13px;color:var(--ci-text-2);margin-bottom:8px}` +
        `.light-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px;font-size:13px}` +
        `.light-range{font-weight:600;color:var(--ci-text)}` +
        `.light-badge{font-size:13px;font-weight:600;padding:2px 8px;border-radius:10px;background:var(--ci-hover);color:var(--ci-text)}` +
        `.light-too_low{background:rgba(249,168,37,0.18);color:#b26a00}` +
        `.light-optimal{background:rgba(67,160,71,0.16);color:#2e7d32}` +
        `.light-too_high{background:rgba(219,68,55,0.16);color:#c62828}` +
        `.light-no_range{background:var(--ci-hover);color:var(--ci-text-2)}` +
        `.light-latest{font-size:13px;color:var(--ci-text-2)}` +
        `.light-empty{margin:0 0 8px;font-size:13px;color:var(--ci-text-2)}` +
        `.light-history{margin:6px 0 8px;padding-left:18px;font-size:13px;color:var(--ci-text-2)}` +
        `.light-history li{margin:2px 0}` +
        `.light-survey-controls{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-top:8px}` +
        `.light-survey-controls label{display:block;font-size:13px;color:var(--ci-text-2);margin-bottom:4px}` +
        `.light-survey-sensor{flex:1;min-width:220px}` +
        `.light-survey-minutes input{width:90px}` +
        `.light-survey-active{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px}` +
        `.light-surveying{font-size:13px;font-weight:600;color:var(--ci-accent)}` +
        // v1.35 — care-tasks card (Yard tab)
        `.care-task-list{list-style:none;margin:0 0 12px;padding:0;display:flex;flex-direction:column;gap:8px}` +
        `.care-task-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:8px 10px;border:1px solid var(--ci-border);border-radius:8px}` +
        `.care-task-disabled{opacity:0.55}` +
        `.care-task-name{font-weight:600;font-size:13px;color:var(--ci-text)}` +
        `.care-task-meta{font-size:13px;color:var(--ci-text-2)}` +
        `.care-task-due{font-size:13px;font-weight:600;padding:2px 8px;border-radius:10px;background:var(--ci-hover);color:var(--ci-text-2);white-space:nowrap}` +
        `.care-task-due.due-now{background:rgba(219,68,55,0.16);color:#c62828}` +
        `.care-task-actions{display:flex;gap:6px;margin-left:auto}` +
        `.care-tasks-empty{margin:0 0 12px;font-size:13px;color:var(--ci-text-2)}` +
        `.care-add{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;border-top:1px solid var(--ci-border);padding-top:12px}` +
        `.care-add label{display:block;font-size:13px;color:var(--ci-text-2);margin-bottom:4px}` +
        `.care-add input,.care-add select{padding:8px 10px;border:1px solid var(--ci-border);border-radius:6px;background:var(--ci-input-bg);color:inherit;font:inherit;box-sizing:border-box}` +
        `.care-add-label{flex:1;min-width:180px}` +
        `.care-add-label input{width:100%}` +
        // v1.32 — advisory "Today's plan" card
        `.daily-plan-card{margin-bottom:18px;padding:14px 16px;border:1px solid var(--ci-border);border-radius:10px;background:var(--ci-card)}` +
        `.plan-summary{margin:4px 0 10px;font-size:13px;color:var(--ci-text)}` +
        `.plan-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}` +
        `.plan-item{display:grid;grid-template-columns:auto auto 1fr;align-items:baseline;gap:8px;font-size:13px}` +
        `.plan-rec{font-size:11px}` +
        `.plan-zone{font-weight:600;color:var(--ci-text)}` +
        `.plan-reason{color:var(--ci-text-2);font-size:12px}` +
        `.plan-skip .plan-zone{color:var(--ci-text-2);text-decoration:line-through}` +
        `.yard-h3{margin:18px 0 8px;font-size:14px}` +
        `.yard-table-wrap{overflow-x:auto}` +
        `.yard-table{width:100%;border-collapse:collapse;font-size:13px}` +
        `.yard-table th{text-align:left;font-weight:600;color:var(--ci-text-2);padding:6px 10px;border-bottom:1px solid var(--ci-border);white-space:nowrap}` +
        `.yard-table td{padding:7px 10px;border-bottom:1px solid var(--ci-border);vertical-align:middle}` +
        `.yard-row-actions{display:flex;gap:6px;justify-content:flex-end}` +
        `.yard-loop-card{padding:14px 16px}` +
        `.yard-loop-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:8px}` +
        `.yard-loop-head strong{font-size:14px}` +
        `.yard-warnings{margin:10px 0 0;padding:0;list-style:none;font-size:12px}` +
        `.yard-warnings li{margin:4px 0;color:#c77800}` +
        `.yard-topups{margin:10px 0 0;padding:0;list-style:none;font-size:12px}` +
        `.yard-topups li{margin:4px 0;color:#1565c0}` +
        // v1.30 — yard map
        `.yard-map-card{padding:14px 16px}` +
        `.yard-map-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}` +
        `.yard-map-wrap{position:relative;width:100%;border-radius:8px;overflow:hidden;background:#1b1b1b;touch-action:none;user-select:none}` +
        `.yard-map-img{display:block;width:100%;height:100%;object-fit:cover;pointer-events:none}` +
        `.yard-map-marker{position:absolute;transform:translate(-50%,-50%);background:none;border:none;padding:0;cursor:grab;display:flex;flex-direction:column;align-items:center;gap:2px;z-index:2}` +
        `.yard-map-marker.dragging{cursor:grabbing;z-index:5}` +
        `.yard-map-dot{width:16px;height:16px;border-radius:50%;background:#e53935;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,0.5)}` +
        `.yard-map-label{font-size:11px;font-weight:600;color:#fff;background:rgba(0,0,0,0.55);padding:1px 5px;border-radius:6px;white-space:nowrap;max-width:120px;overflow:hidden;text-overflow:ellipsis}` +
        `.yard-map-unplaced{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}` +
        `.yard-chip{font-size:12px;padding:3px 9px;border-radius:12px;border:1px solid var(--ci-border,#444);background:var(--ci-card-2,#2a2a2a);color:var(--ci-text,#eee);cursor:pointer}` +
        `.yard-map-hint{margin:8px 0 0;font-size:12px}` +
        `.yard-badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap}` +
        `.yard-badge.ok{background:rgba(67,160,71,0.16);color:#2e7d32}` +
        `.yard-badge.under{background:rgba(249,168,37,0.18);color:#b26a00}` +
        `.yard-badge.over{background:rgba(219,68,55,0.16);color:#c62828}` +
        // v1.25 — controller-cap block-delivery notice in the schedule editor
        `.block-notice{margin:8px 0 0;padding:8px 10px;border-radius:8px;font-size:12px;line-height:1.45;background:rgba(249,168,37,0.14);color:#b26a00;border:1px solid rgba(249,168,37,0.32)}` +
        `.placeholder{background:var(--ci-card);border:1px solid var(--ci-border);border-radius:12px;padding:24px}` +
        // Modal
        `.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.45);z-index:99}` +
        // v1.19.0 — modal cap at 90vh + internal scroll, with the
        // sticky .modal-actions footer below providing the visible
        // bottom edge. padding-bottom is 0 because .modal-actions
        // bridges through the modal's left/right padding (negative
        // margins) and provides its own padding so the buttons row
        // sits flush at the modal's true bottom edge.
        //
        // Prior v1.17.9 attempt used bottom: -24px on .modal-actions
        // (intent: align with modal's 24px padding-bottom), but that
        // actually pushed the sticky element 24px BELOW the visible
        // scroll viewport — buttons went offscreen on tall modals
        // exactly like the original report. Schedule editor revealed
        // it; sensor modal hid it because v1.17.7's search filter
        // kept the list short enough to not scroll.
        `.modal{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--ci-card);color:var(--ci-text);border-radius:12px;padding:24px 24px 0;min-width:320px;max-width:90vw;max-height:90vh;overflow-y:auto;z-index:100;box-shadow:0 10px 40px rgba(0,0,0,0.3)}` +
        `.modal-wide{min-width:420px;max-width:480px}` +
        `.modal h3{margin:0 0 16px;font-size:16px}` +
        `.modal label{display:block;font-size:12px;color:var(--ci-text-2);margin:10px 0 4px}` +
        `.modal input[type=number],.modal input[type=text],.modal input[type=time],.modal input[type=date],.modal select,.modal textarea{width:100%;min-width:0;padding:8px 10px;border:1px solid var(--ci-border);border-radius:6px;font-size:14px;background:var(--ci-input-bg);color:inherit;font-family:inherit;box-sizing:border-box}` +
        // Same shape for textareas anywhere in the panel (Notifications uses one)
        `.weather-form textarea{width:100%;min-width:0;padding:8px 10px;border:1px solid var(--ci-border);border-radius:6px;font-size:14px;background:var(--ci-input-bg);color:inherit;font-family:inherit;box-sizing:border-box;resize:vertical}` +
        `.modal .hint{margin:6px 0 16px;font-size:11px;color:var(--ci-text-2)}` +
        // v1.19.0 — sticky footer pinned to the modal's visible
        // bottom. bottom: 0 sticks at the scroll-viewport edge
        // (NOT -24px — that put it offscreen). Negative left/right
        // margins extend through .modal's horizontal padding so the
        // background sits edge-to-edge; the modal has padding-bottom:0
        // so the buttons' own padding (14px) becomes the visual
        // bottom edge of the modal.
        `.modal-actions{position:sticky;bottom:0;display:flex;gap:8px;justify-content:flex-end;margin:18px -24px 0;padding:14px 24px;background:var(--ci-card);border-top:1px solid var(--ci-border);border-radius:0 0 12px 12px;box-shadow:0 -4px 12px rgba(0,0,0,0.08);z-index:1}` +
        // Two-column row: min-width:0 on each cell lets <input type=time/date> shrink
        // so the right cell doesn't overflow into the left.
        `.row-2{display:grid;grid-template-columns:1fr 1fr;gap:10px}` +
        // ── Today's runs timeline (horizontal time strip below Zones)
        `.today-timeline{margin-top:24px}` +
        // Day calendar — vertical list with date picker, replaces the
        // old horizontal timeline + tomorrow list on the Today screen.
        `.day-cal{margin-top:24px}` +
        `.day-cal-nav{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}` +
        `.day-cal-label{flex:1;font-weight:600;font-size:15px;color:var(--ci-text)}` +
        `.day-cal-count{font-weight:400;font-size:13px;color:var(--ci-text-2);margin-left:6px}` +
        // 2-day window — side-by-side columns on desktop, stacked on mobile.
        `.day-cal-cols{display:grid;grid-template-columns:1fr 1fr;gap:12px}` +
        `.day-cal-col{display:flex;flex-direction:column;min-width:0}` +
        `.day-cal-col-head{display:flex;justify-content:space-between;align-items:baseline;padding:4px 4px 8px;font-weight:600;font-size:13px;color:var(--ci-text)}` +
        `.day-cal-col-count{color:var(--ci-text-2);font-weight:400;font-size:12px}` +
        `.day-cal-arrow{color:var(--ci-text-2);margin:0 6px}` +
        // 24-hour time grid. 1 minute = 1px; full day = 1440px. Scrollable
        // viewport caps at ~600px so the section doesn't dominate the page.
        `.day-cal-grid{position:relative;height:600px;overflow-y:auto;background:var(--ci-card);border:1px solid var(--ci-border);border-radius:12px}` +
        `.day-cal-hours{position:relative;width:100%;height:1440px}` +
        // Solid line at top of each hour.
        `.day-cal-hour{position:absolute;left:0;right:0;height:60px;border-top:1px solid var(--ci-border)}` +
        // Half-hour mark — 50% opacity line at the 30-minute point.
        `.day-cal-hour::after{content:"";position:absolute;left:0;right:0;top:30px;border-top:1px solid var(--ci-border);opacity:0.5}` +
        `.day-cal-hour-label{position:absolute;left:6px;top:2px;font-size:10px;color:var(--ci-text-2);font-variant-numeric:tabular-nums}` +
        `.day-cal-pills{position:absolute;top:0;left:60px;right:8px;height:1440px;pointer-events:none}` +
        `.day-cal-pill{position:absolute;left:0;right:0;background:var(--ci-accent);color:#fff;border-radius:6px;padding:3px 8px;font-size:11px;line-height:1.15;overflow:hidden;cursor:pointer;pointer-events:auto;box-shadow:0 1px 2px rgba(0,0,0,0.15);box-sizing:border-box;transition:min-height 0.12s ease,box-shadow 0.12s ease}` +
        // On hover — expand the pill so all details are visible without
        // a separate tooltip. min-height kicks in immediately so 18px
        // pills become full-info cards; z-index raises above neighbors.
        `.day-cal-pill:hover{min-height:78px;z-index:20;box-shadow:0 6px 16px rgba(0,0,0,0.28);filter:brightness(1.05)}` +
        `.day-cal-pill.past{opacity:0.55;background:var(--ci-text-2)}` +
        `.day-cal-pill.live{background:#43a047;box-shadow:0 0 0 3px rgba(67,160,71,0.35)}` +
        `.day-cal-pill-time{font-weight:700;font-size:10px;opacity:0.95}` +
        `.day-cal-pill-zone{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}` +
        `.day-cal-pill-meta{font-size:10px;opacity:0.9;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}` +
        // On hover, allow the meta line to wrap (full info visible)
        `.day-cal-pill:hover .day-cal-pill-meta{white-space:normal;overflow:visible}` +
        `.day-cal-pill:hover .day-cal-pill-zone{white-space:normal;overflow:visible}` +
        // Red "now" line — only shown on today.
        // v1.19.0 — enhanced "now" line with a left-edge labeled chip
        // and a subtle pulse so it's obvious where the current time is
        // on the day calendar. The line itself remains pointer-events:
        // none so clicks pass through to underlying pills; the label
        // chip can still be hovered for the title tooltip.
        `.day-cal-now{position:absolute;left:0;right:0;border-top:3px solid #db4437;z-index:5;pointer-events:none;animation:day-cal-now-pulse 2.4s ease-in-out infinite}` +
        `.day-cal-now-label{position:absolute;left:0;top:-9px;background:#db4437;color:#fff;font-size:10px;font-weight:700;padding:1px 6px;border-radius:9px;font-variant-numeric:tabular-nums;letter-spacing:0.2px;box-shadow:0 1px 3px rgba(219,68,55,0.45);pointer-events:auto;cursor:help}` +
        `@keyframes day-cal-now-pulse{0%{box-shadow:0 0 0 0 rgba(219,68,55,0.55)}70%{box-shadow:0 0 0 6px rgba(219,68,55,0)}100%{box-shadow:0 0 0 0 rgba(219,68,55,0)}}` +
        `.day-cal-empty-hint{position:absolute;top:24px;left:60px;right:8px;text-align:center;color:var(--ci-text-2);font-size:13px}` +
        // Run history tab
        // Notification target rows (v1.15) — replaces the textarea
        `.notify-target-list{display:flex;flex-direction:column;gap:6px;margin:4px 0 10px}` +
        `.notify-target-row{display:flex;gap:8px;align-items:center}` +
        `.notify-target-select{flex:1;padding:8px 10px;border:1px solid var(--ci-border);border-radius:6px;font-size:14px;background:var(--ci-input-bg);color:inherit;font-family:inherit;min-width:0}` +
        `.notify-target-remove{flex:0 0 auto;padding:6px 10px;background:transparent;border:1px solid var(--ci-border);border-radius:6px;color:var(--ci-text-2);cursor:pointer;font-size:14px;line-height:1}` +
        `.notify-target-remove:hover{color:#db4437;border-color:#db4437}` +
        `.notify-target-actions{margin-bottom:14px}` +
        `.history-toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-bottom:10px}` +
        `.history-toolbar label{display:flex;flex-direction:column;font-size:11px;color:var(--ci-text-2);gap:4px}` +
        `.history-toolbar select{padding:6px 8px;border:1px solid var(--ci-border);border-radius:6px;background:var(--ci-input-bg);color:inherit;font-family:inherit;font-size:13px}` +
        `.history-summary{margin:4px 0 10px;font-size:12px;color:var(--ci-text-2)}` +
        `.history-loading{font-size:12px;color:var(--ci-text-2);margin:6px 0}` +
        `.history-table-wrap{background:var(--ci-card);border:1px solid var(--ci-border);border-radius:12px;overflow-x:auto}` +
        `.history-table{width:100%;border-collapse:collapse;font-size:13px}` +
        `.history-table th{text-align:left;padding:10px 12px;font-weight:600;border-bottom:1px solid var(--ci-border);background:var(--ci-card);color:var(--ci-text-2);font-size:11px;letter-spacing:0.4px;text-transform:uppercase}` +
        `.history-table td{padding:8px 12px;border-bottom:1px solid var(--ci-border);vertical-align:top}` +
        `.history-row:hover{background:var(--ci-hover)}` +
        `.history-when{white-space:nowrap;font-variant-numeric:tabular-nums}` +
        `.history-zone{font-weight:500}` +
        `.history-dim{color:var(--ci-text-2)}` +
        `.history-reason{display:inline-block;font-size:11px;color:var(--ci-text-2);margin-top:2px}` +
        `.history-status{display:inline-block;padding:2px 8px;border-radius:999px;font-weight:600;font-size:10px;letter-spacing:0.3px;text-transform:uppercase}` +
        `.history-status-completed{background:rgba(67,160,71,0.18);color:#2e7d32}` +
        `.history-status-skipped{background:rgba(0,0,0,0.07);color:var(--ci-text-2)}` +
        `.history-status-aborted{background:rgba(219,68,55,0.18);color:#c62828}` +
        `.history-status-running{background:rgba(3,169,244,0.18);color:var(--ci-accent)}` +
        `.history-trigger-toggle{font-size:11px;padding:3px 8px}` +
        `.history-block-progress{display:inline-block;vertical-align:middle;width:70px;height:6px;border-radius:3px;background:var(--ci-border,#444);margin:0 6px;overflow:hidden}` +
        `.history-block-bar{height:100%;background:#43a047;border-radius:3px}` +
        `.history-block-count{font-size:11px;color:var(--ci-text-2,#aaa)}` +
        `.history-expanded-row td{background:var(--ci-hover)}` +
        `.history-triggers{margin:0;font-size:11px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ci-text);white-space:pre-wrap}` +
        `.history-empty{text-align:center;color:var(--ci-text-2);padding:24px}` +
        `:host([data-theme="dark"]) .history-status-completed{background:rgba(67,160,71,0.22);color:#a5d6a7}` +
        `:host([data-theme="dark"]) .history-status-aborted{background:rgba(219,68,55,0.25);color:#ef9a9a}` +
        // Timeline pills are now clickable too (kept for retro-compat)
        `.timeline-pill{cursor:pointer}` +
        `.timeline-track{position:relative;padding-top:18px;padding-bottom:46px;background:var(--ci-card);border:1px solid var(--ci-border);border-radius:12px;padding-left:8px;padding-right:8px}` +
        `.timeline-axis{position:relative;height:14px;border-bottom:1px solid var(--ci-border);margin-bottom:8px}` +
        `.timeline-tick{position:absolute;top:0;transform:translateX(-50%);font-size:10px;color:var(--ci-text-2);white-space:nowrap}` +
        `.timeline-tick:first-child{transform:translateX(0)}` +
        `.timeline-tick:last-child{transform:translateX(-100%)}` +
        `.timeline-bar{position:relative;height:42px}` +
        `.timeline-pill{position:absolute;top:0;height:36px;background:var(--ci-accent);color:#fff;border-radius:6px;padding:4px 6px;font-size:11px;line-height:1.15;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;box-sizing:border-box;cursor:default;display:flex;flex-direction:column;justify-content:center;box-shadow:0 1px 2px rgba(0,0,0,0.15)}` +
        `.timeline-pill-time{font-weight:700;font-size:10px;opacity:0.9}` +
        `.timeline-pill-zone{font-weight:600;overflow:hidden;text-overflow:ellipsis}` +
        `.timeline-pill-past{background:var(--ci-text-2);opacity:0.6}` +
        `.timeline-pill-live{box-shadow:0 0 0 3px rgba(67,160,71,0.35);background:#43a047}` +
        `.timeline-now{position:absolute;top:-4px;bottom:-4px;width:2px;background:#db4437;z-index:2}` +
        `.timeline-now::after{content:"now";position:absolute;top:-14px;left:4px;font-size:9px;color:#db4437;font-weight:700;letter-spacing:0.5px}` +
        // Schedule modal time + duration row — give Start time a wider
        // cell so the native time picker isn't cramped, since Duration
        // is just two compact number inputs.
        // Start time gets just enough width for "6:00 AM" + padding; the
        // wider Duration cell holds the two number inputs. The bumped
        // gap keeps the cells visually distinct on narrow viewports.
        `.schedule-time-row{grid-template-columns:1fr 1.4fr;gap:16px}` +
        // Cap the time input width so the Start cell stops crowding
        // Duration — 120px fits "6:00 AM" plus the native picker chrome.
        // Duration's input widths are unchanged.
        `.schedule-time-row input[type=time]{font-size:15px;text-align:center;max-width:120px}` +
        `.row-2 > *{min-width:0}` +
        // Duration row (hours + minutes side by side inside one cell)
        `.duration-row{display:flex;align-items:center;gap:6px}` +
        `.duration-row input{flex:1;min-width:0;text-align:center}` +
        `.duration-unit{font-size:12px;color:var(--ci-text-2)}` +
        // Mode toggle (weekdays / interval radios)
        `.mode-group{display:flex;gap:8px;margin-bottom:4px}` +
        `.mode-radio{display:inline-flex;align-items:center;gap:6px;padding:8px 12px;border:1px solid var(--ci-border);border-radius:6px;cursor:pointer;font-size:13px;color:var(--ci-text);margin:0;flex:1}` +
        `.mode-radio input{margin:0}` +
        // Multi-zone extra-step rows
        `.extra-steps{display:flex;flex-direction:column;gap:6px;margin-bottom:8px}` +
        `.extra-step-row{display:grid;grid-template-columns:1fr auto auto;gap:8px;align-items:center;padding:6px;border:1px solid var(--ci-border);border-radius:6px;background:var(--ci-hover)}` +
        `.extra-step-row > select{min-width:0}` +
        `.extra-step-dur{display:flex;align-items:center;gap:4px}` +
        `.extra-step-dur input{width:54px;text-align:center}` +
        // v1.32 — the compact h/m and per-zone duration inputs were clipping
        // 2-digit values ("30" -> "3C"): the native number-spinner arrows plus
        // padding ate the interior width. Hide the spinners (min/max/step already
        // constrain the value, and WKWebView in the HA macOS app renders fat
        // steppers) and trim the side padding so the digits fit. `.modal …`
        // outranks the base `.modal input[type=number]` rule.
        `.modal .duration-row input,.modal .extra-step-dur input,.modal .schedule-time-row input[type=number]{appearance:textfield;-moz-appearance:textfield;padding-left:6px;padding-right:6px;min-width:2.6em}` +
        `.modal .duration-row input::-webkit-inner-spin-button,.modal .duration-row input::-webkit-outer-spin-button,.modal .extra-step-dur input::-webkit-inner-spin-button,.modal .extra-step-dur input::-webkit-outer-spin-button,.modal .schedule-time-row input[type=number]::-webkit-inner-spin-button,.modal .schedule-time-row input[type=number]::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}` +
        `.modal .extra-step-dur input{width:3.6em;flex:0 0 auto}` +
        `.weekday-group{display:flex;flex-wrap:wrap;gap:6px}` +
        `.weekday-shortcuts{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}` +
        `.weekday-shortcuts .btn{font-size:11px;padding:4px 8px}` +
        `.weekday-check{display:inline-flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--ci-border);border-radius:6px;cursor:pointer;font-size:12px;color:var(--ci-text);margin:0}` +
        `.weekday-check input{margin-right:4px}` +
        `.enabled-check{display:inline-flex;align-items:center;gap:6px;margin-top:14px;color:var(--ci-text);font-size:13px}` +
        // v1.19.0 — info-bubble + custom popover. Native `title` was
        // delayed on desktop and silent on touch; the popover here
        // shows immediately on :hover, on keyboard :focus, and on
        // click/tap (panel toggles .help-tip-open via _onClick).
        // The bubble itself is `position: relative` so the absolutely-
        // positioned popup anchors to it. z-index is high enough to
        // float above modals (.modal uses z-index:100).
        `.help-tip{position:relative;display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;background:var(--ci-hover);color:var(--ci-text-2);font-size:11px;margin-left:4px;cursor:help;vertical-align:middle;user-select:none;-webkit-user-select:none;border:none;font-family:inherit}` +
        `.help-tip:hover,.help-tip:focus,.help-tip.help-tip-open{background:var(--ci-accent);color:#fff;outline:none}` +
        `.help-tip-popup{position:absolute;left:50%;bottom:calc(100% + 8px);transform:translateX(-50%);background:var(--ci-card);color:var(--ci-text);border:1px solid var(--ci-border);border-radius:8px;padding:10px 12px;font-size:12px;line-height:1.45;font-weight:400;text-align:left;white-space:normal;width:max-content;max-width:280px;box-shadow:0 6px 20px rgba(0,0,0,0.35);opacity:0;pointer-events:none;transition:opacity 0.12s ease,transform 0.12s ease;z-index:200;cursor:default}` +
        // Small arrow pointing down to the bubble
        `.help-tip-popup::after{content:"";position:absolute;left:50%;top:100%;transform:translateX(-50%);border:6px solid transparent;border-top-color:var(--ci-border)}` +
        `.help-tip-popup::before{content:"";position:absolute;left:50%;top:100%;transform:translateX(-50%);border:5px solid transparent;border-top-color:var(--ci-card);margin-top:-1px;z-index:1}` +
        `.help-tip:hover .help-tip-popup,.help-tip:focus .help-tip-popup,.help-tip.help-tip-open .help-tip-popup{opacity:1;pointer-events:auto;transform:translateX(-50%) translateY(-2px)}` +
        // Weather banner
        `.weather-banner{background:var(--ci-card);border:1px solid var(--ci-border);border-radius:12px;padding:16px;margin-bottom:16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}` +
        `.weather-banner-empty{display:flex;align-items:flex-start;gap:12px;grid-template-columns:none}` +
        `.weather-banner-empty code{background:var(--ci-hover);padding:2px 6px;border-radius:4px;font-size:11px}` +
        `.weather-cell{display:flex;align-items:center;gap:10px}` +
        `.weather-cell-icon{font-size:22px;flex-shrink:0}` +
        `.weather-cell-body{min-width:0}` +
        `.weather-cell-label{font-size:11px;color:var(--ci-text-2);text-transform:uppercase;letter-spacing:0.05em}` +
        `.weather-cell-value{font-size:15px;font-weight:600;color:var(--ci-text);margin-top:2px}` +
        // Rain lockout banner
        `.rain-lockout-banner{background:#ffa726;color:#1c1c1c;padding:12px 16px;border-radius:12px;margin-bottom:16px;display:flex;align-items:center;gap:12px}` +
        // v1.17 — softer yellow than rain-lockout (which is a warning)
        // since missed runs are informational + actionable, not blocking.
        `.missed-runs-banner{background:rgba(255,167,38,0.18);color:var(--ci-text);border:1px solid rgba(255,167,38,0.4);padding:10px 14px;border-radius:12px;margin-bottom:16px;display:flex;align-items:center;gap:12px;cursor:pointer;transition:filter 0.12s}` +
        `.missed-runs-banner:hover{filter:brightness(1.05)}` +
        // Section title row + zone hide
        `.section-title-row{display:flex;align-items:center;justify-content:space-between;margin:16px 0 8px}` +
        `.section-title-row .section-title{margin:0}` +
        `.btn-link{background:none;border:none;color:var(--ci-accent);cursor:pointer;font-size:12px;text-decoration:underline;font-family:inherit;padding:0}` +
        `.btn-icon{background:transparent;border:none;cursor:pointer;font-size:14px;padding:4px 6px;border-radius:4px;opacity:0.5;transition:opacity 0.15s}` +
        `.btn-icon:hover{opacity:1;background:var(--ci-hover)}` +
        `.zone-tile.zone-hidden{opacity:0.55;border-style:dashed}` +
        `.zone-tile header{justify-content:space-between}` +
        `.zone-tile header h4{flex:1}` +
        `.section-hint{font-size:12px;color:var(--ci-text-2);margin:0 0 16px}` +
        // ── Zones tab (horizontal rows + 7-day strip) ─────────────
        `.zones-list{display:flex;flex-direction:column;gap:10px}` +
        `.zone-row{background:var(--ci-card);border:1px solid var(--ci-border);border-radius:12px;padding:14px 16px;display:grid;grid-template-columns:minmax(220px,1fr) minmax(280px,2fr) auto;gap:16px;align-items:center}` +
        `.zone-row-hidden{opacity:0.55;border-style:dashed}` +
        `.zone-row-main{display:flex;align-items:center;gap:10px;min-width:0}` +
        `.zone-row-text{min-width:0}` +
        `.zone-row-name{font-weight:600;font-size:15px;color:var(--ci-text)}` +
        `.zone-row-badge{display:inline-block;font-size:10px;background:#bdbdbd;color:#fff;padding:1px 6px;border-radius:4px;margin-left:6px;letter-spacing:0.5px}` +
        `.zone-row-meta{font-size:11px;color:var(--ci-text-2);margin-top:2px;word-break:break-all}` +
        `.zone-row-climate{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}` +
        `.zone-chip{display:inline-flex;align-items:center;gap:4px;font-size:12px;background:var(--ci-hover);border:1px solid var(--ci-border,rgba(0,0,0,0.08));border-radius:999px;padding:2px 8px;color:var(--ci-text);cursor:help}` +
        `.zone-chip-low{background:rgba(219,68,55,0.12);border-color:#db4437;color:#db4437;font-weight:600}` +
        // Auto-detected chips (v1.15) — sibling temp/humidity inferred
        // from a bound moisture sensor's entity_id; dashed border tells
        // users they didn't explicitly bind it.
        `.zone-chip-auto{border-style:dashed;opacity:0.9}` +
        `.zone-row-strip{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}` +
        `.zone-day{background:var(--ci-hover);border:1px solid var(--ci-border);border-radius:6px;padding:6px 4px;text-align:center;font-size:11px;cursor:default}` +
        `.zone-day-today{outline:2px solid var(--ci-accent);outline-offset:-2px}` +
        `.zone-day-on{background:rgba(3,169,244,0.08);border-color:var(--ci-accent)}` +
        `.zone-day-label{font-weight:600;color:var(--ci-text-2)}` +
        `.zone-day-date{font-size:13px;color:var(--ci-text);margin:1px 0}` +
        `.zone-day-dots{display:flex;justify-content:center;gap:2px;min-height:8px;align-items:center}` +
        `.zone-day-dot{width:5px;height:5px;border-radius:50%;background:var(--ci-accent)}` +
        `.zone-day-empty{color:var(--ci-text-2)}` +
        `.zone-day-more{font-size:9px;color:var(--ci-text-2);margin-left:2px}` +
        `.zone-row-actions{display:flex;gap:6px;align-items:center}` +
        `.zone-reorder-group{display:flex;flex-direction:column;gap:2px;margin-right:4px}` +
        `.zone-reorder{font-size:10px;line-height:1;padding:2px 6px;border:1px solid var(--ci-border);border-radius:4px;background:transparent;color:var(--ci-text-2);cursor:pointer}` +
        `.zone-reorder:hover:not([disabled]){background:var(--ci-hover);color:var(--ci-text)}` +
        `.zone-reorder[disabled]{opacity:0.3;cursor:not-allowed}` +
        // v1.35 — per-zone watering diagnosis (expands under the row; the
        // row is a grid, so span every column)
        `.zone-diag{grid-column:1 / -1;border-top:1px solid var(--ci-border);padding-top:10px;font-size:13px}` +
        `.zone-diag-head{font-weight:600;font-size:13px;margin-bottom:6px}` +
        `.zone-diag-ok{color:#2e7d32}` +
        `.zone-diag-warn{color:#b26a00}` +
        `.zone-diag-none{color:var(--ci-text-2)}` +
        `.zone-diag-block{margin:6px 0}` +
        `.zone-diag-sub{display:block;font-size:13px;color:var(--ci-text-2);text-transform:uppercase;letter-spacing:0.03em}` +
        `.zone-diag-block ul{margin:2px 0 0;padding-left:18px;font-size:13px}` +
        `.zone-diag-loading{display:block;font-size:13px;color:var(--ci-text-2)}` +
        `.zone-diag-foot{display:block;margin-top:6px;font-size:13px;color:var(--ci-text-2)}` +
        // ── Sensors tab ───────────────────────────────────────────
        `.sensor-zone-list{display:flex;flex-direction:column;gap:10px}` +
        `.sensor-zone-card{background:var(--ci-card);border:1px solid var(--ci-border);border-radius:12px;padding:14px 16px}` +
        `.sensor-zone-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}` +
        `.sensor-zone-head h4{margin:0;font-size:15px;font-weight:600}` +
        `.sensor-zone-eid{font-size:11px;color:var(--ci-text-2);font-family:var(--ha-font-family-code,monospace);margin-top:2px}` +
        `.sensor-zone-right{display:flex;align-items:center;gap:10px}` +
        `.sensor-live{font-size:18px;font-weight:600;color:var(--ci-accent)}` +
        `.sensor-low{color:#db4437 !important}` +
        `.sensor-reading{font-variant-numeric:tabular-nums;font-weight:500}` +
        `.sensor-reading-row .sensor-label{flex:1}` +
        `.sensor-link{color:inherit;text-decoration:none;border-bottom:1px dotted var(--ci-border)}` +
        `.sensor-link:hover{color:var(--ci-accent);border-bottom-color:var(--ci-accent)}` +
        `.sensor-reading-row{justify-content:space-between}` +
        `.sensor-combined-row{margin-top:4px;padding-top:6px;border-top:1px dashed var(--ci-border,rgba(0,0,0,0.12))}` +
        `.sensor-reading-combined{color:var(--ci-accent);font-weight:700}` +
        `.sensor-unavailable{color:var(--ci-text-2);font-style:italic}` +
        `.sensor-empty{font-size:12px;color:var(--ci-text-2);padding:8px 0}` +
        `.sensor-bound{display:flex;flex-direction:column;gap:4px}` +
        `.sensor-bound-row{display:flex;gap:10px;font-size:12px;color:var(--ci-text)}` +
        `.sensor-label{min-width:80px;color:var(--ci-text-2);font-weight:500}` +
        `.sensor-bound code{font-size:11px;background:var(--ci-hover);padding:1px 4px;border-radius:3px}` +
        `.sensor-pick-list{max-height:280px;overflow-y:auto;border:1px solid var(--ci-border);border-radius:6px;padding:6px;margin-bottom:6px}` +
        // v1.19.0 — search input above each sensor checklist. Live filters
        // rows by entity name + entity_id as the user types. Sits flush
        // with the list (shared border-radius look via stacking).
        `.sensor-picker{margin-bottom:10px}` +
        `.sensor-pick-search{width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid var(--ci-border);border-radius:6px;background:var(--ci-input-bg);color:inherit;font-family:inherit;font-size:13px;margin-bottom:4px}` +
        `.sensor-pick-search:focus{outline:none;border-color:var(--ci-accent);box-shadow:0 0 0 2px rgba(127,205,240,0.18)}` +
        `.sensor-pick-no-match{padding:14px;text-align:center;color:var(--ci-text-2);font-size:12px}` +
        `.sensor-pick{display:flex;align-items:flex-start;gap:8px;padding:6px;border-radius:4px;cursor:pointer;font-size:13px}` +
        // v1.19.0 — the sensor search filter sets row.hidden=true to
        // filter rows, but `.sensor-pick{display:flex}` (an author rule)
        // overrode the UA `[hidden]{display:none}` rule, so hidden rows
        // stayed visible and the filter appeared dead. This higher-
        // specificity rule (class + attribute = 0,2,0 beats 0,1,0)
        // restores the hide. Same guard for the no-match placeholder.
        `.sensor-pick[hidden]{display:none}` +
        `.sensor-pick:hover{background:var(--ci-hover)}` +
        // v1.19 — moisture rows are <div>s holding the main label plus
        // an optional "in avg" mini-toggle (analysis opt-out). The main
        // label grows; the toggle hugs the right edge.
        `.sensor-pick-main{display:flex;align-items:flex-start;gap:8px;flex:1;min-width:0;cursor:pointer}` +
        `.sensor-pick-use{display:flex;align-items:center;gap:4px;font-size:11px;color:var(--ci-text-2);white-space:nowrap;cursor:pointer;flex-shrink:0;padding-left:8px}` +
        `.sensor-pick-use:hover{color:var(--ci-text)}` +
        `.sensor-pick code{font-size:10px;color:var(--ci-text-2)}` +
        `.rain-pick-list{max-height:280px;overflow-y:auto;border:1px solid var(--ci-border,rgba(0,0,0,0.12));border-radius:6px;padding:6px;margin-bottom:8px}` +
        `.rain-pick{display:flex;align-items:flex-start;gap:8px;padding:8px;border-radius:4px;cursor:pointer;font-size:13px}` +
        `.rain-pick:hover{background:var(--ci-hover)}` +
        `.rain-pick code{font-size:10px;color:var(--ci-text-2)}` +
        `.rain-live{font-size:11px;color:var(--ci-accent);font-weight:600;margin-left:6px}` +
        `.rain-primary-tag{display:inline-block;font-size:9px;background:var(--ci-accent);color:#fff;padding:1px 5px;border-radius:3px;margin-left:4px;vertical-align:middle;font-weight:700;letter-spacing:0.4px}` +
        // Settings cards
        `.settings-card{background:var(--ci-card);border:1px solid var(--ci-border);border-radius:12px;padding:16px 20px;margin-bottom:14px;max-width:640px}` +
        `.settings-card h3{margin-top:0}` +
        `.settings-table{width:100%;font-size:13px;border-collapse:collapse}` +
        `.settings-table td{padding:6px 0;color:var(--ci-text)}` +
        `.settings-table td:first-child{color:var(--ci-text-2);width:160px}` +
        `.copy-row{display:flex;align-items:center;gap:8px;margin-top:6px}` +
        `.copy-row code{flex:1;font-size:11px;background:var(--ci-hover,#f0f0f0);padding:6px 8px;border-radius:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}` +
        `.row-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}` +
        `.row-3 > *{min-width:0}` +
        // ── Weather tab ───────────────────────────────────────────
        `.weather-form{background:var(--ci-card);border:1px solid var(--ci-border);border-radius:12px;padding:16px 20px;max-width:640px}` +
        `.weather-form .section-title{margin-top:12px}` +
        `.weather-form label{display:block;font-size:12px;color:var(--ci-text-2);margin:10px 0 4px}` +
        `.weather-form input,.weather-form select{width:100%;min-width:0;padding:8px 10px;border:1px solid var(--ci-border);border-radius:6px;font-size:14px;background:var(--ci-input-bg);color:inherit;font-family:inherit;box-sizing:border-box}` +
        `.forecast{margin-bottom:16px}` +
        `.forecast-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}` +
        `.forecast-cell{background:var(--ci-card);border:1px solid var(--ci-border);border-radius:10px;padding:12px;text-align:center}` +
        `.forecast-date{font-size:12px;color:var(--ci-text-2)}` +
        `.forecast-icon{font-size:28px;margin:4px 0}` +
        `.forecast-label{font-size:13px;font-weight:500}` +
        `.forecast-temp{font-size:12px;color:var(--ci-text-2);margin-top:2px}` +
        // Mobile
        `@media (max-width:700px){` +
        `.sidebar:not(.collapsed){position:fixed;z-index:10;height:100%}` +
        `.sidebar.collapsed{width:56px}` +
        `.root{grid-template-columns:56px 1fr}` +
        `main{padding:12px}` +
        `.page-header{flex-wrap:wrap}` +
        `.schedule-row{flex-direction:column;align-items:stretch}` +
        // Zones: stack name/strip/actions vertically; strip stays scrollable
        `.zone-row{grid-template-columns:1fr;gap:10px}` +
        `.zone-row-strip{overflow-x:auto;grid-template-columns:repeat(7,minmax(40px,1fr))}` +
        `.zone-row-actions{flex-wrap:wrap;justify-content:flex-end}` +
        // Modal: nearly full-width on phones, taller scroll window
        // (95vh - 24px) to maximize content visibility on narrow screens.
        // Sticky footer's negative margin re-tuned to match the 16px
        // mobile padding.
        // v1.19.0 — mobile modal: same scroll/sticky pattern as
        // desktop, retuned for 16px padding. padding-bottom:0 so the
        // sticky .modal-actions edge-to-edge override sits flush.
        `.modal{min-width:0;width:calc(100vw - 24px);max-width:calc(100vw - 24px);max-height:calc(100vh - 24px);padding:16px 16px 0}` +
        `.modal-actions{bottom:0;margin:18px -16px 0;padding:12px 16px}` +
        `.modal-wide{min-width:0}` +
        // Two-column rows become single-column
        `.row-2,.row-3{grid-template-columns:1fr}` +
        // Day calendar: stack the 2-day columns vertically on phones
        `.day-cal-cols{grid-template-columns:1fr}` +
        // Weather banner: keep cells flowing
        `.weather-banner{grid-template-columns:repeat(auto-fit,minmax(120px,1fr));padding:12px}` +
        `.banner-gear{top:6px;right:6px}` +
        // Sensor card head wraps so the Edit button doesn't crowd long names
        `.sensor-zone-head{flex-wrap:wrap}` +
        // Settings cards trim padding
        `.settings-card{padding:14px 16px}` +
        `}`
      );
    }
  }

  // Minimal CSS.escape polyfill for older HA frontends. Only escapes the
  // characters that can appear in HA entity_ids (the `.` is the main one).
  function cssEscape(s) {
    if (typeof window !== "undefined" && typeof window.CSS?.escape === "function") {
      return window.CSS.escape(s);
    }
    return String(s).replace(/([^\w-])/g, "\\$1");
  }

  function _formatRemaining(ms) {
    const total = Math.max(0, Math.round(ms / 1000));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  // Escape for HTML body context (<div>...</div>) AND double-quoted
  // attribute contexts (<a href="..."). Does NOT escape `'` because
  // every attribute in this file uses double quotes — if you ever
  // switch any attribute to single quotes, you MUST update this helper.
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;"); // also escape single-quote so a single-quoted attr sink is safe too
  }
  // escapeAttr is the same function as escapeHtml — the alias just
  // makes interpolation sites self-documenting (developer can see at
  // a glance whether content is going into body or attribute). DO NOT
  // remove without a sweep to retag the call sites.
  const escapeAttr = escapeHtml;

  customElements.define(ELEMENT_NAME, CompleteIrrigationPanel);
  console.info(`[complete-irrigation] panel registered, version ${PANEL_VERSION}`);
})();
