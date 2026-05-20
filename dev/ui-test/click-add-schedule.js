/**
 * Headless-browser repro for "Add Schedule button not functional".
 *
 * Logs in to HA at localhost:8123, navigates to the Irrigation panel,
 * switches to the Schedules tab, clicks "+ Add Schedule", and asserts
 * that the schedule modal appears.
 */
const puppeteer = require("puppeteer");
const fs = require("fs");

const HA_URL = process.env.HA_URL || "http://localhost:8123";
const USER = "test";
const PASS = "testpass";

async function main() {
  const browser = await puppeteer.launch({
    headless: "new",
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });

  // Collect console errors
  const consoleMessages = [];
  page.on("console", (m) =>
    consoleMessages.push(`[${m.type()}] ${m.text()}`)
  );
  page.on("pageerror", (e) => consoleMessages.push(`[pageerror] ${e.message}`));

  try {
    console.log("→ seed HA auth via localStorage");
    const token = fs.readFileSync("/tmp/ha_test_token.txt", "utf8").trim();

    // Visit root first to set up localStorage on the right origin
    await page.goto(HA_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.evaluate((t) => {
      // HA's frontend stores auth in localStorage. Seed it so we don't need
      // to log in via the (custom-element) login form.
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

    console.log("→ go to irrigation panel");
    await page.goto(`${HA_URL}/complete-irrigation`, {
      waitUntil: "networkidle2",
      timeout: 30000,
    });
    await new Promise((r) => setTimeout(r, 3000));

    // Wait for ha-panel-custom and inspect what's actually inside it.
    console.log("→ waiting for ha-panel-custom");
    await page.waitForFunction(
      () => {
        try {
          const ha = document.querySelector("home-assistant");
          const main = ha?.shadowRoot?.querySelector("home-assistant-main");
          const drawer = main?.shadowRoot?.querySelector("ha-drawer");
          const resolver = drawer?.querySelector("partial-panel-resolver");
          const custom = resolver?.querySelector("ha-panel-custom");
          return !!custom;
        } catch (_) { return false; }
      },
      { timeout: 15000 }
    );
    await new Promise((r) => setTimeout(r, 3000));

    // iframe is a light-DOM child of ha-panel-custom. Get its element handle.
    const iframeEl = await page.evaluateHandle(() => {
      const ha = document.querySelector("home-assistant");
      const main = ha?.shadowRoot?.querySelector("home-assistant-main");
      const drawer = main?.shadowRoot?.querySelector("ha-drawer");
      const resolver = drawer?.querySelector("partial-panel-resolver");
      const custom = resolver?.querySelector("ha-panel-custom");
      return custom?.querySelector("iframe") || null;
    });
    if (!iframeEl || (await iframeEl.evaluate((e) => e == null))) {
      throw new Error("Could not find panel iframe");
    }
    const frame = await iframeEl.contentFrame();
    if (!frame) throw new Error("contentFrame() returned null");
    console.log("→ iframe URL:", frame.url());

    // Wait for the custom element to upgrade
    await frame.waitForFunction(
      () => !!document.querySelector("complete-irrigation-panel"),
      { timeout: 15000 }
    );

    // Use shadowRoot to find sidebar items
    console.log("→ click 'Schedules' in sidebar");
    const navResult = await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      const btn = el.shadowRoot.querySelector(
        'button.sidebar-item[data-section="schedules"]'
      );
      if (!btn) return { ok: false, reason: "Schedules button not found" };
      btn.click();
      return { ok: true, current: el._currentSection };
    });
    console.log("  nav result:", navResult);

    await new Promise((r) => setTimeout(r, 500));

    console.log("→ click '+ Add Schedule'");
    const addResult = await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      const btn = el.shadowRoot.querySelector(
        'button[data-action="add-schedule"]'
      );
      if (!btn) {
        // Dump the rendered HTML in the schedules section
        const main = el.shadowRoot.querySelector("main");
        return {
          ok: false,
          reason: "Add Schedule button not in DOM",
          mainHtml: main ? main.innerHTML.slice(0, 1000) : "no main",
        };
      }
      btn.click();
      return {
        ok: true,
        modalOpen: el._scheduleModalOpen,
        modalRendered: !!el.shadowRoot.querySelector(".schedule-form"),
      };
    });
    console.log("  add result:", JSON.stringify(addResult, null, 2));

    await new Promise((r) => setTimeout(r, 500));

    // Final assertion: modal should be visible
    const final = await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      const modal = el.shadowRoot.querySelector(".modal");
      if (!modal) return { visible: false, reason: "no .modal element" };
      const rect = modal.getBoundingClientRect();
      const style = window.getComputedStyle(modal);
      return {
        visible: rect.width > 0 && rect.height > 0 && style.display !== "none",
        width: rect.width,
        height: rect.height,
        display: style.display,
        zIndex: style.zIndex,
      };
    });
    console.log("  modal state:", JSON.stringify(final, null, 2));

    // Close modal + test other nav transitions
    console.log("\n→ test navigation: schedule modal close → Notifications → Today");
    const navTransitions = await frame.evaluate(() => {
      const el = document.querySelector("complete-irrigation-panel");
      // Close modal
      const cancelBtn = el.shadowRoot.querySelector(".modal-cancel");
      cancelBtn?.click();
      const modalGoneAfterCancel = !el.shadowRoot.querySelector(".modal");

      // Click Notifications
      const notif = el.shadowRoot.querySelector('button[data-section="notifications"]');
      notif?.click();
      const onNotif = el._currentSection;

      // Click Today
      const today = el.shadowRoot.querySelector('button[data-section="today"]');
      today?.click();
      const onToday = el._currentSection;

      return { modalGoneAfterCancel, afterNotifClick: onNotif, afterTodayClick: onToday };
    });
    console.log("  ", JSON.stringify(navTransitions, null, 2));

    // Take a screenshot for visual confirmation
    await page.screenshot({ path: "/tmp/panel-add-schedule.png", fullPage: true });
    console.log("→ screenshot saved: /tmp/panel-add-schedule.png");

    // Output any console errors collected
    if (consoleMessages.length > 0) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of consoleMessages) console.log(`  ${m}`);
    }

    // Exit code: 0 if modal is visible, 1 otherwise
    if (final.visible) {
      console.log("\n✓ Add Schedule modal IS visible");
      process.exitCode = 0;
    } else {
      console.log("\n✗ Add Schedule modal is NOT visible");
      process.exitCode = 1;
    }
  } catch (err) {
    console.error("test failed:", err.message);
    process.exitCode = 2;
  } finally {
    await browser.close();
  }
}

main();
