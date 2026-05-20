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
  const ELEMENT_NAME = "complete-irrigation-panel";
  const DEFAULT_MANUAL_MINUTES = 10;
  const MAX_MANUAL_MINUTES = 60;
  const MAX_SCHEDULE_MINUTES = 240;

  if (customElements.get(ELEMENT_NAME)) return;

  const SECTIONS = [
    { id: "today", label: "Today", icon: "📅" },
    { id: "schedules", label: "Schedules", icon: "⏰" },
    { id: "zones", label: "Zones", icon: "🌱" },
    { id: "sensors", label: "Sensors", icon: "📊" },
    { id: "weather", label: "Weather", icon: "🌧️" },
    { id: "notifications", label: "Notifications", icon: "🔔" },
    { id: "settings", label: "Settings", icon: "⚙️" },
  ];

  const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  function emptyEditor() {
    return {
      id: null, // null = creating new
      name: "",
      zone_entity_id: "",
      start_time: "06:00",
      duration_minutes: 15,
      weekdays: [0, 1, 2, 3, 4],
      enabled: true,
    };
  }

  class CompleteIrrigationPanel extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._hass = null;
      this._panel = null;
      this._collapsed = false;
      try {
        this._collapsed = localStorage.getItem(SIDEBAR_STORAGE_KEY) === "true";
      } catch (_) {}
      this._currentSection = "today";

      // Manual-run modal state
      this._runModalOpen = false;
      this._runModalEntityId = null;
      this._runModalZoneName = "";

      // Schedule modal state + cached list
      this._scheduleModalOpen = false;
      this._scheduleEditor = emptyEditor();
      this._schedules = [];
      this._schedulesLoaded = false;

      // Weather + config cached from WS API
      this._config = {};
      this._configLoaded = false;

      // Local manual-run countdowns: entity_id -> deadline epoch ms
      this._localRuns = {};
      this._countdownTimer = null;

      // Hidden zones (per-browser, persisted)
      this._hiddenZones = new Set();
      try {
        const stored = localStorage.getItem(HIDDEN_ZONES_STORAGE_KEY);
        if (stored) this._hiddenZones = new Set(JSON.parse(stored));
      } catch (_) {}
      this._showHidden = false;

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
        if (action === "show-hidden") return this._toggleShowHidden();
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
      if (e.target?.classList.contains("schedule-form")) {
        e.preventDefault();
        this._saveSchedule();
      }
    }

    _onChange(e) {
      const t = e.target;
      if (!t || !t.name) return;
      if (t.name === "weekday") {
        const day = parseInt(t.value, 10);
        const set = new Set(this._scheduleEditor.weekdays);
        if (t.checked) set.add(day);
        else set.delete(day);
        this._scheduleEditor.weekdays = Array.from(set).sort((a, b) => a - b);
      } else if (t.name === "enabled") {
        this._scheduleEditor.enabled = t.checked;
      } else if (t.name in this._scheduleEditor) {
        this._scheduleEditor[t.name] = t.value;
      }
    }

    _onInput(e) {
      // Keep schedule editor state in sync as user types (so re-renders
      // triggered by other changes don't blow away unsaved edits).
      const t = e.target;
      if (
        t &&
        t.name &&
        t.name !== "weekday" &&
        t.name !== "enabled" &&
        t.name in this._scheduleEditor
      ) {
        this._scheduleEditor[t.name] = t.value;
      }
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
      if (zones.length) this._scheduleEditor.zone_entity_id = zones[0];
      this._scheduleModalOpen = true;
      this._renderNow();
    }

    _openEditSchedule(scheduleId) {
      const found = this._schedules.find((s) => s.id === scheduleId);
      if (!found) return;
      this._scheduleEditor = {
        id: found.id,
        name: found.name,
        zone_entity_id: found.zone_entity_id,
        start_time: found.start_time,
        duration_minutes: found.duration_minutes,
        weekdays: [...found.weekdays],
        enabled: found.enabled,
      };
      this._scheduleModalOpen = true;
      this._renderNow();
    }

    _closeAllModals() {
      this._runModalOpen = false;
      this._scheduleModalOpen = false;
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

    _toggleShowHidden() {
      this._showHidden = !this._showHidden;
      this._renderNow();
    }

    _startLocalCountdown(entityId, minutes) {
      this._localRuns[entityId] = Date.now() + minutes * 60 * 1000;
      if (!this._countdownTimer) {
        this._countdownTimer = setInterval(() => {
          const now = Date.now();
          for (const eid of Object.keys(this._localRuns)) {
            if (this._localRuns[eid] <= now) delete this._localRuns[eid];
          }
          if (Object.keys(this._localRuns).length === 0 && this._countdownTimer) {
            clearInterval(this._countdownTimer);
            this._countdownTimer = null;
          }
          this._renderNow();
        }, 1000);
      }
      this._renderNow();
    }

    _stopLocalCountdown(entityId) {
      delete this._localRuns[entityId];
      if (Object.keys(this._localRuns).length === 0 && this._countdownTimer) {
        clearInterval(this._countdownTimer);
        this._countdownTimer = null;
      }
      this._renderNow();
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

    async _runZone(entityId, minutes) {
      if (!this._hass?.callService) return;
      try {
        await this._hass.callService("complete_irrigation", "run_zone", {
          entity_id: entityId,
          minutes,
        });
        this._startLocalCountdown(entityId, minutes);
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
      } catch (err) {
        alert("Failed to stop zone: " + (err?.message || err));
      }
    }

    async _saveSchedule() {
      const e = this._scheduleEditor;
      const minutes = parseInt(e.duration_minutes, 10);
      if (!e.name || !e.name.trim()) return alert("Schedule name is required.");
      if (!e.zone_entity_id) return alert("Pick a zone.");
      if (!minutes || minutes < 1 || minutes > MAX_SCHEDULE_MINUTES)
        return alert(`Duration must be 1–${MAX_SCHEDULE_MINUTES} min.`);
      if (!e.weekdays.length) return alert("Pick at least one weekday.");

      try {
        if (e.id) {
          await this._hass.callService("complete_irrigation", "update_schedule", {
            schedule_id: e.id,
            name: e.name.trim(),
            zone_entity_id: e.zone_entity_id,
            start_time: e.start_time,
            duration_minutes: minutes,
            weekdays: e.weekdays,
            enabled: e.enabled,
          });
        } else {
          await this._hass.callService("complete_irrigation", "add_schedule", {
            name: e.name.trim(),
            zone_entity_id: e.zone_entity_id,
            start_time: e.start_time,
            duration_minutes: minutes,
            weekdays: e.weekdays,
            enabled: e.enabled,
          });
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
    _zones() {
      const ids = this._panel?.config?.zones || [];
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
    _render() {
      const sidebarClass = this._collapsed ? "sidebar collapsed" : "sidebar";

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
        (this._scheduleModalOpen ? this._renderScheduleModal() : "");
    }

    _renderSection() {
      if (this._currentSection === "today") return this._renderToday();
      if (this._currentSection === "schedules") return this._renderSchedules();
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

    _renderSettings() {
      return (
        `<header class="page-header"><h2>Settings</h2></header>` +
        `<section><h3 class="section-title">v1.1 quick reference</h3>` +
        `<div class="placeholder">` +
        `<p><strong>Quick configuration UI coming in v1.2.</strong> All features work today via Developer Tools → Services:</p>` +
        `<table style="width:100%;font-size:13px;border-collapse:collapse;margin-top:12px">` +
        `<tr><td style="padding:6px 0;color:var(--secondary-text-color)">Notifications</td><td><code>complete_irrigation.set_notification_config</code></td></tr>` +
        `<tr><td style="padding:6px 0;color:var(--secondary-text-color)">Test notification</td><td><code>complete_irrigation.test_notification</code></td></tr>` +
        `<tr><td style="padding:6px 0;color:var(--secondary-text-color)">Rain + hot weather</td><td><code>complete_irrigation.set_weather_config</code></td></tr>` +
        `<tr><td style="padding:6px 0;color:var(--secondary-text-color)">Per-zone moisture</td><td><code>complete_irrigation.set_zone_moisture</code></td></tr>` +
        `<tr><td style="padding:6px 0;color:var(--secondary-text-color)">New grass mode</td><td><code>complete_irrigation.start_establishment</code></td></tr>` +
        `<tr><td style="padding:6px 0;color:var(--secondary-text-color)">Clear rain lockout</td><td><code>complete_irrigation.clear_rain_lockout</code></td></tr>` +
        `</table>` +
        `<p style="margin-top:16px;font-size:12px;color:var(--secondary-text-color)">` +
        `iCal feed: <code>/api/complete_irrigation/calendar.ics</code> ` +
        `(subscribe from your phone's calendar app for the next 30 days of planned runs).` +
        `</p>` +
        `</div></section>`
      );
    }

    _renderToday() {
      const allZones = this._zones();
      const hiddenZones = allZones.filter((z) => this._hiddenZones.has(z.entityId));
      const visibleZones = allZones.filter((z) => !this._hiddenZones.has(z.entityId));
      const zonesToShow = this._showHidden ? allZones : visibleZones;

      let hiddenToggle = "";
      if (hiddenZones.length > 0) {
        hiddenToggle = `<button class="btn-link" data-action="show-hidden">${
          this._showHidden
            ? `Hide hidden zones (${hiddenZones.length})`
            : `Show hidden zones (${hiddenZones.length})`
        }</button>`;
      }

      return (
        `<header class="page-header"><h2>Today</h2>` +
        `<span class="version-pill">v1.3.0</span></header>` +
        this._renderRainLockoutBanner() +
        this._renderWeatherBanner() +
        `<section>` +
        `<div class="section-title-row">` +
        `<h3 class="section-title">Zones (${visibleZones.length}${
          hiddenZones.length ? ` + ${hiddenZones.length} hidden` : ""
        })</h3>` +
        hiddenToggle +
        `</div>` +
        (zonesToShow.length === 0
          ? this._renderEmpty()
          : `<div class="zone-grid">${zonesToShow
              .map((z) => this._renderZoneTile(z))
              .join("")}</div>`) +
        `</section>`
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

    _renderWeatherBanner() {
      // Auto-detected sensors plus anything explicitly bound via service.
      const detected = this._autoDetectWeatherSensors();
      const cells = [];

      const sunState = this._readSensor("sun.sun");

      // Temperature (explicit > auto)
      const tempState =
        this._readSensor(this._config?.temperature_sensor) || detected.temperature;
      if (tempState) {
        const unit = tempState.attributes?.unit_of_measurement || "°";
        cells.push(this._weatherCell("🌡️", "Temp", `${tempState.state}${unit}`));
      }

      // Feels like
      if (detected.feels_like) {
        const unit = detected.feels_like.attributes?.unit_of_measurement || "°";
        cells.push(this._weatherCell("🤚", "Feels like", `${detected.feels_like.state}${unit}`));
      }

      // Humidity
      if (detected.humidity) {
        cells.push(this._weatherCell("💧", "Humidity", `${detected.humidity.state}%`));
      }

      // Dew point
      if (detected.dew_point) {
        const unit = detected.dew_point.attributes?.unit_of_measurement || "°";
        cells.push(this._weatherCell("🌫️", "Dew pt", `${detected.dew_point.state}${unit}`));
      }

      // Rain today (explicit > auto)
      const rainState = this._readSensor(this._config?.rain_sensor) || detected.rain;
      if (rainState) {
        const unit = rainState.attributes?.unit_of_measurement || "in";
        cells.push(this._weatherCell("☔", "Rain today", `${rainState.state} ${unit}`));
      }

      // Wind
      if (detected.wind_speed) {
        const unit = detected.wind_speed.attributes?.unit_of_measurement || "mph";
        let val = `${detected.wind_speed.state} ${unit}`;
        if (detected.wind_gust) {
          val += ` (gust ${detected.wind_gust.state})`;
        }
        cells.push(this._weatherCell("💨", "Wind", val));
      }

      // UV
      if (detected.uv) {
        cells.push(this._weatherCell("🔆", "UV index", detected.uv.state));
      }

      // Solar
      if (detected.solar) {
        const unit = detected.solar.attributes?.unit_of_measurement || "W/m²";
        cells.push(this._weatherCell("☀️", "Solar", `${detected.solar.state} ${unit}`));
      }

      // Pressure
      if (detected.pressure) {
        const unit = detected.pressure.attributes?.unit_of_measurement || "";
        cells.push(this._weatherCell("📊", "Pressure", `${detected.pressure.state} ${unit}`));
      }

      // Sunrise/sunset
      if (sunState) {
        const setNext = sunState.attributes?.next_setting;
        const riseNext = sunState.attributes?.next_rising;
        if (riseNext) {
          const dt = new Date(riseNext);
          cells.push(this._weatherCell("🌄", "Sunrise",
            dt.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })));
        }
        if (setNext) {
          const dt = new Date(setNext);
          cells.push(this._weatherCell("🌅", "Sunset",
            dt.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })));
        }
      }

      // Hot weather threshold if configured
      const hotThreshold = this._config?.hot_threshold_f;
      const boostPct = this._config?.boost_percent;
      if (hotThreshold && boostPct) {
        cells.push(this._weatherCell("🔥", "Hot boost",
          `>${hotThreshold}°F = +${boostPct}%`));
      }

      // Empty-state hint if literally nothing was found
      if (cells.length === 0) {
        return (
          `<div class="weather-banner weather-banner-empty">` +
          `<span style="font-size:20px">🌤️</span>` +
          `<div style="flex:1">` +
          `<div style="font-weight:600;font-size:13px">No weather data found yet</div>` +
          `<div style="font-size:12px;color:var(--secondary-text-color)">` +
          `Install the WeatherFlow Tempest integration (or any weather entity) ` +
          `and the banner will auto-populate. Or call ` +
          `<code>complete_irrigation.set_weather_config</code> to bind specific sensors.` +
          `</div></div></div>`
        );
      }

      return `<div class="weather-banner">${cells.join("")}</div>`;
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

      let statusClass, statusLabel;
      if (!zone.available) {
        statusClass = "unavailable";
        statusLabel = "Unavailable";
      } else if (zone.on) {
        statusClass = "running";
        statusLabel = isCountingDown
          ? `Running — ${_formatRemaining(remainingMs)} left`
          : "Running";
      } else {
        statusClass = "idle";
        statusLabel = "Idle";
      }

      const action = zone.on
        ? `<button class="btn btn-stop" data-action="stop" data-entity-id="${escapeAttr(
            zone.entityId
          )}">⏹ Stop${isCountingDown ? " (" + _formatRemaining(remainingMs) + ")" : ""}</button>`
        : `<button class="btn btn-run" data-action="run-now" data-entity-id="${escapeAttr(
            zone.entityId
          )}" data-zone-name="${escapeAttr(zone.name)}"${
            zone.available ? "" : " disabled"
          }>▶ Run Now</button>`;

      const hideAction = isHidden
        ? `<button class="btn-icon" data-action="show-zone" data-entity-id="${escapeAttr(zone.entityId)}" title="Show this zone">👁️</button>`
        : `<button class="btn-icon" data-action="hide-zone" data-entity-id="${escapeAttr(zone.entityId)}" title="Hide this zone">🚫</button>`;

      return (
        `<article class="zone-tile${isHidden ? " zone-hidden" : ""}">` +
        `<header>` +
        `<span class="status-dot ${statusClass}"></span>` +
        `<h4>${escapeHtml(zone.name)}</h4>` +
        hideAction +
        `</header>` +
        `<div class="entity-id">${escapeHtml(zone.entityId)}</div>` +
        `<div class="status-text">${statusLabel}</div>` +
        `<div class="zone-actions">${action}</div>` +
        `</article>`
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
      const days = s.weekdays.map((d) => WEEKDAY_LABELS[d] || "?").join(" ");
      const zoneName = this._zoneName(s.zone_entity_id);
      const enabledClass = s.enabled ? "enabled" : "disabled";
      return (
        `<article class="schedule-row ${enabledClass}">` +
        `<div class="schedule-row-main">` +
        `<div class="schedule-name">${escapeHtml(s.name)}${
          s.enabled ? "" : " (disabled)"
        }</div>` +
        `<div class="schedule-meta">` +
        `${escapeHtml(zoneName)} · ${s.start_time} · ${s.duration_minutes} min · ${escapeHtml(
          days
        )}` +
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
      return (
        `<div class="modal-backdrop"></div>` +
        `<div class="modal" role="dialog" aria-modal="true">` +
        `<form class="modal-form run-form">` +
        `<h3>Run ${escapeHtml(this._runModalZoneName)}</h3>` +
        `<label for="minutes-input">Duration (minutes)</label>` +
        `<input id="minutes-input" name="minutes" type="number" min="1" max="${MAX_MANUAL_MINUTES}" step="1" value="${DEFAULT_MANUAL_MINUTES}" autofocus />` +
        `<p class="hint">Default 10 min. Maximum ${MAX_MANUAL_MINUTES} min.</p>` +
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
      const zones = this._panel?.config?.zones || [];
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
        `<div class="row-2">` +
        `<div>` +
        `<label>Start time ${tip("Time of day (24h, local) to start the run. Defaults to 06:00.")}</label>` +
        `<input name="start_time" type="time" value="${escapeAttr(
          e.start_time
        )}" required />` +
        `</div>` +
        `<div>` +
        `<label>Duration (min) ${tip("How long to run, 1-" + MAX_SCHEDULE_MINUTES + " min. Moisture sensors can adjust this up or down at runtime.")}</label>` +
        `<input name="duration_minutes" type="number" min="1" max="${MAX_SCHEDULE_MINUTES}" step="1" value="${e.duration_minutes}" required />` +
        `</div>` +
        `</div>` +
        `<label>Weekdays ${tip("Pick the days this schedule fires. Defaults to Mon-Fri.")}</label>` +
        `<div class="weekday-group">${weekdayChecks}</div>` +
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

    _styles() {
      return (
        `:host{display:block;height:100%;background:var(--primary-background-color,#fafafa);color:var(--primary-text-color,#212121);font-family:var(--paper-font-body1_-_font-family,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif);font-size:14px}` +
        `*{box-sizing:border-box}` +
        `.root{display:grid;grid-template-columns:auto 1fr;height:100%;min-height:100vh}` +
        `.sidebar{width:220px;background:var(--card-background-color,#fff);border-right:1px solid var(--divider-color,rgba(0,0,0,0.12));display:flex;flex-direction:column;transition:width 0.18s ease}` +
        `.sidebar.collapsed{width:60px}` +
        `.sidebar-header{display:flex;align-items:center;padding:12px;border-bottom:1px solid var(--divider-color,rgba(0,0,0,0.12));gap:8px}` +
        `.collapse-btn{background:transparent;border:1px solid var(--divider-color,rgba(0,0,0,0.12));border-radius:6px;width:28px;height:28px;cursor:pointer;color:inherit;font-size:16px;line-height:1;display:flex;align-items:center;justify-content:center;flex-shrink:0}` +
        `.brand{font-weight:600;white-space:nowrap;overflow:hidden}` +
        `.sidebar.collapsed .brand,.sidebar.collapsed .sidebar-label{display:none}` +
        `nav{display:flex;flex-direction:column;padding:8px 0;gap:2px}` +
        `.sidebar-item{display:flex;align-items:center;gap:12px;padding:10px 14px;background:transparent;border:none;border-left:3px solid transparent;color:var(--secondary-text-color,#727272);font-size:14px;text-align:left;cursor:pointer;font-family:inherit}` +
        `.sidebar-item:hover{background:var(--primary-background-color,#f6f6f6);color:var(--primary-text-color,#212121)}` +
        `.sidebar-item.active{color:var(--primary-color,#03a9f4);background:var(--primary-background-color,rgba(3,169,244,0.08));border-left-color:var(--primary-color,#03a9f4);font-weight:500}` +
        `.sidebar-icon{width:24px;text-align:center;font-size:16px;flex-shrink:0}` +
        `main{padding:24px;overflow:auto}` +
        `.page-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;gap:12px}` +
        `.page-header h2{margin:0;font-size:22px;font-weight:600}` +
        `.version-pill{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,rgba(0,0,0,0.12));padding:4px 10px;border-radius:999px;font-size:11px;color:var(--secondary-text-color,#727272)}` +
        `.section-title{font-size:13px;text-transform:uppercase;letter-spacing:0.05em;color:var(--secondary-text-color,#727272);margin:16px 0 8px}` +
        // Zone tiles
        `.zone-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}` +
        `.zone-tile{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,rgba(0,0,0,0.12));border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:6px}` +
        `.zone-tile header{display:flex;align-items:center;gap:10px}` +
        `.zone-tile h4{margin:0;font-size:15px;font-weight:600}` +
        `.status-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}` +
        `.status-dot.idle{background:#bdbdbd}` +
        `.status-dot.running{background:#43a047;box-shadow:0 0 0 4px rgba(67,160,71,0.2)}` +
        `.status-dot.unavailable{background:#db4437}` +
        `.entity-id{font-size:11px;color:var(--secondary-text-color,#727272);font-family:var(--ha-font-family-code,monospace);word-break:break-all}` +
        `.status-text{font-size:12px;color:var(--secondary-text-color,#727272)}` +
        `.zone-actions{margin-top:8px}` +
        // Schedule list
        `.schedule-list{display:flex;flex-direction:column;gap:8px}` +
        `.schedule-row{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,rgba(0,0,0,0.12));border-radius:10px;padding:12px 14px;display:flex;align-items:center;justify-content:space-between;gap:12px}` +
        `.schedule-row.disabled{opacity:0.55}` +
        `.schedule-row-main{flex:1;min-width:0}` +
        `.schedule-name{font-weight:600;font-size:15px}` +
        `.schedule-meta{color:var(--secondary-text-color,#727272);font-size:12px;margin-top:4px}` +
        `.schedule-row-actions{display:flex;gap:6px;flex-shrink:0}` +
        // Buttons
        `.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:8px 14px;border-radius:8px;border:1px solid var(--divider-color,rgba(0,0,0,0.12));background:var(--card-background-color,#fff);color:var(--primary-text-color,#212121);font-family:inherit;font-size:13px;font-weight:500;cursor:pointer}` +
        `.btn:hover{background:var(--primary-background-color,#f6f6f6)}` +
        `.btn:disabled{opacity:0.5;cursor:not-allowed}` +
        `.btn-run,.btn-stop,.btn-primary{color:#fff}` +
        `.btn-run{background:var(--primary-color,#03a9f4);border-color:var(--primary-color,#03a9f4);width:100%}` +
        `.btn-stop{background:#db4437;border-color:#db4437;width:100%}` +
        `.btn-primary{background:var(--primary-color,#03a9f4);border-color:var(--primary-color,#03a9f4)}` +
        `.btn-secondary{background:transparent}` +
        `.btn-small{padding:6px 10px;font-size:12px}` +
        `.empty{background:var(--card-background-color,#fff);border:1px dashed var(--divider-color,rgba(0,0,0,0.2));border-radius:12px;padding:24px;text-align:center;color:var(--secondary-text-color,#727272)}` +
        `.placeholder{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,rgba(0,0,0,0.12));border-radius:12px;padding:24px}` +
        // Modal
        `.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.45);z-index:99}` +
        `.modal{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--card-background-color,#fff);color:var(--primary-text-color,#212121);border-radius:12px;padding:24px;min-width:320px;max-width:90vw;z-index:100;box-shadow:0 10px 40px rgba(0,0,0,0.3)}` +
        `.modal-wide{min-width:420px;max-width:480px}` +
        `.modal h3{margin:0 0 16px;font-size:16px}` +
        `.modal label{display:block;font-size:12px;color:var(--secondary-text-color,#727272);margin:10px 0 4px}` +
        `.modal input[type=number],.modal input[type=text],.modal input[type=time],.modal select{width:100%;padding:8px 10px;border:1px solid var(--divider-color,rgba(0,0,0,0.18));border-radius:6px;font-size:14px;background:var(--primary-background-color,#fff);color:inherit;font-family:inherit}` +
        `.modal .hint{margin:6px 0 16px;font-size:11px;color:var(--secondary-text-color,#727272)}` +
        `.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:18px}` +
        `.row-2{display:grid;grid-template-columns:1fr 1fr;gap:10px}` +
        `.weekday-group{display:flex;flex-wrap:wrap;gap:6px}` +
        `.weekday-check{display:inline-flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--divider-color,rgba(0,0,0,0.18));border-radius:6px;cursor:pointer;font-size:12px;color:var(--primary-text-color,#212121);margin:0}` +
        `.weekday-check input{margin-right:4px}` +
        `.enabled-check{display:inline-flex;align-items:center;gap:6px;margin-top:14px;color:var(--primary-text-color,#212121);font-size:13px}` +
        `.help-tip{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;background:var(--primary-background-color,#f0f0f0);color:var(--secondary-text-color,#727272);font-size:11px;margin-left:4px;cursor:help;vertical-align:middle}` +
        `.help-tip:hover{background:var(--primary-color,#03a9f4);color:#fff}` +
        // Weather banner
        `.weather-banner{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,rgba(0,0,0,0.12));border-radius:12px;padding:16px;margin-bottom:16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}` +
        `.weather-banner-empty{display:flex;align-items:flex-start;gap:12px;grid-template-columns:none}` +
        `.weather-banner-empty code{background:var(--primary-background-color,#f0f0f0);padding:2px 6px;border-radius:4px;font-size:11px}` +
        `.weather-cell{display:flex;align-items:center;gap:10px}` +
        `.weather-cell-icon{font-size:22px;flex-shrink:0}` +
        `.weather-cell-body{min-width:0}` +
        `.weather-cell-label{font-size:11px;color:var(--secondary-text-color,#727272);text-transform:uppercase;letter-spacing:0.05em}` +
        `.weather-cell-value{font-size:15px;font-weight:600;color:var(--primary-text-color,#212121);margin-top:2px}` +
        // Rain lockout banner
        `.rain-lockout-banner{background:#ffa726;color:#1c1c1c;padding:12px 16px;border-radius:12px;margin-bottom:16px;display:flex;align-items:center;gap:12px}` +
        // Section title row + zone hide
        `.section-title-row{display:flex;align-items:center;justify-content:space-between;margin:16px 0 8px}` +
        `.section-title-row .section-title{margin:0}` +
        `.btn-link{background:none;border:none;color:var(--primary-color,#03a9f4);cursor:pointer;font-size:12px;text-decoration:underline;font-family:inherit;padding:0}` +
        `.btn-icon{background:transparent;border:none;cursor:pointer;font-size:14px;padding:4px 6px;border-radius:4px;opacity:0.5;transition:opacity 0.15s}` +
        `.btn-icon:hover{opacity:1;background:var(--primary-background-color,#f0f0f0)}` +
        `.zone-tile.zone-hidden{opacity:0.55;border-style:dashed}` +
        `.zone-tile header{justify-content:space-between}` +
        `.zone-tile header h4{flex:1}` +
        // Mobile
        `@media (max-width:700px){.sidebar:not(.collapsed){position:fixed;z-index:10;height:100%}.sidebar.collapsed{width:56px}.root{grid-template-columns:56px 1fr}.schedule-row{flex-direction:column;align-items:stretch}}`
      );
    }
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
  console.info("[complete-irrigation] panel registered, version v1.3.0");
})();
