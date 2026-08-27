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
  if (diff < 60) return "همین حالا";
  if (diff < 3600) return `${Math.floor(diff / 60)} دقیقه پیش`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ساعت پیش`;
  if (diff < 604800) return `${Math.floor(diff / 86400)} روز پیش`;
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
    stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ${extra}>${d}</svg>`;

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
  link: P('<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>'),
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
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => {
    el.style.transition = "opacity .25s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 260);
  }, kind === "bad" ? 6000 : 3200);
}

export function note(kind, html) {
  const ic = { ok: icon.check, warn: icon.alert, bad: icon.alert, info: icon.info }[kind] || "";
  return `<div class="note ${kind}">${ic}<div>${html}</div></div>`;
}

export function empty(text, ico = icon.info) {
  return `<div class="empty">${ico.replace('width="16" height="16"', 'width="34" height="34"')}
    <div>${esc(text)}</div></div>`;
}

/* ---- theme ---- */
export function currentTheme() {
  return localStorage.getItem("mmd-theme")
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

export function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  localStorage.setItem("mmd-theme", next);
  document.documentElement.setAttribute("data-theme", next);
  window.dispatchEvent(new CustomEvent("mmd:theme", { detail: next }));
}

/* ---- confirmation ---- */
export function confirmDialog(title, body, confirmLabel = "تأیید") {
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.style.cssText = "position:fixed;inset:0;z-index:150;display:grid;place-items:center;background:rgba(0,0,0,.45);padding:20px";
    wrap.innerHTML = `<div class="card" style="max-width:420px;width:100%;margin:0">
      <h2>${esc(title)}</h2><p class="muted small">${body}</p>
      <div class="btn-row" style="justify-content:flex-end;margin-top:16px">
        <button class="btn ghost" data-no>انصراف</button>
        <button class="btn danger" data-yes>${esc(confirmLabel)}</button>
      </div></div>`;
    const done = (v) => { wrap.remove(); resolve(v); };
    wrap.querySelector("[data-no]").onclick = () => done(false);
    wrap.querySelector("[data-yes]").onclick = () => done(true);
    wrap.onclick = (e) => { if (e.target === wrap) done(false); };
    document.body.append(wrap);
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
        title="${esc(label)}">${icon.eye || "👁"}</button>` : ""}
      <button class="btn ghost small secret-copy" data-for="${id}">${icon.copy || "⧉"}</button>
    </div>
    ${hint ? `<p class="muted small" style="margin:6px 0 0">${esc(hint)}</p>` : ""}
  </div>`;
}

/* Wire every secretRow under `root`. Call after render. */
export function wireSecrets(root = document, copiedLabel = "کپی شد") {
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
