/**
 * v1.9.2 smoke — wind decimals + today's timeline.
 *
 *  1. Wind cell text matches "X.Y mph" (one decimal place).
 *  2. Today's timeline renders below Zones, has axis ticks + at least
 *     one pill if a schedule fires today.
 */
const puppeteer = require("puppeteer");
const fs = require("fs");
const WebSocket = require("ws");

const HA_URL = process.env.HA_URL || "http://localhost:8123";

async function addTodayScheduleViaWS(token, zone) {
  // Create a schedule that fires every day so the timeline has data.
  return new Promise((resolve, reject) => {
    const ws = new WebSocket("ws://localhost:8123/api/websocket");
    let mid = 1;
    let scheduleId = null;
    ws.on("message", (raw) => {
      const m = JSON.parse(raw.toString());
      if (m.type === "auth_required") {
        ws.send(JSON.stringify({ type: "auth", access_token: token }));
      } else if (m.type === "auth_ok") {
        ws.send(
          JSON.stringify({
            id: mid++,
            type: "call_service",
            domain: "complete_irrigation",
            service: "add_schedule",
            service_data: {
              name: "TEST Timeline",
              zone_entity_id: zone,
              start_time: "06:00",
              duration_minutes: 15,
              weekdays: [0, 1, 2, 3, 4, 5, 6],
            },
          })
        );
      } else if (m.type === "result") {
        ws.close();
        if (m.success) resolve();
        else reject(new Error(JSON.stringify(m)));
      }
    });
    ws.on("error", reject);
  });
}

async function cleanupTestSchedule(token) {
  return new Promise((resolve) => {
    const ws = new WebSocket("ws://localhost:8123/api/websocket");
    let mid = 1;
    ws.on("message", (raw) => {
      const m = JSON.parse(raw.toString());
      if (m.type === "auth_required") {
        ws.send(JSON.stringify({ type: "auth", access_token: token }));
      } else if (m.type === "auth_ok") {
        ws.send(JSON.stringify({ id: mid++, type: "complete_irrigation/list_schedules" }));
      } else if (m.type === "result" && m.result?.schedules) {
        const test = m.result.schedules.find((s) => s.name === "TEST Timeline");
        if (test) {
          ws.send(
            JSON.stringify({
              id: mid++,
              type: "call_service",
              domain: "complete_irrigation",
              service: "delete_schedule",
              service_data: { schedule_id: test.id },
            })
          );
        } else {
          ws.close();
          resolve();
        }
      } else if (m.type === "result") {
        ws.close();
        resolve();
      }
    });
  });
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
    await cleanupTestSchedule(token);
    await addTodayScheduleViaWS(token, "switch.test_lawn");
    await new Promise((r) => setTimeout(r, 1500));

    await loadPanel(page);
    const frame = await getFrame(page);

    // ── 1. Wind decimals — only meaningful if dev HA has a wind sensor ──
    console.log("\n→ Wind decimals");
    const wind = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const cells = r.querySelectorAll(".weather-cell");
      for (const c of cells) {
        const label = c.querySelector(".weather-cell-label")?.textContent.trim();
        if (label === "Wind") {
          return c.querySelector(".weather-cell-value")?.textContent.trim();
        }
      }
      return null;
    });
    if (wind == null) {
      console.log("  (no wind cell on banner — dev HA has no wind sensor, skipping)");
    } else {
      // Should match "<number>.<one digit> <unit>" optionally with "(gust <number>.<one digit>)"
      const oneDecimal = /^\d+\.\d\s+\S+/.test(wind);
      if (oneDecimal) pass(`wind cell shows 1 decimal: "${wind}"`);
      else fail(`wind cell doesn't show 1 decimal: "${wind}"`);
    }

    // ── 2. Today's timeline ──
    console.log("\n→ Today's timeline below Zones");
    const tl = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const section = r.querySelector(".today-timeline");
      if (!section) return { present: false };
      const ticks = section.querySelectorAll(".timeline-tick").length;
      const pills = section.querySelectorAll(".timeline-pill").length;
      const hasNow = !!section.querySelector(".timeline-now");
      const heading = section.querySelector(".section-title")?.textContent || "";
      // Confirm it's positioned AFTER the zones grid
      const zonesGrid = r.querySelector(".zone-grid");
      const afterZones = zonesGrid
        ? section.compareDocumentPosition(zonesGrid) & Node.DOCUMENT_POSITION_PRECEDING
        : false;
      return { present: true, ticks, pills, hasNow, heading, afterZones: !!afterZones };
    });
    if (!tl.present) fail("today-timeline section missing");
    else pass(`timeline section present (heading: "${tl.heading}")`);
    if (tl.ticks < 4) fail(`expected ≥4 hour ticks, got ${tl.ticks}`);
    else pass(`${tl.ticks} hour ticks on axis`);
    if (tl.pills < 1) fail(`expected ≥1 pill for today's schedule, got ${tl.pills}`);
    else pass(`${tl.pills} pill(s) for today's runs`);
    if (!tl.hasNow) fail("no 'now' marker on timeline");
    else pass("'now' marker present");
    if (!tl.afterZones) fail("timeline is not positioned after Zones grid");
    else pass("timeline positioned after Zones grid");
  } catch (err) {
    console.error("test failed:", err.message);
    fails++;
  } finally {
    await cleanupTestSchedule(token).catch(() => {});
    if (msgs.length > 0) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of msgs.slice(0, 6)) console.log(`  ${m}`);
    }
    await browser.close();
  }
  console.log(fails === 0 ? "\n✓ ALL v1.9.2 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
