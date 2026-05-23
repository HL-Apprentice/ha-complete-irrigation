/**
 * v1.13.2 — multi-zone schedules show up on EVERY bound zone's 7-day strip.
 *
 * Setup: create a daily multi-zone schedule [lawn, garden].
 * Expected: both switch.test_lawn AND switch.test_garden rows on the
 * Zones tab show fire-dots every day this week.
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

  // Create a daily multi-zone schedule: lawn (primary, 10m) + garden (5m)
  await callWS(token, {
    type: "call_service",
    domain: "complete_irrigation",
    service: "add_schedule",
    service_data: {
      name: "TEST v1132 multizone",
      zone_entity_id: "switch.test_lawn",
      start_time: "06:00",
      duration_minutes: 10,
      weekdays: [0, 1, 2, 3, 4, 5, 6],
      zone_steps: [
        { zone_entity_id: "switch.test_lawn", duration_minutes: 10 },
        { zone_entity_id: "switch.test_garden", duration_minutes: 5 },
      ],
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
    // Navigate to Zones tab
    let frame = await getFrame(page);
    await frame.evaluate(() =>
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('button.sidebar-item[data-section="zones"]')
        .click()
    );
    await new Promise((r) => setTimeout(r, 500));
    frame = await getFrame(page);

    console.log("\n→ Multi-zone schedule appears on BOTH zone rows");
    // For each zone row, count cells that have at least one fire-dot
    const result = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const rows = r.querySelectorAll(".zone-row");
      const out = {};
      for (const row of rows) {
        // The zone's switch entity id is on the hide-zone or show-zone btn
        const btn = row.querySelector('[data-action^="hide-zone"], [data-action^="show-zone"]');
        if (!btn) continue;
        const eid = btn.getAttribute("data-entity-id");
        const dotCells = row.querySelectorAll(".zone-day-on").length;
        const totalCells = row.querySelectorAll(".zone-day").length;
        // Pull the tooltip text from the first day-on cell to confirm the
        // multi-zone schedule name is referenced
        const firstOn = row.querySelector(".zone-day-on");
        const tooltip = firstOn?.getAttribute("title") || "";
        out[eid] = { dotCells, totalCells, tooltip };
      }
      return out;
    });
    console.log("  Zone strip data:", JSON.stringify(result, null, 2));

    const lawn = result["switch.test_lawn"];
    const garden = result["switch.test_garden"];
    if (!lawn) fail("test_lawn row not found");
    else if (lawn.dotCells < 7)
      fail(`test_lawn should have 7 day-on cells (daily); got ${lawn.dotCells}`);
    else pass(`test_lawn: ${lawn.dotCells}/${lawn.totalCells} days with fires`);

    if (!garden) fail("test_garden row not found");
    else if (garden.dotCells < 7)
      fail(`test_garden should ALSO have 7 day-on cells (it's a zone_step); got ${garden.dotCells}`);
    else pass(`test_garden: ${garden.dotCells}/${garden.totalCells} days with fires`);

    if (lawn && garden && !garden.tooltip.includes("TEST v1132"))
      fail(`test_garden tooltip should mention schedule name; got: "${garden.tooltip}"`);
    else if (garden && garden.tooltip.includes("TEST v1132"))
      pass(`test_garden tooltip references multi-zone schedule`);
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
  console.log(fails === 0 ? "\n✓ ALL v1.13.2 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
