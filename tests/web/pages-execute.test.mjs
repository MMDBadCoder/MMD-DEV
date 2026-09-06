/* Every page module must survive being loaded and run.
 *
 * This exists because of two real outages a week apart. A webhook card was
 * added to the tickets page and its `${dashboardCard(_gf, ...)}` landed in the
 * wrong function, so opening a ticket threw "_gf is not defined". That was
 * fixed; opening a ticket then threw "hook is not defined" - the same mistake,
 * one identifier over.
 *
 * Neither was catchable by what we had. `node --check` parses the file and it
 * parses fine. imports.test.mjs walks the source, but it flattens every
 * declaration in a file into one set with no notion of scope, so a variable
 * declared in one function and used in another looks correct. An identifier
 * that resolves to nothing is a RUNTIME error, and nothing but running it
 * finds it reliably.
 *
 * So this runs them. The module tree is mirrored to a temp directory with the
 * network and DOM leaves - api, ui, i18n, main, grafana, router, terminal -
 * replaced by stubs; every other module, and every page, is the real file.
 * Then each exported function is called, and every event handler it wired is
 * called too, because handlers are code that ships and a bad name inside one
 * is invisible until a customer clicks it.
 *
 * The stubs are GENERATED from each real module's export list, so a new export
 * cannot quietly go missing from a stub and turn into a fake failure here. */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const WEB = path.join(ROOT, "web/js");
/* Stubbed: the leaves that reach the network, the DOM, or the user. A
   dialog module is on this list because it returns a promise that only
   settles when somebody clicks, which no test will ever do. */
const STUBBED = ["api", "ui", "i18n", "main", "grafana", "router", "terminal",
                 "dangerdialog"];

const exportsOf = (src) => {
  const out = new Set();
  for (const m of src.matchAll(/export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)/g))
    out.add(m[1]);
  for (const m of src.matchAll(/export\s*\{([^}]*)\}/g))
    for (const part of m[1].split(","))
      if (part.trim()) out.add(part.split(/\s+as\s+/).pop().trim());
  return out;
};

/* A value that accepts anything done to it, so execution keeps going instead
   of stopping at the first unstubbed detail. It is deliberately uncapped:
   truncating a long chain like `f(x).split(".").pop().toLowerCase()` to a
   plain string part-way through makes the next call fail and reads as a page
   bug. Nothing here recurses on its own - a new proxy appears only when code
   actually reaches for one - so there is nothing for a cap to protect. Array methods really do call
   their callback: `${rows.map(r => `<td>${label(r)}</td>`).join("")}` is
   exactly the kind of place a bad name hides, and skipping the callback would
   step straight over the code this test exists to run. */
const ANY = () => {
  const call1 = (fn) => (typeof fn === "function" ? fn(ANY(), 0, []) : ANY());
  return new Proxy(function () {}, {
    get(_t, k) {
      if (k === "then") return undefined;               // never thenable: `await` must settle
      // "" in a template, 0 for a numeric or default conversion, so that
      // `new Date(value)` yields a valid date instead of an "Invalid time value"
      // that looks like a page bug but is only an unset stub.
      if (k === Symbol.toPrimitive) return (hint) => (hint === "string" ? "" : 0);
      if (k === Symbol.iterator) return function* () { yield ANY(); };
      if (k === "toString" || k === "valueOf") return () => "";
      if (k === "toJSON") return () => ({});
      if (k === "length") return 1;
      if (k === "map" || k === "flatMap") return (fn) => [call1(fn)];
      if (k === "forEach") return (fn) => { call1(fn); };
      if (k === "filter" || k === "sort" || k === "find" || k === "some" || k === "every")
        return (fn) => { const r = call1(fn); return k === "filter" || k === "sort" ? [r] : r; };
      if (k === "reduce") return (fn, init) =>
        (typeof fn === "function" ? fn(init ?? ANY(), ANY(), 0, []) : init);
      if (k === "join") return () => "";
      if (typeof k === "symbol") return undefined;
      return ANY();
    },
    apply: () => ANY(),
    construct: () => ANY(),
    has: () => true,
    set: () => true,
    deleteProperty: () => true,
    // The target is a function, so `prototype` is a non-configurable own
    // property that these traps are not allowed to hide. Delegating keeps the
    // proxy legal under Object.keys and spread.
    getOwnPropertyDescriptor: Reflect.getOwnPropertyDescriptor,
    ownKeys: Reflect.ownKeys,
  });
};

/* The shared prelude every generated stub imports. Fake elements record the
   handlers assigned to them so the test can call them afterwards. */
