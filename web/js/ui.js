/* Small DOM helpers, icons and shared chrome. */
import { t } from "./i18n.js";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export { fmtMoney, fmtNum, fmtFa, money, moneyPerHour } from "./i18n.js";

export function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso), diff = (Date.now() - d) / 1000;
  if (diff < 60) return t("time.now");
  if (diff < 3600) return t("time.minutesAgo", Math.floor(diff / 60));
  if (diff < 86400) return t("time.hoursAgo", Math.floor(diff / 3600));
  if (diff < 604800) return t("time.daysAgo", Math.floor(diff / 86400));
  return d.toLocaleDateString("fa-IR");
}

/* Gregorian dates rendered with the Persian calendar, which is what a Persian
   reader expects to see next to Persian text. */
export const stamp = (iso) => (iso
  ? new Date(iso).toLocaleString("fa-IR",
      { dateStyle: "short", timeStyle: "short" })
  : "—");

/* Inline SVG so the whole app stays self-contained - no icon font, no CDN. */
const P = (d, extra = "") =>
  `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
    aria-hidden="true" focusable="false" ${extra}>${d}</svg>`;

export const icon = {
  machine: P('<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M7 21h10M12 17v4"/>'),
  power: P('<path d="M12 3v9"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/>'),
  sliders: P('<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>'),
  plug: P('<path d="M9 2v6M15 2v6M7 8h10v4a5 5 0 0 1-10 0z"/><path d="M12 17v5"/>'),
  card: P('<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/>'),
  clock: P('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
  shield: P('<path d="M12 3l8 3v6c0 5-3.4 8.4-8 9-4.6-.6-8-4-8-9V6z"/>'),
  users: P('<path d="M16 20v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 20v-2a4 4 0 0 0-3-3.9"/>'),
  logout: P('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5M21 12H9"/>'),
  sun: P('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
  moon: P('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>'),
  plus: P('<path d="M12 5v14M5 12h14"/>'),
  trash: P('<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/>'),
  copy: P('<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'),
  eye: P('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>'),
  expand: P('<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>'),
  shrink: P('<path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/>'),
  check: P('<path d="M20 6L9 17l-5-5"/>'),
  alert: P('<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>'),
  info: P('<circle cx="12" cy="12" r="9"/><path d="M12 16v-5M12 8h.01"/>'),
  minus: P('<path d="M5 12h14"/>'),
  refresh: P('<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>'),
  box: P('<path d="M21 8v8a2 2 0 0 1-1 1.7l-7 4a2 2 0 0 1-2 0l-7-4A2 2 0 0 1 3 16V8a2 2 0 0 1 1-1.7l7-4a2 2 0 0 1 2 0l7 4A2 2 0 0 1 21 8z"/><path d="M3.3 7L12 12l8.7-5M12 22V12"/>'),
  bolt: P('<path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z"/>'),
  cpu: P('<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>'),
  sparkle: P('<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/>'),
  claude: P('<path d="M12 2v20M2 12h20M4.9 4.9l14.2 14.2M19.1 4.9L4.9 19.1"/><circle cx="12" cy="12" r="3"/>'),
  openrouter: P('<path d="M3 7h13l-3-3M16 7l-3 3M21 17H8l3-3M8 17l3 3"/>'),
  hermes: P('<path d="M12 3v18M8 6c-3-2-5 0-5 2 3 0 5 1 5 4M16 6c3-2 5 0 5 2-3 0-5 1-5 4"/><path d="M9 15c0 2 6 2 6 4"/>'),
  // OpenAI's mark, in the same stylised spirit as `claude` above: the
  // hexagonal outline with the three-fold interior knot, drawn in strokes
  // rather than reproduced. Recognisable beside the product name, which is
  // what a tab needs it to be.
  codex: P('<path d="M12 2.6l8.1 4.7v9.4L12 21.4 3.9 16.7V7.3z"/>'
           + '<path d="M12 7.4v9.2M8.1 9.7l7.8 4.6M15.9 9.7l-7.8 4.6"/>'),
  // A claw, for the product named after one. Three talons over a palm reads at
  // 16px where a literal crab pincer turns to mush.
  openclaw: P('<path d="M6.5 3.2c-1 4.8-.2 8.4 2.4 11.3"/>'
              + '<path d="M12 2.2c-.5 5.6-.3 9.6 0 12.3"/>'
              + '<path d="M17.5 3.2c1 4.8.2 8.4-2.4 11.3"/>'
              + '<path d="M6.6 14.6c1.7 3.5 9.1 3.5 10.8 0"/>'),
  opencode: P('<path d="M8 7 3 12l5 5M16 7l5 5-5 5M14 4l-4 16"/>'),
  openwebui: P('<path d="M3 5h18v14H3zM3 9h18"/><circle cx="6" cy="7" r=".5"/><path d="M8 14h8M10 17h4"/>'),
  lock: P('<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>'),
  save: P('<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/>'),
  arrow: P('<path d="M5 12h14M13 6l6 6-6 6"/>'),
  download: P('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5M12 15V3"/>'),
  upload: P('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5M12 3v12"/>'),
  archive: P('<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M10 13h4"/>'),
  folder: P('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
  folderplus: P('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 11v6M9 14h6"/>'),
  home: P('<path d="M3 10l9-7 9 7v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9 21v-8h6v8"/>'),
  arrowup: P('<path d="M12 19V5M5 12l7-7 7 7"/>'),
  monitor: P('<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/><path d="M6 8h6M6 11h4"/>'),
  chart: P('<path d="M3 3v18h18"/><path d="M7 15l4-5 3 3 5-7"/>'),
  chat: P('<path d="M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'),
  // Telegram's paper plane. `chat` was standing in for it on the Hermes
  // button, which is a generic speech bubble and reads as "messages" rather
  // than as the one service the section is about.
  telegram: P('<path d="M22 2 15 22l-4-9-9-4z"/><path d="M22 2 11 13"/>'),
  bell: P('<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/>'),
  more: P('<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>'),
  link: P('<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>'),

  // Four nav marks that were standing in for concepts they do not mean.
  //
  // `user` is ONE person, for the account page. `users` - a group - was doing
  // that job on the admin link and reads as "the customer list", which is a
  // page inside administration rather than administration itself.
  user: P('<circle cx="12" cy="8" r="4"/><path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1"/>'),
  // A handset, not a bubble. The SMS page was wearing `bell`, which means
  // "notification" in every interface that has one, and a bubble here would
  // be indistinguishable from support beside it.
  phone: P('<rect x="6" y="2" width="12" height="20" rx="2.5"/><path d="M11 18.5h2"/><path d="M9 6h6"/>'),
  // A lifebuoy: the one shape that reads as HELP rather than as messaging.
  // Support was wearing `chat`, a speech bubble, which is what a customer
  // would reasonably read as "send a message" - the same thing the SMS page
  // beside it does.
  lifebuoy: P('<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.6"/>'
              + '<path d="M5.6 5.6l3.9 3.9M14.5 14.5l3.9 3.9M18.4 5.6l-3.9 3.9M9.5 14.5l-3.9 3.9"/>'),
  // A cog for administration. Settings, not people.
  cog: P('<circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 9 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 9a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/>'),
};

/* Machine state as a pill with a coloured dot.
 *
 * Lives here rather than in one page because the admin list used to print the
 * state as bare text while every other view showed a pill - so the one screen
 * that shows every machine at once was the hardest to read at a glance, sitting
 * next to an account-status column that DID have the treatment.
 *
 * green = running, amber = mid-change, red = needs attention, grey = stopped. */
const STATE_TONE = {
  on: "on",
  starting: "busy", stopping: "busy", provisioning: "busy",
  resetting: "busy", archiving: "busy", pending: "busy",
  archived: "bad", error: "bad", deleting: "bad",
};

export function statePill(state) {
  const label = t("machine.state." + state);
  return `<span class="pill"><span class="dot ${STATE_TONE[state] || ""}"></span>${
    label === "machine.state." + state ? esc(state) : label}</span>`;
}

export function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.setAttribute("role", kind === "bad" ? "alert" : "status");
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => {
    el.style.transition = "opacity .25s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 260);
  }, kind === "bad" ? 6000 : 3200);
}

/* One keyboard contract for every modal. `cleanup` restores focus to the
   control that opened it, so closing a dialog never strands a keyboard user at
   the top of the document. */
export function activateDialog(wrap, initial, onCancel) {
  const previous = document.activeElement;
  const focusable = () => [...wrap.querySelectorAll(
    'button:not([disabled]),input:not([disabled]),a[href],select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')];
  const keydown = (event) => {
    if (event.key === "Escape") { event.preventDefault(); onCancel(); return; }
    if (event.key !== "Tab") return;
    const items = focusable();
    if (!items.length) { event.preventDefault(); return; }
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  };
  document.addEventListener("keydown", keydown);
  (initial || focusable()[0])?.focus();
  return () => {
    document.removeEventListener("keydown", keydown);
    if (previous?.isConnected) previous.focus();
  };
}

export function note(kind, html) {
  const ic = { ok: icon.check, warn: icon.alert, bad: icon.alert, info: icon.info }[kind] || "";
  const live = kind === "bad" ? ' role="alert"' : kind === "ok" ? ' role="status"' : "";
  return `<div class="note ${kind}"${live}>${ic}<div>${html}</div></div>`;
}

let formErrorSequence = 0;

export function clearFormErrors(form = document) {
  const root = typeof form === "string" ? $(form) : form;
  if (!root) return;
  $$('[aria-invalid="true"]', root).forEach((input) => {
    input.removeAttribute("aria-invalid");
    const errorId = input.dataset.formErrorId;
    if (errorId) {
      const ids = (input.getAttribute("aria-describedby") || "")
        .split(/\s+/).filter((id) => id && id !== errorId);
      if (ids.length) input.setAttribute("aria-describedby", ids.join(" "));
      else input.removeAttribute("aria-describedby");
      delete input.dataset.formErrorId;
    }
  });
  $$("[data-form-error]", root).forEach((el) => el.remove());
}

/* Keep the summary for sighted recovery, and connect the same failure to the
   exact field for assistive technology. The API code chooses the field; the
   API's English detail is never rendered. */
export function formError(message, { form = "form", messageRoot = "#msg", field = null } = {}) {
  const formEl = typeof form === "string" ? $(form) : form;
  clearFormErrors(formEl);
  const messageEl = typeof messageRoot === "string" ? $(messageRoot) : messageRoot;
  if (messageEl) messageEl.innerHTML = note("bad", esc(message));
  const input = field ? $(field, formEl || document) : null;
  if (!input) return;
  const id = `form-error-${++formErrorSequence}`;
  const error = document.createElement("p");
  error.id = id;
  error.className = "tiny bad-text field-error";
  error.dataset.formError = "true";
  error.textContent = message;
  input.closest(".field")?.append(error);
  input.setAttribute("aria-invalid", "true");
  input.dataset.formErrorId = id;
  input.setAttribute("aria-describedby", [input.getAttribute("aria-describedby"), id]
    .filter(Boolean).join(" "));
  input.focus();
}

const RECOVERY = {
  insufficient_credit: ["recovery.credit", "/console/billing"],
  mem_shrink_running: ["recovery.poweroff", "/console"],
  machine_off: ["recovery.poweron", "/console"],
  no_ssh_key: ["recovery.sshkey", "/console/connections/ssh"],
  rdp_needs_memory: ["recovery.resources", "/console/resources"],
  apt_repair_failed: ["recovery.support", "/console/support"],
  reset_failed: ["recovery.support", "/console/support"],
  power_failed: ["recovery.retry", null],
  resize_failed: ["recovery.retry", null],
  port_failed: ["recovery.retry", null],
  fs_failed: ["recovery.retry", null],
};

/* An error is incomplete until it says what can be done next. Pages may wire
   data-recovery-retry to repeat their own request; known state changes use a
   normal link so recovery remains usable with keyboard navigation. */
export function recoveryNote(err, { retry = false } = {}) {
  const action = RECOVERY[err?.code] || (retry
    ? ["recovery.retry", null] : ["recovery.support", "/console/support"]);
  const control = action[1]
    ? `<a class="btn sm ghost" href="${action[1]}">${t(action[0])}${icon.arrow}</a>`
    : `<button class="btn sm ghost" data-recovery-retry>${icon.refresh}${t(action[0])}</button>`;
  return note("bad", `<div class="recovery-error"><div>${esc(err?.message || t("recovery.unknown"))}</div>${control}</div>`);
}

export function wireRecovery(retry, root = document) {
  const button = root.querySelector("[data-recovery-retry]");
  if (button && retry) button.onclick = retry;
}

export function empty(text, ico = icon.info, action = null) {
  const control = action ? `<a class="btn primary" href="${esc(action.href)}">${
    action.icon ? icon[action.icon] || "" : ""}${esc(action.label)}</a>` : "";
  return `<div class="empty">${ico.replace('width="16" height="16"', 'width="34" height="34"')}
    <div>${esc(text)}</div>${control}</div>`;
}

/* ---- theme ---- */
export function currentTheme() {
  try {
    return localStorage.getItem("mmd-theme")
        || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  } catch {
    return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
}

export function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  try { localStorage.setItem("mmd-theme", next); } catch { /* appearance still changes */ }
  document.documentElement.setAttribute("data-theme", next);
  window.dispatchEvent(new CustomEvent("mmd:theme", { detail: next }));
}

/* ---- confirmation ---- */
/* opts.cancelLabel  - the second button's text. Defaults to the localized cancel label, which is
                       right for a destructive question and wrong for an
                       informational one where BOTH answers are a real choice.
   opts.tone          - "danger" (default) or "primary" for the confirm button. */
export function confirmDialog(title, body, confirmLabel = null, opts = {}) {
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "danger-wrap";
    const titleId = `dialog-title-${Math.random().toString(36).slice(2)}`;
    wrap.innerHTML = `<div class="card" role="dialog" aria-modal="true"
      aria-labelledby="${titleId}" style="max-width:420px;width:100%;margin:0">
      <h2 id="${titleId}">${esc(title)}</h2><div class="muted small">${body}</div>
      <div class="btn-row" style="justify-content:flex-end;margin-top:16px">
        <button class="btn ghost" data-no>${esc(opts.cancelLabel || t("common.cancel"))}</button>
        <button class="btn ${opts.tone === "primary" ? "primary" : "danger"}"
                data-yes>${esc(confirmLabel || t("common.confirm"))}</button>
      </div></div>`;
    let cleanup = () => {};
    const done = (v) => { cleanup(); wrap.remove(); resolve(v); };
    wrap.querySelector("[data-no]").onclick = () => done(false);
    wrap.querySelector("[data-yes]").onclick = () => done(true);
    wrap.onclick = (e) => { if (e.target === wrap) done(false); };
    document.body.append(wrap);
    cleanup = activateDialog(wrap, wrap.querySelector("[data-no]"), () => done(false));
  });
}

