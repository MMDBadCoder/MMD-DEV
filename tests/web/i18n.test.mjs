/* Interface translation. Node's built-in runner - no dependency to install. */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const WEB = path.join(ROOT, "web/js");
const i18n = await import(path.join(WEB, "i18n.js"));
const { t, hasKey, allKeys, fmtMoney, fmtNum, fmtFa, translateError, CURRENCY } = i18n;

/* Some entries are functions taking a value. To exercise them we pass a probe
   that answers any property access, so a template like `${p.label}` renders
   instead of throwing - the point is to check the surrounding TEXT. */
const PROBE = new Proxy({}, {
  get: (_, k) => (k === Symbol.toPrimitive ? () => 1 : k === "toString" ? () => "1" : 1),
});
const render = (v) => (typeof v === "function" ? String(v(PROBE)) : v);

const jsFiles = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
  e.isDirectory() ? jsFiles(path.join(dir, e.name))
  : e.name.endsWith(".js") ? [path.join(dir, e.name)] : []);

// ---- catalogue integrity ------------------------------------------------
test("the catalogue is not empty", () => {
  assert.ok(allKeys().length > 100);
});

test("every key used by a page exists in the catalogue", () => {
  const missing = new Set();
  for (const f of jsFiles(WEB)) {
    if (f.endsWith("i18n.js")) continue;
    const src = fs.readFileSync(f, "utf8");
    for (const m of src.matchAll(/\bt\(\s*"([^"]+)"/g)) {
      // A trailing dot means the key is built by concatenation -
      // t("machine.state." + status) - so the literal alone is not a key.
      // Those prefixes are covered by the dynamic-prefix test below.
      if (m[1].endsWith(".")) continue;
      if (!hasKey(m[1])) missing.add(`${path.relative(ROOT, f)} -> ${m[1]}`);
    }
  }
  assert.deepEqual([...missing], [], "untranslated keys referenced by pages");
});

test("every dynamically-built key prefix has all its variants", () => {
  // t("machine.state." + status) must resolve for every status the API can
  // return, or the interface silently prints a raw key at customers.
  const expect = {
    "machine.state.": ["on", "off", "starting", "stopping", "provisioning",
                       "archiving", "archived", "error", "pending", "none"],
    "billing.kind.": ["grant", "charge_hour", "charge_partial", "adjustment"],
    "act.a.": ["register", "sign_in", "password_change", "power_on", "power_off",
               "size_change", "port_publish", "port_unpublish",
               "packages_installed", "approve", "reject", "grant_credit",
               "set_admin", "delete_user", "settings_update", "provision_failed"],
    "tools.preset.": ["editors", "monitoring", "shell", "network", "build",
                      "python", "databases", "media"],
  };
  const missing = [];
  for (const [prefix, variants] of Object.entries(expect))
    for (const v of variants) if (!hasKey(prefix + v)) missing.push(prefix + v);
  assert.deepEqual(missing, []);
});

