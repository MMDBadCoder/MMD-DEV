/* Every name a module uses must actually reach it.
 *
 * This exists because of a real outage: a shared chart module was added and the
 * import line into pages/machine.js silently failed to apply, so usageChart and
 * savedWindow were simply undefined. The overview page threw a ReferenceError
 * and rendered nothing for every customer.
 *
 * `node --check` cannot catch that - the file parses perfectly. Nor can loading
 * the module, because an undefined identifier is a RUNTIME error, not a link
 * error. So this walks the source: if a module uses a name that some other
 * local module exports, and has neither imported nor declared it, that is the
 * bug. */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const WEB = path.join(ROOT, "web/js");

const files = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
  e.isDirectory() ? files(path.join(dir, e.name))
  : e.name.endsWith(".js") ? [path.join(dir, e.name)] : []);

/* Comments and string bodies are not code; a name mentioned in prose must not
   count as a use, or every explanatory comment becomes a false positive. */
const strip = (src) => src
  .replace(/\/\*[\s\S]*?\*\//g, " ")
  .replace(/(^|[^:])\/\/[^\n]*/g, "$1 ")
  .replace(/`(?:\\[\s\S]|\$\{[^}]*\}|[^`\\])*`/g, (m) =>
    // Template literals DO contain code inside ${...}; keep those, drop the text.
    (m.match(/\$\{[^}]*\}/g) || []).join(" "))
  .replace(/'(?:\\.|[^'\\])*'/g, " ")
  .replace(/"(?:\\.|[^"\\])*"/g, " ");

const exportsOf = (src) => {
  const out = new Set();
  for (const m of src.matchAll(/export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)/g))
    out.add(m[1]);
  for (const m of src.matchAll(/export\s*\{([^}]*)\}/g))
    for (const part of m[1].split(","))
      out.add(part.split(/\s+as\s+/).pop().trim());
  out.delete("");
  return out;
};

const importsOf = (src) => {
  const out = new Set();
  for (const m of src.matchAll(/import\s+([^;]*?)\s+from\s+["'][^"']+["']/g)) {
    const clause = m[1];
    for (const b of (clause.match(/\{([^}]*)\}/)?.[1] || "").split(","))
      if (b.trim()) out.add(b.split(/\s+as\s+/).pop().trim());
    const dflt = clause.replace(/\{[^}]*\}/, "").replace(/,/g, "").trim();
    if (dflt && !dflt.startsWith("*")) out.add(dflt);
  }
  return out;
};

/* Anything the file defines itself, at any depth - a local of the same name is
   a shadow, not a missing import. */
const declaredIn = (code) => {
  const out = new Set();
  // Take the identifier TOKEN out of each binding rather than trusting a split -
  // `new Promise((resolve) => …)` leaves a stray paren on the name otherwise,
  // and the parameter then looks like a missing import.
  const addNames = (text) => {
    for (const id of text.match(/[A-Za-z_$][\w$]*/g) || []) out.add(id);
  };
  for (const m of code.matchAll(/(?:function|class)\s+([A-Za-z_$][\w$]*)/g)) out.add(m[1]);
  for (const m of code.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g)) out.add(m[1]);
  for (const m of code.matchAll(/(?:const|let|var)\s*\{([^}]*)\}/g)) addNames(m[1]);
  for (const m of code.matchAll(/\(([^)]*)\)\s*=>/g)) addNames(m[1]);
  for (const m of code.matchAll(/([A-Za-z_$][\w$]*)\s*=>/g)) out.add(m[1]);
  for (const m of code.matchAll(/function\s*[A-Za-z_$\w$]*\s*\(([^)]*)\)/g)) addNames(m[1]);
  for (const m of code.matchAll(/catch\s*\(([^)]*)\)/g)) addNames(m[1]);
  out.delete("");
  return out;
};

test("every module imports the shared names it uses", () => {
  const all = files(WEB);
  const everyExport = new Map();          // name -> module that exports it
  for (const f of all)
    for (const name of exportsOf(fs.readFileSync(f, "utf8")))
      everyExport.set(name, path.relative(ROOT, f));

  const problems = [];
  for (const f of all) {
    const src = fs.readFileSync(f, "utf8");
    const code = strip(src);
    const imported = importsOf(src);
    const declared = declaredIn(code);
    const mine = exportsOf(src);
    const rel = path.relative(ROOT, f);

    for (const [name, from] of everyExport) {
      if (imported.has(name) || declared.has(name) || mine.has(name)) continue;
      if (from === rel) continue;
      // Used as a bare identifier: a call, or a value.
      const used = new RegExp(`(^|[^\\w$.])${name}\\s*[(,)\\];:}]`).test(code);
      if (used) problems.push(`${rel} uses ${name}() but never imports it (exported by ${from})`);
    }
  }
  assert.deepEqual(problems, [], "\n  " + problems.join("\n  "));
});
