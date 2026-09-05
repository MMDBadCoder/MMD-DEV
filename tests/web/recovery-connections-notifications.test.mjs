import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const ui = fs.readFileSync("web/js/ui.js", "utf8");
const connections = fs.readFileSync("web/js/pages/connections.js", "utf8");
const main = fs.readFileSync("web/js/main.js", "utf8");
const resources = fs.readFileSync("web/js/pages/resources.js", "utf8");

test("common recoveries point to the state that resolves the error", () => {
  for (const code of ["insufficient_credit", "mem_shrink_running", "machine_off",
                      "no_ssh_key", "rdp_needs_memory", "apt_repair_failed"])
    assert.match(ui, new RegExp(`${code}:`));
  assert.match(ui, /data-recovery-retry/);
});

test("workspace and managed AI prerequisites point to their exact setup page", () => {
  const exact = {
    no_workspace: "/console",
    no_such_workspace: "/console",
    telegram_not_configured: "/console/account",
    telegram_bad_token: "/console/account",
    telegram_bad_users: "/console/account",
    needs_openrouter: "/console/ai/openrouter",
  };
  for (const [code, path] of Object.entries(exact))
    assert.match(ui, new RegExp(`${code}: \\["recovery\\.[^"]+", "${path}"\\]`));

  for (const code of ["openrouter_not_ready", "openclaw_not_ready", "busy",
                      "one_key_at_a_time"])
    assert.match(ui, new RegExp(`${code}: \\["recovery\\.retry", null\\]`));
});

test("high-risk pages use the shared actionable error component", () => {
  assert.match(connections, /recoveryNote/);
  assert.match(resources, /recoveryNote/);
});

test("one launcher presents all four connection methods", () => {
  for (const marker of ["conn.tab.terminal", '"SSH"', '"RDP"',
                        "conn.launch.published"])
    assert.match(connections, new RegExp(marker));
  assert.match(connections, /d\.applications/);
});

test("notifications are durable API state, not transient toasts", () => {
  assert.match(main, /api\/notifications/);
  assert.match(main, /notification-panel/);
  assert.match(main, /notifications\/read-all/);
  assert.match(main, /data-notification/);
});

test("reset names Hermes among data that is removed", () => {
  assert.match(resources, /reset\.d\.hermes/);
});
