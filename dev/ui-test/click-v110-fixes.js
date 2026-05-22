/**
 * v1.10.0 smoke — Settings tab additions + sensor deep-link.
 *
 *  1. Settings → "Schedule timing" card with zone-buffer input.
 *  2. Settings → "Weekly reminder" card with Snooze button (and Resume
 *     when snoozed).
 *  3. Sensors tab → bound sensor friendly name is a deep link to
 *     /developer-tools/state with target="_top".
 */
const puppeteer = require("puppeteer");
const fs = require("fs");
const WebSocket = require("ws");

const HA_URL = process.env.HA_URL || "http://localhost:8123";

async function bindMoistureViaWS(token) {
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
            service: "set_zone_moisture",
            service_data: {
              zone_entity_id: "switch.test_lawn",
              moisture_entities: ["sensor.test_lawn_moisture"],
              combine_mode: "primary",
            },
          })
        );
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
    // Make sure there's at least one bound moisture sensor for #23/#85 deep link
    await bindMoistureViaWS(token);
    await new Promise((r) => setTimeout(r, 800));

    await loadPanel(page);

    // ── 1. Settings: Schedule timing card ──
    console.log("\n→ Settings / Schedule timing");
    await nav(page, "settings");
    let frame = await getFrame(page);
    const settings = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        bufferForm: !!r.querySelector('form[data-form="zone-buffer"]'),
        bufferInput: !!r.querySelector('input[name="zone_buffer_seconds"]'),
        snoozeBtn: !!r.querySelector('[data-action="weekly-snooze-30"]'),
        v110: r.textContent.includes("v1.10.0"),
      };
    });
    if (!settings.bufferForm) fail("zone-buffer form missing");
    else pass("zone-buffer form present");
    if (!settings.bufferInput) fail("zone_buffer_seconds input missing");
    else pass("zone_buffer_seconds input present");
    if (!settings.snoozeBtn) fail("'Snooze 30 days' button missing");
    else pass("snooze button present");
    if (!settings.v110) fail("v1.10.0 not visible");
    else pass("v1.10.0 visible");

    // ── 2. Click Snooze, verify Resume now appears ──
    console.log("\n→ Snooze toggles state");
    await frame.evaluate(() =>
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('[data-action="weekly-snooze-30"]')
        .click()
    );
    // Snooze calls a service → fetchConfig → re-render. Give it a beat.
    await new Promise((r) => setTimeout(r, 1200));
    frame = await getFrame(page);
    const snoozed = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        hasResume: !!r.querySelector('[data-action="weekly-unsnooze"]'),
        hint: r.querySelector(".settings-card .section-hint")?.textContent || "",
      };
    });
    if (!snoozed.hasResume) fail(`Resume button didn't appear (hint="${snoozed.hint}")`);
    else pass("Snooze flipped state — 'Resume now' button visible");

    // Resume to clean up state for next run
    await frame.evaluate(() => {
      const btn = document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('[data-action="weekly-unsnooze"]');
      btn?.click();
    });
    await new Promise((r) => setTimeout(r, 800));

    // ── 3. Sensor deep-link (Sensors tab) ──
    console.log("\n→ Sensors / deep link");
    await nav(page, "sensors");
    frame = await getFrame(page);
    const link = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const a = r.querySelector(".sensor-link");
      if (!a) return null;
      return {
        href: a.getAttribute("href"),
        target: a.getAttribute("target"),
        text: a.textContent.trim(),
      };
    });
    if (!link) fail("no .sensor-link found on any bound sensor row");
    else {
      if (!/\/developer-tools\/state\?entity_id=sensor\./.test(link.href))
        fail(`link href wrong: ${link.href}`);
      else pass(`deep link href: ${link.href}`);
      if (link.target !== "_top")
        fail(`link target should be _top, got "${link.target}"`);
      else pass(`link target="_top" (opens in parent window)`);
    }
  } catch (err) {
    console.error("test failed:", err.message);
    fails++;
  } finally {
    if (msgs.length > 0) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of msgs.slice(0, 5)) console.log(`  ${m}`);
    }
    await browser.close();
  }
  console.log(fails === 0 ? "\n✓ ALL v1.10.0 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
