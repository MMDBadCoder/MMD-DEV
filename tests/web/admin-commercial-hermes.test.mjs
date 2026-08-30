import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (file) => fs.readFileSync(path.join(ROOT, file), "utf8");
const users = read("web/js/pages/adminusers.js");
const admin = read("web/js/pages/admin.js");
const ai = read("web/js/pages/ai.js");

test("the admin user list offers power off only for running workspaces", () => {
  assert.match(users, /u\.workspace\?\.state === "on"/);
  assert.match(users, /\/api\/admin\/workspaces\/\$\{b\.dataset\.poweroff\}\/power-off/);
  assert.match(users, /adm\.poweroff\.confirm\.body/);
});

test("commercial Claude settings are visible from the admin overview", () => {
  assert.match(admin, /get\("\/api\/admin\/ai-pricing"\)/);
  assert.match(admin, /claude\.usd_to_toman/);
  assert.match(admin, /claude\.discount_percent/);
  assert.match(admin, /\/console\/admin\/claude/);
});

test("Hermes links to OpenRouter's highest-discount model view", () => {
  assert.match(ai, /https:\/\/openrouter\.ai\/models\?discount=true&order=discount-high-to-low/);
  assert.match(ai, /target="_blank" rel="noopener noreferrer"/);
});
