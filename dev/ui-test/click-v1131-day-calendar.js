/**
 * v1.13.1 — vertical day calendar with prev/next navigation.
 *
 *  1. Today screen has .day-cal section (no more horizontal timeline).
 *  2. Label says "Today — …" by default, shows run count.
 *  3. Click → advances to "Tomorrow"; click again → date label.
 *  4. "Today" button appears once you've shifted off zero, resets to 0.
 *  5. Click a row → opens schedule edit modal.
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
  // Seed an everyday schedule so the calendar has rows
  await callWS(token, {
    type: "call_service",
    domain: "complete_irrigation",
    service: "add_schedule",
    service_data: {
      name: "TEST v1131 daily",
      zone_entity_id: "switch.test_lawn",
      start_time: "06:00",
      duration_minutes: 10,
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

    // ── 1. Old structures gone, day-cal present ──
    console.log("\n→ day-cal renders, old structures gone");
    const surf = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        dayCal: !!r.querySelector(".day-cal"),
        oldTimeline: !!r.querySelector(".today-timeline"),
        oldTomorrow: !!r.querySelector(".tomorrow-list"),
        label: r.querySelector(".day-cal-label")?.textContent.trim() || "",
        rows: r.querySelectorAll(".day-cal-row").length,
        todayBtn: !!r.querySelector('[data-action="day-cal-today"]'),
      };
    });
    if (!surf.dayCal) fail(".day-cal section missing");
    else pass(".day-cal section present");
    if (surf.oldTimeline) fail("old .today-timeline still rendered");
    else pass("old horizontal timeline removed");
    if (surf.oldTomorrow) fail("old .tomorrow-list still rendered");
    else pass("old tomorrow-list removed");
    if (!surf.label.startsWith("Today")) fail(`label should start with "Today", got "${surf.label}"`);
    else pass(`label: "${surf.label}"`);
    if (surf.todayBtn) fail("Today button shouldn't show at offset 0");
    else pass("Today button hidden at offset 0");
    console.log(`  ${surf.rows} rows for today`);

    // ── 2. Click → advance to tomorrow ──
    console.log("\n→ Click → label becomes 'Tomorrow', Today button appears");
    await frame.evaluate(() => {
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('[data-action="day-cal-next"]')
        .click();
    });
    await new Promise((r) => setTimeout(r, 300));
    frame = await getFrame(page);
    const tomorrow = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        label: r.querySelector(".day-cal-label")?.textContent.trim() || "",
        todayBtn: !!r.querySelector('[data-action="day-cal-today"]'),
        rows: r.querySelectorAll(".day-cal-row").length,
      };
    });
    if (!tomorrow.label.startsWith("Tomorrow"))
      fail(`label should start with "Tomorrow", got "${tomorrow.label}"`);
    else pass(`label advanced to "${tomorrow.label}"`);
    if (!tomorrow.todayBtn) fail("Today button missing after shift");
    else pass("Today button appears after shift");

    // ── 3. Click → again → just a date label ──
    console.log("\n→ Click → again → plain date label");
    await frame.evaluate(() => {
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('[data-action="day-cal-next"]')
        .click();
    });
    await new Promise((r) => setTimeout(r, 300));
    frame = await getFrame(page);
    const day2 = await frame.evaluate(
      () =>
        document
          .querySelector("complete-irrigation-panel")
          .shadowRoot.querySelector(".day-cal-label")
          ?.textContent.trim() || ""
    );
    if (day2.startsWith("Today") || day2.startsWith("Tomorrow"))
      fail(`day+2 label should be plain date, got "${day2}"`);
    else pass(`day+2 label: "${day2}"`);

    // ── 4. "Today" button resets ──
    console.log("\n→ Today button resets to offset 0");
    await frame.evaluate(() => {
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('[data-action="day-cal-today"]')
        .click();
    });
    await new Promise((r) => setTimeout(r, 300));
    frame = await getFrame(page);
    const reset = await frame.evaluate(
      () =>
        document
          .querySelector("complete-irrigation-panel")
          .shadowRoot.querySelector(".day-cal-label")
          ?.textContent.trim() || ""
    );
    if (!reset.startsWith("Today")) fail(`Today button didn't reset; label="${reset}"`);
    else pass(`Today button reset to "${reset}"`);

    // ── 5. Click a row → opens schedule edit ──
    console.log("\n→ Click row opens schedule edit");
    const clicked = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const rows = r.querySelectorAll(".day-cal-row");
      for (const row of rows) {
        if ((row.getAttribute("title") || "").includes("TEST v1131 daily")) {
          row.click();
          return true;
        }
      }
      return false;
    });
    if (!clicked) fail("couldn't find TEST v1131 row");
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
      if (!open.form) fail("clicking row didn't open modal");
      else if (!open.name.startsWith("TEST v1131"))
        fail(`wrong schedule loaded: "${open.name}"`);
      else pass(`schedule edit modal opened with "${open.name}"`);
    }
  } catch (err) {
    console.error("test failed:", err.message);
    fails++;
  } finally {
    await cleanup(token).catch(() => {});
    if (msgs.length > 0) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of msgs.slice(0, 4)) console.log(`  ${m}`);
    }
    await browser.close();
  }
  console.log(fails === 0 ? "\n✓ ALL v1.13.1 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
