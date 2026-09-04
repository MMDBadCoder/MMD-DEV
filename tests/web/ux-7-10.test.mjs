import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (name) => fs.readFileSync(path.join(ROOT, name), "utf8");
const ui = read("web/js/ui.js");
const users = read("web/js/pages/adminusers.js");
const machine = read("web/js/pages/machine.js");
const connections = read("web/js/pages/connections.js");
const css = read("web/app.css");

test("account deletion names impact and requires the affected identity", () => {
  assert.match(users, /destructiveDialog/);
  assert.match(users, /expect: b\.dataset\.username/);
  assert.match(ui, /danger\.irrecoverable/);
  assert.match(ui, /input\.value\.trim\(\)\.toLowerCase\(\) !== expect\.toLowerCase\(\)/);
});

test("first-run guidance ends at a usable shell, and stays finished", () => {
  // Credit, power, terminal: the shortest path to a prompt, which is the
  // point the machine is actually usable. SSH keys and publishing a port were
  // once steps four and five; they are features a customer reaches for when
  // they need them, and listing them left the checklist permanently
  // unfinished for everyone who never wanted either.
  for (const key of ["onboarding.credit", "onboarding.power", "onboarding.terminal"])
    assert.match(machine, new RegExp(key));
  assert.doesNotMatch(machine, /onboarding\.ssh|onboarding\.publish/);
  assert.match(connections, /mmd-onboarding-terminal/);
  // Completed once, gone for good - not recomputed from live state, which
  // brought the whole checklist back whenever a machine was powered down.
  assert.match(machine, /localStorage\.getItem\(ONBOARDED\) === "1"/);
  assert.match(machine, /localStorage\.setItem\(ONBOARDED, "1"\)/);
});

test("phone layout uses a bottom navigation rail and bottom-sheet confirmations", () => {
  assert.match(css, /\.header \.nav\{position:fixed/);
  assert.match(css, /\.danger-wrap\{align-items:end/);
  assert.match(css, /min-height:44px/);
  assert.match(css, /-webkit-overflow-scrolling:touch/);
});

test("shared design primitives own destructive, empty and focus behaviour", () => {
  assert.match(ui, /export function destructiveDialog/);
  assert.match(ui, /export function empty\(text, ico = icon\.info, action = null\)/);
  assert.match(css, /button:focus-visible,a:focus-visible/);
  assert.match(css, /--space-1:/);
});

test("all inline messages keep space from both neighbouring boxes", () => {
  assert.match(css, /\.note\{[^}]*margin:14px 0;/s);
  for (const kind of ["ok", "warn", "bad", "info"])
    assert.match(css, new RegExp(`\\.note\\.${kind}\\{`));
});
