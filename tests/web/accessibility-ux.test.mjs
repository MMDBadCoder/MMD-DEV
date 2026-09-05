import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const read = (path) => fs.readFileSync(path, "utf8");
const html = read("web/index.html");
const css = read("web/app.css");
const ui = read("web/js/ui.js");
const danger = read("web/js/dangerdialog.js");
const main = read("web/js/main.js");
const auth = read("web/js/pages/auth.js");
const users = read("web/js/pages/adminusers.js");
const charts = read("web/js/usagechart.js");

test("keyboard users can skip directly to console content", () => {
  assert.match(html, /class="skip-link"[^>]+href="#main-content"/);
  assert.match(main, /id="main-content"/);
  assert.match(css, /\.skip-link:focus/);
});

test("dialogs announce themselves, trap focus, and restore it", () => {
  assert.match(ui, /export function activateDialog/);
  assert.match(ui, /event\.key !== "Tab"/);
  assert.match(ui, /previous\?\.isConnected[^\n]+previous\.focus/);
  for (const source of [ui, danger]) {
    assert.match(source, /role="dialog"/);
    assert.match(source, /aria-modal="true"/);
    assert.match(source, /aria-labelledby/);
    assert.match(source, /activateDialog/);
  }
});

test("transient and background feedback is announced", () => {
  assert.match(html, /id="toasts"[^>]+aria-live="polite"/);
  assert.match(ui, /setAttribute\("role", kind === "bad" \? "alert" : "status"\)/);
  assert.match(main, /operation-strip[^>]+role="status"[^>]+aria-live="polite"/);
});

test("sign-in choices implement the tab keyboard pattern", () => {
  assert.match(auth, /role="tablist"/);
  assert.equal((auth.match(/role="tab"/g) || []).length, 2);
  assert.equal((auth.match(/role="tabpanel"/g) || []).length, 2);
  assert.match(auth, /aria-selected/);
  assert.match(auth, /ArrowLeft/);
  assert.match(auth, /ArrowRight/);
});

test("technical copy and reveal controls have accessible names", () => {
  const secret = ui.slice(ui.indexOf("export function secretRow"));
  assert.match(secret, /aria-label/);
  assert.match(secret, /common\.reveal/);
  assert.match(secret, /common\.copy/);
});

test("phone navigation keeps primary tasks visible and moves the rest into more", () => {
  assert.equal((main.match(/mobile: true/g) || []).length, 4);
  assert.match(main, /id="nav-more"/);
  assert.match(main, /id="mobile-nav-sheet"/);
  assert.match(main, /aria-controls="mobile-nav-sheet"/);
  assert.match(main, /wireMobileNav/);
  assert.match(css, /\.header \.nav a:not\(\.mobile-primary\)/);
});

test("motion preferences and sortable table state are exposed", () => {
  assert.match(css, /prefers-reduced-motion:reduce/);
  assert.match(users, /aria-sort/);
  assert.match(users, /aria-hidden="true"/);
});

test("spend charts pair hidden geometry with keyboard-readable data", () => {
  const spend = charts.slice(charts.indexOf("export function spendChart"),
                             charts.indexOf("export const LINE_COLOURS"));
  assert.match(spend, /aria-hidden="true"/);
  assert.match(spend, /<details/);
  assert.match(spend, /<table/);
});
