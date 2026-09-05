import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const machine = fs.readFileSync("web/js/pages/machine.js", "utf8");
const ai = fs.readFileSync("web/js/pages/ai.js", "utf8");
const ui = fs.readFileSync("web/js/ui.js", "utf8");
const css = fs.readFileSync("web/app.css", "utf8");
const sms = fs.readFileSync("web/js/pages/smsprefs.js", "utf8");
const connections = fs.readFileSync("web/js/pages/connections.js", "utf8");
const support = fs.readFileSync("web/js/pages/support.js", "utf8");
const adminUser = fs.readFileSync("web/js/pages/adminuser.js", "utf8");
const backup = fs.readFileSync("web/js/pages/adminbackup.js", "utf8");
const billing = fs.readFileSync("web/js/pages/billing.js", "utf8");
const adminUsers = fs.readFileSync("web/js/pages/adminusers.js", "utf8");

test("an account without compute gets two equal first-run paths", () => {
  assert.match(machine, /journey-choice-grid/);
  assert.match(machine, /workspace\.choice\.api\.title/);
  assert.match(machine, /workspace\.choice\.machine\.title/);
  assert.match(machine, /href="\/console\/ai\/openrouter"/);
});

test("OpenRouter shows confirmed limit state and polls a pending update", () => {
  assert.match(ai, /h\.limit_usd/);
  assert.match(ai, /h\.limit_synced_at/);
  assert.match(ai, /h\.limit_sync_pending/);
  assert.match(ai, /repoll\("openrouter", 5000\)/);
});

test("recoverable AI prerequisites are links instead of disabled controls", () => {
  assert.match(ai, /openClawRecovery/);
  assert.match(ai, /hermesRecovery/);
  assert.match(ai, /managedToggle/);
  assert.doesNotMatch(ai, /needs_openrouter[^\n]+\? "disabled"/);
});

test("shared form errors identify, describe, and focus the invalid field", () => {
  assert.match(ui, /export function formError/);
  assert.match(ui, /aria-invalid/);
  assert.match(ui, /aria-describedby/);
  assert.match(ui, /input\.focus\(\)/);
  assert.match(css, /input\[aria-invalid="true"\]/);
});

test("each customer can configure the unified balance notification step", () => {
  assert.match(sms, /id="credit-step"/);
  assert.match(sms, /credit_step_toman: value/);
  assert.match(sms, /"credit_step"/);
  assert.doesNotMatch(sms, /credit_added|spend_milestone/);
});

test("secondary forms connect validation failures to the affected field", () => {
  assert.match(support, /formError\(t\("tk\.incomplete"/);
  assert.match(support, /field: subject\.length < 3 \? "#tk-subj" : "#tk-body"/);
  assert.match(adminUser, /field: badName \? "#admin-name" : "#admin-phone"/);
});

test("recoverable prerequisites do not hide behind disabled controls", () => {
  assert.match(connections, /location\.href = d\.rdp\.memory_ok \? "\/console" : "\/console\/resources"/);
  assert.match(connections, /\$\("#newkey"\)\.focus\(\)/);
  assert.doesNotMatch(backup, /id="bk-now"[^>]*disabled/);
  assert.match(backup, /adm\.bk\.configure\.first/);
});

test("priority phone tables become labelled cards without changing desktop tables", () => {
  assert.match(css, /table\.mobile-cards td::before\{content:attr\(data-label\)/);
  for (const page of [billing, support, adminUsers]) {
    assert.match(page, /table class="mobile-cards"/);
    assert.match(page, /data-label=/);
  }
});
