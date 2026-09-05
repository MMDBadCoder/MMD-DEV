import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const page = fs.readFileSync("web/js/pages/adminactivity.js", "utf8");
const nav = fs.readFileSync("web/js/pages/adminnav.js", "utf8");
const main = fs.readFileSync("web/js/main.js", "utf8");

test("the global audit log is reachable from the admin navigation", () => {
  assert.match(nav, /console\/admin\/activity/);
  assert.match(main, /adminActivityPage/);
  assert.match(main, /console\/admin\/activity/);
});

test("the audit page supports search, pagination and mobile labels", () => {
  assert.match(page, /api\/admin\/activity\?limit=/);
  assert.match(page, /encodeURIComponent\(query\)/);
  assert.match(page, /audit-prev/);
  assert.match(page, /audit-next/);
  assert.match(page, /class="mobile-cards"/);
  assert.match(page, /data-label=/);
});
