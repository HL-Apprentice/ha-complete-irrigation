/**
 * Repro for v1.3.0 bugs (flicker on hover + random popups during countdown).
 *
 * The cause: countdown timer's full shadowRoot rebuild every 1s wipes and
 * recreates all buttons, causing :hover flicker and shifted click targets
 * during mousedown→mouseup. v1.3.1 updates only the countdown text content.
 *
 * Test:
 *   1. Click Run Now on test_lawn, fill duration, submit.
 *   2. Verify countdown is rendered.
 *   3. Capture the DOM identity of the Stop button + countdown span.
 *   4. Wait ~3 seconds.
 *   5. Verify the SAME DOM nodes are still there (proves no rebuild) and
 *      that the countdown text DID update (proves the text-update path
 *      is working).
 */
const puppeteer = require("puppeteer");
const fs = require("fs");

const HA_URL = process.env.HA_URL || "http://localhost:8123";

async function main() {
  const browser = await puppeteer.launch({
    headless: "new",
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });

  const consoleMessages = [];
  page.on("console", (m) => consoleMessages.push(`[${m.type()}] ${m.text()}`));
  page.on("pageerror", (e) => consoleMessages.push(`[pageerror] ${e.message}`));

  try {
    const token = fs.readFileSync("/tmp/ha_test_token.txt", "utf8").trim();
    await page.goto(HA_URL, { waitUntil: "domcontentloaded" });
    await page.evaluate((t) => {
      localStorage.setItem("hassTokens", JSON.stringify({
        access_token: t, token_type: "Bearer", refresh_token: "",
        expires_in: 1800, hassUrl: location.origin,
        clientId: location.origin + "/", expires: Date.now() + 1800000,
      }));
    }, token);

    await page.goto(`${HA_URL}/complete-irrigation`, { waitUntil: "networkidle2" });
    await page.waitForFunction(() => {
      try {
        const ha = document.querySelector("home-assistant");
        const main = ha?.shadowRoot?.querySelector("home-assistant-main");
        const drawer = main?.shadowRoot?.querySelector("ha-drawer");
        const resolver = drawer?.querySelector("partial-panel-resolver");
        const custom = resolver?.querySelector("ha-panel-custom");
        return !!custom?.querySelector("iframe");
      } catch (_) { return false; }
    }, { timeout: 20000 });
    await new Promise((r) => setTimeout(r, 3000));

    // Reach into the iframe by running everything inside one big
    // page-side evaluator. Stops detached-frame errors when HA pushes
    // hass updates between our calls.
    async function inPanel(fn, ...args) {
      return page.evaluate(async (fnStr, ...inner) => {
        const ha = document.querySelector("home-assistant");
        const main = ha?.shadowRoot?.querySelector("home-assistant-main");
        const drawer = main?.shadowRoot?.querySelector("ha-drawer");
        const resolver = drawer?.querySelector("partial-panel-resolver");
        const custom = resolver?.querySelector("ha-panel-custom");
        const iframe = custom?.querySelector("iframe");
        if (!iframe?.contentWindow) throw new Error("no iframe");
        const panel = iframe.contentDocument.querySelector(
          "complete-irrigation-panel"
        );
        if (!panel) throw new Error("no panel element");
        const f = new Function("return (" + fnStr + ")")();
        return f(panel, iframe.contentDocument, iframe.contentWindow, ...inner);
      }, fn.toString(), ...args);
    }

    await new Promise((r) => setTimeout(r, 2500));

    console.log("→ click Run Now on switch.test_lawn");
    await inPanel((panel) => {
      const btn = panel.shadowRoot.querySelector(
        'button[data-action="run-now"][data-entity-id="switch.test_lawn"]'
      );
      if (!btn) throw new Error("Run Now button not found");
      btn.click();
    });
    await new Promise((r) => setTimeout(r, 300));

    console.log("→ submit modal with 2 min");
    await inPanel((panel) => {
      const input = panel.shadowRoot.querySelector('input[name="minutes"]');
      input.value = "2";
      const form = panel.shadowRoot.querySelector(".run-form");
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    await new Promise((r) => setTimeout(r, 1500));

    const before = await inPanel((panel) => {
      const stopBtn = panel.shadowRoot.querySelector(
        'button[data-action="stop"][data-entity-id="switch.test_lawn"]'
      );
      const cdSpan = panel.shadowRoot.querySelector(
        '[data-countdown-for="switch.test_lawn"]'
      );
      if (stopBtn) stopBtn._testMarker = "stop-" + Math.random();
      if (cdSpan) cdSpan._testMarker = "cd-" + Math.random();
      return {
        stopFound: !!stopBtn,
        cdFound: !!cdSpan,
        stopMarker: stopBtn?._testMarker || null,
        cdMarker: cdSpan?._testMarker || null,
        cdText: cdSpan?.textContent || null,
      };
    });
    console.log("  before wait:", JSON.stringify(before, null, 2));

    console.log("→ wait 3 seconds (3 countdown ticks)");
    await new Promise((r) => setTimeout(r, 3200));

    const after = await inPanel((panel) => {
      const stopBtn = panel.shadowRoot.querySelector(
        'button[data-action="stop"][data-entity-id="switch.test_lawn"]'
      );
      const cdSpan = panel.shadowRoot.querySelector(
        '[data-countdown-for="switch.test_lawn"]'
      );
      return {
        stopMarker: stopBtn?._testMarker || null,
        cdMarker: cdSpan?._testMarker || null,
        cdText: cdSpan?.textContent || null,
      };
    });
    console.log("  after wait:", JSON.stringify(after, null, 2));

    let ok = true;
    if (before.stopMarker !== after.stopMarker) {
      console.log("✗ Stop button was REBUILT (markers differ) — flicker bug still present");
      ok = false;
    } else {
      console.log("✓ Stop button DOM node identity preserved across ticks");
    }
    if (before.cdMarker !== after.cdMarker) {
      console.log("✗ Countdown span was REBUILT — flicker bug still present");
      ok = false;
    } else {
      console.log("✓ Countdown span DOM node identity preserved");
    }
    if (before.cdText === after.cdText) {
      console.log("✗ Countdown text did NOT advance (1Hz tick broken)");
      ok = false;
    } else {
      console.log(`✓ Countdown text advanced: ${before.cdText} → ${after.cdText}`);
    }

    if (consoleMessages.length > 0) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of consoleMessages) console.log(`  ${m}`);
    }

    process.exitCode = ok ? 0 : 1;
  } catch (err) {
    console.error("test failed:", err.message);
    process.exitCode = 2;
  } finally {
    await browser.close();
  }
}

main();
