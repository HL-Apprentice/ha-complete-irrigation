/**
 * v1.13.3 — Today's day calendar is a 24-hour time grid.
 *
 *  1. .day-cal-grid replaces .day-cal-rows; 24 .day-cal-hour markers.
 *  2. Each hour marker has a solid top border (computed style).
 *  3. ::after pseudo at the half-hour has 50% opacity (computed style).
 *  4. Scheduled run is rendered as a .day-cal-pill with top = start-minutes.
 *  5. Click a pill → opens schedule edit modal.
 *  6. Prev/next/today navigation still works (regression of v1.13.1).
 */
const puppeteer = require("puppeteer");
const fs = require("fs");
const WebSocket = require("ws");

const HA_URL = process.env.HA_URL || "http://localhost:8123";

async function callWS(token, payload) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket("ws://localhost:8123/api/websocket");
    let mid = 1;
    ws.on("message", (raw) => {
      const m = JSON.parse(raw.toString());
      if (m.type === "auth_required")
        ws.send(JSON.stringify({ type: "auth", access_token: token }));
      else if (m.type === "auth_ok")
        ws.send(JSON.stringify({ id: mid++, ...payload }));
      else if (m.type === "result") {
        ws.close();
        if (m.success) resolve(m.result);
        else reject(new Error(JSON.stringify(m)));
      }
    });
    ws.on("error", reject);
  });
}

async function cleanup(token) {
  try {
    const r = await callWS(token, { type: "complete_irrigation/list_schedules" });
    for (const s of r.schedules) {
      if ((s.name || "").startsWith("TEST ") || (s.name || "").startsWith("New grass"))
        await callWS(token, {
          type: "call_service",
          domain: "complete_irrigation",
          service: "delete_schedule",
          service_data: { schedule_id: s.id },
        });
    }
  } catch (_) {}
}

