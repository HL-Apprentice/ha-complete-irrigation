/**
 * v1.4.0 Schedule modal verifier.
 *
 * Validates:
 *  1. Modal opens with new fields present (mode toggle, h/m duration inputs).
 *  2. Start time and Duration cells do NOT overlap horizontally (the
 *     .row-2 overflow fix.)
 *  3. Switching mode to "interval" reveals interval_days + interval_anchor
 *     and hides the weekday-group.
 *  4. Hours+minutes inputs combine into editor.duration_minutes.
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

  let exitCode = 0;
  const fail = (msg) => {
    console.log(`  ✗ ${msg}`);
    exitCode = 1;
  };
  const pass = (msg) => console.log(`  ✓ ${msg}`);

  try {
    const token = fs.readFileSync("/tmp/ha_test_token.txt", "utf8").trim();
    await page.goto(HA_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.evaluate((t) => {
      const tokens = {
        access_token: t,
        token_type: "Bearer",
        refresh_token: "",
        expires_in: 1800,
        hassUrl: window.location.origin,
        clientId: window.location.origin + "/",
        expires: Date.now() + 1800 * 1000,
      };
      localStorage.setItem("hassTokens", JSON.stringify(tokens));
    }, token);

    await page.goto(`${HA_URL}/complete-irrigation`, {
      waitUntil: "networkidle2",
      timeout: 30000,
    });
    await new Promise((r) => setTimeout(r, 3000));

    await page.waitForFunction(
      () => {
        const ha = document.querySelector("home-assistant");
        const main = ha?.shadowRoot?.querySelector("home-assistant-main");
        const drawer = main?.shadowRoot?.querySelector("ha-drawer");
        const resolver = drawer?.querySelector("partial-panel-resolver");
        return !!resolver?.querySelector("ha-panel-custom");
      },
      { timeout: 15000 }
    );
    await new Promise((r) => setTimeout(r, 3000));

    const iframeEl = await page.evaluateHandle(() => {
      const ha = document.querySelector("home-assistant");
      const main = ha?.shadowRoot?.querySelector("home-assistant-main");
      const drawer = main?.shadowRoot?.querySelector("ha-drawer");
      const resolver = drawer?.querySelector("partial-panel-resolver");
      const custom = resolver?.querySelector("ha-panel-custom");
      return custom?.querySelector("iframe") || null;
    });
    const frame = await iframeEl.contentFrame();
    if (!frame) throw new Error("contentFrame() returned null");

    await frame.waitForFunction(
      () => !!document.querySelector("complete-irrigation-panel"),
      { timeout: 15000 }
    );

    // ── nav to schedules + open modal ─────────────────────────────
    console.log("→ open Add Schedule modal");
    await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      el.shadowRoot
        .querySelector('button.sidebar-item[data-section="schedules"]')
        .click();
    });
    await new Promise((r) => setTimeout(r, 400));
    await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      el.shadowRoot
        .querySelector('button[data-action="add-schedule"]')
        .click();
    });
    await new Promise((r) => setTimeout(r, 400));

    // ── 1. modal opens with new fields ────────────────────────────
    const initialState = await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      const r = el.shadowRoot;
      return {
        modalPresent: !!r.querySelector(".schedule-form"),
        modeRadios: r.querySelectorAll('input[name="mode"]').length,
        durationH: !!r.querySelector('input[name="duration_h"]'),
        durationM: !!r.querySelector('input[name="duration_m"]'),
        weekdayGroup: !!r.querySelector(".weekday-group"),
        intervalDays: !!r.querySelector('input[name="interval_days"]'),
      };
    });
    console.log("  initial state:", JSON.stringify(initialState));
    if (!initialState.modalPresent) fail("modal didn't open");
    else pass("modal open");
    if (initialState.modeRadios !== 2) fail(`expected 2 mode radios, got ${initialState.modeRadios}`);
    else pass("mode toggle (2 radios) present");
    if (!initialState.durationH || !initialState.durationM)
      fail("duration_h / duration_m inputs missing");
    else pass("hours + minutes duration inputs present");
    if (!initialState.weekdayGroup) fail("weekday-group should show by default");
    else pass("weekdays mode renders weekday-group by default");
    if (initialState.intervalDays) fail("interval_days should be hidden in weekdays mode");
    else pass("interval fields hidden in weekdays mode");

    // ── 2. row-2 overlap check ────────────────────────────────────
    const overlap = await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      const r = el.shadowRoot;
      const row = r.querySelector(".row-2");
      if (!row) return { ok: false, reason: "no .row-2" };
      const cells = row.children;
      if (cells.length !== 2) return { ok: false, reason: "wrong cell count" };
      const a = cells[0].getBoundingClientRect();
      const b = cells[1].getBoundingClientRect();
      // left cell's right edge must not exceed right cell's left edge
      return { ok: a.right <= b.left + 1, aRight: a.right, bLeft: b.left };
    });
    if (!overlap.ok)
      fail(`row-2 cells overlap: a.right=${overlap.aRight} b.left=${overlap.bLeft}`);
    else pass("row-2 cells do not overlap horizontally");

    // ── 3. switch to interval mode, fields swap ───────────────────
    await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      const intervalRadio = el.shadowRoot.querySelector(
        'input[name="mode"][value="interval"]'
      );
      intervalRadio.checked = true;
      intervalRadio.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await new Promise((r) => setTimeout(r, 400));
    const intervalState = await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      const r = el.shadowRoot;
      return {
        weekdayGroup: !!r.querySelector(".weekday-group"),
        intervalDays: !!r.querySelector('input[name="interval_days"]'),
        intervalAnchor: !!r.querySelector('input[name="interval_anchor"]'),
      };
    });
    console.log("  interval state:", JSON.stringify(intervalState));
    if (intervalState.weekdayGroup) fail("weekday-group should disappear in interval mode");
    else pass("weekday-group hides in interval mode");
    if (!intervalState.intervalDays || !intervalState.intervalAnchor)
      fail("interval_days / interval_anchor missing in interval mode");
    else pass("interval_days + interval_anchor render in interval mode");

    // ── 4. duration sync: type 2h 15m → editor.duration_minutes=135
    await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      const r = el.shadowRoot;
      const h = r.querySelector('input[name="duration_h"]');
      const m = r.querySelector('input[name="duration_m"]');
      h.value = "2";
      h.dispatchEvent(new Event("input", { bubbles: true }));
      m.value = "15";
      m.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await new Promise((r) => setTimeout(r, 200));
    const dur = await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      return el._scheduleEditor.duration_minutes;
    });
    console.log("  duration_minutes after 2h15m input:", dur);
    if (dur !== 135) fail(`expected duration_minutes=135, got ${dur}`);
    else pass("hours + minutes combine to duration_minutes (2h15m = 135)");

    await page.screenshot({
      path: "/tmp/panel-v14-schedule-modal.png",
      fullPage: true,
    });
    console.log("→ screenshot: /tmp/panel-v14-schedule-modal.png");

    if (consoleMessages.length > 0) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of consoleMessages) console.log(`  ${m}`);
    }
  } catch (err) {
    console.error("test failed:", err.message);
    exitCode = 2;
  } finally {
    await browser.close();
  }
  console.log(exitCode === 0 ? "\n✓ ALL v1.4 CHECKS PASSED" : "\n✗ FAILURES (see above)");
  process.exitCode = exitCode;
}

main();