/* Destructive account-level actions need the same visual accounting as a
   factory reset, even when the server endpoint does not require a password.
   Re-typing the affected identity prevents deleting the adjacent table row. */
export function destructiveDialog({ title, intro, destroys, keeps = [], expect, label }) {
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "danger-wrap";
    const titleId = `dialog-title-${Math.random().toString(36).slice(2)}`;
    wrap.innerHTML = `<div class="card danger-card" role="dialog" aria-modal="true" aria-labelledby="${titleId}">
      <h2 class="danger-title" id="${titleId}">${esc(title)}</h2><p class="muted small">${esc(intro)}</p>
      <div class="impact-grid">
        <div class="copybox bad"><div class="copybox-h">${t("danger.deleted")}</div>
          <ul>${destroys.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
        <div class="copybox ok"><div class="copybox-h">${t("danger.kept")}</div>
          <ul>${keeps.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
      </div>
      <div class="note bad">${icon.alert}<div>${t("danger.irrecoverable")}</div></div>
      <label for="destructive-expect">${t("danger.type", expect)}</label>
      <input id="destructive-expect" class="ltr mono" dir="ltr" autocomplete="off"
        spellcheck="false" placeholder="${esc(expect)}">
      <div class="btn-row dialog-actions">
        <button class="btn ghost" data-no>${t("common.cancel")}</button>
        <button class="btn danger" data-yes disabled>${esc(label)}</button>
      </div></div>`;
    const input = wrap.querySelector("#destructive-expect");
    const yes = wrap.querySelector("[data-yes]");
    input.oninput = () => { yes.disabled = input.value.trim().toLowerCase() !== expect.toLowerCase(); };
    let cleanup = () => {};
    const done = (value) => { cleanup(); wrap.remove(); resolve(value); };
    wrap.querySelector("[data-no]").onclick = () => done(false);
    yes.onclick = () => done(true);
    document.body.append(wrap);
    cleanup = activateDialog(wrap, input, () => done(false));
  });
}


