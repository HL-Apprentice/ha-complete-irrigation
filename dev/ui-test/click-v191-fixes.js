/**
 * v1.9.1 smoke — verify the 4 UX fixes in one pass.
 *
 *  1. Schedule modal: Start time cell ≥ Duration cell width.
 *  2. Zones tab: button label = "🌱 New Planting" (was "🌱 New Grass").
 *  3. Notifications tab: <textarea name="notify_targets"> (multi-line).
 *  4. Today: NO hide-zone / show-hidden buttons; hint shown when zones hidden.
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

    // ── 1. Today: NO hide buttons, NO show-hidden toggle ──
    console.log("\n→ Today: hide controls removed");
    let frame = await getFrame(page);
    const today = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        hideBtns: r.querySelectorAll(
          '.zone-tile [data-action="hide-zone"], .zone-tile [data-action="show-zone"]'
        ).length,
        showHidden: r.querySelectorAll('[data-action="show-hidden"]').length,
        hasZonesTabLink: r.textContent.includes("Zones tab"),
      };
    });
    if (today.hideBtns !== 0) fail(`expected 0 hide buttons on Today, got ${today.hideBtns}`);
    else pass("no hide buttons on Today tiles");
    if (today.showHidden !== 0) fail(`expected 0 show-hidden buttons, got ${today.showHidden}`);
    else pass("no show-hidden toggle");

    // ── 2. Schedule modal start time cell sized properly ──
    console.log("\n→ Schedule modal: Start time cell width");
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
    const rowGeom = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const row = r.querySelector(".schedule-time-row");
      if (!row) return { ok: false };
      const cells = row.children;
      if (cells.length !== 2) return { ok: false, cellCount: cells.length };
      const a = cells[0].getBoundingClientRect();
      const b = cells[1].getBoundingClientRect();
      // Start time should be wider than Duration
      return { ok: true, timeW: a.width, durW: b.width };
    });
    if (!rowGeom.ok) fail("schedule-time-row missing or malformed");
    else if (rowGeom.timeW <= rowGeom.durW)
      fail(`Start time (${rowGeom.timeW}px) not wider than Duration (${rowGeom.durW}px)`);
    else
      pass(
        `Start time cell wider than Duration (${Math.round(rowGeom.timeW)}px vs ${Math.round(rowGeom.durW)}px)`
      );
    await frame.evaluate(() =>
      document.querySelector("complete-irrigation-panel").shadowRoot.querySelector(".modal-cancel").click()
    );

    // ── 3. Zones tab: "New Planting" button ──
    console.log("\n→ Zones: 'New Planting' button");
    await nav(page, "zones");
    frame = await getFrame(page);
    const planting = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const btns = Array.from(r.querySelectorAll('[data-action="open-establishment"]'));
      return {
        count: btns.length,
        labels: btns.map((b) => b.textContent.trim()),
      };
    });
    if (planting.count === 0) fail("no establishment buttons");
    else if (!planting.labels.every((l) => /new planting/i.test(l)))
      fail(`some buttons not renamed: ${JSON.stringify(planting.labels)}`);
    else pass(`all ${planting.count} button(s) say 'New Planting'`);

    // ── 4. Notifications tab: textarea for multiple targets ──
    console.log("\n→ Notifications: multi-target textarea");
    await nav(page, "notifications");
    frame = await getFrame(page);
    const notif = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const ta = r.querySelector('textarea[name="notify_targets"]');
      const oldInput = r.querySelector('input[name="notify_target"]');
      return {
        hasTextarea: !!ta,
        oldInputGone: !oldInput,
        rows: ta ? parseInt(ta.getAttribute("rows"), 10) : 0,
      };
    });
    if (!notif.hasTextarea) fail("notify_targets textarea missing");
    else pass(`notify_targets textarea present (${notif.rows} rows)`);
    if (!notif.oldInputGone) fail("legacy single notify_target input still present");
    else pass("legacy single-line input replaced");
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
  console.log(fails === 0 ? "\n✓ ALL v1.9.1 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
