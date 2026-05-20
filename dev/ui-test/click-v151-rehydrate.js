/**
 * v1.5.1 — Verifies:
 *  1. The countdown hydrates after a "cold" panel load when a run is
 *     already active (started via WS before the panel opened).
 *  2. The Today zone tile no longer renders the entity_id.
 */
const puppeteer = require("puppeteer");
const fs = require("fs");
const WebSocket = require("ws");

const HA_URL = process.env.HA_URL || "http://localhost:8123";
const ZONE = "switch.test_lawn";

async function startRunViaWS(token, minutes) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket("ws://localhost:8123/api/websocket");
    let mid = 1;
    ws.on("message", (raw) => {
      const m = JSON.parse(raw.toString());
      if (m.type === "auth_required") {
        ws.send(JSON.stringify({ type: "auth", access_token: token }));
      } else if (m.type === "auth_ok") {
        const id = mid++;
        ws.send(
          JSON.stringify({
            id,
            type: "call_service",
            domain: "complete_irrigation",
            service: "run_zone",
            service_data: { entity_id: ZONE, minutes },
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

async function stopRunViaWS(token) {
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
            service: "stop_zone",
            service_data: { entity_id: ZONE },
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

async function main() {
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
  const token = fs.readFileSync("/tmp/ha_test_token.txt", "utf8").trim();

  try {
    // Make sure nothing is running first
    await stopRunViaWS(token);
    await new Promise((r) => setTimeout(r, 500));

    console.log("→ start a 5-min run via WS (before panel loads)");
    await startRunViaWS(token, 5);
    await new Promise((r) => setTimeout(r, 1500));

    console.log("→ open panel (cold load)");
    await loadPanel(page);

    // Wait a beat for _fetchActiveRuns to complete
    await new Promise((r) => setTimeout(r, 2500));

    // ── 1. Countdown hydrated ─────────────────────────────────────
    console.log("\n→ verify countdown is hydrated");
    let frame = await getFrame(page);
    const cd = await frame.evaluate((entityId) => {
      const el = document.querySelector("complete-irrigation-panel");
      const r = el.shadowRoot;
      const localRuns = el._localRuns || {};
      const deadlineMs = localRuns[entityId];
      const remaining = deadlineMs ? deadlineMs - Date.now() : 0;
      const tile = Array.from(r.querySelectorAll(".zone-tile")).find((t) =>
        t.querySelector(".btn-stop")
      );
      const statusText = tile?.querySelector(".status-text")?.textContent || "";
      const cdSpan = tile?.querySelector("[data-countdown-for]")?.textContent || "";
      return { hasDeadline: !!deadlineMs, remainingMs: remaining, statusText, cdSpan };
    }, ZONE);
    console.log("  hydrated state:", JSON.stringify(cd));
    if (!cd.hasDeadline) fail("_localRuns is empty — _fetchActiveRuns didn't hydrate");
    else pass("_localRuns hydrated with deadline");
    if (cd.remainingMs < 1000) fail(`remaining ms is ${cd.remainingMs} — too low`);
    else pass(`countdown remaining > 0 (${Math.round(cd.remainingMs/1000)}s)`);
    if (!/left/i.test(cd.statusText)) fail(`status text doesn't show countdown: "${cd.statusText}"`);
    else pass(`status text shows countdown: "${cd.statusText}"`);

    // ── 2. Entity ID hidden on Today ──────────────────────────────
    console.log("\n→ verify entity_id hidden in Today zone tile");
    frame = await getFrame(page);
    const eidVisible = await frame.evaluate((entityId) => {
      const el = document.querySelector("complete-irrigation-panel");
      const r = el.shadowRoot;
      // Today tiles
      const tiles = r.querySelectorAll(".zone-tile");
      let foundInTile = false;
      for (const t of tiles) {
        if (t.textContent.includes(entityId)) {
          foundInTile = true;
          break;
        }
      }
      // Also confirm Zones tab still shows it
      const zonesBtn = r.querySelector('button.sidebar-item[data-section="zones"]');
      zonesBtn?.click();
      // Re-query after the nav click
      return new Promise((resolve) => {
        setTimeout(() => {
          const rows = el.shadowRoot.querySelectorAll(".zone-row");
          let foundInRow = false;
          for (const row of rows) {
            if (row.textContent.includes(entityId)) {
              foundInRow = true;
              break;
            }
          }
          resolve({ foundInTile, foundInRow });
        }, 300);
      });
    }, ZONE);
    console.log("  entity_id visible:", JSON.stringify(eidVisible));
    if (eidVisible.foundInTile) fail(`entity_id "${ZONE}" still shown in Today tile`);
    else pass("entity_id hidden in Today tile");
    if (!eidVisible.foundInRow) fail(`entity_id "${ZONE}" missing from Zones tab row (should still be there)`);
    else pass("entity_id still shown in Zones tab row");

    // cleanup
    await stopRunViaWS(token);

    await page.screenshot({
      path: "/tmp/panel-v151-rehydrate.png",
      fullPage: true,
    });
  } catch (err) {
    console.error("test failed:", err.message);
    fails++;
    await stopRunViaWS(token).catch(() => {});
  } finally {
    if (msgs.length > 0) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of msgs) console.log(`  ${m}`);
    }
    await browser.close();
  }
  console.log(fails === 0 ? "\n✓ ALL v1.5.1 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
