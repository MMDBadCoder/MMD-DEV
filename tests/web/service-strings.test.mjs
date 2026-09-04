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
const { t, hasKey } = await import(path.join(ROOT, "web/js/i18n.js"));

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

test("sign-in uses only username-or-phone and password", () => {
  assert.ok(signIn.includes('id="identifier"'), signIn);
  assert.ok(signIn.includes('id="pw"'), signIn);
  for (const field of ['id="email"', 'id="phone"', 'id="fullname"'])
    assert.ok(!signIn.includes(field), `${field} leaked into sign-in`);
  assert.match(signIn, /identifier:\s*\$\("#identifier"\)/);
});

test("sign-in does not call the username-availability endpoint", () => {
  assert.ok(!signIn.includes("username-available"), signIn);
});

test("sign-up collects every required identity field", () => {
  for (const field of ['id="fullname"', 'id="phone"', 'id="uname"', 'id="pw"'])
    assert.ok(signUp.includes(field), `${field} missing from signup`);
  assert.ok(!signUp.includes('id="email"'));
  assert.ok(signUp.includes("username-available"));
  assert.ok(signUp.includes("phone-available"));
});

test("the Hermes secrets are laid out one per row", () => {
  // An API key, a URL and a password are read character by character. The
  // auto-fit grid packed them into ~260px boxes that each scrolled sideways.
  const AI = read("web/js/pages/ai.js");
  const hermes = AI.slice(AI.indexOf("function renderHermes"),
                          AI.indexOf("function usageCard"));
  // Only the `details` section. The explanatory cards legitimately use an
  // auto-fit grid - those hold prose, not values to be copied. The credentials
  // used to sit above the action button; they are their own card now, so this
  // slices the section rather than a position on the page.
  const grid = hermes.slice(hermes.indexOf("details:"), hermes.indexOf("billing:"));
  assert.ok(grid.includes("grid-template-columns:1fr"), grid.slice(0, 400));
  assert.ok(!grid.includes("minmax(260px"), "still packing secrets into narrow columns");
});

/* ---- the Telegram walkthrough, removed ----------------------------------
 *
 * The Claude Code tab used to carry a six-step guide to wiring the CLI up to a
 * Telegram bot. It was accurate, and it was still the wrong thing to ship:
 * every step ran inside the customer's own machine using a third-party plugin,
 * so the platform was teaching a workflow it does not provide, cannot support
 * and does not control. Hermes has a Telegram gateway the platform actually
 * manages; that is where the feature belongs.
 *
 * The six tests that pinned the walkthrough went with it. This one replaces
 * them, because a deleted section is easy to reintroduce by accident.
 */
const AI_SRC = read("web/js/pages/ai.js");

test("the Claude tab no longer teaches Telegram setup", () => {
  for (const marker of ["telegramCard", "TG_STEPS", "claude-plugins-official",
                        "/telegram:configure"]) {
    assert.ok(!AI_SRC.includes(marker), `${marker} is back on the AI page`);
  }
  // ...and its strings went with it, rather than lingering as dead catalogue.
  for (const key of ["tg.title", "tg.s1", "tg.prereq"]) {
    assert.ok(!hasKey(key), `${key} is still in the catalogue`);
  }
});

/* Every AI tab explains what the thing IS before offering controls. */
test("every AI tab has a 'what is this' card", () => {
  for (const key of ["openrouter", "claude", "codex", "hermes", "openclaw"]) {
    assert.ok(AI.includes(`aboutCard("${key}")`), `${key} has no about card`);
    // A title is a few words ("Codex چیست" is ten characters); a body has to
    // actually explain something.
    assert.ok(t(`ai.about.${key}.title`), `ai.about.${key}.title is missing`);
    assert.ok(String(t(`ai.about.${key}.body`)).length > 80,
              `ai.about.${key}.body does not explain anything`);
  }
});

