/**
 * v1.6.1 — Verify dark theme text is actually readable.
 *
 * Forces dark theme via localStorage, loads the panel, then walks
 * every section and checks that text on dark backgrounds has enough
 * luminance contrast to be visible. Fails fast on any "dark text on
 * dark bg" combo.
 */
const puppeteer = require("puppeteer");
const fs = require("fs");

const HA_URL = process.env.HA_URL || "http://localhost:8123";

async function loadPanelDark(page) {
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
  // Force dark theme by setting the panel's internal _theme + re-rendering.
  // (Avoids a page reload which would invalidate our iframe handle.)
  const frame = await getFrame(page);
  await frame.evaluate(() => {
    const el = document.querySelector("complete-irrigation-panel");
    el._theme = "dark";
    el._renderNow();
  });
  await new Promise((r) => setTimeout(r, 400));
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
    await loadPanelDark(page);

    // Sanity: data-theme must be "dark" on the host
    let frame = await getFrame(page);
    const theme = await frame.evaluate(
      () => document.querySelector("complete-irrigation-panel").getAttribute("data-theme")
    );
    if (theme !== "dark") fail(`expected data-theme="dark", got "${theme}"`);
    else pass(`dark theme active`);

    // Helper: compute relative luminance of an rgb(r,g,b) string
    const checkContrast = async (frame, selectors) => {
      return await frame.evaluate((sels) => {
        const root = document.querySelector("complete-irrigation-panel").shadowRoot;
        const parseRgba = (s) => {
          const m = s.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?/);
          return m ? [+m[1], +m[2], +m[3], m[4] != null ? +m[4] : 1] : null;
        };
        const parseRgb = (s) => {
          const v = parseRgba(s);
          return v ? [v[0], v[1], v[2]] : null;
        };
        const lum = ([r, g, b]) => {
          const f = (c) => {
            c /= 255;
            return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
          };
          return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
        };
        const contrast = (a, b) => {
          const lA = lum(a), lB = lum(b);
          const [hi, lo] = lA > lB ? [lA, lB] : [lB, lA];
          return (hi + 0.05) / (lo + 0.05);
        };
        // Walk up the parent chain compositing partially-transparent
        // backgrounds against the next opaque one we find. (Without
        // this, a rgba(...,0.12) tint reads as "solid bright blue"
        // even though it visually shows as a faint tint over the
        // dark card behind it.)
        const composite = (top, bottom) => {
          const a = top[3];
          return [
            Math.round(top[0] * a + bottom[0] * (1 - a)),
            Math.round(top[1] * a + bottom[1] * (1 - a)),
            Math.round(top[2] * a + bottom[2] * (1 - a)),
          ];
        };
        const bgOf = (el) => {
          let cur = el;
          let stack = [];
          while (cur && cur !== root) {
            const cs = getComputedStyle(cur);
            const c = parseRgba(cs.backgroundColor || "");
            if (c && c[3] > 0) {
              stack.push(c);
              if (c[3] >= 1) break;
            }
            cur = cur.parentElement;
          }
          // Bottom layer = host background
          const host = document.querySelector("complete-irrigation-panel");
          const hostBg = parseRgb(getComputedStyle(host).backgroundColor) || [255, 255, 255];
          let result = hostBg;
          for (let i = stack.length - 1; i >= 0; i--) {
            result = composite(stack[i], result);
          }
          return result;
        };
        const out = [];
        for (const sel of sels) {
          const el = root.querySelector(sel);
          if (!el) {
            out.push({ sel, ok: false, reason: "not found" });
            continue;
          }
          const cs = getComputedStyle(el);
          const fg = parseRgb(cs.color);
          const bg = bgOf(el);
          if (!fg || !bg) {
            out.push({ sel, ok: false, reason: "no color" });
            continue;
          }
          const c = contrast(fg, bg);
          out.push({
            sel,
            fg: fg.join(","),
            bg: bg.join(","),
            ratio: c.toFixed(2),
            // WCAG AA for normal text = 4.5; for body text aim for 4.0+ to be safe
            ok: c >= 4.0,
          });
        }
        return out;
      }, selectors);
    };

    // ── Today section ─────────────────────────────────────────
    console.log("\n→ Today");
    frame = await getFrame(page);
    const todaySels = [
      ".page-header h2",
      ".zone-tile h4",
      ".status-text",
      ".section-title",
      ".version-pill",
    ];
    for (const r of await checkContrast(frame, todaySels)) {
      if (!r.ok) fail(`${r.sel}: fg=rgb(${r.fg}) on bg=rgb(${r.bg}) → ratio ${r.ratio}`);
      else pass(`${r.sel}: ${r.ratio}:1`);
    }

    // ── Schedules ─────────────────────────────────────────────
    console.log("\n→ Schedules");
    await nav(page, "schedules");
    frame = await getFrame(page);
    for (const r of await checkContrast(frame, [".page-header h2", ".schedule-name", ".schedule-meta"])) {
      if (!r.ok) fail(`${r.sel}: ratio ${r.ratio} on bg=rgb(${r.bg})`);
      else pass(`${r.sel}: ${r.ratio}:1`);
    }

    // ── Zones ─────────────────────────────────────────────────
    console.log("\n→ Zones");
    await nav(page, "zones");
    frame = await getFrame(page);
    for (const r of await checkContrast(frame, [".zone-row-name", ".zone-row-meta", ".zone-day-label", ".zone-day-date"])) {
      if (!r.ok) fail(`${r.sel}: ratio ${r.ratio} fg=rgb(${r.fg}) on bg=rgb(${r.bg})`);
      else pass(`${r.sel}: ${r.ratio}:1`);
    }

    // ── Sensors ───────────────────────────────────────────────
    console.log("\n→ Sensors");
    await nav(page, "sensors");
    frame = await getFrame(page);
    for (const r of await checkContrast(frame, [".sensor-zone-head h4", ".sensor-zone-eid", ".sensor-label"])) {
      if (!r.ok) fail(`${r.sel}: ratio ${r.ratio}`);
      else pass(`${r.sel}: ${r.ratio}:1`);
    }

    // ── Weather ───────────────────────────────────────────────
    console.log("\n→ Weather");
    await nav(page, "weather");
    frame = await getFrame(page);
    for (const r of await checkContrast(frame, [".page-header h2", ".weather-form label", ".section-title"])) {
      if (!r.ok) fail(`${r.sel}: ratio ${r.ratio}`);
      else pass(`${r.sel}: ${r.ratio}:1`);
    }

    // ── Notifications ─────────────────────────────────────────
    console.log("\n→ Notifications");
    await nav(page, "notifications");
    frame = await getFrame(page);
    for (const r of await checkContrast(frame, [".page-header h2", ".weather-form label", ".enabled-check"])) {
      if (!r.ok) fail(`${r.sel}: ratio ${r.ratio}`);
      else pass(`${r.sel}: ${r.ratio}:1`);
    }

    // ── Settings ──────────────────────────────────────────────
    console.log("\n→ Settings");
    await nav(page, "settings");
    frame = await getFrame(page);
    for (const r of await checkContrast(frame, [".settings-card h3", ".settings-table td", ".section-hint"])) {
      if (!r.ok) fail(`${r.sel}: ratio ${r.ratio}`);
      else pass(`${r.sel}: ${r.ratio}:1`);
    }

    await page.screenshot({ path: "/tmp/panel-v161-dark.png", fullPage: true });
    console.log("\n→ screenshot: /tmp/panel-v161-dark.png");
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
  console.log(fails === 0 ? "\n✓ ALL DARK CONTRAST CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
