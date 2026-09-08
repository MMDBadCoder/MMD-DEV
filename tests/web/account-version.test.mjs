import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (name) => fs.readFileSync(path.join(ROOT, name), "utf8");

test("profile management is named Account while the old route remains compatible", () => {
  const main = read("web/js/main.js");
  const i18n = read("web/js/i18n.js");
  assert.match(main, /href: "\/console\/account", key: "nav\.account"/);
  assert.match(main, /route\("\/console\/security"/);
  assert.match(i18n, /"nav\.account": "حساب کاربری"/);
  assert.doesNotMatch(i18n, /"nav\.security"/);
});

test("the customer header and the API agree on the release", () => {
  /* Compared rather than pinned. The fallback in the header is what a customer
     sees before `me` arrives, so a release that bumps one and forgets the
     other ships a version number that is wrong for the first second of every
     visit - and pinning the literal meant editing this test every release,
     which is how the two drifted apart in the first place. */
  const api = read("control/mmd/version.py").match(/APP_VERSION = "([^"]+)"/);
  const header = read("web/js/main.js").match(/me\?\.version \|\| "([^"]+)"/);
  assert.ok(api, "no APP_VERSION in version.py");
  assert.ok(header, "no version fallback in the header");
  assert.equal(header[1], api[1],
               "the header fallback and APP_VERSION are different releases");
});