test("a tab waiting on the worker does not drag the customer back to it", () => {
  // The re-render timer keeps running after the customer navigates away.
  // Reported as "when you go to another page it turns you back to the OpenClaw
  // tab", so the poll must check where they are before re-rendering.
  assert.ok(AI.includes("currentPath() ==="), "repoll does not check the path");
  assert.ok(!/setTimeout\(\(\) => aiPage\(/.test(AI),
            "an unguarded self-poll is back");
});


/* ---- one order for all five tabs ----------------------------------------
 *
 * Reported as: the explainer is second on Claude Code and last elsewhere, the
 * usage table swaps places with it between two tabs, and the dashboard address
 * is the first thing on OpenClaw but buried mid-card on Hermes. Each was
 * defensible alone; the set was not.
 */
test("every tab renders through the shared section order", () => {
  for (const fn of ["renderOpenRouter", "renderClaude", "renderCodex",
                    "renderHermes", "renderOpenClaw"]) {
    const body = AI_SRC.slice(AI_SRC.indexOf(`function ${fn}`));
    const call = body.slice(0, body.indexOf("`);"));
    assert.ok(call.includes("sections({"),
              `${fn} builds its own layout instead of using sections()`);
  }
});

test("the order is declared once, and puts controls before reference", () => {
  const m = AI_SRC.match(/const SECTION_ORDER = \[([^\]]+)\]/);
  assert.ok(m, "SECTION_ORDER is gone");
  const order = m[1].split(",").map((s) => s.trim().replace(/"/g, "")).filter(Boolean);
  assert.deepEqual(order,
    ["status", "details", "usage", "billing", "about", "privacy"]);
});

test("no tab hand-places the cards the order controls", () => {
  // A tab that emits `${aboutCard(...)}` outside the sections() object has
  // opted out of the order without saying so.
  for (const key of ["status", "details", "usage", "billing", "about", "privacy"]) {
    void key;
  }
  const stray = AI_SRC.match(/\n\s+\$\{(aboutCard|usageCard)\(/g) || [];
  assert.deepEqual(stray, [], "a card is placed outside sections()");
});

test("the billing explainer sits with the supplier that does the billing", () => {
  // It describes how OpenRouter meters and charges, and BOTH Hermes and
  // OpenClaw spend that same key - so on the Hermes tab it was one supplier's
  // rules filed under one of its two consumers.
  const or = AI_SRC.slice(AI_SRC.indexOf("function renderOpenRouter"),
                          AI_SRC.indexOf("function renderHermes"));
  assert.ok(or.includes('t("ai.hermes.how.title")'),
            "the billing card did not move to the OpenRouter tab");
  const hermes = AI_SRC.slice(AI_SRC.indexOf("function renderHermes"),
                              AI_SRC.indexOf("function usageCard"));
  assert.ok(!hermes.includes('t("ai.hermes.how.body")'),
            "the billing card is still duplicated on the Hermes tab");
});


/* ---- the two Telegram sections are one feature --------------------------
 *
 * Hermes and OpenClaw offer the same thing over the same bot, built weeks
 * apart, and had drifted into different headers, pill wording and button
 * icons - which invites a customer to wonder what the difference is when
 * there is none.
 */
test("both services render the Telegram section through one shell", () => {
  assert.ok(AI_SRC.includes("function telegramSection("), "no shared section");
  const calls = (AI_SRC.match(/telegramSection\(\{/g) || []).length;
  assert.equal(calls, 2, `expected Hermes and OpenClaw to use it, found ${calls}`);
  // ...and neither hand-rolls its own header any more.
  assert.ok(!AI_SRC.includes('t("ai.openclaw.telegram.title")'),
            "OpenClaw still has its own heading");
});

test("the Telegram buttons carry Telegram's own mark", () => {
  // `chat` is a generic speech bubble - it reads as "messages", not as the one
  // service the section is about.
  const uiSrc = read("web/js/ui.js");
  assert.ok(uiSrc.includes("telegram: P("), "no telegram icon defined");
  const tg = AI_SRC.slice(AI_SRC.indexOf("function telegramSection"));
  const section = tg.slice(0, tg.indexOf("function repoll"));
  assert.ok(!section.includes("icon.chat"), "still using the generic chat bubble");
});

test("all seven AI tabs stay inside the page", () => {
  const css = read("web/app.css");
  assert.ok(AI_SRC.includes('class="tabs2 ai-tabs"'));
  assert.match(css, /\.ai-tabs\s*\{[^}]*flex-wrap:\s*wrap/);
});
