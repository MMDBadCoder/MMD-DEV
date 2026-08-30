import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const main = fs.readFileSync("web/js/main.js", "utf8");
const machine = fs.readFileSync("web/js/pages/machine.js", "utf8");
const resources = fs.readFileSync("web/js/pages/resources.js", "utf8");

test("machine state and its primary action share one hero", () => {
  assert.match(machine, /machine-hero/);
  assert.match(machine, /machine-power/);
});

test("power-on confirmation uses authoritative balance and hourly costs", () => {
  for (const field of ["credits", "rate_idle_per_hour", "rate_on_per_hour"])
    assert.match(machine, new RegExp(`w\\.${field}`));
});

test("resize confirmation compares the old and new cost", () => {
  assert.match(resources, /before\.max_per_hour/);
  assert.match(resources, /after\.max_per_hour/);
  assert.match(resources, /res\.preview\.difference/);
});

test("operation progress is global and refreshes without a page reload", () => {
  assert.match(main, /api\/operations/);
  assert.match(main, /operation-strip/);
  assert.match(main, /setInterval\(async \(\) =>/);
  assert.match(main, /}, 3000\)/);
});
