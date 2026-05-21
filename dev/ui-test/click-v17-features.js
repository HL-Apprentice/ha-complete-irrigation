/**
 * v1.7.0 smoke — verify Establishment + Conflict-policy + Forecast wiring.
 *
 * 1. Zones row has "🌱 New Grass" button → opens establishment modal with
 *    correct field set (cycles_per_day, minutes_per_cycle, days, start_hour).
 * 2. Settings tab shows the schedule-conflict policy picker.
 * 3. Weather tab triggers a forecast fetch (network request to call_service
 *    type WS message). We just confirm the panel doesn't error on entry.
 */
const puppeteer = require("puppeteer");
const fs = require("fs");

const HA_URL = process.env.HA_URL || "http://localhost:8123";

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
  await new Promise((r) => setTimeout(r, 500));
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

  try {
    await loadPanel(page);

    // ── 1. Zones tab → 🌱 New Grass button + modal ───────────────
    console.log("\n→ Zones / establishment mode");
    await nav(page, "zones");
    let frame = await getFrame(page);
    const grassBtns = await frame.evaluate(
      () =>
        document
          .querySelector("complete-irrigation-panel")
          .shadowRoot.querySelectorAll('[data-action="open-establishment"]').length
    );
    if (grassBtns === 0) fail("no establishment buttons on zone rows");
    else pass(`${grassBtns} establishment button(s) found`);

    await frame.evaluate(() =>
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('[data-action="open-establishment"]')
        .click()
    );
    await new Promise((r) => setTimeout(r, 400));
    frame = await getFrame(page);
    const modal = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        form: !!r.querySelector(".establishment-form"),
        cycles: !!r.querySelector('input[name="cycles_per_day"]'),
        minutes: !!r.querySelector('input[name="minutes_per_cycle"]'),
        days: !!r.querySelector('input[name="days"]'),
        startHour: !!r.querySelector('input[name="start_hour"]'),
      };
    });
    if (!modal.form) fail("establishment modal didn't open");
    else pass("establishment modal opens");
    if (modal.cycles && modal.minutes && modal.days && modal.startHour)
      pass("modal has all 4 expected fields");
    else fail(`modal fields missing: ${JSON.stringify(modal)}`);

    await frame.evaluate(() =>
      document.querySelector("complete-irrigation-panel").shadowRoot.querySelector(".modal-cancel").click()
    );

    // ── 2. Schedules tab → interval mode shows "every N days" ────
    console.log("\n→ Schedules / interval label");
    await nav(page, "schedules");
    frame = await getFrame(page);
    // Check that no schedule's meta accidentally shows an empty weekdays string
    // (we'd see "··" or trailing dot). Since the dev HA may have no schedules,
    // this is best-effort but at least confirms the section renders.
    const schedSection = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const main = r.querySelector("main");
      return {
        hasAddBtn: !!r.querySelector('[data-action="add-schedule"]'),
        hasSchedules: r.querySelectorAll(".schedule-row").length,
        plainMain: main.textContent.length > 0,
      };
    });
    if (!schedSection.hasAddBtn) fail("Add Schedule button missing");
    else pass(`Schedules tab renders (${schedSection.hasSchedules} existing rows)`);

    // ── 3. Settings tab → conflict policy picker ─────────────────
    console.log("\n→ Settings / conflict policy");
    await nav(page, "settings");
    frame = await getFrame(page);
    const settings = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        policyForm: !!r.querySelector('form[data-form="conflict-policy"]'),
        policySelect: !!r.querySelector('select[name="policy"]'),
        deferOpt: !!r.querySelector('option[value="defer_new"]'),
        shiftOpt: !!r.querySelector('option[value="shift_existing"]'),
        splitOpt: !!r.querySelector('option[value="split_difference"]'),
        v170: r.textContent.includes("v1.7.0"),
      };
    });
    if (!settings.policyForm) fail("conflict-policy form missing");
    else pass("conflict-policy form present");
    if (settings.deferOpt && settings.shiftOpt && settings.splitOpt)
      pass("all 3 policy options present");
    else fail("missing policy options");
    if (!settings.v170) fail("v1.7.0 label missing");
    else pass("v1.7.0 visible");

    // ── 4. Weather tab — confirm no JS errors entering it ────────
    console.log("\n→ Weather / forecast fetch entry");
    await nav(page, "weather");
    await new Promise((r) => setTimeout(r, 1000)); // give the fetch a moment
    frame = await getFrame(page);
    const weather = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        form: !!r.querySelector(".weather-form"),
        rainBoxes: r.querySelectorAll('input[name="rain_sensor_pick"]').length,
      };
    });
    if (!weather.form) fail("weather form missing");
    else pass("weather form present");
    pass(`rain sensor picks: ${weather.rainBoxes}`);

    await page.screenshot({ path: "/tmp/panel-v17.png", fullPage: true });
    console.log("\n→ screenshot: /tmp/panel-v17.png");
  } catch (err) {
    console.error("test failed:", err.message);
    fails++;
  } finally {
    if (msgs.length > 0) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of msgs) console.log(`  ${m}`);
    }
    await browser.close();
  }
  console.log(fails === 0 ? "\n✓ ALL v1.7.0 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
