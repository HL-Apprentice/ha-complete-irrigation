/**
 * Complete Irrigation Panel — v0.3.0 (Slice 2 added).
 *
 * Vanilla Web Component. Variant 2 layout (sidebar + main) per ADR-0002,
 * collapsible. Today view shows configured zones with live status,
 * Run-Now buttons, and a duration modal.
 *
 * Properties HA sets on us:
 *   this.hass   — full HA state object (states, callService, etc.)
 *   this.panel  — { config: { zones, controller_domain, _panel_custom } }
 */

(function () {
  "use strict";

  const SIDEBAR_STORAGE_KEY = "complete_irrigation_sidebar_collapsed";
  const ELEMENT_NAME = "complete-irrigation-panel";
  const DEFAULT_MANUAL_MINUTES = 10;
  const MAX_MANUAL_MINUTES = 60;

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
      this._modalOpen = false;
      this._modalEntityId = null;
      this._modalZoneName = "";
      this._modalMinutes = DEFAULT_MANUAL_MINUTES;
      this._renderScheduled = false;
      this._onClick = this._onClick.bind(this);
      this._onSubmit = this._onSubmit.bind(this);
    }

    // ── HA-set properties ──────────────────────────────────────────
    set hass(value) {
      this._hass = value;
      this._scheduleRender();
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
      this._scheduleRender();
    }

    disconnectedCallback() {
      this.shadowRoot.removeEventListener("click", this._onClick);
      this.shadowRoot.removeEventListener("submit", this._onSubmit);
    }

    _onClick(e) {
      const path = e.composedPath ? e.composedPath() : [];
      for (const node of path) {
        if (!node || node === this.shadowRoot || node === this) break;
        if (!(node instanceof HTMLElement)) continue;

        if (node.classList.contains("collapse-btn")) return this._toggleSidebar();
        if (node.classList.contains("modal-cancel")) return this._closeModal();
        if (node.classList.contains("modal-backdrop")) return this._closeModal();

        if (node.dataset.action === "run-now") {
          e.stopPropagation();
          return this._openModal(node.dataset.entityId, node.dataset.zoneName);
        }
        if (node.dataset.action === "stop") {
          e.stopPropagation();
          return this._stopZone(node.dataset.entityId);
        }
        if (node.dataset.section) return this._navigateTo(node.dataset.section);
      }
    }

    _onSubmit(e) {
      if (e.target && e.target.classList.contains("modal-form")) {
        e.preventDefault();
        const input = e.target.querySelector('input[name="minutes"]');
        const minutes = parseInt(input?.value || "0", 10);
        if (!minutes || minutes < 1 || minutes > MAX_MANUAL_MINUTES) {
          alert("Duration must be between 1 and " + MAX_MANUAL_MINUTES + " minutes");
          return;
        }
        this._runZone(this._modalEntityId, minutes);
        this._closeModal();
      }
    }

    _scheduleRender() {
      if (this._renderScheduled) return;
      this._renderScheduled = true;
      queueMicrotask(() => {
        this._renderScheduled = false;
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
      });
    }

    _toggleSidebar() {
      this._collapsed = !this._collapsed;
      try {
        localStorage.setItem(SIDEBAR_STORAGE_KEY, String(this._collapsed));
      } catch (_) {}
      this._scheduleRender();
    }

    _navigateTo(sectionId) {
      this._currentSection = sectionId;
      this._scheduleRender();
    }

    _openModal(entityId, zoneName) {
      this._modalOpen = true;
      this._modalEntityId = entityId;
      this._modalZoneName = zoneName || entityId;
      this._modalMinutes = DEFAULT_MANUAL_MINUTES;
      this._scheduleRender();
    }

    _closeModal() {
      this._modalOpen = false;
      this._modalEntityId = null;
      this._modalZoneName = "";
      this._scheduleRender();
    }

    // ── Service calls ──────────────────────────────────────────────
    async _runZone(entityId, minutes) {
      if (!this._hass || !this._hass.callService) {
        console.error("[complete-irrigation] hass not available — cannot run zone");
        return;
      }
      try {
        await this._hass.callService("complete_irrigation", "run_zone", {
          entity_id: entityId,
          minutes: minutes,
        });
        console.info("[complete-irrigation] run_zone", entityId, minutes + "m");
      } catch (err) {
        console.error("[complete-irrigation] run_zone failed:", err);
        alert("Failed to start zone: " + (err && err.message ? err.message : err));
      }
    }

    async _stopZone(entityId) {
      if (!this._hass || !this._hass.callService) return;
      try {
        await this._hass.callService("complete_irrigation", "stop_zone", {
          entity_id: entityId,
        });
        console.info("[complete-irrigation] stop_zone", entityId);
      } catch (err) {
        console.error("[complete-irrigation] stop_zone failed:", err);
        alert("Failed to stop zone: " + (err && err.message ? err.message : err));
      }
    }

    // ── Data helpers ───────────────────────────────────────────────
    _zones() {
      const ids = (this._panel && this._panel.config && this._panel.config.zones) || [];
      return ids.map((entityId) => {
        const state = this._hass && this._hass.states ? this._hass.states[entityId] : null;
        const friendly =
          (state && state.attributes && state.attributes.friendly_name) ||
          entityId.replace(/^switch\./, "").replace(/_/g, " ");
        return {
          entityId: entityId,
          name: friendly,
          on: state && state.state === "on",
          available: !!state,
        };
      });
    }

    // ── Rendering ──────────────────────────────────────────────────
    _render() {
      const sidebarClass = this._collapsed ? "sidebar collapsed" : "sidebar";

      const navItems = SECTIONS.map(function (s) {
        return (
          '<button class="sidebar-item ' +
          (s.id === this._currentSection ? "active" : "") +
          '" data-section="' +
          s.id +
          '" title="' +
          escapeHtml(s.label) +
          '">' +
          '<span class="sidebar-icon">' +
          s.icon +
          "</span>" +
          '<span class="sidebar-label">' +
          escapeHtml(s.label) +
          "</span>" +
          "</button>"
        );
      }, this).join("");

      this.shadowRoot.innerHTML =
        "<style>" +
        this._styles() +
        "</style>" +
        '<div class="root">' +
        '<aside class="' +
        sidebarClass +
        '">' +
        '<div class="sidebar-header">' +
        '<button class="collapse-btn" title="Toggle sidebar">' +
        "<span>" +
        (this._collapsed ? "›" : "‹") +
        "</span>" +
        "</button>" +
        '<span class="brand">💧 Irrigation</span>' +
        "</div>" +
        "<nav>" +
        navItems +
        "</nav>" +
        "</aside>" +
        "<main>" +
        this._renderSection() +
        "</main>" +
        "</div>" +
        (this._modalOpen ? this._renderModal() : "");
    }

    _renderSection() {
      if (this._currentSection === "today") return this._renderToday();
      const section = SECTIONS.find(function (s) {
        return s.id === this._currentSection;
      }) || { icon: "", label: "Section" };
      return (
        '<div class="placeholder">' +
        "<h2>" +
        section.icon +
        " " +
        escapeHtml(section.label) +
        "</h2>" +
        "<p>Coming in a later slice — see the project roadmap for details.</p>" +
        "</div>"
      );
    }

    _renderToday() {
      const zones = this._zones();
      return (
        '<header class="page-header">' +
        "<h2>Today</h2>" +
        '<span class="version-pill">v0.3.0 — manual run</span>' +
        "</header>" +
        "<section>" +
        '<h3 class="section-title">Zones (' +
        zones.length +
        ")</h3>" +
        (zones.length === 0
          ? this._renderEmpty()
          : '<div class="zone-grid">' +
            zones
              .map(function (z) {
                return this._renderZoneTile(z);
              }, this)
              .join("") +
            "</div>") +
        "</section>"
      );
    }

    _renderEmpty() {
      return '<div class="empty"><p>No zones configured. Re-run setup from Settings → Devices & services.</p></div>';
    }

    _renderZoneTile(zone) {
      const statusClass = !zone.available ? "unavailable" : zone.on ? "running" : "idle";
      const statusLabel = !zone.available ? "Unavailable" : zone.on ? "Running" : "Idle";
      const action = zone.on
        ? '<button class="btn btn-stop" data-action="stop" data-entity-id="' +
          escapeAttr(zone.entityId) +
          '">⏹ Stop</button>'
        : '<button class="btn btn-run" data-action="run-now" data-entity-id="' +
          escapeAttr(zone.entityId) +
          '" data-zone-name="' +
          escapeAttr(zone.name) +
          '"' +
          (zone.available ? "" : " disabled") +
          ">▶ Run Now</button>";

      return (
        '<article class="zone-tile">' +
        "<header>" +
        '<span class="status-dot ' +
        statusClass +
        '"></span>' +
        "<h4>" +
        escapeHtml(zone.name) +
        "</h4>" +
        "</header>" +
        '<div class="entity-id">' +
        escapeHtml(zone.entityId) +
        "</div>" +
        '<div class="status-text">' +
        statusLabel +
        "</div>" +
        '<div class="zone-actions">' +
        action +
        "</div>" +
        "</article>"
      );
    }

    _renderModal() {
      return (
        '<div class="modal-backdrop"></div>' +
        '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">' +
        '<form class="modal-form">' +
        '<h3 id="modal-title">Run ' +
        escapeHtml(this._modalZoneName) +
        "</h3>" +
        '<label for="minutes-input">Duration (minutes)</label>' +
        '<input id="minutes-input" name="minutes" type="number" min="1" max="' +
        MAX_MANUAL_MINUTES +
        '" step="1" value="' +
        this._modalMinutes +
        '" autofocus />' +
        '<p class="hint">Default 10 min. Maximum ' +
        MAX_MANUAL_MINUTES +
        " min.</p>" +
        '<div class="modal-actions">' +
        '<button type="button" class="btn btn-secondary modal-cancel">Cancel</button>' +
        '<button type="submit" class="btn btn-primary">Run</button>' +
        "</div>" +
        "</form>" +
        "</div>"
      );
    }

    _styles() {
      return (
        ":host{display:block;height:100%;background:var(--primary-background-color,#fafafa);color:var(--primary-text-color,#212121);font-family:var(--paper-font-body1_-_font-family,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif);font-size:14px}" +
        "*{box-sizing:border-box}" +
        ".root{display:grid;grid-template-columns:auto 1fr;height:100%;min-height:100vh}" +
        ".sidebar{width:220px;background:var(--card-background-color,#fff);border-right:1px solid var(--divider-color,rgba(0,0,0,0.12));display:flex;flex-direction:column;transition:width 0.18s ease}" +
        ".sidebar.collapsed{width:60px}" +
        ".sidebar-header{display:flex;align-items:center;padding:12px;border-bottom:1px solid var(--divider-color,rgba(0,0,0,0.12));gap:8px}" +
        ".collapse-btn{background:transparent;border:1px solid var(--divider-color,rgba(0,0,0,0.12));border-radius:6px;width:28px;height:28px;cursor:pointer;color:inherit;font-size:16px;line-height:1;display:flex;align-items:center;justify-content:center;flex-shrink:0}" +
        ".brand{font-weight:600;white-space:nowrap;overflow:hidden}" +
        ".sidebar.collapsed .brand,.sidebar.collapsed .sidebar-label{display:none}" +
        "nav{display:flex;flex-direction:column;padding:8px 0;gap:2px}" +
        ".sidebar-item{display:flex;align-items:center;gap:12px;padding:10px 14px;background:transparent;border:none;border-left:3px solid transparent;color:var(--secondary-text-color,#727272);font-size:14px;text-align:left;cursor:pointer;font-family:inherit}" +
        ".sidebar-item:hover{background:var(--primary-background-color,#f6f6f6);color:var(--primary-text-color,#212121)}" +
        ".sidebar-item.active{color:var(--primary-color,#03a9f4);background:var(--primary-background-color,rgba(3,169,244,0.08));border-left-color:var(--primary-color,#03a9f4);font-weight:500}" +
        ".sidebar-icon{width:24px;text-align:center;font-size:16px;flex-shrink:0}" +
        "main{padding:24px;overflow:auto}" +
        ".page-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}" +
        ".page-header h2{margin:0;font-size:22px;font-weight:600}" +
        ".version-pill{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,rgba(0,0,0,0.12));padding:4px 10px;border-radius:999px;font-size:11px;color:var(--secondary-text-color,#727272)}" +
        ".section-title{font-size:13px;text-transform:uppercase;letter-spacing:0.05em;color:var(--secondary-text-color,#727272);margin:16px 0 8px}" +
        ".zone-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}" +
        ".zone-tile{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,rgba(0,0,0,0.12));border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:6px}" +
        ".zone-tile header{display:flex;align-items:center;gap:10px}" +
        ".zone-tile h4{margin:0;font-size:15px;font-weight:600}" +
        ".status-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}" +
        ".status-dot.idle{background:#bdbdbd}" +
        ".status-dot.running{background:#43a047;box-shadow:0 0 0 4px rgba(67,160,71,0.2)}" +
        ".status-dot.unavailable{background:#db4437}" +
        ".entity-id{font-size:11px;color:var(--secondary-text-color,#727272);font-family:var(--ha-font-family-code,monospace);word-break:break-all}" +
        ".status-text{font-size:12px;color:var(--secondary-text-color,#727272)}" +
        ".zone-actions{margin-top:8px}" +
        ".btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:8px 14px;border-radius:8px;border:1px solid var(--divider-color,rgba(0,0,0,0.12));background:var(--card-background-color,#fff);color:var(--primary-text-color,#212121);font-family:inherit;font-size:13px;font-weight:500;cursor:pointer;width:100%}" +
        ".btn:hover{background:var(--primary-background-color,#f6f6f6)}" +
        ".btn:disabled{opacity:0.5;cursor:not-allowed}" +
        ".btn-run{background:var(--primary-color,#03a9f4);color:#fff;border-color:var(--primary-color,#03a9f4)}" +
        ".btn-run:hover{filter:brightness(1.05);background:var(--primary-color,#03a9f4)}" +
        ".btn-stop{background:#db4437;color:#fff;border-color:#db4437}" +
        ".btn-stop:hover{filter:brightness(1.05);background:#db4437}" +
        ".empty{background:var(--card-background-color,#fff);border:1px dashed var(--divider-color,rgba(0,0,0,0.2));border-radius:12px;padding:24px;text-align:center;color:var(--secondary-text-color,#727272)}" +
        ".placeholder{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,rgba(0,0,0,0.12));border-radius:12px;padding:24px}" +
        ".placeholder h2{margin-top:0}" +
        // Modal
        ".modal-backdrop{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.45);z-index:99}" +
        ".modal{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--card-background-color,#fff);color:var(--primary-text-color,#212121);border-radius:12px;padding:24px;min-width:320px;max-width:90vw;z-index:100;box-shadow:0 10px 40px rgba(0,0,0,0.3)}" +
        ".modal h3{margin:0 0 16px;font-size:16px}" +
        ".modal label{display:block;font-size:12px;color:var(--secondary-text-color,#727272);margin-bottom:6px}" +
        ".modal input[type=number]{width:100%;padding:8px 10px;border:1px solid var(--divider-color,rgba(0,0,0,0.18));border-radius:6px;font-size:16px;background:var(--primary-background-color,#fff);color:inherit;font-family:inherit}" +
        ".modal .hint{margin:6px 0 16px;font-size:11px;color:var(--secondary-text-color,#727272)}" +
        ".modal-actions{display:flex;gap:8px;justify-content:flex-end}" +
        ".btn-primary{background:var(--primary-color,#03a9f4);color:#fff;border-color:var(--primary-color,#03a9f4);width:auto;padding:8px 18px}" +
        ".btn-primary:hover{filter:brightness(1.05);background:var(--primary-color,#03a9f4)}" +
        ".btn-secondary{background:transparent;width:auto;padding:8px 18px}" +
        // Mobile
        "@media (max-width:700px){.sidebar:not(.collapsed){position:fixed;z-index:10;height:100%}.sidebar.collapsed{width:56px}.root{grid-template-columns:56px 1fr}}"
      );
    }
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
  console.info("[complete-irrigation] panel registered, version v0.3.0");
})();
