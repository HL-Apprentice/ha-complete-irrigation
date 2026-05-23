/**
 * v1.12.1 — schedule modal must NOT render <input type=time> anymore
 * (it crashes WKWebView in the macOS HA app). Replaced with two
 * <input type=number> for hours + minutes.
 *
 *  1. Open New Schedule modal — confirm no <input type=time>; two
 *     number inputs named start_time_h + start_time_m exist instead.
 *  2. Fill h=7, m=45, save → schedule persists with start_time "07:45".
 *  3. Edit the saved schedule → number inputs show 7 and 45.
 */
const puppeteer = require("puppeteer");
const fs = require("fs");
const WebSocket = require("ws");

const HA_URL = process.env.HA_URL || "http://localhost:8123";

async function listSchedules(token) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket("ws://localhost:8123/api/websocket");
    let mid = 1;
    ws.on("message", (raw) => {
      const m = JSON.parse(raw.toString());
      if (m.type === "auth_required")
        ws.send(JSON.stringify({ type: "auth", access_token: token }));
      else if (m.type === "auth_ok")
        ws.send(JSON.stringify({ id: mid++, type: "complete_irrigation/list_schedules" }));
      else if (m.type === "result") {
        ws.close();
        if (m.success) resolve(m.result.schedules);
        else reject(new Error(JSON.stringify(m)));
      }
    });
  });
}

async function deleteSched(token, id) {
  return new Promise((resolve) => {
    const ws = new WebSocket("ws://localhost:8123/api/websocket");
    let mid = 1;
    ws.on("message", (raw) => {
      const m = JSON.parse(raw.toString());
      if (m.type === "auth_required")
        ws.send(JSON.stringify({ type: "auth", access_token: token }));
      else if (m.type === "auth_ok")
        ws.send(
          JSON.stringify({
            id: mid++,
            type: "call_service",
            domain: "complete_irrigation",
            service: "delete_schedule",
            service_data: { schedule_id: id },
          })
        );
      else if (m.type === "result") {
        ws.close();
        resolve();
      }
    });
  });
}

async function cleanup(token) {
  try {
    const scheds = await listSchedules(token);
    for (const s of scheds) {
      if ((s.name || "").startsWith("TEST v1121")) await deleteSched(token, s.id);
    }
  } catch (_) {}
}

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
  await new Promise((r) => setTimeout(r, 400));
}

async function main() {
  const token = fs.readFileSync("/tmp/ha_test_token.txt", "utf8").trim();
  await cleanup(token);

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
    await nav(page, "schedules");
    let frame = await getFrame(page);

    // ── 1. Modal has no <input type=time>, has the two number inputs ──
    console.log("\n→ No <input type=time> in modal");
    await frame.evaluate(() =>
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('[data-action="add-schedule"]')
        .click()
    );
    await new Promise((r) => setTimeout(r, 400));
    frame = await getFrame(page);
    const inputs = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return {
        timeInputs: r.querySelectorAll('input[type="time"]').length,
        startH: !!r.querySelector('input[name="start_time_h"]'),
        startM: !!r.querySelector('input[name="start_time_m"]'),
      };
    });
    if (inputs.timeInputs > 0)
      fail(`${inputs.timeInputs} <input type=time> still in modal (crashes WKWebView)`);
    else pass("0 native time inputs (WKWebView safe)");
    if (!inputs.startH || !inputs.startM) fail("start_time_h/m number inputs missing");
    else pass("start_time_h + start_time_m number inputs present");

    // ── 2. Fill 7:45, save → persists ──
    console.log("\n→ Fill h=7, m=45 → save → persists as '07:45'");
    await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      r.querySelector('input[name="name"]').value = "TEST v1121 745am";
      r.querySelector('input[name="name"]').dispatchEvent(
        new Event("input", { bubbles: true })
      );
      const h = r.querySelector('input[name="start_time_h"]');
      h.value = "7";
      h.dispatchEvent(new Event("input", { bubbles: true }));
      const m = r.querySelector('input[name="start_time_m"]');
      m.value = "45";
      m.dispatchEvent(new Event("input", { bubbles: true }));
      r.querySelector(".schedule-form").requestSubmit();
    });
    await new Promise((r) => setTimeout(r, 1500));

    const scheds = await listSchedules(token);
    const sched = scheds.find((s) => s.name === "TEST v1121 745am");
    if (!sched) fail("schedule didn't persist");
    else if (sched.start_time !== "07:45")
      fail(`expected start_time '07:45', got '${sched.start_time}'`);
    else pass(`schedule persisted with start_time '${sched.start_time}'`);

    // ── 3. Re-open in edit modal → inputs show 7 and 45 ──
    if (sched) {
      console.log("\n→ Edit modal shows h=7, m=45");
      frame = await getFrame(page);
      const opened = await frame.evaluate(() => {
        const r = document.querySelector("complete-irrigation-panel").shadowRoot;
        const rows = r.querySelectorAll(".schedule-row");
        for (const row of rows) {
          if (row.textContent.includes("TEST v1121 745am")) {
            const btn = row.querySelector('[data-action="edit-schedule"]');
            btn?.click();
            return !!btn;
          }
        }
        return false;
      });
      if (!opened) fail("couldn't find edit button");
      else {
        await new Promise((r) => setTimeout(r, 400));
        frame = await getFrame(page);
        const vals = await frame.evaluate(() => {
          const r = document.querySelector("complete-irrigation-panel").shadowRoot;
          return {
            h: r.querySelector('input[name="start_time_h"]')?.value,
            m: r.querySelector('input[name="start_time_m"]')?.value,
          };
        });
        if (vals.h !== "7" || vals.m !== "45")
          fail(`edit inputs wrong: h=${vals.h}, m=${vals.m}`);
        else pass("edit modal pre-fills h=7, m=45 from start_time");
      }
    }
  } catch (err) {
    console.error("test failed:", err.message);
    fails++;
  } finally {
    await cleanup(token).catch(() => {});
    if (msgs.length > 0) {
      console.log("\n=== BROWSER CONSOLE ===");
      for (const m of msgs.slice(0, 4)) console.log(`  ${m}`);
    }
    await browser.close();
  }
  console.log(fails === 0 ? "\n✓ ALL v1.12.1 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
