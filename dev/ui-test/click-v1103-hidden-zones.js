/**
 * v1.10.3 — hidden zones excluded from scheduling dropdowns.
 *
 * Strategy:
 *  1. Add a schedule that uses switch.test_garden (via WS).
 *  2. Open the panel + hide switch.test_garden via the Zones tab.
 *  3. New Schedule modal → dropdown must NOT include switch.test_garden.
 *  4. Edit the existing schedule → dropdown MUST include it (so editing
 *     a pre-existing schedule on a now-hidden zone still works).
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
      if (m.type === "auth_required") {
        ws.send(JSON.stringify({ type: "auth", access_token: token }));
      } else if (m.type === "auth_ok") {
        ws.send(JSON.stringify({ id: mid++, ...payload }));
      } else if (m.type === "result") {
        ws.close();
        if (m.success) resolve(m.result);
        else reject(new Error(JSON.stringify(m)));
      }
    });
    ws.on("error", reject);
  });
}

async function addScheduleOnGarden(token) {
  await callWS(token, {
    type: "call_service",
    domain: "complete_irrigation",
    service: "add_schedule",
    service_data: {
      name: "TEST Garden Sched",
      zone_entity_id: "switch.test_garden",
      start_time: "06:00",
      duration_minutes: 10,
      weekdays: [0, 1, 2, 3, 4, 5, 6],
    },
  });
}

async function cleanupTestSchedule(token) {
  try {
    const result = await callWS(token, {
      type: "complete_irrigation/list_schedules",
    });
    for (const s of (result?.schedules) || []) {
      if (s.name === "TEST Garden Sched") {
        await callWS(token, {
          type: "call_service",
          domain: "complete_irrigation",
          service: "delete_schedule",
          service_data: { schedule_id: s.id },
        });
      }
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
    await addScheduleOnGarden(token);
    await new Promise((r) => setTimeout(r, 800));

    await loadPanel(page);

    // Hide switch.test_garden via the panel's hidden-zones localStorage so
    // we don't need the (removed) hide button on Today.
    let frame = await getFrame(page);
    await frame.evaluate(() => {
      localStorage.setItem(
        "complete_irrigation_hidden_zones",
        JSON.stringify(["switch.test_garden"])
      );
      const el = document.querySelector("complete-irrigation-panel");
      el._hiddenZones = new Set(["switch.test_garden"]);
      el._renderNow();
    });
    await new Promise((r) => setTimeout(r, 300));

    // ── 1. New schedule modal — dropdown excludes the hidden zone ──
    console.log("\n→ New schedule / hidden zone excluded");
    await nav(page, "schedules");
    frame = await getFrame(page);
    await frame.evaluate(() =>
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('[data-action="add-schedule"]')
        .click()
    );
    await new Promise((r) => setTimeout(r, 400));
    frame = await getFrame(page);
    const newModalOpts = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const sel = r.querySelector('select[name="zone_entity_id"]');
      return sel ? Array.from(sel.options).map((o) => o.value) : null;
    });
    if (!newModalOpts) fail("zone dropdown not found in new modal");
    else {
      console.log("  options:", JSON.stringify(newModalOpts));
      if (newModalOpts.includes("switch.test_garden"))
        fail("hidden zone switch.test_garden still in new-schedule dropdown");
      else pass("hidden zone excluded from new-schedule dropdown");
      if (!newModalOpts.includes("switch.test_lawn"))
        fail("visible zone switch.test_lawn missing from dropdown");
      else pass("visible zones still present");
    }
    // close
    await frame.evaluate(() =>
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector(".modal-cancel")
        .click()
    );
    await new Promise((r) => setTimeout(r, 300));

    // ── 2. Editing the existing schedule on the hidden zone keeps it ──
    console.log("\n→ Edit existing schedule on hidden zone keeps option");
    frame = await getFrame(page);
    // Find + click the edit button for TEST Garden Sched
    const opened = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const rows = r.querySelectorAll(".schedule-row");
      for (const row of rows) {
        if (row.textContent.includes("TEST Garden Sched")) {
          const btn = row.querySelector('[data-action="edit-schedule"]');
          if (btn) {
            btn.click();
            return true;
          }
        }
      }
      return false;
    });
    if (!opened) fail("couldn't open edit modal for TEST Garden Sched");
    else {
      await new Promise((r) => setTimeout(r, 400));
      frame = await getFrame(page);
      const editOpts = await frame.evaluate(() => {
        const r = document.querySelector("complete-irrigation-panel").shadowRoot;
        const sel = r.querySelector('select[name="zone_entity_id"]');
        return sel
          ? { values: Array.from(sel.options).map((o) => o.value), selected: sel.value }
          : null;
      });
      if (!editOpts) fail("zone dropdown not found in edit modal");
      else {
        console.log("  edit options:", JSON.stringify(editOpts));
        if (!editOpts.values.includes("switch.test_garden"))
          fail("editing schedule on a hidden zone DROPPED the zone from dropdown");
        else pass("hidden zone preserved in edit modal (because schedule uses it)");
        if (editOpts.selected !== "switch.test_garden")
          fail(`editor's selected zone wrong: ${editOpts.selected}`);
        else pass("editor selected the schedule's bound (hidden) zone correctly");
      }
      await frame.evaluate(() =>
        document
          .querySelector("complete-irrigation-panel")
          .shadowRoot.querySelector(".modal-cancel")
          .click()
      );
    }
  } catch (err) {
    console.error("test failed:", err.message);
    fails++;
  } finally {
    await cleanupTestSchedule(token).catch(() => {});
    if (msgs.length > 0) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of msgs.slice(0, 4)) console.log(`  ${m}`);
    }
    await browser.close();
  }
  console.log(fails === 0 ? "\n✓ ALL v1.10.3 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
