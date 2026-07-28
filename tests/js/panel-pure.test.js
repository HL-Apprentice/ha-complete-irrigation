// Unit tests for the panel's PURE helpers (v1.62).
//
// Until now the 11k-line panel had no automated verification of any kind — not
// even a syntax check in check.sh or CI — which is where several shipped
// regressions landed (v1.60.4's total render crash, the counter-scale/label
// rasterisation bugs). These cover the functions that are pure enough to test
// without a browser: escaping, geometry, EXIF decode, i18n substitution.
//
// Uses ONLY node:test + node:assert, so it adds no dependency to the project.
// Run: node --test tests/js/

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

// The panel is a browser file: it declares `class ... extends HTMLElement` at
// load time and registers a custom element. Shim the two globals it touches so
// the module can be required under Node; the panel itself guards
// customElements, so leaving it undefined skips registration.
global.HTMLElement = class {};

const panel = require(
  path.join(
    __dirname,
    "..",
    "..",
    "custom_components",
    "complete_irrigation",
    "frontend",
    "complete-irrigation-panel.js"
  )
);

// ── escaping — the XSS boundary for every string the panel renders ─────

test("escapeHtml neutralises the characters that break out of text", () => {
  assert.strictEqual(
    panel.escapeHtml('<script>alert("x")</script>'),
    "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"
  );
  assert.strictEqual(panel.escapeHtml("a & b"), "a &amp; b");
});

