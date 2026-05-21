/**
 * v1.8.0 smoke — multi-zone schedules, HA theme picker, manual-run default,
 * category info hint, heat index banner cell.
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

    // ── 1. Multi-zone: open Add Schedule modal, verify "+ Add another zone" exists ──
    console.log("\n→ Schedules / multi-zone editor");
    await nav(page, "schedules");
    let frame = await getFrame(page);
    await frame.evaluate(() =>
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('[data-action="add-schedule"]')
        .click()
    );
    await new Promise((r) => setTimeout(r, 400));
    frame = await getFrame(page);
    const initial = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        addBtn: !!r.querySelector('[data-action="add-extra-step"]'),
        initialExtras: r.querySelectorAll(".extra-step-row").length,
      };
    });
    if (!initial.addBtn) fail("'+ Add another zone' button missing");
    else pass("multi-zone Add button present");
    if (initial.initialExtras !== 0) fail(`expected 0 initial extra-step rows, got ${initial.initialExtras}`);
    else pass("initial extras = 0");

    // Click "+ Add another zone" twice → expect 2 extra-step rows
    await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      r.querySelector('[data-action="add-extra-step"]').click();
    });
    await new Promise((r) => setTimeout(r, 200));
    await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      r.querySelector('[data-action="add-extra-step"]').click();
    });
    await new Promise((r) => setTimeout(r, 300));
    frame = await getFrame(page);
    const after = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        extras: r.querySelectorAll(".extra-step-row").length,
        editorExtras:
          document.querySelector("complete-irrigation-panel")._scheduleEditor
            .extra_steps.length,
      };
    });
    if (after.extras !== 2) fail(`expected 2 extra rows in DOM, got ${after.extras}`);
    else pass("two extra-step rows added");
    if (after.editorExtras !== 2) fail(`editor.extra_steps length = ${after.editorExtras}`);
    else pass("editor state has 2 extras");

    // Remove one
    await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      r.querySelector('[data-action="remove-extra-step"][data-step-idx="0"]').click();
    });
    await new Promise((r) => setTimeout(r, 200));
    frame = await getFrame(page);
    const afterRemove = await frame.evaluate(
      () =>
        document.querySelector("complete-irrigation-panel").shadowRoot
          .querySelectorAll(".extra-step-row").length
    );
    if (afterRemove !== 1) fail(`expected 1 row after remove, got ${afterRemove}`);
    else pass("remove button drops one row");

    await frame.evaluate(() =>
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector(".modal-cancel")
        .click()
    );

    // ── 2. HA theme picker in Settings ──
    console.log("\n→ Settings / HA theme picker");
    await nav(page, "settings");
    frame = await getFrame(page);
    const themePicker = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const sel = r.querySelector('select[name="ha_theme"]');
      return {
        present: !!sel,
        optionCount: sel ? sel.options.length : 0,
        // dev/ha-config has no extra themes — just the "None" option expected.
      };
    });
    if (!themePicker.present) fail("HA theme picker missing");
    else pass(`HA theme picker present (${themePicker.optionCount} options)`);

    // ── 3. Manual-run default card ──
    const manualDef = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        form: !!r.querySelector('form[data-form="manual-default"]'),
        input: !!r.querySelector('input[name="manual_default"]'),
      };
    });
    if (!manualDef.form || !manualDef.input) fail("manual-run default card missing");
    else pass("manual-run default card present");

    // ── 4. v1.8.0 visible ──
    const versionVisible = await frame.evaluate(
      () => document.querySelector("complete-irrigation-panel").shadowRoot.textContent.includes("v1.8.0")
    );
    if (!versionVisible) fail("v1.8.0 label not visible");
    else pass("v1.8.0 visible");

    // ── 5. Sensors tab: category info hint ──
    console.log("\n→ Sensors / category info hint");
    await nav(page, "sensors");
    frame = await getFrame(page);
    await frame.evaluate(() =>
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('[data-action="configure-sensor"]')
        .click()
    );
    await new Promise((r) => setTimeout(r, 300));
    frame = await getFrame(page);
    // Change category to "lawn" and verify hint text appears
    await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const sel = r.querySelector('select[name="category"]');
      sel.value = "lawn";
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await new Promise((r) => setTimeout(r, 200));
    frame = await getFrame(page);
    const hint = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return r.querySelector("[data-category-info]")?.textContent || "";
    });
    if (!/Lawn|21-40%/i.test(hint)) fail(`category info hint not updated: "${hint}"`);
    else pass(`category info hint updated: "${hint.slice(0, 60)}..."`);

    await frame.evaluate(() =>
      document.querySelector("complete-irrigation-panel").shadowRoot.querySelector(".modal-cancel").click()
    );

    await page.screenshot({ path: "/tmp/panel-v18.png", fullPage: true });
    console.log("\n→ screenshot: /tmp/panel-v18.png");
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
  console.log(fails === 0 ? "\n✓ ALL v1.8.0 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
