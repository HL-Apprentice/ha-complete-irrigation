/**
 * v1.10.4 — reorder zones via ▲▼ on the Zones tab.
 *
 *  1. Default order: switch.test_lawn before switch.test_garden.
 *  2. Click ▼ on test_lawn → swap. test_garden first now.
 *  3. New order applied to Today tiles too.
 *  4. New order persisted to coordinator config (verify via WS get_config).
 *  5. ▲ on first row + ▼ on last row are disabled.
 */
const puppeteer = require("puppeteer");
const fs = require("fs");
const WebSocket = require("ws");

const HA_URL = process.env.HA_URL || "http://localhost:8123";

async function getConfigViaWS(token) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket("ws://localhost:8123/api/websocket");
    let mid = 1;
    ws.on("message", (raw) => {
      const m = JSON.parse(raw.toString());
      if (m.type === "auth_required") {
        ws.send(JSON.stringify({ type: "auth", access_token: token }));
      } else if (m.type === "auth_ok") {
        ws.send(JSON.stringify({ id: mid++, type: "complete_irrigation/get_config" }));
      } else if (m.type === "result") {
        ws.close();
        if (m.success) resolve(m.result);
        else reject(new Error(JSON.stringify(m)));
      }
    });
    ws.on("error", reject);
  });
}

async function resetOrderViaWS(token) {
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
            service: "set_general_config",
            service_data: { zone_order: [] },
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
  await resetOrderViaWS(token);

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
    await nav(page, "zones");
    let frame = await getFrame(page);

    // ── 1. Default order ──
    console.log("\n→ default order (no zone_order config)");
    const orderBefore = await frame.evaluate(() =>
      Array.from(
        document.querySelector("complete-irrigation-panel").shadowRoot.querySelectorAll(".zone-row")
      ).map((el) => el.querySelector('[data-action^="zone-move"]')?.dataset.entityId)
    );
    console.log("  order:", JSON.stringify(orderBefore));
    if (
      orderBefore[0] !== "switch.test_lawn" ||
      orderBefore[1] !== "switch.test_garden"
    ) {
      fail(`unexpected default order: ${orderBefore.join(", ")}`);
    } else {
      pass("default order is test_lawn → test_garden");
    }

    // ── 2. Disabled arrows on edges ──
    console.log("\n→ edge arrows disabled");
    const edges = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const rows = r.querySelectorAll(".zone-row");
      const firstUp = rows[0].querySelector('[data-action="zone-move-up"]');
      const lastDown = rows[rows.length - 1].querySelector('[data-action="zone-move-down"]');
      return {
        firstUpDisabled: firstUp.disabled,
        lastDownDisabled: lastDown.disabled,
      };
    });
    if (!edges.firstUpDisabled) fail("first row ▲ should be disabled");
    else pass("first row ▲ disabled");
    if (!edges.lastDownDisabled) fail("last row ▼ should be disabled");
    else pass("last row ▼ disabled");

    // ── 3. Click ▼ on first row, expect swap ──
    console.log("\n→ click ▼ on test_lawn");
    await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const rows = r.querySelectorAll(".zone-row");
      rows[0].querySelector('[data-action="zone-move-down"]').click();
    });
    await new Promise((r) => setTimeout(r, 1200));
    frame = await getFrame(page);
    const orderAfter = await frame.evaluate(() =>
      Array.from(
        document.querySelector("complete-irrigation-panel").shadowRoot.querySelectorAll(".zone-row")
      ).map((el) => el.querySelector('[data-action^="zone-move"]')?.dataset.entityId)
    );
    console.log("  order:", JSON.stringify(orderAfter));
    if (
      orderAfter[0] !== "switch.test_garden" ||
      orderAfter[1] !== "switch.test_lawn"
    ) {
      fail(`expected garden-then-lawn, got ${orderAfter.join(", ")}`);
    } else {
      pass("Zones tab order swapped");
    }

    // ── 4. Today tab reflects the new order ──
    console.log("\n→ Today tab uses same order");
    await nav(page, "today");
    frame = await getFrame(page);
    const todayOrder = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const tiles = r.querySelectorAll(".zone-tile");
      const ids = [];
      for (const t of tiles) {
        const stopBtn = t.querySelector("[data-entity-id]");
        if (stopBtn) ids.push(stopBtn.getAttribute("data-entity-id"));
      }
      return ids;
    });
    console.log("  today order:", JSON.stringify(todayOrder));
    if (
      todayOrder[0] !== "switch.test_garden" ||
      todayOrder[1] !== "switch.test_lawn"
    ) {
      fail(`Today tiles in wrong order: ${todayOrder.join(", ")}`);
    } else {
      pass("Today tiles reflect new order");
    }

    // ── 5. Persistence (server side) ──
    console.log("\n→ persisted server-side");
    const cfg = await getConfigViaWS(token);
    console.log("  config.zone_order:", JSON.stringify(cfg?.zone_order));
    if (
      !Array.isArray(cfg?.zone_order) ||
      cfg.zone_order[0] !== "switch.test_garden" ||
      cfg.zone_order[1] !== "switch.test_lawn"
    ) {
      fail(`server didn't persist new order: ${JSON.stringify(cfg?.zone_order)}`);
    } else {
      pass("server-side zone_order persisted");
    }
  } catch (err) {
    console.error("test failed:", err.message);
    fails++;
  } finally {
    await resetOrderViaWS(token).catch(() => {});
    if (msgs.length > 0) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of msgs.slice(0, 4)) console.log(`  ${m}`);
    }
    await browser.close();
  }
  console.log(fails === 0 ? "\n✓ ALL v1.10.4 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
