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
  const ELEMENT_NAME = "complete-irrigation-panel";
  const DEFAULT_MANUAL_MINUTES = 10;
  const MAX_MANUAL_MINUTES = 60;
  const MAX_SCHEDULE_MINUTES = 480; // 8 hours

  if (customElements.get(ELEMENT_NAME)) return;

  const SECTIONS = [
    { id: "today", label: "Today", icon: "📅" },
    { id: "schedules", label: "Schedules", icon: "⏰" },
    { id: "zones", label: "Zones", icon: "🌱" },
    { id: "history", label: "History", icon: "📜" },
    { id: "sensors", label: "Sensors", icon: "📊" },
    { id: "weather", label: "Weather", icon: "🌧️" },
    { id: "notifications", label: "Notifications", icon: "🔔" },
    { id: "settings", label: "Settings", icon: "⚙️" },
  ];

  const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

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
      // Optional daily-window cap for interval_hours mode (v1.14.1).
      // Empty string = no cap (legacy continuous-across-days behavior).
      interval_end_time: "",
      // Active period (v1.12). Empty strings mean "no bound".
      start_date: "",
      end_date: "",
      repeat_annually: false,
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
      // The total run length for each active run, in minutes. Lets the
      // tile show "4:52 left of 10 min" instead of just "4:52 left".
      this._localRunDurations = {};
      this._countdownTimer = null;

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
      for (const z of zones) {
        const s = this._hass.states[z];
        parts.push(s ? `${z}=${s.state}` : `${z}=_`);
      }
      // Also include sun + a few common Tempest sensors
      for (const eid of ["sun.sun"]) {
        const s = this._hass.states[eid];
        if (s) parts.push(`${eid}=${s.state}`);
      }
      // Weather-relevant sensors (auto-detected) — include them so the
      // banner refreshes when the data changes, but not for every other
      // sensor in HA.
      for (const eid of Object.keys(this._hass.states)) {
        if (/^sensor\.(tempest|weatherflow)_/.test(eid)) {
          parts.push(`${eid}=${this._hass.states[eid].state}`);
        }
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
      this._scheduleRender();
    }

    disconnectedCallback() {
      this.shadowRoot.removeEventListener("click", this._onClick);
      this.shadowRoot.removeEventListener("submit", this._onSubmit);
      this.shadowRoot.removeEventListener("change", this._onChange);
      this.shadowRoot.removeEventListener("input", this._onInput);
    }

    _onClick(e) {
      const path = e.composedPath ? e.composedPath() : [];
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
        if (action === "delete-schedule")
          return this._deleteSchedule(node.dataset.scheduleId);
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
        if (action === "clear-interval-end-time") {
          this._scheduleEditor.interval_end_time = "";
          return this._renderNow();
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
          return;
        }
        if (
          t.name === "combine_mode" ||
          t.name === "category" ||
          t.name === "min_pct" ||
          t.name === "target_pct" ||
          t.name === "max_pct"
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
      if (!t || !t.name) return;
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
        this._safeRender();
      });
    }

    _renderNow() {
      // Synchronous render for user actions (navigation, modal toggles)
      // so the UI feels instantaneous instead of waiting on the next frame.
      this._renderScheduled = false;
      this._safeRender();
    }

    _safeRender() {
      try {
        this._render();
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
      if (sectionId === "weather") {
        const w = this._findWeatherEntity();
        if (w && !this._forecastCache[w.entity_id]) {
          this._fetchForecast(w.entity_id);
        }
      }
      this._renderNow();
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
      const name = this._haTheme;
      if (!name || !this._haThemes || !this._haThemes[name]) return "";
      const theme = this._haThemes[name];
      // HA theme variables are stored with hyphenated keys like
      // "primary-color" → CSS variable --primary-color. Apply each on
      // :host. Our --ci-* vars resolve via these, so the panel auto-themes.
      const lines = [];
      for (const [k, v] of Object.entries(theme)) {
        if (typeof v !== "string") continue;
        lines.push(`--${k}: ${v};`);
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
        if (typeof theme[haKey] === "string") {
          for (const ciKey of ciKeys) {
            lines.push(`${ciKey}: ${theme[haKey]};`);
          }
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
      // Merge legacy single target into the multi-list for display.
      const targetsList = Array.isArray(n.notify_targets)
        ? n.notify_targets
        : n.notify_target
        ? [n.notify_target]
        : [];
      const targetsText = targetsList.join("\n");
      const qStart = n.quiet_hours_start || "22:00";
      const qEnd = n.quiet_hours_end || "07:00";
      const enabled = n.enabled !== false; // default true
      const lowMoistureAlerts = n.low_moisture_alerts !== false; // default true
      const tip = (text) =>
        `<span class="help-tip" title="${escapeAttr(text)}" aria-label="${escapeAttr(text)}">ⓘ</span>`;

      return (
        `<header class="page-header"><h2>Notifications</h2>` +
        `<span class="version-pill">v1.14.1</span></header>` +
        `<form class="weather-form" data-form="notifications">` +
        `<label class="enabled-check"><input type="checkbox" name="enabled"${
          enabled ? " checked" : ""
        } /> Notifications enabled ${tip("Master switch. Turn off to silence all push notifications without losing your config.")}</label>` +
        `<label>Notify targets ${tip("One HA notify service per line (e.g. notify.mobile_app_pete_iphone). Add as many as you want — every notification fans out to all of them.")}</label>` +
        `<textarea name="notify_targets" rows="3" placeholder="notify.mobile_app_pete_iphone\nnotify.mobile_app_pat_iphone">${escapeHtml(targetsText)}</textarea>` +
        `<p class="section-hint" style="margin:6px 0 12px">Find your phone's notify service at <code>Developer Tools → Services</code> and search for <code>notify.mobile_app</code>.</p>` +
        `<h3 class="section-title">Quiet hours</h3>` +
        `<p class="section-hint">Non-urgent notifications received in this window are bundled into a single morning summary.</p>` +
        `<div class="row-2">` +
        `<div><label>Start ${tip("24h, e.g. 22:00")}</label><input name="quiet_hours_start" type="time" value="${escapeAttr(qStart)}" /></div>` +
        `<div><label>End ${tip("24h, e.g. 07:00 — morning summary fires at this time")}</label><input name="quiet_hours_end" type="time" value="${escapeAttr(qEnd)}" /></div>` +
        `</div>` +
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
      // Parse the textarea: split on newlines + commas, trim, drop empties.
      const raw = data.get("notify_targets") || "";
      const targets = String(raw)
        .split(/[\n,]+/)
        .map((s) => s.trim())
        .filter(Boolean);
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
      // low_moisture_alerts is a panel-side preference (stored in the
      // notifications blob so it survives restarts).
      const lowToggle = form.querySelector('input[name="low_moisture_alerts"]');
      try {
        await this._hass.callService(
          "complete_irrigation",
          "set_notification_config",
          payload
        );
        if (lowToggle) {
          await this._hass.callService(
            "complete_irrigation",
            "set_notification_config",
            { low_moisture_alerts: lowToggle.checked }
          );
        }
        await this._fetchConfig();
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
      const tip = (text) =>
        `<span class="help-tip" title="${escapeAttr(text)}" aria-label="${escapeAttr(text)}">ⓘ</span>`;
      const icalUrl = "/api/complete_irrigation/calendar.ics";
      const repoUrl = "https://github.com/HL-Apprentice/ha-complete-irrigation";

      const policy = c.conflict_policy || "defer_new";
      const zoneBuffer = c.zone_buffer_seconds != null ? c.zone_buffer_seconds : 30;
      const snoozedUntil = c.weekly_reminder_snoozed_until || "";
      const policyOpt = (val, label) =>
        `<option value="${val}"${policy === val ? " selected" : ""}>${label}</option>`;

      return (
        `<header class="page-header"><h2>Settings</h2>` +
        `<span class="version-pill">v1.14.1</span></header>` +
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
        `<tr><td>Version</td><td><strong>v1.14.1</strong></td></tr>` +
        `<tr><td>Repository</td><td><a href="${repoUrl}" target="_blank">${escapeHtml(repoUrl)}</a></td></tr>` +
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
        `<span class="version-pill">v1.14.1</span></div></header>` +
        this._renderRainLockoutBanner() +
        this._renderWeatherBanner() +
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

      let statusClass, statusLabel;
      if (!zone.available) {
        statusClass = "unavailable";
        statusLabel = "Unavailable";
      } else if (zone.on) {
        statusClass = "running";
        statusLabel = isCountingDown
          ? `Running — ${cdSpan} left${totalLabel}`
          : "Running";
      } else {
        statusClass = "idle";
        statusLabel = "Idle";
      }

      const action = zone.on
        ? `<button class="btn btn-stop" data-action="stop" data-entity-id="${escapeAttr(
            zone.entityId
          )}">⏹ Stop${isCountingDown ? " (" + cdSpan + ")" : ""}</button>`
        : `<button class="btn btn-run" data-action="run-now" data-entity-id="${escapeAttr(
            zone.entityId
          )}" data-zone-name="${escapeAttr(zone.name)}"${
            zone.available ? "" : " disabled"
          }>▶ Run Now</button>`;

      // PRD #4 — hide/show is managed in the Zones tab only.
      return (
        `<article class="zone-tile${isHidden ? " zone-hidden" : ""}">` +
        `<header>` +
        `<span class="status-dot ${statusClass}"></span>` +
        `<h4>${escapeHtml(zone.name)}</h4>` +
        `</header>` +
        `<div class="status-text">${statusLabel}</div>` +
        `<div class="zone-actions">${action}</div>` +
        `</article>`
      );
    }

    // ── Zones tab ──────────────────────────────────────────────────
    _renderZones() {
      const zones = this._zones();
      if (zones.length === 0) {
        return (
          `<header class="page-header"><h2>Zones</h2>` +
          `<span class="version-pill">v1.14.1</span></header>` +
          `<div class="empty"><p>No zones configured. Add them via Settings → Devices &amp; Services.</p></div>`
        );
      }
      const rows = zones
        .map((z, i) => this._renderZoneRow(z, i, zones.length))
        .join("");
      return (
        `<header class="page-header"><h2>Zones</h2>` +
        `<span class="version-pill">v1.14.1</span></header>` +
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
        hideBtn +
        `</div>` +
        `</article>`
      );
    }

    _renderZoneClimateChips(zoneCfg) {
      const chips = [];
      const moistures = this._readPercentSensors(zoneCfg.moisture_entities || []);
      if (moistures.length > 0) {
        const combined = this._combineReadings(moistures, zoneCfg.combine_mode);
        const minPct = zoneCfg.min_pct;
        const low = minPct != null && combined !== null && combined < minPct;
        chips.push(
          `<span class="zone-chip${low ? " zone-chip-low" : ""}" title="${escapeAttr(
            moistures.map((m) => `${m.friendly}: ${m.value.toFixed(1)}%`).join("\n")
          )}">💧 ${combined.toFixed(0)}%${moistures.length > 1 ? ` (${moistures.length})` : ""}</span>`
        );
      }
      // Temp — always averaged, unit pulled from the first sensor
      const tempEids = zoneCfg.temperature_entities || [];
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
          chips.push(
            `<span class="zone-chip" title="${escapeAttr(
              readings.map((r) => `${r.friendly}: ${r.value.toFixed(1)}${unit}`).join("\n")
            )}">🌡️ ${avg.toFixed(0)}${unit}${readings.length > 1 ? ` (${readings.length})` : ""}</span>`
          );
        }
      }
      // Humidity
      const humEids = zoneCfg.humidity_entities || [];
      if (humEids.length > 0) {
        const readings = humEids
          .map((eid) => this._readSensor(eid))
          .filter(Boolean)
          .map((s) => parseFloat(s.state))
          .filter((v) => !Number.isNaN(v));
        if (readings.length > 0) {
          const avg = readings.reduce((a, b) => a + b, 0) / readings.length;
          chips.push(
            `<span class="zone-chip">💨 ${avg.toFixed(0)}%${readings.length > 1 ? ` (${readings.length})` : ""}</span>`
          );
        }
      }
      return chips.join("");
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
      // Returns [{start_time, name}] for schedules that fire on `dayDate`
      // for this zone. Mirrors the server-side run_planner logic for
      // weekday + interval modes (just enough to show on the strip).
      if (!this._schedules) return [];
      const result = [];
      for (const s of this._schedules) {
        if (!s.enabled) continue;
        // Match the zone as either the primary OR any zone_steps entry —
        // multi-zone schedules fire ALL their bound zones, so each one
        // should see this schedule on its 7-day strip.
        const stepIds = Array.isArray(s.zone_steps)
          ? s.zone_steps.map((st) => st.zone_entity_id)
          : [];
        if (s.zone_entity_id !== zoneEntityId && !stepIds.includes(zoneEntityId))
          continue;
        // Common end_date filter
        if (s.end_date) {
          const end = new Date(s.end_date + "T00:00:00");
          if (dayDate > end) continue;
        }
        if (s.mode === "interval") {
          if (!s.interval_anchor || !s.interval_days) continue;
          const anchor = new Date(s.interval_anchor + "T00:00:00");
          if (Number.isNaN(anchor.getTime())) continue;
          const diffDays = Math.floor((dayDate - anchor) / 86400000);
          if (diffDays < 0) continue;
          if (diffDays % s.interval_days !== 0) continue;
          result.push({ start_time: s.start_time, name: s.name });
        } else if (s.mode === "interval_hours") {
          if (!s.interval_anchor || !s.interval_hours) continue;
          // Skip if dayDate is before the anchor's calendar date.
          const anchor = new Date(s.interval_anchor + "T00:00:00");
          if (Number.isNaN(anchor.getTime())) continue;
          if (dayDate.getTime() + 86400000 <= anchor.getTime()) continue;
          // Render one entry showing the first firing of the day's cycle.
          // The Today timeline shows every individual hourly cycle; the
          // 7-day strip just needs one marker to indicate "yes, fires
          // today" — we use the start_time for ordering.
          const windowSuffix = s.interval_end_time
            ? ` ${s.start_time}–${s.interval_end_time}`
            : "";
          result.push({
            start_time: s.start_time,
            name: `${s.name} (every ${s.interval_hours}h${windowSuffix})`,
          });
        } else {
          // weekdays mode — convert JS Sun=0 to ISO Mon=0
          const isoDow = (dayDate.getDay() + 6) % 7;
          const weekdays = s.weekdays || [];
          if (weekdays.includes(isoDow)) {
            result.push({ start_time: s.start_time, name: s.name });
          }
        }
      }
      return result.sort((a, b) => a.start_time.localeCompare(b.start_time));
    }

    _todaysRuns() {
      // Backward-compat shim — today's runs only.
      const d = new Date();
      d.setHours(0, 0, 0, 0);
      return this._runsForDay(d);
    }

    _runsForDay(day) {
      // All scheduled runs firing on `day` (a Date at local midnight),
      // expanded into one entry per zone-step. Each entry is
      // {start_minutes, zone_entity_id, zone_name, duration_minutes,
      // schedule_name, schedule_id}. Used by Today timeline + Tomorrow list.
      if (!this._schedules) return [];
      const today = day;
      const isoDow = (today.getDay() + 6) % 7; // JS Sun=0 → ISO Mon=0
      const out = [];
      // Inter-zone valve buffer, matches the server-side run_planner.
      const ZONE_BUFFER_SECONDS = 30;

      // Pre-compute today bounds + helpers.
      const todayEnd = new Date(today.getTime() + 86400000);
      // Parse "HH:MM" → [hours, minutes] tuple.
      const parseHHMM = (str) => {
        const parts = (str || "00:00").split(":").map((n) => parseInt(n, 10));
        return [parts[0] || 0, parts[1] || 0];
      };

      // For each firing, expand the schedule's zone_steps starting at
      // the given minute offset from midnight.
      const pushFiring = (s, startMinutes) => {
        const steps =
          Array.isArray(s.zone_steps) && s.zone_steps.length > 0
            ? s.zone_steps
            : [{ zone_entity_id: s.zone_entity_id, duration_minutes: s.duration_minutes }];
        let cursorSec = startMinutes * 60;
        for (const step of steps) {
          out.push({
            start_minutes: Math.floor(cursorSec / 60),
            zone_entity_id: step.zone_entity_id,
            zone_name: this._zoneName(step.zone_entity_id),
            duration_minutes: step.duration_minutes,
            schedule_name: s.name,
            schedule_id: s.id,  // enables click-to-edit
          });
          cursorSec += step.duration_minutes * 60 + ZONE_BUFFER_SECONDS;
        }
      };

      for (const s of this._schedules) {
        if (!s.enabled) continue;
        // Skip schedules past their end date
        if (s.end_date) {
          const end = new Date(s.end_date + "T00:00:00");
          if (today > end) continue;
        }
        const [hh, mm] = parseHHMM(s.start_time);

        if (s.mode === "interval") {
          if (!s.interval_anchor || !s.interval_days) continue;
          const anchor = new Date(s.interval_anchor + "T00:00:00");
          if (Number.isNaN(anchor.getTime())) continue;
          const diffDays = Math.floor((today - anchor) / 86400000);
          if (diffDays < 0 || diffDays % s.interval_days !== 0) continue;
          pushFiring(s, hh * 60 + mm);
        } else if (s.mode === "interval_hours") {
          // every N hours from anchor + start_time.
          if (!s.interval_anchor || !s.interval_hours) continue;
          const anchorDt = new Date(s.interval_anchor + "T00:00:00");
          anchorDt.setHours(hh, mm, 0, 0);
          if (Number.isNaN(anchorDt.getTime())) continue;
          // v1.14.1: with interval_end_time set, the schedule fires every
          // N hours from start_time EACH DAY, capped at end_time. Without
          // it, the legacy continuous-across-days behavior applies.
          if (s.interval_end_time) {
            const anchorOnly = new Date(s.interval_anchor + "T00:00:00");
            if (today < anchorOnly) continue;
            const [endH, endM] = parseHHMM(s.interval_end_time);
            const startMin = hh * 60 + mm;
            const endMin = endH * 60 + endM;
            const stepMin = s.interval_hours * 60;
            for (let m = startMin; m <= endMin; m += stepMin) {
              pushFiring(s, m);
            }
          } else {
            const stepMs = s.interval_hours * 3600000;
            // Walk forward from the anchor until we land in [today, todayEnd).
            let cursor = anchorDt.getTime();
            if (cursor < today.getTime()) {
              const skip = Math.ceil((today.getTime() - cursor) / stepMs);
              cursor += skip * stepMs;
            }
            while (cursor < todayEnd.getTime()) {
              const dt = new Date(cursor);
              pushFiring(s, dt.getHours() * 60 + dt.getMinutes());
              cursor += stepMs;
            }
          }
        } else {
          // weekdays
          if (!(s.weekdays || []).includes(isoDow)) continue;
          pushFiring(s, hh * 60 + mm);
        }
      }
      return out.sort((a, b) => a.start_minutes - b.start_minutes);
    }

    _renderTodaysTimeline() {
      const runs = this._todaysRuns();
      const now = new Date();
      const nowMin = now.getHours() * 60 + now.getMinutes();
      const pct = (m) => (m / 1440) * 100;
      const fmtTime = (m) => {
        const h = Math.floor(m / 60);
        const mm = String(m % 60).padStart(2, "0");
        const ampm = h >= 12 ? "PM" : "AM";
        const h12 = h % 12 || 12;
        return `${h12}:${mm} ${ampm}`;
      };

      if (runs.length === 0) {
        return (
          `<section class="today-timeline">` +
          `<h3 class="section-title">Today's runs</h3>` +
          `<div class="empty"><p>No runs scheduled for today.</p></div>` +
          `</section>`
        );
      }

      // Hour ticks every 6h — keeps the axis readable
      const ticks = [0, 360, 720, 1080, 1440]
        .map((m) => {
          const label = m === 1440 ? "12 AM" : fmtTime(m);
          return `<span class="timeline-tick" style="left:${pct(m)}%">${label}</span>`;
        })
        .join("");

      // One pill per run, anchored at the start time
      const pills = runs
        .map((r) => {
          const widthPct = Math.max(2, pct(r.duration_minutes));
          const past = r.start_minutes + r.duration_minutes < nowMin;
          const live =
            r.start_minutes <= nowMin && nowMin < r.start_minutes + r.duration_minutes;
          const cls =
            "timeline-pill" +
            (past ? " timeline-pill-past" : "") +
            (live ? " timeline-pill-live" : "");
          const title = `${r.schedule_name} → ${r.zone_name}\n${fmtTime(r.start_minutes)} for ${r.duration_minutes} min\nClick to edit`;
          return (
            `<div class="${cls}" style="left:${pct(r.start_minutes)}%;width:${widthPct}%" title="${escapeAttr(title)}" data-action="open-schedule-edit" data-schedule-id="${escapeAttr(r.schedule_id)}">` +
            `<span class="timeline-pill-time">${fmtTime(r.start_minutes)}</span>` +
            `<span class="timeline-pill-zone">${escapeHtml(r.zone_name)} · ${r.duration_minutes}m</span>` +
            `</div>`
          );
        })
        .join("");

      const nowMarker = `<div class="timeline-now" style="left:${pct(nowMin)}%" title="Now: ${fmtTime(nowMin)}"></div>`;

      return (
        `<section class="today-timeline">` +
        `<h3 class="section-title">Today's runs (${runs.length})</h3>` +
        `<div class="timeline-track">` +
        `<div class="timeline-axis">${ticks}</div>` +
        `<div class="timeline-bar">${nowMarker}${pills}</div>` +
        `</div>` +
        `</section>` +
        this._renderTomorrowList()
      );
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

      const fmtTime = (m) => {
        const h = Math.floor(m / 60);
        const mm = String(m % 60).padStart(2, "0");
        const ampm = h >= 12 ? "PM" : "AM";
        const h12 = h % 12 || 12;
        return `${h12}:${mm} ${ampm}`;
      };

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
            if (r.start_minutes + r.duration_minutes < nowMin) {
              cls += " past";
              status = " · Past";
            } else if (
              r.start_minutes <= nowMin &&
              nowMin < r.start_minutes + r.duration_minutes
            ) {
              cls += " live";
              status = " · Running now";
            }
          }
          const endMin = r.start_minutes + r.duration_minutes;
          // Hover-card payload: split into structured data-attrs so the
          // tooltip element can render rich, multi-line info instantly
          // (native title="..." takes ~1s and can't be styled).
          const hoverTitle = `${r.schedule_name} → ${r.zone_name}`;
          const hoverWhen = `${fmtTime(r.start_minutes)} – ${fmtTime(endMin)} (${r.duration_minutes} min)`;
          const hoverHint = "Click to edit schedule";
          return (
            `<div class="${cls}" style="top:${top}px;height:${height}px" ` +
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

      const nowMarker = isToday
        ? `<div class="day-cal-now" style="top:${nowMin}px" title="Now: ${fmtTime(nowMin)}"></div>`
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
        const hasTriggers = r.triggers && Object.keys(r.triggers).length > 0;
        const expanded = this._historyExpanded.has(r.id);
        const triggerCell = hasTriggers
          ? `<button class="btn btn-small history-trigger-toggle" data-action="history-toggle-triggers" data-record-id="${escapeAttr(r.id)}">${expanded ? "▾" : "▸"} ${Object.keys(r.triggers).join(", ")}</button>`
          : `<span class="history-dim">—</span>`;
        const expandedBlock =
          expanded && hasTriggers
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

      const rowsHtml = filtered.length
        ? filtered.map(fmtRow).join("")
        : `<tr><td colspan="6" class="history-empty">No runs match these filters.</td></tr>`;

      const loadingNote = !this._runHistoryLoaded
        ? `<p class="history-loading">Loading…</p>`
        : "";

      return (
        `<header class="page-header"><h2>Run history</h2>` +
        `<span class="version-pill">v1.14.1</span></header>` +
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
          `<span class="version-pill">v1.14.1</span></header>` +
          `<div class="empty"><p>No zones configured.</p></div>`
        );
      }
      const cards = zones
        .map((z) => this._renderSensorZoneCard(z))
        .join("");
      return (
        `<header class="page-header"><h2>Sensors</h2>` +
        `<span class="version-pill">v1.14.1</span></header>` +
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

      const tip = (text) =>
        `<span class="help-tip" title="${escapeAttr(text)}" aria-label="${escapeAttr(text)}">ⓘ</span>`;

      const sensorChecks = (moistureCandidates.length > 0
        ? moistureCandidates
        : allSensors.map((s) => s.entity_id).filter((id) => id.startsWith("sensor.")).sort()
      )
        .map((eid) => {
          const checked = e.moisture_entities.includes(eid);
          const friendly = this._hass.states[eid]?.attributes?.friendly_name || eid;
          return (
            `<label class="sensor-pick"><input type="checkbox" name="moisture_entity" value="${escapeAttr(eid)}"${
              checked ? " checked" : ""
            } /><span><strong>${escapeHtml(friendly)}</strong><br /><code>${escapeHtml(eid)}</code></span></label>`
          );
        })
        .join("");

      return (
        `<div class="modal-backdrop"></div>` +
        `<div class="modal modal-wide" role="dialog" aria-modal="true">` +
        `<form class="modal-form sensor-form">` +
        `<h3>Moisture sensors for ${escapeHtml(zoneName)}</h3>` +
        `<label>Sensors ${tip("Pick one or more soil-moisture sensors. If you pick multiple, choose how to combine their readings below.")}</label>` +
        `<div class="sensor-pick-list">${sensorChecks || '<div class="empty">No sensors found in HA. Add a moisture sensor first.</div>'}</div>` +
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
      if (matches.length === 0) {
        return `<div class="empty" style="margin-bottom:8px">No matching sensors found in HA.</div>`;
      }
      const rows = matches
        .map((eid) => {
          const friendly = this._hass.states[eid]?.attributes?.friendly_name || eid;
          const checked = selected.includes(eid);
          return (
            `<label class="sensor-pick"><input type="checkbox" name="${inputName}" value="${escapeAttr(eid)}"${
              checked ? " checked" : ""
            } /><span><strong>${escapeHtml(friendly)}</strong><br /><code>${escapeHtml(eid)}</code></span></label>`
          );
        })
        .join("");
      return `<div class="sensor-pick-list">${rows}</div>`;
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
      const tip = (text) =>
        `<span class="help-tip" title="${escapeAttr(text)}" aria-label="${escapeAttr(text)}">ⓘ</span>`;
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

      const tip = (text) =>
        `<span class="help-tip" title="${escapeAttr(text)}" aria-label="${escapeAttr(text)}">ⓘ</span>`;

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
        `<span class="version-pill">v1.14.1</span></header>` +
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
      return (
        `<article class="schedule-row ${enabledClass}">` +
        `<div class="schedule-row-main">` +
        `<div class="schedule-name">${escapeHtml(s.name)}${
          s.enabled ? "" : " (disabled)"
        }</div>` +
        `<div class="schedule-meta">` +
        `${escapeHtml(zoneName)} · ${s.start_time} · ${durLabel} · ${escapeHtml(recurrence)}${escapeHtml(periodLabel)}` +
        `</div>` +
        `</div>` +
        `<div class="schedule-row-actions">` +
        `<button class="btn btn-small" data-action="toggle-schedule" data-schedule-id="${escapeAttr(
          s.id
        )}" data-enabled="${s.enabled}">${s.enabled ? "Disable" : "Enable"}</button>` +
        `<button class="btn btn-small" data-action="edit-schedule" data-schedule-id="${escapeAttr(
          s.id
        )}">Edit</button>` +
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

      const tip = (text) =>
        `<span class="help-tip" title="${escapeAttr(text)}" aria-label="${escapeAttr(text)}">ⓘ</span>`;

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
        // Optional daily-window cap (v1.14.1). Empty = legacy continuous.
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
          .map((step, i) => this._renderExtraStepRow(step, i, zoneOpts))
          .join("") +
        `<button type="button" class="btn btn-small" data-action="add-extra-step">+ Add another zone</button>` +
        `</div>` +
        `<label class="enabled-check"><input type="checkbox" name="enabled"${
          e.enabled ? " checked" : ""
        } />Enabled ${tip("Toggle off to keep the schedule but stop it from firing. Useful while traveling.")}</label>` +
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

    _renderExtraStepRow(step, idx, zoneOpts) {
      // One row per extra step: zone picker + duration h/m + remove button.
      const totalMin = parseInt(step.duration_minutes, 10) || 0;
      const h = Math.floor(totalMin / 60);
      const m = totalMin % 60;
      // Re-render zone options with this step's selected value
      const opts = zoneOpts.replace(/ selected/g, "").replace(
        new RegExp(`value="${step.zone_entity_id.replace(/[.\\^$*+?()|[\]{}]/g, "\\$&")}"`),
        `value="${step.zone_entity_id}" selected`
      );
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
        `.btn-small{padding:6px 10px;font-size:12px}` +
        `.empty{background:var(--ci-card);border:1px dashed var(--ci-border);border-radius:12px;padding:24px;text-align:center;color:var(--ci-text-2)}` +
        `.placeholder{background:var(--ci-card);border:1px solid var(--ci-border);border-radius:12px;padding:24px}` +
        // Modal
        `.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.45);z-index:99}` +
        `.modal{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--ci-card);color:var(--ci-text);border-radius:12px;padding:24px;min-width:320px;max-width:90vw;z-index:100;box-shadow:0 10px 40px rgba(0,0,0,0.3)}` +
        `.modal-wide{min-width:420px;max-width:480px}` +
        `.modal h3{margin:0 0 16px;font-size:16px}` +
        `.modal label{display:block;font-size:12px;color:var(--ci-text-2);margin:10px 0 4px}` +
        `.modal input[type=number],.modal input[type=text],.modal input[type=time],.modal input[type=date],.modal select,.modal textarea{width:100%;min-width:0;padding:8px 10px;border:1px solid var(--ci-border);border-radius:6px;font-size:14px;background:var(--ci-input-bg);color:inherit;font-family:inherit;box-sizing:border-box}` +
        // Same shape for textareas anywhere in the panel (Notifications uses one)
        `.weather-form textarea{width:100%;min-width:0;padding:8px 10px;border:1px solid var(--ci-border);border-radius:6px;font-size:14px;background:var(--ci-input-bg);color:inherit;font-family:inherit;box-sizing:border-box;resize:vertical}` +
        `.modal .hint{margin:6px 0 16px;font-size:11px;color:var(--ci-text-2)}` +
        `.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:18px}` +
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
        `.day-cal-now{position:absolute;left:0;right:0;border-top:2px solid #db4437;z-index:2;pointer-events:none}` +
        `.day-cal-empty-hint{position:absolute;top:24px;left:60px;right:8px;text-align:center;color:var(--ci-text-2);font-size:13px}` +
        // Run history tab
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
        `.weekday-group{display:flex;flex-wrap:wrap;gap:6px}` +
        `.weekday-shortcuts{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}` +
        `.weekday-shortcuts .btn{font-size:11px;padding:4px 8px}` +
        `.weekday-check{display:inline-flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--ci-border);border-radius:6px;cursor:pointer;font-size:12px;color:var(--ci-text);margin:0}` +
        `.weekday-check input{margin-right:4px}` +
        `.enabled-check{display:inline-flex;align-items:center;gap:6px;margin-top:14px;color:var(--ci-text);font-size:13px}` +
        `.help-tip{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;background:var(--ci-hover);color:var(--ci-text-2);font-size:11px;margin-left:4px;cursor:help;vertical-align:middle}` +
        `.help-tip:hover{background:var(--ci-accent);color:#fff}` +
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
        `.sensor-pick{display:flex;align-items:flex-start;gap:8px;padding:6px;border-radius:4px;cursor:pointer;font-size:13px}` +
        `.sensor-pick:hover{background:var(--ci-hover)}` +
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
        // Modal: nearly full-width on phones
        `.modal{min-width:0;width:calc(100vw - 24px);max-width:calc(100vw - 24px);padding:16px}` +
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

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function escapeAttr(s) {
    return escapeHtml(s);
  }

  customElements.define(ELEMENT_NAME, CompleteIrrigationPanel);
  console.info("[complete-irrigation] panel registered, version v1.14.1");
})();
