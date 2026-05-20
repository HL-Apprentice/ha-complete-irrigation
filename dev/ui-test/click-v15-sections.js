/**
 * v1.5.0 — Verify all four built-out sections render and behave.
 *
 * 1. Today: weather banner contains a Condition cell (weather.* detected)
 * 2. Zones: horizontal rows with 7-day strip + hide toggle
 * 3. Sensors: zone cards + Configure modal opens
 * 4. Weather: form renders with rain_sensor + temp_sensor pickers
 */
const puppeteer = require("puppeteer");
const fs = require("fs");

const HA_URL = process.env.HA_URL || "http://localhost:8123";

async function enterPanel(page) {
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

  try {
    const frame = await enterPanel(page);

    // ── 1. Today: weather banner condition cell ──────────────────
    console.log("\n→ Today / weather banner");
    const today = await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      const banner = el.shadowRoot.querySelector(".weather-banner");
      if (!banner) return { hasBanner: false };
      const labels = Array.from(banner.querySelectorAll(".weather-cell-label")).map(
        (e) => e.textContent.trim()
      );
      return { hasBanner: true, labels };
    });
    if (!today.hasBanner) fail("no weather banner on Today");
    else pass(`weather banner present (${today.labels.length} cells)`);
    if (today.labels?.includes("Condition")) pass("Condition cell present");
    else fail(`Condition cell missing (labels: ${JSON.stringify(today.labels)})`);

    // ── 2. Zones tab ─────────────────────────────────────────────
    console.log("\n→ Zones tab");
    await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      el.shadowRoot
        .querySelector('button.sidebar-item[data-section="zones"]')
        .click();
    });
    await new Promise((r) => setTimeout(r, 400));
    const zones = await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      const r = el.shadowRoot;
      const rows = r.querySelectorAll(".zone-row");
      const stripCells = r.querySelectorAll(".zone-row .zone-day");
      const hideBtns = r.querySelectorAll('[data-action="hide-zone"], [data-action="show-zone"]');
      return { rowCount: rows.length, stripCount: stripCells.length, hideBtnCount: hideBtns.length };
    });
    if (zones.rowCount === 0) fail("no zone rows rendered");
    else pass(`${zones.rowCount} zone row(s) rendered`);
    // 7 day strip per row
    const expectedStrip = zones.rowCount * 7;
    if (zones.stripCount !== expectedStrip)
      fail(`expected ${expectedStrip} day cells (${zones.rowCount} rows × 7), got ${zones.stripCount}`);
    else pass("7-day strip per zone");
    if (zones.hideBtnCount !== zones.rowCount)
      fail(`expected ${zones.rowCount} hide/show buttons, got ${zones.hideBtnCount}`);
    else pass("hide/show button per zone");

    // ── 3. Sensors tab + Configure modal ─────────────────────────
    console.log("\n→ Sensors tab");
    await frame.evaluate(() => {
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('button.sidebar-item[data-section="sensors"]')
        .click();
    });
    await new Promise((r) => setTimeout(r, 400));
    const sensors = await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      const r = el.shadowRoot;
      return {
        cards: r.querySelectorAll(".sensor-zone-card").length,
        configureBtns: r.querySelectorAll('[data-action="configure-sensor"]').length,
      };
    });
    if (sensors.cards === 0) fail("no sensor zone cards rendered");
    else pass(`${sensors.cards} sensor zone card(s)`);

    // Open the first Configure modal
    if (sensors.configureBtns > 0) {
      await frame.evaluate(() => {
        document
          .querySelector("complete-irrigation-panel")
          .shadowRoot.querySelector('[data-action="configure-sensor"]')
          .click();
      });
      await new Promise((r) => setTimeout(r, 400));
      const modal = await frame.evaluate(() => {
        const el = document.querySelector("complete-irrigation-panel");
        const r = el.shadowRoot;
        return {
          form: !!r.querySelector(".sensor-form"),
          combineSelect: !!r.querySelector('select[name="combine_mode"]'),
          combineHasPickOne:
            r.querySelector('select[name="combine_mode"] option[value=""]')?.textContent?.includes("Pick"),
          minPct: r.querySelector('input[name="min_pct"]')?.value,
          targetPct: r.querySelector('input[name="target_pct"]')?.value,
          maxPct: r.querySelector('input[name="max_pct"]')?.value,
        };
      });
      if (!modal.form) fail("sensor modal didn't open");
      else pass("sensor modal opens");
      if (modal.combineSelect) pass("combine_mode select present");
      else fail("combine_mode select missing");
      if (modal.combineHasPickOne) pass("combine_mode requires user choice (no default)");
      else fail("combine_mode appears to have a default");
      // close
      await frame.evaluate(() => {
        document
          .querySelector("complete-irrigation-panel")
          .shadowRoot.querySelector(".modal-cancel")
          .click();
      });
    }

    // ── 4. Weather tab ───────────────────────────────────────────
    console.log("\n→ Weather tab");
    await frame.evaluate(() => {
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('button.sidebar-item[data-section="weather"]')
        .click();
    });
    await new Promise((r) => setTimeout(r, 400));
    const weather = await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      const r = el.shadowRoot;
      return {
        form: !!r.querySelector(".weather-form"),
        rainSensor: !!r.querySelector('select[name="rain_sensor"]'),
        tempSensor: !!r.querySelector('select[name="temperature_sensor"]'),
        hotF: !!r.querySelector('input[name="hot_threshold_f"]'),
        boost: !!r.querySelector('input[name="boost_percent"]'),
        forecastPresent: !!r.querySelector(".forecast"),
      };
    });
    if (!weather.form) fail("weather form not rendered");
    else pass("weather form present");
    if (weather.rainSensor && weather.tempSensor) pass("rain + temp sensor pickers present");
    else fail(`missing pickers: rain=${weather.rainSensor} temp=${weather.tempSensor}`);
    if (weather.hotF && weather.boost) pass("hot threshold + boost inputs present");
    else fail(`missing config inputs`);
    // forecast may or may not be present depending on weather entity attrs
    console.log(`  forecast: ${weather.forecastPresent ? "present" : "absent (no forecast attrs)"}`);

    await page.screenshot({
      path: "/tmp/panel-v15-final.png",
      fullPage: true,
    });
    console.log("\n→ screenshot: /tmp/panel-v15-final.png");
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
  console.log(fails === 0 ? "\n✓ ALL v1.5.0 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
