/* Two presentation defects a customer reported, and the gate that let a third
   one through. */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");
const CSS = read("web/app.css");

const rule = (selector) => {
  const i = CSS.indexOf(selector + "{");
  assert.notEqual(i, -1, `no rule for ${selector}`);
  return CSS.slice(i, CSS.indexOf("}", i));
};

// ---- reported: digits are too widely spaced ------------------------------
test("values read as a phrase use proportional figures", () => {
  // Measured: tabular figures pad Persian numerals to a uniform advance and
  // make «۴۹۹٬۳۲۷ تومان» 9.6% wider, which shows as gaps between digits.
  for (const sel of [".credit-chip", ".stat .v"]) {
    assert.match(rule(sel), /font-variant-numeric:\s*proportional-nums/, sel);
  }
});

test("columns that must align keep tabular figures", () => {
  // Removing them here would make the ledger ragged - the opposite bug.
  assert.match(rule("td.num,th.num"), /font-variant-numeric:\s*tabular-nums/);
});

test("the stylistic sets are off, as the comment always claimed", () => {
  // `font-feature-settings:"ss01","ss02"` turns them ON; a bare tag means 1.
  assert.doesNotMatch(CSS, /font-feature-settings:\s*"ss01"\s*,/);
  assert.match(CSS, /font-feature-settings:\s*"ss01" 0\s*,\s*"ss02" 0/);
});

test("a number never wraps away from its unit", () => {
  assert.match(rule(".stat .v"), /white-space:\s*nowrap/);
  assert.match(rule(".stat .v small"), /white-space:\s*nowrap/);
});

// ---- reported: machine state is hard to read in the admin list -----------
test("the admin list shows machine state as a pill, not bare text", () => {
  const admin = read("web/js/pages/admin.js");
  assert.match(admin, /statePill\(u\.workspace\.state\)/);
  assert.doesNotMatch(admin, /t\("machine\.state\." \+ u\.workspace\.state\)/);
});

test("one definition of the state pill, shared by admin and the customer view", () => {
  assert.match(read("web/js/ui.js"), /export function statePill/);
  assert.match(read("web/js/pages/machine.js"), /statePill\(/);
  // and no page redefines it
  for (const f of fs.readdirSync(path.join(ROOT, "web/js/pages"))) {
    assert.doesNotMatch(read("web/js/pages/" + f), /^function pill\(/m, f);
  }
});

test("every machine state the API can return gets a colour", () => {
  const ui = read("web/js/ui.js");
  const tone = ui.slice(ui.indexOf("const STATE_TONE"), ui.indexOf("};", ui.indexOf("const STATE_TONE")));
  // Straight from WorkspaceState in models/__init__.py.
  const states = ["provisioning", "off", "starting", "on", "stopping",
                  "resetting", "archiving", "archived", "deleting", "error"];
  for (const s of states) {
    // `off` is intentionally toneless - a grey dot is the resting state.
    if (s === "off") continue;
    assert.ok(tone.includes(s + ":"), `no tone for state "${s}"`);
  }
});

// ---- the gate that let a stray brace ship --------------------------------
test("the syntax gate parses modules, not scripts", () => {
  // `node --check x.js` exits 0 on a file with `import` AND a syntax error:
  // Node treats .js as CommonJS and gives up instead of failing. Every file
  // under web/js is a module, so the gate was passing everything.
  const run = read("tests/run.sh");
  assert.match(run, /\.mjs/, "run.sh still checks .js files as scripts");
  assert.match(run, /parsed as modules/);
});