test("toolset keys match the backend catalogue exactly", () => {
  const py = fs.readFileSync(path.join(ROOT, "control/mmd/presets.py"), "utf8");
  const keys = [...py.matchAll(/^\s{4}"([a-z]+)": Preset\(/gm)].map((m) => m[1]);
  assert.ok(keys.length >= 5, "expected to find presets in the catalogue");
  const missing = keys.filter((k) => !hasKey(`tools.preset.${k}`));
  assert.deepEqual(missing, [], "toolsets with no Persian label");
});

test("no catalogue value is an empty string", () => {
  for (const k of allKeys()) {
    const v = render(t(k, PROBE));
    assert.notEqual(String(v).trim(), "", `empty: ${k}`);
  }
});

test("an unknown key returns the key rather than blank", () => {
  // A blank label is invisible in the UI; the key is at least diagnosable.
  assert.equal(t("no.such.key"), "no.such.key");
});

// ---- language policy ----------------------------------------------------
const LATIN_ALLOWED = new Set([
  "brand",              // the product name
  "billing.tx.amount",  // the currency word, checked separately
  "res.vcpu",           // vCPU is the term developers use
  "machine.subtitle",   // "Ubuntu 24.04 · 2 vCPU · 4 GB" - all technical
]);

test("customer-facing labels are in Persian", () => {
  const offenders = [];
  for (const k of allKeys()) {
    if (LATIN_ALLOWED.has(k)) continue;
    const v = render(t(k, PROBE));
    const persian = /[؀-ۿ]/.test(v);
    const latinWords = (v.match(/[A-Za-z]{4,}/g) || []).filter((w) =>
      // Established technical terms stay Latin on purpose.
      !["Ubuntu", "Docker", "Claude", "Code", "Codex", "systemd", "apt", "root",
        "Vazirmatn", "MMD", "DEV", "TCP", "UDP", "SSH", "vCPU", "API", "http",
        "https", "port"].includes(w));
    if (!persian && v.length > 3) offenders.push(`${k} = ${v}`);
    else if (latinWords.length > 2) offenders.push(`${k} has untranslated words: ${latinWords}`);
  }
  assert.deepEqual(offenders, []);
});

test("currency is Toman", () => {
  assert.equal(CURRENCY, "تومان");
});

// ---- server error codes -------------------------------------------------
test("every error code the API can emit has a Persian translation", () => {
  const app = fs.readFileSync(path.join(ROOT, "control/mmd/app.py"), "utf8");
  const codes = new Set([...app.matchAll(/fail\(\s*\d+\s*,\s*"([a-z_]+)"/g)].map((m) => m[1]));
  const portsPy = fs.readFileSync(path.join(ROOT, "control/mmd/ports.py"), "utf8");
  for (const m of portsPy.matchAll(/PortError\([^,]+,\s*"([a-z_]+)"\)/g)) codes.add(m[1]);
  assert.ok(codes.size > 10, "expected to find error codes to check");
  const missing = [...codes].filter((c) => !hasKey(`err.${c}`));
  assert.deepEqual(missing, [], "error codes with no Persian text");
});

test("a coded error is rendered in Persian", () => {
  const msg = translateError({ code: "bad_credentials", message: "Incorrect" }, 401);
  assert.match(msg, /[؀-ۿ]/);
  assert.ok(!msg.includes("Incorrect"));
});

test("a coded error can use the values the server sent", () => {
  const msg = translateError(
    { code: "insufficient_credit", needed: 480, balance: 12 }, 402);
  assert.match(msg, /۴۸۰/);
  assert.match(msg, /۱۲/);
});

test("capacity refusals distinguish CPU from memory", () => {
  const cpu = translateError({ code: "no_capacity", resource: "cpu" }, 503);
  const mem = translateError({ code: "no_capacity", resource: "memory" }, 503);
  assert.notEqual(cpu, mem);
  assert.match(cpu, /CPU/);
});

test("an unknown code falls back to the server message", () => {
  const msg = translateError({ code: "brand_new_code", message: "Something" }, 500);
  assert.equal(msg, "Something");
});

test("a 422 validation array becomes readable Persian, never [object Object]", () => {
  const msg = translateError(
    [{ loc: ["body", "password"], msg: "String should have at least 10 characters" }], 422);
  assert.ok(!msg.includes("[object"));
  assert.match(msg, /[؀-ۿ]/);
  assert.match(msg, /10/);
});

test("multiple validation failures are joined", () => {
  const msg = translateError([
    { loc: ["body", "email"], msg: "value is not a valid email address" },
    { loc: ["body", "password"], msg: "Field required" },
  ], 422);
  assert.ok(msg.includes("،"));
});

test("a plain string detail passes through", () => {
  assert.equal(translateError("سلام", 400), "سلام");
});

test("a missing detail still yields something readable", () => {
  assert.match(translateError(undefined, 500), /[؀-ۿ]/);
});

// ---- formatting ---------------------------------------------------------
test("money uses Persian digits and grouping", () => {
  const s = fmtMoney(5000000);
  assert.match(s, /[۰-۹]/);
  assert.ok(!/[0-9]/.test(s));
});

test("money never shows a fraction of a Toman", () => {
  assert.ok(!fmtMoney(123.456).includes("."));
  assert.ok(!fmtMoney(123.456).includes("٫"));
});

test("money rounds rather than truncates", () => {
  assert.equal(fmtMoney(0.6), fmtMoney(1));
});

test("money handles zero, null and undefined", () => {
  for (const v of [0, null, undefined]) assert.equal(fmtMoney(v), fmtMoney(0));
});

test("technical numbers stay in Latin digits for copy-paste", () => {
  const s = fmtNum(2048);
  assert.match(s, /[0-9]/);
  assert.ok(!/[۰-۹]/.test(s));
});

test("prose numbers use Persian digits", () => {
  assert.match(fmtFa(12), /[۰-۹]/);
});

test("fmtNum keeps requested decimals", () => {
  assert.equal(fmtNum(0.5, 1), "0.5");
});