const PRELUDE = `
export const handlers = [];
export const ANY = ${ANY.toString()};
export const el = () => new Proxy({}, {
  get(_t, k) {
    if (k === "then") return undefined;   // an element is not thenable; awaiting one must not hang
    if (k === "value" || k === "textContent" || k === "innerHTML") return "";
    if (k === "dataset" || k === "style") return {};
    if (k === "classList") return { add() {}, remove() {}, toggle() {}, contains: () => false };
    if (k === "hidden" || k === "disabled" || k === "checked") return false;
    if (k === "addEventListener") return (_ev, fn) => { if (typeof fn === "function") handlers.push(fn); };
    if (typeof k === "symbol") return undefined;
    return ANY();
  },
  set(_t, k, v) {
    if (typeof v === "function" && String(k).startsWith("on")) handlers.push(v);
    return true;
  },
});
`;

const stubFor = (name, names) => {
  const lines = [`import { ANY, el, handlers } from "./__stub.js";`,
                 `export { handlers };`];
  for (const n of names) {
    if (name === "ui" && (n === "$" || n === "$$"))
      lines.push(`export const ${n} = () => ${n === "$" ? "el()" : "[el(), el()]"};`);
    else lines.push(`export const ${n} = ANY();`);   // callable and indexable
  }
  return lines.join("\n") + "\n";
};

const buildMirror = () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mmd-pages-"));
  fs.cpSync(WEB, dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "__stub.js"), PRELUDE);
  for (const name of STUBBED) {
    const real = path.join(WEB, `${name}.js`);
    if (!fs.existsSync(real)) continue;
    const names = exportsOf(fs.readFileSync(real, "utf8"));
    assert.ok(names.size, `${name}.js exports nothing - stub generation would be empty`);
    fs.writeFileSync(path.join(dir, `${name}.js`), stubFor(name, names));
  }
  return dir;
};

const installGlobals = () => {
  const g = globalThis;
  g.window = g.window || g;
  g.document = g.document || ANY();
  g.location = g.location || { pathname: "/", search: "", hash: "", href: "https://x/", assign() {}, replace() {} };
  g.history = g.history || { pushState() {}, replaceState() {} };
  g.localStorage = g.localStorage || { getItem: () => null, setItem() {}, removeItem() {} };
  g.sessionStorage = g.sessionStorage || g.localStorage;
  g.fetch = g.fetch || (async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => "" }));
  g.WebSocket = g.WebSocket || function () { return ANY(); };
  g.alert = g.alert || (() => {});
  g.confirm = g.confirm || (() => true);
  g.prompt = g.prompt || (() => "");
  g.matchMedia = g.matchMedia || (() => ({ matches: false, addEventListener() {} }));
  g.requestAnimationFrame = g.requestAnimationFrame || (() => 0);
  /* Timers are neutered, but not identically. A setTimeout callback must
     still run - `await new Promise(r => setTimeout(r, 300))` is a normal way
     to pace a page, and a timeout that never fires hangs the test rather than
     failing it. Firing immediately and capping the total keeps a page that
     re-arms itself from spinning forever. setInterval only ever repeats, so
     it never fires at all. */
  let fired = 0;
  g.setTimeout = (fn) => { if (typeof fn === "function" && fired++ < 500) queueMicrotask(fn); return 0; };
  g.setInterval = () => 0;
  g.clearTimeout = () => {};
  g.clearInterval = () => {};
};

test("every page module loads, runs, and its handlers run", { timeout: 120_000 }, async () => {
  const dir = buildMirror();
  installGlobals();
  const stub = await import(pathToFileURL(path.join(dir, "__stub.js")).href);

  const pages = fs.readdirSync(path.join(WEB, "pages")).filter((f) => f.endsWith(".js")).sort();
  const problems = [];

  for (const file of pages) {
    const url = pathToFileURL(path.join(dir, "pages", file)).href;
    let mod;
    try {
      mod = await import(url);
    } catch (e) {
      problems.push(`pages/${file} failed to load: ${e.message}`);
      continue;
    }
    for (const [name, fn] of Object.entries(mod)) {
      // The router only ever calls the page entry points; other exports are
      // helpers with their own argument contracts, and feeding them route
      // params would report their complaints as page failures.
      if (typeof fn !== "function" || !name.endsWith("Page")) continue;
      // router.js builds params with Object.fromEntries over the matched
      // groups, so it is always an object and its values are always strings.
      for (const [label, arg] of [["no id", {}], ["id", { id: "1" }]]) {
        stub.handlers.length = 0;
        try {
          await fn(arg);
        } catch (e) {
          problems.push(`pages/${file} ${name}(${label}) threw: ${e.message}`);
          continue;
        }
        // Handlers are shipped code too. `_gf`/`hook` happened to be on the
        // render path; the next one may only be reachable from a click.
        for (const h of stub.handlers.splice(0)) {
          try {
            await h(new Proxy({}, {
            get: (_t, k) => (k === "preventDefault" || k === "stopPropagation" ? () => {}
                             : k === "then" ? undefined : ANY()),
            set: () => true,
          }));
          } catch (e) {
            problems.push(`pages/${file} ${name}(${label}) handler threw: ${e.message}`);
          }
        }
      }
    }
  }
  fs.rmSync(dir, { recursive: true, force: true });
  assert.deepEqual(problems, [], "\n  " + problems.join("\n  "));
});
