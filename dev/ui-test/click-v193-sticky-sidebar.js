/**
 * v1.9.3 — Verify the sidebar stays put when <main> scrolls.
 *
 * Loads the panel at a short viewport (forces vertical overflow in main),
 * captures the sidebar's bounding rect, scrolls main programmatically,
 * captures again, and asserts the sidebar's top didn't move.
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

async function main() {
  const browser = await puppeteer.launch({
    headless: "new",
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  });
  const page = await browser.newPage();
  // Force a short viewport so the Today page overflows into a scroll.
  await page.setViewport({ width: 1100, height: 500 });
  const msgs = [];
  page.on("console", (m) => msgs.push(`[${m.type()}] ${m.text()}`));
  page.on("pageerror", (e) => msgs.push(`[pageerror] ${e.message}`));

  let fails = 0;
  const fail = (m) => { console.log(`  ✗ ${m}`); fails++; };
  const pass = (m) => console.log(`  ✓ ${m}`);

  try {
    await loadPanel(page);
    const frame = await getFrame(page);

    // ── 1. Sidebar height matches viewport, main has overflow:auto ──
    console.log("\n→ Layout sanity");
    const layout = await frame.evaluate(() => {
      const root = document.querySelector("complete-irrigation-panel");
      const sidebar = root.shadowRoot.querySelector(".sidebar");
      const mainEl = root.shadowRoot.querySelector("main");
      const sbRect = sidebar.getBoundingClientRect();
      const mainStyle = getComputedStyle(mainEl);
      return {
        sidebarHeight: sbRect.height,
        sidebarTop: sbRect.top,
        viewportHeight: window.innerHeight,
        mainOverflowY: mainStyle.overflowY,
        mainScrollHeight: mainEl.scrollHeight,
        mainClientHeight: mainEl.clientHeight,
      };
    });
    console.log("  layout:", JSON.stringify(layout));
    if (layout.sidebarHeight < layout.viewportHeight - 4)
      fail(`sidebar height (${layout.sidebarHeight}) shorter than viewport (${layout.viewportHeight})`);
    else pass(`sidebar fills viewport (${Math.round(layout.sidebarHeight)}px ≈ ${layout.viewportHeight}px)`);
    if (layout.mainOverflowY !== "auto" && layout.mainOverflowY !== "scroll")
      fail(`main overflow-y is "${layout.mainOverflowY}", expected auto/scroll`);
    else pass(`main has overflow-y: ${layout.mainOverflowY}`);
    if (layout.mainScrollHeight <= layout.mainClientHeight)
      fail(
        `main content doesn't overflow (scroll ${layout.mainScrollHeight} ≤ client ${layout.mainClientHeight}) — test needs more content`
      );
    else
      pass(
        `main has scrollable overflow (${layout.mainScrollHeight}px > ${layout.mainClientHeight}px)`
      );

    // ── 2. Scroll <main>, sidebar shouldn't budge ──
    console.log("\n→ Scroll main, verify sidebar.top unchanged");
    const before = await frame.evaluate(() => {
      const sb = document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector(".sidebar");
      return sb.getBoundingClientRect().top;
    });
    await frame.evaluate(() => {
      const m = document.querySelector("complete-irrigation-panel").shadowRoot.querySelector("main");
      m.scrollTop = 300;
    });
    await new Promise((r) => setTimeout(r, 200));
    const after = await frame.evaluate(() => {
      const sb = document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector(".sidebar");
      const m = document.querySelector("complete-irrigation-panel").shadowRoot.querySelector("main");
      return { sidebarTop: sb.getBoundingClientRect().top, mainScrollTop: m.scrollTop };
    });
    console.log(`  before sidebar.top=${before}, after sidebar.top=${after.sidebarTop}, main.scrollTop=${after.mainScrollTop}`);
    if (after.mainScrollTop < 5) {
      fail(`main didn't actually scroll (scrollTop=${after.mainScrollTop}) — can't verify sidebar`);
    } else if (Math.abs(before - after.sidebarTop) > 1) {
      fail(`sidebar moved ${after.sidebarTop - before}px — expected to stay put`);
    } else {
      pass(`sidebar.top stayed at ${before}px while main scrolled ${after.mainScrollTop}px`);
    }

    // ── 3. Body itself should NOT have scrolled (no double scroll) ──
    const bodyScroll = await frame.evaluate(() => ({
      scrollY: window.scrollY,
      docScrollTop: document.documentElement.scrollTop,
      bodyScrollHeight: document.body.scrollHeight,
      bodyClientHeight: document.body.clientHeight,
    }));
    console.log("  body scroll state:", JSON.stringify(bodyScroll));
    if (bodyScroll.scrollY !== 0 || bodyScroll.docScrollTop !== 0)
      fail(
        `body scrolled (scrollY=${bodyScroll.scrollY}, docTop=${bodyScroll.docScrollTop}) — only <main> should scroll`
      );
    else pass("body did not scroll — only <main> handles vertical overflow");

    await page.screenshot({ path: "/tmp/panel-v193-sticky.png", fullPage: false });
  } catch (err) {
    console.error("test failed:", err.message);
    fails++;
  } finally {
    if (msgs.length > 0) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of msgs.slice(0, 5)) console.log(`  ${m}`);
    }
    await browser.close();
  }
  console.log(fails === 0 ? "\n✓ ALL v1.9.3 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