/* A credential the customer is meant to have: shown on demand, copied in one
   click.

   Hidden by default rather than printed, because these pages get screen-shared
   and screenshotted when someone is asking for help - the moment a support
   conversation starts is exactly when a secret is most likely to be captured.
   Hiding it is not access control (the value is in the DOM either way); it
   stops the accidental disclosure, which is the realistic threat.

   The value is written with textContent, never interpolated into HTML. */
export function secretRow({ label, value, hint = "", masked = true }) {
  const id = "sec-" + Math.random().toString(36).slice(2, 9);
  return `<div class="stat" style="align-items:stretch">
    <div class="k">${esc(label)}</div>
    <div class="between" style="gap:8px;margin-top:4px">
      <code class="ltr mono secret-v" id="${id}" data-v="${esc(value ?? "")}"
        data-masked="${masked ? "1" : "0"}"
        style="flex:1;min-width:0;overflow-x:auto;white-space:nowrap;font-size:14px;
               padding:7px 10px;border-radius:8px;background:var(--surface);
               border:1px solid var(--border)"
        >${masked ? "••••••••••••" : esc(value ?? "")}</code>
      ${masked ? `<button class="btn ghost small secret-eye" data-for="${id}"
        title="${esc(label)}" aria-label="${t("common.reveal")} ${esc(label)}">${icon.eye || "👁"}</button>` : ""}
      <button class="btn ghost small secret-copy" data-for="${id}"
        aria-label="${t("common.copy")} ${esc(label)}">${icon.copy || "⧉"}</button>
    </div>
    ${hint ? `<p class="muted small" style="margin:6px 0 0">${esc(hint)}</p>` : ""}
  </div>`;
}

/* Wire every secretRow under `root`. Call after render. */
export function wireSecrets(root = document, copiedLabel = t("common.copied")) {
  $$(".secret-eye", root).forEach((b) => {
    b.onclick = () => {
      const el = $("#" + b.dataset.for, root);
      if (!el) return;
      const shown = el.dataset.masked === "0";
      el.dataset.masked = shown ? "1" : "0";
      el.textContent = shown ? "••••••••••••" : (el.dataset.v || "");
    };
  });
  $$(".secret-copy", root).forEach((b) => {
    b.onclick = async () => {
      const el = $("#" + b.dataset.for, root);
      if (!el) return;
      try {
        await navigator.clipboard.writeText(el.dataset.v || "");
        toast(copiedLabel, "ok");
      } catch { toast(copiedLabel, "bad"); }
    };
  });
}
