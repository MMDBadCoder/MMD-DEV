import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const router = fs.readFileSync("web/js/router.js", "utf8");
const main = fs.readFileSync("web/js/main.js", "utf8");
const connections = fs.readFileSync("web/js/pages/connections.js", "utf8");
const ai = fs.readFileSync("web/js/pages/ai.js", "utf8");
const machine = fs.readFileSync("web/js/pages/machine.js", "utf8");

test("a failed route renders a recoverable error instead of rejecting silently", () => {
  assert.match(router, /setErrorHandler/);
  assert.match(router, /await r\.view\(params\)/);
  assert.match(main, /setErrorHandler\(\(error\)/);
  assert.match(main, /wireRecovery\(\(\) => navigate/);
});

test("stale asynchronous routes restore the current URL", () => {
  assert.match(router, /const mine = \+\+generation/);
  assert.match(router, /if \(mine !== generation\) return resolve\(\)/);
});

test("connection failures remain visible and do not immediately redraw", () => {
  const ssh = connections.slice(connections.indexOf('$("#ssh-toggle").onclick'),
                                connections.indexOf('/* ---- rdp ----'));
  const rdp = connections.slice(connections.indexOf('$("#rdp-toggle").onclick'),
                                connections.indexOf("function codeBlock"));
  for (const handler of [ssh, rdp]) {
    assert.match(handler, /recoveryNote/);
    assert.match(handler, /b\.disabled = false/);
    assert.doesNotMatch(handler, /}\s*connectionsPage\(/);
  }
});

test("background refreshes pause while the document is hidden", () => {
  assert.match(main, /document\.hidden \|\| !state\.me/);
  assert.match(ai, /if \(document\.hidden\) return repoll/);
  assert.match(machine, /if \(document\.hidden\)/);
});

test("global polling is single-flight, retains good state, and backs off", () => {
  assert.match(main, /shellRefreshRunning/);
  assert.match(main, /Promise\.all\(\[refreshOperations\(\), refreshNotifications\(\)\]\)/);
  assert.match(main, /Math\.min\(Math\.max\(shellRefreshDelay \* 2, 6000\), 60000\)/);
  assert.doesNotMatch(main, /catch \{ state\.operations = \[\]; \}/);
  assert.doesNotMatch(main, /setInterval\(async/);
});

test("an expired session stops background polling and returns to sign-in", () => {
  assert.match(main, /if \(error\.status === 401\) state\.me = null/g);
  assert.match(main, /if \(!state\.me\) \{\s*shellRefreshRunning = false;\s*navigate\("\/signin"\)/);
});

test("notification links cannot leave the authenticated console", () => {
  assert.match(main, /startsWith\("\/console"\)/);
});

test("an embedded dashboard is only rendered where its config was fetched", () => {
  // A `_gf is not defined` ReferenceError reached production this way: an
  // automated edit put `dashboardCard(_gf, …)` into the ticket DETAIL view,
  // whose function never called grafanaConfig(). Syntax checks pass, every
  // test passed, and the page died the moment an operator opened one ticket.
  //
  // Checked per function rather than per file, because a file can hold both a
  // list view that fetches the config and a detail view that does not.
  const dir = "web/js/pages";
  const offenders = [];
  for (const name of fs.readdirSync(dir).filter((f) => f.endsWith(".js"))) {
    const src = fs.readFileSync(`${dir}/${name}`, "utf8");
    if (!src.includes("dashboardCard(")) continue;
    const starts = [...src.matchAll(/^(?:export )?async function (\w+)/gm)]
      .map((m) => ({ at: m.index, fn: m[1] }));
    starts.push({ at: src.length, fn: null });
    for (let i = 0; i < starts.length - 1; i += 1) {
      const body = src.slice(starts[i].at, starts[i + 1].at);
      if (body.includes("dashboardCard(") && !body.includes("grafanaConfig()")) {
        offenders.push(`${name}:${starts[i].fn}`);
      }
    }
  }
  assert.deepEqual(offenders, [],
                   "these render a dashboard without fetching its config");
});
