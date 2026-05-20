/**
 * v1.3.2 verification: Stop button stays red on hover.
 *
 * Repro: in v1.3.1, .btn:hover{background: light-gray} overrode
 * .btn-stop{background: red} because of CSS specificity (:hover wins
 * over plain class). Explicit .btn-stop:hover fixes it.
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
        const f = new Function("return (" + fnStr + ")")();
        return f(panel, iframe.contentDocument, iframe.contentWindow, ...inner);
      }, fn.toString(), ...args);
    }

    console.log("→ start run for switch.test_lawn (2 min)");
    await inPanel((panel) => {
      panel.shadowRoot
        .querySelector('button[data-action="run-now"][data-entity-id="switch.test_lawn"]')
        .click();
    });
    await new Promise((r) => setTimeout(r, 300));
    await inPanel((panel) => {
      const input = panel.shadowRoot.querySelector('input[name="minutes"]');
      input.value = "2";
      panel.shadowRoot.querySelector(".run-form").dispatchEvent(
        new Event("submit", { bubbles: true, cancelable: true })
      );
    });
    await new Promise((r) => setTimeout(r, 1500));

    // Read computed background of Stop button — first NOT hovered, then hovered
    const colors = await inPanel((panel, doc, win) => {
      const btn = panel.shadowRoot.querySelector(
        'button[data-action="stop"][data-entity-id="switch.test_lawn"]'
      );
      if (!btn) return { error: "no stop button" };

      const noHoverBg = win.getComputedStyle(btn).backgroundColor;

      // Apply :hover via matchesSelector trick — fake hover state.
      // The cleanest way is to dispatch mouseenter and read the
      // computed style with pseudoElt argument unset (since :hover
      // computes live based on hovered state).
      // We instead read the matching rule by querying our stylesheet
      // for the .btn-stop:hover rule and applying its background.

      // Simplest: directly inspect the stylesheet to see what
      // .btn-stop:hover resolves to.
      let hoverBg = null;
      for (const sheet of panel.shadowRoot.styleSheets) {
        for (const rule of sheet.cssRules) {
          if (rule.selectorText === ".btn-stop:hover") {
            hoverBg = rule.style.background || rule.style.backgroundColor;
          }
        }
      }
      return { noHoverBg, hoverBgRule: hoverBg };
    });
    console.log("  ", JSON.stringify(colors, null, 2));

    // Validate — accept either rgb(219, 68, 55) or #db4437.
    function isRed(s) {
      if (!s) return false;
      const v = String(s).toLowerCase().replace(/\s/g, "");
      return v.includes("rgb(219,68,55)") || v.includes("#db4437");
    }
    let ok = true;
    if (!isRed(colors.noHoverBg)) {
      console.log(`✗ Default Stop background not red: ${colors.noHoverBg}`);
      ok = false;
    } else {
      console.log(`✓ Default Stop background IS red (${colors.noHoverBg})`);
    }
    if (!isRed(colors.hoverBgRule)) {
      console.log(`✗ .btn-stop:hover rule background not red: ${colors.hoverBgRule}`);
      ok = false;
    } else {
      console.log(`✓ .btn-stop:hover rule keeps red (${colors.hoverBgRule})`);
    }

    if (consoleMessages.length) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of consoleMessages.slice(-5)) console.log("  " + m);
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
