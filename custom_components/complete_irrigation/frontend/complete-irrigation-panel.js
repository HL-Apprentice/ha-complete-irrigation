/**
 * Complete Irrigation Panel — v0.1 skeleton.
 *
 * Vanilla Web Component (no Lit yet). Variant 2 layout per ADR-0002:
 * sidebar nav + main content with a collapsible sidebar.
 *
 * Properties HA sets on us:
 *   this.hass   — full HA state object (states, services, etc.)
 *   this.panel  — { config: { zones, controller_domain, _panel_custom } }
 *
 * Event handling uses delegation on the host element so re-renders
 * (which replace shadowRoot.innerHTML) don't drop handlers.
 */

(function () {
  "use strict";

  const SIDEBAR_STORAGE_KEY = "complete_irrigation_sidebar_collapsed";
  const ELEMENT_NAME = "complete-irrigation-panel";

  // Guard against double registration if HA somehow loads the script twice.
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
      this._renderScheduled = false;
      this._onClick = this._onClick.bind(this);
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
      // Single delegated handler that survives every re-render.
      this.shadowRoot.addEventListener("click", this._onClick);
      this._scheduleRender();
    }

    disconnectedCallback() {
      this.shadowRoot.removeEventListener("click", this._onClick);
    }

    // Walk up from the click target to find an action element.
    _onClick(e) {
      const path = e.composedPath ? e.composedPath() : [];
      for (const node of path) {
        if (!node || node === this.shadowRoot || node === this) break;
        if (node.classList && node.classList.contains("collapse-btn")) {
          this._toggleSidebar();
          return;
        }
        if (node.dataset && node.dataset.section) {
          this._navigateTo(node.dataset.section);
          return;
        }
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
          // Never let a render error propagate — it would leave the
          // panel blank AND potentially confuse HA's main frontend.
          console.error("[complete-irrigation] render failed:", err);
          this.shadowRoot.innerHTML =
            '<div style="padding:24px;color:#db4437;font-family:sans-serif;">' +
            '<h3>Irrigation panel error</h3>' +
            '<p>The panel failed to render. Check browser console for details.</p>' +
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

    // ── Data helpers ───────────────────────────────────────────────
    _zones() {
      const ids = (this._panel && this._panel.config && this._panel.config.zones) || [];
      return ids.map((entityId) => {
        const state =
          this._hass && this._hass.states ? this._hass.states[entityId] : null;
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
        '<span>' +
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
        "</div>";
    }

    _renderSection() {
      if (this._currentSection === "today") return this._renderToday();
      const section =
        SECTIONS.find(function (s) {
          return s.id === this._currentSection;
        }, this) || { icon: "", label: "Section" };
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
        '<span class="version-pill">v0.1 — read-only preview</span>' +
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
        "</section>" +
        "<section>" +
        '<p class="footnote">' +
        "Weather header, calendar, manual run, schedules, and live sensor " +
        "breakdowns land in subsequent vertical slices. This v0.1 view " +
        "confirms the integration is installed and your zones are wired up." +
        "</p>" +
        "</section>"
      );
    }

    _renderEmpty() {
      return (
        '<div class="empty"><p>No zones configured. Re-run setup from Settings → Devices & services.</p></div>'
      );
    }

    _renderZoneTile(zone) {
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
        "</article>"
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
        ".entity-id{font-size:11px;color:var(--secondary-text-color,#727272);font-family:var(--ha-font-family-code,monospace)}" +
        ".status-text{font-size:12px;color:var(--secondary-text-color,#727272)}" +
        ".empty{background:var(--card-background-color,#fff);border:1px dashed var(--divider-color,rgba(0,0,0,0.2));border-radius:12px;padding:24px;text-align:center;color:var(--secondary-text-color,#727272)}" +
        ".placeholder{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,rgba(0,0,0,0.12));border-radius:12px;padding:24px}" +
        ".placeholder h2{margin-top:0}" +
        ".footnote{color:var(--secondary-text-color,#727272);font-size:12px;margin-top:24px;line-height:1.6}" +
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

  customElements.define(ELEMENT_NAME, CompleteIrrigationPanel);
  console.info("[complete-irrigation] panel registered, version v0.1");
})();