test("escapeAttr neutralises quote-escapes out of an attribute", () => {
  const out = panel.escapeAttr('" onerror="alert(1)');
  assert.ok(!/[^&]"/.test(out), `unescaped quote survived: ${out}`);
});

test("escaping is null/undefined safe (a missing plant name must not throw)", () => {
  for (const v of [null, undefined, 0, false]) {
    assert.doesNotThrow(() => panel.escapeHtml(v));
    assert.doesNotThrow(() => panel.escapeAttr(v));
  }
});

test("cssEscape output is safe to interpolate into a selector", () => {
  assert.doesNotThrow(() => panel.cssEscape('switch.a"b'));
  assert.ok(typeof panel.cssEscape("switch.zone_1") === "string");
});

// ── rotation geometry — must mirror the tested Python exactly ──────────

test("ciNormalizeRotation wraps to (-180, 180] instead of clamping", () => {
  assert.strictEqual(panel.ciNormalizeRotation(0), 0);
  assert.strictEqual(panel.ciNormalizeRotation(12), 12);
  assert.strictEqual(panel.ciNormalizeRotation(180), 180);
  assert.strictEqual(panel.ciNormalizeRotation(181), -179);
  assert.strictEqual(panel.ciNormalizeRotation(360), 0);
  assert.strictEqual(panel.ciNormalizeRotation(365), 5);
  assert.strictEqual(panel.ciNormalizeRotation(-181), 179);
});

test("ciNormalizeRotation degrades junk to 0 rather than NaN-poisoning a transform", () => {
  for (const bad of [null, undefined, "abc", NaN, Infinity, -Infinity, {}]) {
    assert.strictEqual(panel.ciNormalizeRotation(bad), 0, `for ${String(bad)}`);
  }
});

test("ciNormalizeRotation never returns -0 (it would serialise as '-0')", () => {
  assert.ok(!Object.is(panel.ciNormalizeRotation(-0), -0));
  assert.ok(!Object.is(panel.ciNormalizeRotation(360), -0));
});

test("ciCoverScale: no rotation needs no upscale", () => {
  assert.strictEqual(panel.ciCoverScale(1536, 1285, 0), 1);
  assert.strictEqual(panel.ciCoverScale(100, 100, 0), 1);
});

test("ciCoverScale: a square at 45deg needs sqrt(2)", () => {
  assert.ok(Math.abs(panel.ciCoverScale(500, 500, 45) - Math.SQRT2) < 1e-12);
});

test("ciCoverScale covers the frame at every angle (no empty corners)", () => {
  // The property that matters: after rotating by deg and scaling by k, every
  // corner of the w x h frame is still inside the scaled, rotated image.
  for (const [w, h] of [
    [1536, 1285],
    [1024, 1024],
    [800, 1400],
  ]) {
    for (let deg = -180; deg <= 180; deg += 7) {
      const k = panel.ciCoverScale(w, h, deg);
      const r = (-deg * Math.PI) / 180;
      const cos = Math.cos(r);
      const sin = Math.sin(r);
      for (const [cx, cy] of [
        [-w / 2, -h / 2],
        [w / 2, -h / 2],
        [-w / 2, h / 2],
        [w / 2, h / 2],
      ]) {
        const ix = cx * cos - cy * sin;
        const iy = cx * sin + cy * cos;
        assert.ok(Math.abs(ix) <= (k * w) / 2 + 1e-6, `x ${w}x${h} @${deg}`);
        assert.ok(Math.abs(iy) <= (k * h) / 2 + 1e-6, `y ${w}x${h} @${deg}`);
      }
    }
  }
});

test("ciCoverScale is symmetric in direction and safe on degenerate sizes", () => {
  assert.strictEqual(panel.ciCoverScale(1536, 1285, 15), panel.ciCoverScale(1536, 1285, -15));
  for (const [w, h] of [
    [0, 100],
    [100, 0],
    [-5, 10],
    [NaN, 10],
  ]) {
    assert.strictEqual(panel.ciCoverScale(w, h, 30), 1, `${w}x${h}`);
  }
});

// ── EXIF GPS — hand-rolled endianness; a wrong decode misplaces a plant ─

function jpegWithGps({ big = false, latRef = "N", lonRef = "E" } = {}) {
  // Minimal APP1/TIFF/GPS-IFD JPEG: 1 deg 30 min 0 sec => 1.5 degrees.
  const head = Buffer.from([0xff, 0xd8, 0xff, 0xe1]);
  const parts = [];
  const tiff = Buffer.alloc(8);
  if (big) {
    tiff.write("MM", 0, "ascii");
    tiff.writeUInt16BE(42, 2);
    tiff.writeUInt32BE(8, 4);
  } else {
    tiff.write("II", 0, "ascii");
    tiff.writeUInt16LE(42, 2);
    tiff.writeUInt32LE(8, 4);
  }
  const u16 = (v) => {
    const b = Buffer.alloc(2);
    big ? b.writeUInt16BE(v) : b.writeUInt16LE(v);
    return b;
  };
  const u32 = (v) => {
    const b = Buffer.alloc(4);
    big ? b.writeUInt32BE(v) : b.writeUInt32LE(v);
    return b;
  };
  // IFD0: one entry pointing at the GPS IFD
  const ifd0 = Buffer.concat([u16(1), u16(0x8825), u16(4), u32(1), u32(26), u32(0)]);
  // GPS IFD: refs + two rationals (deg/min/sec each)
  const gpsEntries = [
    Buffer.concat([u16(1), u16(2), u32(2), Buffer.from(latRef + "\0" + "\0\0")]),
    Buffer.concat([u16(2), u16(5), u32(3), u32(86)]),
    Buffer.concat([u16(3), u16(2), u32(2), Buffer.from(lonRef + "\0" + "\0\0")]),
    Buffer.concat([u16(4), u16(5), u32(3), u32(110)]),
  ];
  const gpsIfd = Buffer.concat([u16(gpsEntries.length), ...gpsEntries, u32(0)]);
  const rats = Buffer.concat([
    u32(1), u32(1), u32(30), u32(1), u32(0), u32(1), // lat 1deg 30min 0sec
    u32(1), u32(1), u32(30), u32(1), u32(0), u32(1), // lon 1deg 30min 0sec
  ]);
  const body = Buffer.concat([Buffer.from("Exif\0\0"), tiff, ifd0, gpsIfd, rats]);
  const len = Buffer.alloc(2);
  len.writeUInt16BE(body.length + 2);
  parts.push(head, len, body);
  return Buffer.concat(parts);
}

test("exifGps returns null when there is no GPS data (must not fabricate a location)", () => {
  const plainJpeg = Buffer.from([0xff, 0xd8, 0xff, 0xdb, 0x00, 0x03, 0x00, 0x00, 0xff, 0xd9]);
  const out = panel.exifGps(
    plainJpeg.buffer.slice(plainJpeg.byteOffset, plainJpeg.byteOffset + plainJpeg.length)
  );
  assert.strictEqual(out, null);
});

test("exifGps never throws on truncated or garbage input", () => {
  for (const bytes of [[], [0xff], [0xff, 0xd8], [0xff, 0xd8, 0xff, 0xe1, 0x00, 0x02]]) {
    const b = Buffer.from(bytes);
    assert.doesNotThrow(() =>
      panel.exifGps(b.buffer.slice(b.byteOffset, b.byteOffset + b.length))
    );
  }
});

// ── i18n substitution ─────────────────────────────────────────────────

// NOTE: ciApplyPack's documented contract is "translated text, or NULL on a
// miss" — callers at :360 and :396 branch on null to keep the original. An
// earlier draft of this test asserted it returned the input unchanged; that was
// the test being wrong about the contract, not the code.
test("ciApplyPack returns null on a miss so the caller keeps the original", () => {
  const pack = { strings: { Hello: "Hallo" }, patterns: [] };
  assert.strictEqual(panel.ciApplyPack("Untouched", pack), null);
});

test("ciApplyPack substitutes an exact string", () => {
  const pack = { strings: { Hello: "Hallo" }, patterns: [] };
  assert.strictEqual(panel.ciApplyPack("Hello", pack), "Hallo");
});

test("ciApplyPack applies regex patterns cumulatively", () => {
  const pack = { strings: {}, patterns: [[/^Water (.+)$/, "Giesse $1"]] };
  assert.strictEqual(panel.ciApplyPack("Water Roses", pack), "Giesse Roses");
});

test("ciApplyPack survives a pack missing its keys (reachable via a partial pack)", () => {
  // A null pack is NOT reachable — callers only run with an active pack — but an
  // incomplete one is, and it must miss rather than throw.
  for (const partial of [{}, { strings: {} }, { patterns: [] }]) {
    assert.doesNotThrow(() => panel.ciApplyPack("text", partial));
    assert.strictEqual(panel.ciApplyPack("text", partial), null);
  }
});

// ── formatting ────────────────────────────────────────────────────────

test("_formatRemaining renders m:ss and never goes negative", () => {
  assert.strictEqual(panel._formatRemaining(0), "0:00");
  assert.strictEqual(panel._formatRemaining(65000), "1:05");
  assert.strictEqual(panel._formatRemaining(-5000), "0:00");
});

test("fmtTimeOfDay renders 12-hour time across the day boundary", () => {
  assert.strictEqual(panel.fmtTimeOfDay(0), "12:00 AM");
  assert.strictEqual(panel.fmtTimeOfDay(12 * 60), "12:00 PM");
  assert.strictEqual(panel.fmtTimeOfDay(13 * 60 + 5), "1:05 PM");
});

test("the panel reports a version (the badge users read when reporting bugs)", () => {
  assert.match(panel.PANEL_VERSION, /^v\d+\.\d+\.\d+$/);
});
