/* Claude Code and Hermes are two services. Their words must not be shared.
 *
 * Reported twice, the same mistake in two layers:
 *
 *   1. The API validated /api/workspace/ai/hermes with `AiAction`, whose
 *      pattern is Claude Code's vocabulary, so enabling Hermes answered
 *      «action String should match pattern '^(install|unlink)$'».
 *   2. Enabling Hermes then toasted `ai.done`, which reads
 *      «Claude Code آمادهٔ استفاده است» - the wrong product entirely.
 *
 * Both came from one assumption: that two AI features are the same feature.
 * They are not. Claude Code is INSTALLED on a machine; Hermes is ENABLED as an
 * intent a worker reconciles. This pins the interface half.
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");
const { t } = await import(path.join(ROOT, "web/js/i18n.js"));

const AI = read("web/js/pages/ai.js");
// The Hermes half of the page: from its click handler to the end of the block.
const HERMES = AI.slice(AI.indexOf('$("#hermes-go").onclick'),
                        AI.indexOf("/* What the tokens have actually cost."));

test("the Hermes handler exists and posts the enable/disable verbs", () => {
  assert.ok(HERMES.includes('"disable" : "enable"'), HERMES);
});

test("the Hermes handler does not borrow Claude Code's toast", () => {
  // `ai.done` names the other product out loud. Reusing it here is how a
  // customer got told Claude Code was ready after enabling Hermes.
  assert.ok(!HERMES.includes('t("ai.done")'), HERMES);
  assert.ok(!HERMES.includes('t("ai.unlinked")'), HERMES);
});

test("Hermes has its own sentences, and they do not mention Claude", () => {
  for (const key of ["ai.hermes.done.enabled", "ai.hermes.done.disabled"]) {
    const s = t(key);
    assert.ok(s && s.length > 5, `${key} is missing`);
    assert.ok(!/claude/i.test(s), `${key} names the wrong product: ${s}`);
    assert.ok(/Hermes/.test(s), `${key} should name Hermes: ${s}`);
  }
});

test("Claude Code's own string still names Claude Code", () => {
  // The fix must not have been "make ai.done generic", which would have
  // removed information from the page that was correct where it was used.
  assert.ok(/Claude Code/.test(t("ai.done")), t("ai.done"));
});

/* Sign-in and signup have deliberately different identity fields. */
const AUTH = read("web/js/pages/auth.js");
const signIn = AUTH.slice(AUTH.indexOf("export function signInPage"),
                          AUTH.indexOf("export function signUpPage"));
const signUp = AUTH.slice(AUTH.indexOf("export function signUpPage"));

test("sign-in uses only username and password", () => {
  assert.ok(signIn.includes('id="username"'), signIn);
  assert.ok(signIn.includes('id="pw"'), signIn);
  for (const field of ['id="email"', 'id="phone"', 'id="fullname"'])
    assert.ok(!signIn.includes(field), `${field} leaked into sign-in`);
  assert.match(signIn, /username:\s*\$\("#username"\)/);
});

test("sign-in does not call the username-availability endpoint", () => {
  assert.ok(!signIn.includes("username-available"), signIn);
});

test("sign-up collects every required identity field", () => {
  for (const field of ['id="fullname"', 'id="phone"', 'id="email"', 'id="uname"', 'id="pw"'])
    assert.ok(signUp.includes(field), `${field} missing from signup`);
  assert.ok(signUp.includes("username-available"));
});

test("the Hermes secrets are laid out one per row", () => {
  // An API key, a URL and a password are read character by character. The
  // auto-fit grid packed them into ~260px boxes that each scrolled sideways.
  const AI = read("web/js/pages/ai.js");
  const hermes = AI.slice(AI.indexOf("function renderHermes"),
                          AI.indexOf("function usageCard"));
  // Only the secrets block. The explanatory card below it legitimately uses
  // an auto-fit grid - that one holds prose, not values to be copied.
  const grid = hermes.slice(hermes.indexOf("h.ready ?"), hermes.indexOf('class="btn-row"'));
  assert.ok(grid.includes("grid-template-columns:1fr"), grid.slice(0, 400));
  assert.ok(!grid.includes("minmax(260px"), "still packing secrets into narrow columns");
});

/* ---- the Telegram channel documentation ---------------------------------
 *
 * Documented in the product rather than linked away, because the two things
 * that decide whether it works at all are properties of this platform: the
 * workspace must be running, and it must reach api.telegram.org. Measured on
 * the host - it answers from a workspace in 24 ms - and `--channels` is
 * accepted by the installed CLI even though `claude --help` does not list it.
 */
const AI_SRC = read("web/js/pages/ai.js");

test("the Telegram section sits under the sign-in card, not at the end", () => {
  // It is a follow-on from being signed in; below the usage and privacy cards
  // it would read as unrelated.
  const tg = AI_SRC.indexOf("${telegramCard()}");
  const usage = AI_SRC.indexOf("${usageCard(usage)}");
  assert.ok(tg > 0 && usage > 0, "both cards must render");
  assert.ok(tg < usage, "the Telegram card must come before the usage card");
});

test("every documented step carries the exact command", () => {
  for (const cmd of [
    "/plugin install telegram@claude-plugins-official",
    "/telegram:configure <TOKEN>",
    "claude --channels plugin:telegram@claude-plugins-official",
    "/telegram:access pair <CODE>",
    "/telegram:access policy allowlist",
  ]) {
    assert.ok(AI_SRC.includes(cmd), `missing: ${cmd}`);
  }
});

test("the lock-down step is documented, not left as an afterthought", () => {
  // Until the policy is `allowlist`, anyone who knows the bot can pair with it.
  assert.ok(AI_SRC.includes("/telegram:access policy allowlist"));
  assert.ok(/allowlist/.test(t("tg.s6")) === false, "the command belongs in markup");
  assert.ok(t("tg.s6").length > 20, "the step must explain WHY, not just show a command");
});

test("each step has a Persian sentence to go with its command", () => {
  for (const k of ["tg.s1", "tg.s2", "tg.s3", "tg.s4", "tg.s5", "tg.s6"]) {
    const v = t(k);
    assert.ok(v && v !== k, `${k} is missing`);
    assert.ok(/[؀-ۿ]/.test(v), `${k} is not Persian: ${v}`);
  }
});

test("the prerequisites are stated before the steps", () => {
  // Nobody should work through six steps to find out the machine had to be on.
  // Scoped to telegramCard: `${rows}` appears in usageCard too, which sits
  // earlier in the file, so a whole-file indexOf compares the wrong things.
  const card = AI_SRC.slice(AI_SRC.indexOf("function telegramCard"),
                            AI_SRC.indexOf("function renderClaude"));
  const prereq = card.indexOf('t("tg.prereq")');
  const steps = card.indexOf("${rows}");
  assert.ok(prereq > 0 && steps > 0, "both must render inside the card");
  assert.ok(prereq < steps, "prerequisites must precede the steps");
  assert.ok(/[؀-ۿ]/.test(t("tg.prereq")));
});

test("it links to the upstream README", () => {
  assert.ok(AI_SRC.includes(
    "github.com/anthropics/claude-plugins-official/blob/main/external_plugins/telegram/README.md"));
});
