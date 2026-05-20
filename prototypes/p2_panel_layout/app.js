// P2 — Panel Layout Prototype
// Three variants on one page, switchable via ?variant=1|2|3 and the bottom bar.

const MOCK = {
  weather: {
    condition: "Partly cloudy",
    temp: 78,
    feels_like: 81,
    humidity: 45,
    heat_index: 82,
    dew_point: 58,
    rain_today: 0.0,
    rain_yesterday: 0.2,
    rain_rate: 0.0,
    wind_speed: 8,
    wind_gust: 12,
    wind_direction: "WSW",
    pressure: 30.04,
    uv_index: 7,
    solar: 740,
    sunrise: "5:54 AM",
    sunset: "8:21 PM",
    forecast_high: 84,
    forecast_low: 62,
  },
  rain_lockout: null, // try setting to { until: "14:30", reason: "0.6 in past 6h — 12h lockout" }
  zones: [
    {
      id: "north",
      name: "North Lawn",
      status: "idle",
      category: "Lawn",
      moisture: {
        current: 32,
        min: 21,
        target: 31,
        max: 40,
        sensors: [
          { pos: "N", val: 30 },
          { pos: "S", val: 34 },
        ],
      },
      soil_temp: 68,
      last_watered: "2 hours ago",
      next_run: "Tomorrow 6:00 AM",
    },
    {
      id: "south",
      name: "South Lawn",
      status: "running",
      category: "Lawn",
      moisture: {
        current: 24,
        min: 21,
        target: 31,
        max: 40,
        sensors: [{ pos: "S", val: 24 }],
      },
      soil_temp: 70,
      last_watered: "Running now — 7:32 remaining",
      next_run: "Tomorrow 6:30 AM",
    },
    {
      id: "east",
      name: "East Vegetable",
      status: "skipped",
      category: "Vegetable garden",
      moisture: {
        current: 68,
        min: 41,
        target: 61,
        max: 80,
        sensors: [{ pos: "E", val: 68 }],
      },
      soil_temp: 65,
      last_watered: "Yesterday 7:00 PM",
      next_run: "Skipped today (above max — already saturated)",
    },
  ],
  upcoming_runs: [
    { zone: "North Lawn", day: "Today", time: "6:00 PM", duration: 18, skipped: false },
    { zone: "South Lawn", day: "Today", time: "6:30 PM", duration: 22, skipped: false },
    { zone: "East Vegetable", day: "Today", time: "7:00 PM", duration: 25, skipped: true },
    { zone: "North Lawn", day: "Tomorrow", time: "6:00 AM", duration: 20, skipped: false },
    { zone: "South Lawn", day: "Tomorrow", time: "6:32 AM", duration: 22, skipped: false },
  ],
};

// ─── HTML builders (shared across variants) ───────────────────────────
function html(strings, ...values) {
  return strings.reduce((acc, s, i) => acc + s + (values[i] ?? ""), "");
}

function rainLockoutBanner() {
  if (!MOCK.rain_lockout) return "";
  return html`
    <div class="rain-lockout-banner">
      <div>🌧️ Rain lockout active until ${MOCK.rain_lockout.until} — all zones paused.
        <div style="opacity: 0.85; font-size: 12px;">${MOCK.rain_lockout.reason}</div>
      </div>
      <button>Resume now</button>
    </div>
  `;
}

function weatherCard() {
  const w = MOCK.weather;
  return html`
    <div class="card weather-card">
      <div>
        <div class="weather-temp">${w.temp}<span class="weather-temp-unit">°F</span></div>
        <div class="weather-condition">${w.condition} · Feels like ${w.feels_like}°</div>
        <div class="weather-condition" style="font-size: 12px;">↑${w.forecast_high}° ↓${w.forecast_low}°</div>
      </div>
      <div class="weather-grid">
        ${cell("Humidity", w.humidity + "%")}
        ${cell("Heat index", w.heat_index + "°")}
        ${cell("Dew point", w.dew_point + "°")}
        ${cell("Rain today", w.rain_today.toFixed(2) + " in")}
        ${cell("Rain yest.", w.rain_yesterday.toFixed(2) + " in")}
        ${cell("Wind", w.wind_speed + " mph " + w.wind_direction)}
        ${cell("Gust", w.wind_gust + " mph")}
        ${cell("UV index", w.uv_index)}
        ${cell("Solar", w.solar + " W/m²")}
        ${cell("Pressure", w.pressure + " inHg")}
        ${cell("Sunrise", w.sunrise)}
        ${cell("Sunset", w.sunset)}
      </div>
    </div>
  `;
}
function cell(label, value) {
  return html`<div><div class="label">${label}</div><div class="value">${value}</div></div>`;
}

function calendarStrip(title, period = "today") {
  let runs = MOCK.upcoming_runs;
  if (period === "today") runs = runs.filter((r) => r.day === "Today");
  return html`
    <div class="card">
      <div class="section-header" style="margin-bottom: var(--space-3);">
        <h2>${title}</h2>
        <div class="calendar-toggle">
          <button class="${period === "today" ? "active" : ""}" data-period="today">Today</button>
          <button class="${period === "week" ? "active" : ""}" data-period="week">This week</button>
        </div>
      </div>
      <div class="calendar-strip">
        ${runs
          .map(
            (r) => html`
              <div class="run-chip ${r.skipped ? "skipped" : ""}">
                <div class="time">${r.day} ${r.time}</div>
                <div class="zone">${r.zone} · ${r.duration} min</div>
              </div>
            `
          )
          .join("")}
      </div>
    </div>
  `;
}

