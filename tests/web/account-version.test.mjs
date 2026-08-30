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

test("the customer header and API share release 1.5.0", () => {
  assert.match(read("control/mmd/version.py"), /APP_VERSION = "1\.5\.0"/);
  assert.match(read("web/js/main.js"), /me\?\.version \|\| "1\.5\.0"/);
});
