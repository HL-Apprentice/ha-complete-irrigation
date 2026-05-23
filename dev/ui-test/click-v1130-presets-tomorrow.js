/**
 * v1.13.0 — three asks:
 *   1. Weekdays mode has shortcut buttons (Every day / Weekdays / Weekends).
 *   2. Today screen has a "Tomorrow's runs" vertical list below the timeline.
 *   3. Click a today-pill OR tomorrow-row → opens schedule edit modal.
 */
const puppeteer = require("puppeteer");
const fs = require("fs");
const WebSocket = require("ws");

const HA_URL = process.env.HA_URL || "http://localhost:8123";

async function addSchedule(token, name, weekdays = [0, 1, 2, 3, 4, 5, 6]) {
  return new Promise((resolve, reject) => {
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
            service: "add_schedule",
            service_data: {
              name,
              zone_entity_id: "switch.test_lawn",
              start_time: "06:00",
              duration_minutes: 15,
              weekdays,
            },
          })
        );
      else if (m.type === "result") {
        ws.close();
        if (m.success) resolve();
        else reject(new Error(JSON.stringify(m)));
      }
    });
  });
}

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
      if ((s.name || "").startsWith("TEST v1130")) await deleteSched(token, s.id);
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
  // Pre-seed an everyday schedule so tomorrow has runs
  await addSchedule(token, "TEST v1130 daily");

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

    // ── 1. Weekday preset shortcuts inside the schedule modal ──
    console.log("\n→ Weekday preset shortcuts");
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
    const shortcutBtns = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return Array.from(
        r.querySelectorAll('[data-action="weekday-preset"]')
      ).map((b) => b.dataset.preset);
    });
    if (shortcutBtns.length !== 3) fail(`expected 3 preset buttons, got ${shortcutBtns.length}`);
    else pass(`3 preset buttons present: ${shortcutBtns.join(", ")}`);

    // Click "weekends" → only Sat + Sun should be checked
    await frame.evaluate(() =>
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('[data-action="weekday-preset"][data-preset="weekends"]')
        .click()
    );
    await new Promise((r) => setTimeout(r, 300));
    frame = await getFrame(page);
    const weekendsChecks = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return Array.from(r.querySelectorAll('input[name="weekday"]'))
        .filter((cb) => cb.checked)
        .map((cb) => parseInt(cb.value, 10))
        .sort();
    });
    if (JSON.stringify(weekendsChecks) !== JSON.stringify([5, 6]))
      fail(`Weekends-only preset gave ${weekendsChecks.join(",")} (expected [5,6])`);
    else pass("'Weekends only' preset picks Sat + Sun");

    // Click "all" → all 7 checked
    await frame.evaluate(() =>
      document
        .querySelector("complete-irrigation-panel")
        .shadowRoot.querySelector('[data-action="weekday-preset"][data-preset="all"]')
        .click()
    );
    await new Promise((r) => setTimeout(r, 300));
    frame = await getFrame(page);
    const allChecks = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      return Array.from(r.querySelectorAll('input[name="weekday"]:checked')).length;
    });
    if (allChecks !== 7) fail(`'Every day' should check all 7; got ${allChecks}`);
    else pass("'Every day' preset checks all 7");
    await frame.evaluate(() =>
      document.querySelector("complete-irrigation-panel").shadowRoot.querySelector(".modal-cancel").click()
    );

    // ── 2. Tomorrow's runs list on Today screen ──
    console.log("\n→ Tomorrow's runs list");
    await nav(page, "today");
    frame = await getFrame(page);
    const tom = await frame.evaluate(() => {
      const r = document.querySelector("complete-irrigation-panel").shadowRoot;
      const section = r.querySelector(".tomorrow-list");
      if (!section) return { present: false };
      const heading = section.querySelector(".section-title")?.textContent || "";
      const rows = section.querySelectorAll(".tomorrow-row").length;
      return { present: true, heading, rows };
    });
    if (!tom.present) fail("tomorrow-list section missing");
    else pass(`tomorrow-list present, heading: "${tom.heading}", ${tom.rows} row(s)`);
    if (tom.rows < 1) fail("expected at least 1 tomorrow row (we pre-seeded a daily schedule)");
    else pass(`${tom.rows} tomorrow row(s) rendered`);

    // ── 3. Click a tomorrow row → opens schedule edit modal ──
    if (tom.rows > 0) {
      console.log("\n→ Click tomorrow row opens edit modal");
      // Click the row that matches our TEST schedule by its title-attr
      const clicked = await frame.evaluate(() => {
        const r = document.querySelector("complete-irrigation-panel").shadowRoot;
        const rows = r.querySelectorAll(".tomorrow-row");
        for (const row of rows) {
          if ((row.getAttribute("title") || "").includes("TEST v1130 daily")) {
            row.click();
            return true;
          }
        }
        return false;
      });
      if (!clicked) fail("couldn't find a tomorrow-row for TEST v1130 daily");
      else {
        await new Promise((r) => setTimeout(r, 600));
        frame = await getFrame(page);
        const modal = await frame.evaluate(() => {
          const r = document.querySelector("complete-irrigation-panel").shadowRoot;
          return {
            form: !!r.querySelector(".schedule-form"),
            nameInput: r.querySelector('input[name="name"]')?.value || "",
            section: document.querySelector("complete-irrigation-panel")._currentSection,
          };
        });
        if (!modal.form) fail("clicking tomorrow row didn't open schedule modal");
        else pass(`schedule edit modal opened (current section: ${modal.section})`);
        if (!modal.nameInput.startsWith("TEST v1130"))
          fail(`wrong schedule loaded: "${modal.nameInput}"`);
        else pass(`right schedule loaded: "${modal.nameInput}"`);
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
  console.log(fails === 0 ? "\n✓ ALL v1.13.0 CHECKS PASSED" : `\n✗ ${fails} FAILURE(S)`);
  process.exitCode = fails === 0 ? 0 : 1;
}

main();
