/* The storage tab, which is now a frame around one dashboard.
 *
 * The assertions moved with the content. What a distribution view has to get
 * right did not change - parts that sum to the whole, and colour never asked
 * to carry meaning alone - but those properties now belong to the dashboard
 * JSON rather than to hand-written markup, so that is where they are checked.
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");
// The bands come from their own module, which touches no DOM.
const { band, WARN_PERCENT } = await import(path.join(ROOT, "web/js/disk.js"));
const SRC = read("web/js/pages/adminstorage.js");
const APP = read("control/mmd/app.py");
const BOARD = JSON.parse(read("observability/grafana/dashboards/mmd-storage.json"));

const panel = (t) => BOARD.panels.find((p) => (p.title || "").includes(t));
const queries = BOARD.panels.flatMap((p) => (p.targets || []).map((x) => x.expr));

test("the page draws nothing itself", () => {
  // A number shown in two places is a number that can disagree with itself,
  // and only one of the two had a time axis.
  assert.ok(SRC.includes('dashboardCard(gf, "storage")'), "no embedded dashboard");
  assert.doesNotMatch(SRC, /distbar|poolbar|const bars =/,
                      "the page is still drawing its own chart");
  assert.doesNotMatch(SRC, /get\("\/api\/admin\/storage"\)/,
                      "the page still fetches figures it does not display");
});

test("the pool's parts are accounted for, not just the workspaces", () => {
  // Customer machines are only part of what fills the pool; images, snapshots
  // and overhead are the rest. A view that omits them makes the pool look
  // emptier than it is.
  const p = panel("Where the pool has gone");
  assert.ok(p, "no pool breakdown panel");
  const joined = (p.targets || []).map((t) => t.expr).join(" ");
  assert.match(joined, /mmd_workspace_disk_used_mib/, "customer usage missing");
  assert.match(joined, /mmd_pool_used_gib - /, "platform overhead missing");
  assert.match(joined, /mmd_pool_free_gib/, "free space missing");
  // Stacked, so the bands add to the pool rather than overlapping.
  assert.equal(p.fieldConfig.defaults.custom.stacking.mode, "normal");
});

test("overcommitment is stated rather than left to be inferred", () => {
  const p = panel("Overcommit");
  assert.ok(p, "no overcommit panel");
  assert.match(p.targets[0].expr, /mmd_pool_committed_gib \/ mmd_pool_total_gib/);
  assert.ok((p.description || "").length > 40,
            "the ratio is shown without saying what it means");
});

test("per-workspace distribution is ranked and readable", () => {
  const p = panel("Who is using the most disk");
  assert.ok(p, "no per-workspace panel");
  assert.equal(p.type, "table");
  // The value column is named after what it measures, and Prometheus's
  // plumbing columns are excluded.
  const org = (p.transformations || []).find((x) => x.id === "organize");
  assert.ok(org, "no column transformation");
  assert.ok(org.options.excludeByName.Time, "the Time column is still shown");
});

test("disk bands never rely on colour alone", () => {
  // validate_palette.js scores this system's warn against its bad at ΔE 3.3
  // for deuteranopia, well under the floor - so every band carries an icon.
  for (const pct of [0, 50, WARN_PERCENT, 99, 100]) {
    const b = band(pct);
    assert.ok(b.key, `no band key at ${pct}%`);
  }
  assert.ok(band(WARN_PERCENT).ic, "the warning band has no icon");
});

test("the pool guard's floor is exported so it can be alerted on", () => {
  assert.match(APP, /mmd_pool_free_gib/);
  assert.ok(queries.some((q) => q.includes("mmd_pool_free_gib")),
            "nothing on the dashboard watches free space");
});
