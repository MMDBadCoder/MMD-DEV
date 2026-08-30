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
  assert.match(users, /expect: b\.dataset\.email/);
  assert.match(ui, /danger\.irrecoverable/);
  assert.match(ui, /input\.value\.trim\(\)\.toLowerCase\(\) !== expect\.toLowerCase\(\)/);
});

test("first-run guidance covers funding through publishing and records terminal use", () => {
  for (const key of ["onboarding.credit", "onboarding.power", "onboarding.terminal",
                     "onboarding.ssh", "onboarding.publish"]) assert.match(machine, new RegExp(key));
  assert.match(connections, /mmd-onboarding-terminal/);
  assert.match(machine, /steps\.every/);
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
