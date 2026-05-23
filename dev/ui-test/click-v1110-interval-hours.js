/**
 * v1.11.0 — "every N hours" scheduling mode.
 *
 *  1. Add Schedule modal has 3 recurrence radios (weekdays / every N days
 *     / every N hours).
 *  2. Picking "every N hours" hides weekdays + shows hours + first-run-date.
 *  3. Saving an interval_hours schedule round-trips through WS — server
 *     returns mode=interval_hours, interval_hours=N, interval_anchor=date.
 *  4. Schedule list row shows "every N hours" recurrence label.
 */
const puppeteer = require("puppeteer");
const fs = require("fs");
const WebSocket = require("ws");

const HA_URL = process.env.HA_URL || "http://localhost:8123";

async function listSchedules(token) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket("ws://localhost:8123/api/websocket");
    let mid = 1;
    ws.on("message", (raw) => {
      const m = JSON.parse(raw.toString());
      if (m.type === "auth_required") {
        ws.send(JSON.stringify({ type: "auth", access_token: token }));
      } else if (m.type === "auth_ok") {
        ws.send(JSON.stringify({ id: mid++, type: "complete_irrigation/list_schedules" }));
      } else if (m.type === "result") {
        ws.close();
        if (m.success) resolve(m.result.schedules);
        else reject(new Error(JSON.stringify(m)));
      }
    });
  });
}

async function deleteSchedule(token, id) {
  return new Promise((resolve) => {
    const ws = new WebSocket("ws://localhost:8123/api/websocket");
    let mid = 1;
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
            service: "delete_schedule",
            service_data: { schedule_id: id },
          })
        );
      } else if (m.type === "result") {
        ws.close();
        resolve();
      }
    });
  });
}

async function cleanup(token) {
  try {
    const scheds = await listSchedules(token);
    for (const s of scheds) {
      if (s.name && s.name.startsWith("TEST ")) await deleteSchedule(token, s.id);
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

async function nav(page, section) {
  const frame = await getFrame(page);
  await frame.evaluate((sec) => {
    document
      .querySelector("complete-irrigation-panel")
      .shadowRoot.querySelector(`button.sidebar-item[data-section="${sec}"]`)
      .click();
  }, section);
  await new Promise((r) => setTimeout(r, 400));
}

async function main() {
  const token = fs.readFileSync("/tmp/ha_test_token.txt", "utf8").trim();
  await cleanup(token);

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
    await nav(page, "schedules");
    let frame = await getFrame(page);

    // ── 1. Open Add Schedule, verify 3 radios ──
    console.log("\n→ Add Schedule has 3 recurrence radios");
    await frame.evaluate(() =>
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('[data-action="add-schedule"]')
        .click()
    );
    await new Promise((r) => setTimeout(r, 400));
    frame = await getFrame(page);
    const radios = await frame.evaluate(() =>
      Array.from(
        document
          .querySelector("complete-irrigation-panel")
          .shadowRoot.querySelectorAll('input[name="mode"]')
      ).map((el) => el.value)
    );
    console.log("  radios:", JSON.stringify(radios));
    if (!radios.includes("interval_hours")) fail("'interval_hours' radio missing");
    else pass("'interval_hours' radio present");
    if (radios.length !== 3) fail(`expected 3 radios, got ${radios.length}`);
    else pass("3 radios total");

    // ── 2. Pick interval_hours → fields swap ──
    console.log("\n→ Pick 'Every N hours' → hours + anchor fields show");
    await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const radio = r.querySelector('input[name="mode"][value="interval_hours"]');
      radio.checked = true;
      radio.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await new Promise((r) => setTimeout(r, 400));
    frame = await getFrame(page);
    const fields = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        hoursInput: !!r.querySelector('input[name="interval_hours"]'),
        anchorInput: !!r.querySelector('input[name="interval_anchor"]'),
        daysInput: !!r.querySelector('input[name="interval_days"]'),
        weekdayChecks: r.querySelectorAll('input[name="weekday"]').length,
      };
    });
    if (!fields.hoursInput) fail("interval_hours input missing");
    else pass("interval_hours input present");
    if (!fields.anchorInput) fail("interval_anchor input missing");
    else pass("interval_anchor input present");
    if (fields.daysInput || fields.weekdayChecks > 0)
      fail("weekday/days fields shouldn't show in hours mode");
    else pass("weekday/days fields correctly hidden");

    // ── 3. Fill + Save → WS round-trip ──
    console.log("\n→ Save 'every 4 hours' schedule");
    await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      r.querySelector('input[name="name"]').value = "TEST Every 4h";
      r.querySelector('input[name="name"]').dispatchEvent(new Event("input", { bubbles: true }));
      const hrs = r.querySelector('input[name="interval_hours"]');
      hrs.value = "4";
      hrs.dispatchEvent(new Event("input", { bubbles: true }));
      r.querySelector(".schedule-form").requestSubmit();
    });
    await new Promise((r) => setTimeout(r, 1500));

    const scheds = await listSchedules(token);
    const sched = scheds.find((s) => s.name === "TEST Every 4h");
    if (!sched) fail("schedule didn't persist via service call");
    else {
      console.log("  persisted:", JSON.stringify({
        mode: sched.mode,
        interval_hours: sched.interval_hours,
        interval_anchor: sched.interval_anchor,
      }));
      if (sched.mode !== "interval_hours")
        fail(`expected mode=interval_hours, got ${sched.mode}`);
      else pass("mode=interval_hours persisted");
      if (sched.interval_hours !== 4)
        fail(`expected interval_hours=4, got ${sched.interval_hours}`);
      else pass("interval_hours=4 persisted");
      if (!sched.interval_anchor)
        fail("interval_anchor not persisted");
      else pass(`interval_anchor=${sched.interval_anchor} persisted`);
    }

    // ── 4. Schedule list shows 'every N hours' label ──
    console.log("\n→ Schedule row shows 'every 4 hours' label");
    frame = await getFrame(page);
    const rowText = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const rows = r.querySelectorAll(".schedule-row");
      for (const row of rows) {
        if (row.textContent.includes("TEST Every 4h")) {
          return row.querySelector(".schedule-meta")?.textContent || "";
        }
      }
      return null;
    });
    if (!rowText) fail("schedule row not found");
    else {
      console.log("  meta:", rowText);
      if (!/every 4 hours/.test(rowText)) fail(`label wrong: "${rowText}"`);
      else pass("schedule meta shows 'every 4 hours'");
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
  console.log(fails === 0 ? "\n✓ ALL v1.11.0 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