async function loadPanel(page) {
  const token = fs.readFileSync("/tmp/ha_test_token.txt", "utf8").trim();
  await page.goto(HA_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.evaluate((t) => {
    localStorage.setItem(
      "hassTokens",
      JSON.stringify({
        access_token: t,
        token_type: "Bearer",
        refresh_token: "",
        expires_in: 1800,
        hassUrl: window.location.origin,
        clientId: window.location.origin + "/",
        expires: Date.now() + 1800 * 1000,
      })
    );
  }, token);
  await page.goto(`${HA_URL}/complete-irrigation`, {
    waitUntil: "networkidle2",
    timeout: 30000,
  });
  await new Promise((r) => setTimeout(r, 4000));
}

async function getFrame(page) {
  const iframeEl = await page.evaluateHandle(() => {
    const ha = document.querySelector("home-assistant");
    const main = ha?.shadowRoot?.querySelector("home-assistant-main");
    const drawer = main?.shadowRoot?.querySelector("ha-drawer");
    const resolver = drawer?.querySelector("partial-panel-resolver");
    return resolver?.querySelector("ha-panel-custom")?.querySelector("iframe");
  });
  const frame = await iframeEl.contentFrame();
  await frame.waitForFunction(
    () => !!document.querySelector("complete-irrigation-panel"),
    { timeout: 15000 }
  );
  return frame;
}

async function main() {
  const token = fs.readFileSync("/tmp/ha_test_token.txt", "utf8").trim();
  await cleanup(token);
  // Seed an everyday schedule so the grid has at least one pill
  await callWS(token, {
    type: "call_service",
    domain: "complete_irrigation",
    service: "add_schedule",
    service_data: {
      name: "TEST v1133 grid",
      zone_entity_id: "switch.test_lawn",
      start_time: "06:30",
      duration_minutes: 15,
      weekdays: [0, 1, 2, 3, 4, 5, 6],
    },
  });

  const browser = await puppeteer.launch({
    headless: "new",
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1400, height: 950 });
  const msgs = [];
  page.on("console", (m) => msgs.push(`[${m.type()}] ${m.text()}`));
  page.on("pageerror", (e) => msgs.push(`[pageerror] ${e.message}`));

  let fails = 0;
  const fail = (m) => { console.log(`  ✗ ${m}`); fails++; };
  const pass = (m) => console.log(`  ✓ ${m}`);

  try {
    await loadPanel(page);
    let frame = await getFrame(page);

    // ── 1. New grid structure ──
    console.log("\n→ Time grid renders with 24 hour markers");
    const surf = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        grid: !!r.querySelector(".day-cal-grid"),
        oldRows: !!r.querySelector(".day-cal-rows"),
        oldRow: !!r.querySelector(".day-cal-row"),
        hours: r.querySelectorAll(".day-cal-hour").length,
        pills: r.querySelectorAll(".day-cal-pill").length,
        label: r.querySelector(".day-cal-label")?.textContent.trim() || "",
      };
    });
    if (!surf.grid) fail(".day-cal-grid missing");
    else pass(".day-cal-grid present");
    if (surf.oldRows) fail("old .day-cal-rows still rendered");
    else pass("old .day-cal-rows removed");
    if (surf.oldRow) fail("old .day-cal-row still rendered");
    else pass("old .day-cal-row removed");
    if (surf.hours !== 24) fail(`expected 24 hour markers, got ${surf.hours}`);
    else pass(`24 hour markers rendered`);
    if (surf.pills < 1) fail(`expected ≥1 pill, got ${surf.pills}`);
    else pass(`${surf.pills} pill(s) rendered`);
    console.log(`  label: "${surf.label}"`);

    // ── 2. Hour line is solid; half-hour line is 50% opacity ──
    console.log("\n→ Hour line solid, half-hour line at 50% opacity");
    const lines = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const hour = r.querySelector(".day-cal-hour");
      if (!hour) return null;
      const hourCs = getComputedStyle(hour);
      const halfCs = getComputedStyle(hour, "::after");
      return {
        hourBorderTop: hourCs.borderTopStyle + " " + hourCs.borderTopWidth,
        hourOpacity: hourCs.opacity,
        halfContent: halfCs.content,
        halfBorderTop: halfCs.borderTopStyle + " " + halfCs.borderTopWidth,
        halfOpacity: halfCs.opacity,
      };
    });
    if (!lines) fail("no .day-cal-hour to inspect");
    else {
      console.log("  ", JSON.stringify(lines));
      if (!lines.hourBorderTop.startsWith("solid")) fail(`hour line not solid: ${lines.hourBorderTop}`);
      else pass(`hour line solid (${lines.hourBorderTop})`);
      if (parseFloat(lines.hourOpacity) < 0.99) fail(`hour line opacity should be 1.0, got ${lines.hourOpacity}`);
      else pass(`hour line full opacity`);
      // Half-hour ::after: content must be set (not "none"); opacity ~0.5
      if (!lines.halfContent || lines.halfContent === "none")
        fail(`::after pseudo missing content (got "${lines.halfContent}")`);
      else pass(`::after pseudo present`);
      const halfOp = parseFloat(lines.halfOpacity);
      if (!(halfOp > 0.4 && halfOp < 0.6))
        fail(`half-hour opacity should be ~0.5, got ${lines.halfOpacity}`);
      else pass(`half-hour opacity = ${lines.halfOpacity} (50%)`);
    }

    // ── 3. Pill positioned at 06:30 ──
    console.log("\n→ Pill positioned at correct minute offset");
    const pillPos = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const p = r.querySelector(".day-cal-pill");
      if (!p) return null;
      return { top: p.style.top, height: p.style.height, title: p.getAttribute("title") };
    });
    if (!pillPos) fail("no pill to check position");
    else {
      console.log("  ", JSON.stringify(pillPos));
      // 06:30 = 390 minutes → 390px top
      if (pillPos.top !== "390px")
        fail(`pill top should be 390px (06:30), got ${pillPos.top}`);
      else pass(`pill top = 390px (06:30)`);
      // 15 min duration → 15px height (max(18,15) = 18)
      if (pillPos.height !== "18px" && pillPos.height !== "15px")
        fail(`pill height should be ~15–18px, got ${pillPos.height}`);
      else pass(`pill height = ${pillPos.height}`);
    }

    // ── 4. Click pill → opens edit modal ──
    console.log("\n→ Click pill → opens schedule edit modal");
    const clicked = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const p = r.querySelector('.day-cal-pill[data-action="open-schedule-edit"]');
      if (!p) return false;
      p.click();
      return true;
    });
    if (!clicked) fail("no clickable pill");
    else {
      await new Promise((r) => setTimeout(r, 600));
      frame = await getFrame(page);
      const open = await frame.evaluate(() => {
        const r = document.querySelector("complete-irrigation-panel").shadowRoot;
        return {
          form: !!r.querySelector(".schedule-form"),
          name: r.querySelector('input[name="name"]')?.value || "",
        };
      });
      if (!open.form) fail("clicking pill didn't open modal");
      else if (!open.name.startsWith("TEST v1133"))
        fail(`wrong schedule loaded: "${open.name}"`);
      else pass(`schedule edit modal opened with "${open.name}"`);
    }
    // (prev/next/today nav regression already covered by click-v1131-day-calendar.js)
  } catch (err) {
    console.error("test failed:", err.message);
    fails++;
  } finally {
    await cleanup(token).catch(() => {});
    if (msgs.length > 0) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of msgs.slice(0, 6)) console.log(`  ${m}`);
    }
    await browser.close();
  }
  console.log(fails === 0 ? "\n✓ ALL v1.13.3 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
