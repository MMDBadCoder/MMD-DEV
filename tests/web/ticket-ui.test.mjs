/* The three defects two customers reported, pinned so they cannot come back. */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");
const { t } = await import(path.join(ROOT, "web/js/i18n.js"));
const { usageChart } = await import(path.join(ROOT, "web/js/usagechart.js"));
const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();

// ---- reported: «بیشترین ۰٫۵۱ هسته» ran together as one phrase -------------
test("a value label carries its colon", () => {
  assert.ok(t("billing.chart.peak").endsWith(":"), t("billing.chart.peak"));
  assert.ok(t("chart.now").endsWith(":"), t("chart.now"));
});

test("the standalone axis label does NOT carry a colon", () => {
  // billing.chart.now sits at the right-hand end of a bar chart's time axis
  // with nothing after it. A colon there would be dangling punctuation.
  assert.ok(!t("billing.chart.now").includes(":"), t("billing.chart.now"));
});

test("the chart legend labels both numbers it shows", () => {
  const series = [0.1, 0.51, 0.31].map((v, i) => ({ ts: i, value: v }));
  const out = text(usageChart(series, 2, "#000", "هسته"));
  assert.ok(out.includes(t("chart.now")), out);
  assert.ok(out.includes(t("billing.chart.peak")), out);
  // ...and the value still follows its label rather than floating loose.
  assert.match(out, new RegExp(`${t("billing.chart.peak")}\\s*\\S`));
});

// ---- reported: "your reply" shown when the customer wrote last ------------
test("the reply box asks for a REPLY only when there is one to make", () => {
  const src = read("web/js/pages/support.js");
  assert.match(src, /const lastIsStaff = !!d\.messages\.at\(-1\)\?\.from_staff/);
  assert.match(src, /lastIsStaff \? t\("tk\.reply"\) : t\("tk\.followup"\)/);
  assert.match(src, /lastIsStaff \? t\("tk\.reply\.ph"\) : t\("tk\.followup\.ph"\)/);
});

test("the two labels actually differ", () => {
  assert.notEqual(t("tk.reply"), t("tk.followup"));
  assert.notEqual(t("tk.reply.ph"), t("tk.followup.ph"));
});

test("the staff queue keeps its own wording", () => {
  // An operator IS always replying, so that label was never wrong.
  assert.match(read("web/js/pages/admintickets.js"), /t\("tk\.answer"\)/);
});

// ---- reported: the nav counter says 1 but the list looks unchanged --------
test("the unread badge is styled outside the tab bar", () => {
  const css = read("web/app.css");
  // The whole bug: .badge existed only as `.tabs2 .badge`, so on the support
  // list it rendered as unstyled inline text.
  assert.match(css, /^\.badge\{/m, "no bare .badge rule");
  assert.match(css, /^\.badge\.new\{/m, "no .badge.new rule");
});

test("an unread row is marked, not just its badge", () => {
  const css = read("web/app.css");
  assert.match(css, /^tr\.unread td\{/m);
  for (const page of ["web/js/pages/support.js", "web/js/pages/admintickets.js"]) {
    const src = read(page);
    assert.match(src, /class="clickable\$\{k\.unread \? " unread" : ""\}"/, page);
    assert.match(src, /class="badge new"/, page);
  }
});

// ---- reported: the red counter did not go down as tickets were read ------
test("the session object is refetched on every navigation", () => {
  const src = read("web/js/main.js");
  // It used to be `if (state.me === null) await refreshMe()`, so a signed-in
  // customer kept the object fetched at page load - and the header badge, drawn
  // from it, never moved until a hard reload.
  assert.doesNotMatch(src, /if \(state\.me === null\) await refreshMe\(\)/);
  assert.match(src, /setGuard\(async \(r\) => \{[\s\S]*?await refreshMe\(\);/);
});

test("reading a thread refreshes the counter before the page draws", () => {
  // The guard runs BEFORE the view marks the ticket read, so without this the
  // badge would only catch up on the next navigation.
  for (const page of ["web/js/pages/support.js", "web/js/pages/admintickets.js"]) {
    const src = read(page);
    assert.match(src, /refreshMe/, page);
    assert.match(src, /import \{ render, refreshMe \} from "\.\.\/main\.js";/, page);
  }
});

test("remaining time is shown in days, not hours", () => {
  for (const page of ["web/js/pages/machine.js", "web/js/pages/billing.js"]) {
    const src = read(page);
    assert.match(src, /days_remaining/, page);
    assert.doesNotMatch(src, /hours_remaining/, page);
  }
  assert.equal(t("machine.days"), "روز");
});

// ---- reported: clicking the logo did not reach the homepage --------------
test("the console logo links to the public homepage", () => {
  const src = read("web/js/main.js");
  assert.match(src, /<a href="\/" class="brand"/);
  assert.doesNotMatch(src, /<a href="\/console" class="brand"/);
});
