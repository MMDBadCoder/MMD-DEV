/* The storage tab.
 *
 * Mostly about the two things a distribution view gets wrong: parts that do not
 * sum to the whole, and colour asked to carry meaning it cannot.
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");
// The bands come from their own module, which touches no DOM - that is why
// they can be tested directly rather than through a page.
const { band, WARN_PERCENT } = await import(path.join(ROOT, "web/js/disk.js"));
const SRC = read("web/js/pages/adminstorage.js");
const CSS = read("web/app.css");
const APP = read("control/mmd/app.py");

test("one definition of the bands, used by both admin pages", () => {
  for (const f of ["web/js/pages/adminstorage.js", "web/js/pages/adminusers.js"]) {
    assert.ok(read(f).includes('from "../disk.js"'), `${f} has its own copy`);
  }
  assert.equal(WARN_PERCENT, 85, "drifted from CONFIG.disk_warn_percent");
});

test("the bands match the thresholds the worker acts on", () => {
  // A panel showing green while the worker is raising an alarm would be worse
  // than showing nothing.
  assert.equal(band(10).key, "ok");
  assert.equal(band(60).key, "mid");
  assert.equal(band(85).key, "warn");
  assert.equal(band(100).key, "bad");
  assert.equal(band(140).key, "bad");
});

test("the two states colour cannot separate carry an icon as well", () => {
  // validate_palette.js scores this system's warn (#b26a00) against its bad
  // (#d13438) at DeltaE 12.8 normal / 3.3 deutan - both under the floor. So
  // warn and bad must not be distinguishable by hue alone.
  assert.ok(band(90).ic, "the warn band has no icon");
  assert.ok(band(120).ic, "the critical band has no icon");
  assert.ok(CSS.includes(".diskchip svg"), "the chip has no icon styling");
});

test("every chip prints the number, so identity is never colour alone", () => {
  assert.ok(/fmtFa\(pct\)\}٪/.test(SRC), "the percentage is not on the chip");
});

test("the pool's parts are accounted for, not just the workspaces", () => {
  // A distribution whose segments silently fail to sum to the whole misleads.
  // The pool also holds the golden images every workspace is cloned from.
  assert.ok(APP.includes('"unattributed_gib"'),
            "platform data is not reported, so the segments cannot sum");
  assert.ok(SRC.includes("unattributed_gib"), "the chart ignores platform data");
  assert.ok(SRC.includes("seg.free"), "free space is not in the key");
});

test("the bar chart is one series in one hue", () => {
  // Every bar means GiB written, so hue carries no information; a second
  // categorical colour would imply a distinction that does not exist.
  assert.ok(CSS.includes(".distbar {"), "no bar mark defined");
  const bar = CSS.slice(CSS.indexOf(".distbar {"), CSS.indexOf(".distval"));
  assert.ok(bar.includes("background: var(--brand)"), "the base bar is not one hue");
});

test("segments are separated so two fills never read as one", () => {
  const pool = CSS.slice(CSS.indexOf(".poolbar {"), CSS.indexOf(".poolkey"));
  assert.ok(/gap:\s*2px/.test(pool), "no surface gap between stacked segments");
});

test("every row prints its own value, so nothing lives in geometry alone", () => {
  // There is no separate table: the distribution rows ARE the table. That is
  // only true while each row carries the owner, the figure and the percentage
  // next to its bar - a bar with the number removed would put the data back
  // into length alone.
  const row = SRC.slice(SRC.indexOf("const bars ="), SRC.indexOf('.join("")'));
  assert.ok(row.includes("distname"), "rows do not name their owner");
  assert.ok(row.includes("distval"), "rows do not print the figure");
  assert.ok(row.includes("chip("), "rows do not print the percentage");
});

test("overcommitment is stated rather than left to be inferred", () => {
  assert.ok(SRC.includes("adm.storage.overcommit"));
  assert.ok(SRC.includes("adm.storage.explain"),
            "the page shows the ratio without explaining what protects it");
});
