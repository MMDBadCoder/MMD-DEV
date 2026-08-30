import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (file) => fs.readFileSync(path.join(ROOT, file), "utf8");
const users = read("web/js/pages/adminusers.js");
const admin = read("web/js/pages/admin.js");
const openrouter = read("web/js/pages/adminopenrouter.js");
const claude = read("web/js/pages/aipricing.js");
const policy = read("web/js/pages/adminmonitor.js");
const ai = read("web/js/pages/ai.js");

test("the admin user list offers power off only for running workspaces", () => {
  assert.match(users, /u\.workspace\?\.state === "on"/);
  assert.match(users, /\/api\/admin\/workspaces\/\$\{b\.dataset\.poweroff\}\/power-off/);
  assert.match(users, /adm\.poweroff\.confirm\.body/);
});

test("the overview contains no commercial or capacity configuration", () => {
  assert.doesNotMatch(admin, /api\/admin\/ai-pricing|api\/admin\/settings|api\/admin\/capacity/);
});

test("OpenRouter owns exchange rate and supplier policy without a discount", () => {
  assert.match(openrouter, /api\/admin\/openrouter/);
  assert.match(openrouter, /usd_to_toman/);
  assert.match(openrouter, /workspace_id/);
  assert.match(openrouter, /guardrail_id/);
  assert.doesNotMatch(openrouter, /discount_percent:/);
});

test("Claude owns the only editable AI discount", () => {
  assert.match(claude, /claude_discount_percent/);
  assert.doesNotMatch(claude, /id="usdrate"|openrouter_discount_percent/);
});

test("capacity policy, rates and monitoring share their dedicated page", () => {
  assert.match(policy, /api\/admin\/settings/);
  assert.match(policy, /api\/admin\/capacity/);
  assert.match(policy, /api\/admin\/metrics/);
});

test("Hermes links to OpenRouter's highest-discount model view", () => {
  assert.match(ai, /https:\/\/openrouter\.ai\/models\?discount=true&order=discount-high-to-low/);
  assert.match(ai, /target="_blank" rel="noopener noreferrer"/);
});

test("Hermes activation offers an optional Telegram gateway with an allowlist", () => {
  assert.match(ai, /id="tg-option"/);
  assert.match(ai, /telegram_token/);
  assert.match(ai, /telegram_users/);
  assert.match(ai, /https:\/\/t\.me\/BotFather/);
  assert.match(ai, /telegram_ready/);
});

test("Telegram can be enabled later or disabled without disabling Hermes", () => {
  assert.match(ai, /id="tg-enable"/);
  assert.match(ai, /id="tg-disable"/);
  assert.match(ai, /action: "enable", \.\.\.payload/);
});

test("Hermes reuses account Telegram defaults and waits for a published dashboard", () => {
  assert.match(ai, /telegram_profile_configured/);
  assert.match(ai, /href="\/console\/account"/);
  assert.match(ai, /h\.dashboard_ready/);
  assert.match(ai, /ai\.hermes\.host\.preparing/);
});

test("customers get separate OpenRouter, Claude and Hermes tabs", () => {
  for (const tab of ["openrouter", "claude", "hermes"])
    assert.match(ai, new RegExp(`key: "${tab}"`));
  assert.match(ai, /ai\.hermes\.openrouter\.default/);
});