function moistureBar(m) {
  const pctPos = (v) => `${Math.min(100, Math.max(0, ((v - 0) / (m.max + 20)) * 100))}%`;
  return html`
    <div class="moisture-bar">
      <div class="moisture-bar-fill" style="width: ${pctPos(m.current)}"></div>
      <div class="moisture-bar-markers">
        <div class="moisture-target-line" style="left: ${pctPos(m.target)}; background: var(--accent-color);"></div>
      </div>
      <div class="moisture-bar-text">${m.current}% / target ${m.target}%</div>
    </div>
  `;
}

function zoneCard(z) {
  const statusLabel = {
    idle: "Idle",
    running: "Running",
    queued: "Queued",
    skipped: "Skipped today",
  }[z.status];
  return html`
    <div class="card zone-card">
      <div class="zone-header">
        <div>
          <div class="zone-name">${z.name}</div>
          <div class="zone-category">${z.category}</div>
        </div>
        <div>
          <span class="status-dot ${z.status}"></span>
          <span class="zone-status-text">${statusLabel}</span>
        </div>
      </div>

      ${moistureBar(z.moisture)}

      <div class="moisture-sensors">
        ${z.moisture.sensors.map((s) => html`<span>${s.pos}: ${s.val}%</span>`).join("")}
        <span>min ${z.moisture.min}% · max ${z.moisture.max}%</span>
      </div>

      <div class="zone-meta">
        <div>
          <div class="label">Soil temp</div>
          <div class="value">${z.soil_temp}°F</div>
        </div>
        <div>
          <div class="label">Last watered</div>
          <div class="value">${z.last_watered}</div>
        </div>
        <div style="grid-column: span 2;">
          <div class="label">Next run</div>
          <div class="value">${z.next_run}</div>
        </div>
      </div>

      <div class="zone-actions">
        <button class="primary">Run now</button>
        <button>Edit schedule</button>
        <button title="Open sensor in HA">⚙</button>
      </div>
    </div>
  `;
}

function zoneGrid() {
  return html`
    <div class="card">
      <div class="section-header"><h2>Zones</h2></div>
      <div class="zone-grid">
        ${MOCK.zones.map(zoneCard).join("")}
      </div>
    </div>
  `;
}

// ─── Variant 1: Single full-width column ──────────────────────────────
function variant1() {
  return html`
    <div class="layout-1">
      ${rainLockoutBanner()}
      ${weatherCard()}
      ${calendarStrip("Today's runs")}
      ${zoneGrid()}
    </div>
  `;
}

// ─── Variant 2: Sidebar nav + main ────────────────────────────────────
function variant2() {
  return html`
    <div class="layout-2">
      <nav class="sidebar">
        <div class="sidebar-brand">💧 Complete Irrigation</div>
        <div class="sidebar-item active"><span class="sidebar-item-icon">📅</span>Today</div>
        <div class="sidebar-item"><span class="sidebar-item-icon">⏰</span>Schedules</div>
        <div class="sidebar-item"><span class="sidebar-item-icon">🌱</span>Zones</div>
        <div class="sidebar-item"><span class="sidebar-item-icon">📊</span>Sensors</div>
        <div class="sidebar-item"><span class="sidebar-item-icon">🌧️</span>Weather</div>
        <div class="sidebar-item"><span class="sidebar-item-icon">🔔</span>Notifications</div>
        <div class="sidebar-item"><span class="sidebar-item-icon">⚙️</span>Settings</div>
      </nav>
      <div class="main">
        ${rainLockoutBanner()}
        ${weatherCard()}
        ${calendarStrip("Today's runs")}
        ${zoneGrid()}
      </div>
    </div>
  `;
}

// ─── Variant 3: Tabbed top ────────────────────────────────────────────
function variant3() {
  return html`
    <div class="layout-3">
      <div class="tabs">
        <div class="tab active">📅 Today</div>
        <div class="tab">⏰ Schedules</div>
        <div class="tab">🌱 Zones</div>
        <div class="tab">📊 Sensors</div>
        <div class="tab">🌧️ Weather</div>
        <div class="tab">🔔 Notifications</div>
        <div class="tab" style="margin-left: auto;">⚙️ Settings</div>
      </div>
      <div class="main">
        ${rainLockoutBanner()}
        ${weatherCard()}
        ${calendarStrip("Today's runs")}
        ${zoneGrid()}
      </div>
    </div>
  `;
}

// ─── Router + switcher ────────────────────────────────────────────────
const VARIANTS = { "1": variant1, "2": variant2, "3": variant3 };

function getVariant() {
  const params = new URLSearchParams(location.search);
  return VARIANTS[params.get("variant")] ? params.get("variant") : "1";
}

function setVariant(n) {
  const url = new URL(location);
  url.searchParams.set("variant", n);
  history.replaceState(null, "", url);
  render();
}

function toggleTheme() {
  document.body.classList.toggle("dark");
  document.querySelector(".theme-toggle").textContent =
    document.body.classList.contains("dark") ? "☀️ light" : "🌙 dark";
}

function render() {
  const v = getVariant();
  document.getElementById("app").innerHTML = VARIANTS[v]();
  document.querySelectorAll(".variant-switcher .variant-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.v === v);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".variant-switcher .variant-btn").forEach((b) => {
    b.addEventListener("click", () => setVariant(b.dataset.v));
  });
  document.querySelector(".theme-toggle").addEventListener("click", toggleTheme);
  render();
});
