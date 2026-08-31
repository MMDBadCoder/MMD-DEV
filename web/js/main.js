/* MMD-DEV console shell: session, chrome, routes. */
import { get, post } from "./api.js";
import { $, icon, fmtMoney, fmtFa, toast, currentTheme, toggleTheme } from "./ui.js";
import { t } from "./i18n.js";
import { route, setGuard, setNotFound, startRouter, navigate, currentPath } from "./router.js";

import { landingPage } from "./pages/landing.js";
import { signInPage, signUpPage } from "./pages/auth.js";
import { machinePage, teardownTerminal } from "./pages/machine.js";
import { filesPage } from "./pages/files.js";
import { resourcesPage } from "./pages/resources.js";
import { portsPage } from "./pages/ports.js";
import { connectionsPage } from "./pages/connections.js";
import { billingPage } from "./pages/billing.js";
import { activityPage } from "./pages/activity.js";
import { securityPage } from "./pages/security.js";
import { adminPage } from "./pages/admin.js";
import { adminUsersPage } from "./pages/adminusers.js";
import { adminUserPage } from "./pages/adminuser.js";
import { adminMonitorPage } from "./pages/adminmonitor.js";
import { adminHermesPage } from "./pages/adminhermes.js";
import { adminOpenRouterPage } from "./pages/adminopenrouter.js";
import { aiPage } from "./pages/ai.js";
import { supportPage } from "./pages/support.js";
import { adminTicketsPage } from "./pages/admintickets.js";
import { aiPricingPage } from "./pages/aipricing.js";
import { adminStoragePage } from "./pages/adminstorage.js";
import { adminOpenClawPage } from "./pages/adminopenclaw.js";
import { adminBackupPage } from "./pages/adminbackup.js";

export const state = { me: null, operations: [], notifications: [], notificationUnread: 0 };

export async function refreshMe() {
  try {
    state.me = await get("/api/me");
  } catch {
    state.me = null;
  }
  return state.me;
}

async function refreshOperations() {
  if (!state.me) { state.operations = []; return; }
  try { state.operations = (await get("/api/operations")).operations || []; }
  catch { state.operations = []; }
}

async function refreshNotifications() {
  if (!state.me) { state.notifications = []; state.notificationUnread = 0; return; }
  try {
    const out = await get("/api/notifications");
    state.notifications = out.notifications || [];
    state.notificationUnread = out.unread || 0;
  } catch { state.notifications = []; state.notificationUnread = 0; }
}

function notificationCenter() {
  const rows = state.notifications.length ? state.notifications.map((n) => `
    <a class="notification-item ${n.read ? "" : "unread"} ${n.severity}"
       href="${n.href || "/console"}" data-notification="${n.id}">
      <span>${icon[n.severity === "success" ? "check" : n.severity === "info" ? "info" : "alert"]}</span>
      <div><b>${t("notification." + n.code, n.detail)}</b>
        <small>${new Date(n.created_at).toLocaleString("fa-IR")}</small></div>
    </a>`).join("") : `<div class="empty small">${t("notification.empty")}</div>`;
  return `<aside class="notification-panel" id="notification-panel" hidden>
    <div class="notification-head"><b>${t("nav.notifications")}</b>
      ${state.notificationUnread ? `<button class="btn sm ghost" id="notifications-read">${t("notification.readall")}</button>` : ""}
    </div><div class="notification-list">${rows}</div></aside>`;
}

function operationStrip() {
  const op = state.operations.find((o) => ["queued", "running"].includes(o.status))
    || state.operations.find((o) => o.status === "failed");
  if (!op) return "";
  const failed = op.status === "failed";
  return `<div class="operation-strip ${failed ? "failed" : ""}">
    ${failed ? icon.alert : '<span class="spinner"></span>'}
    <div><b>${t("op.kind." + op.kind)}</b>
      <span>${t("op.progress." + op.progress_code)}</span></div>
    <small>${t("op.status." + op.status)}</small>
  </div>`;
}

// Connections sits second, right after the overview: it is what a developer
// opens most often once the machine is running.
const NAV = [
  { href: "/console", key: "nav.overview", ic: "chart" },
  { href: "/console/connections", key: "nav.connections", ic: "link" },
  { href: "/console/files", key: "nav.files", ic: "folder" },
  { href: "/console/resources", key: "nav.resources", ic: "sliders" },
  { href: "/console/ai", key: "nav.ai", ic: "sparkle" },
  { href: "/console/ports", key: "nav.ports", ic: "plug" },
  { href: "/console/billing", key: "nav.billing", ic: "card" },
  { href: "/console/activity", key: "nav.activity", ic: "clock" },
  { href: "/console/account", key: "nav.account", ic: "shield" },
  { href: "/console/support", key: "nav.support", ic: "chat" },
];

function chrome(bodyHtml) {
  const me = state.me;
  const path = currentPath();
  const low = me && me.credits < 1000;
  // Unread counters ride on the nav item they belong to, so an answer is
  // visible from any page rather than only from the support list.
  const badge = (key) => {
    const n = key === "nav.support" ? (state.me?.unread_tickets || 0) : 0;
    return n ? `<span class="navbadge">${fmtFa(n)}</span>` : "";
  };
  const nav = NAV.map((n) => `<a href="${n.href}" class="${
      path === n.href || (n.href !== "/console" && path.startsWith(n.href + "/")) ? "active" : ""}">
      ${icon[n.ic]}<span>${t(n.key)}</span>${badge(n.key)}</a>`).join("")
    + (me?.is_admin ? `<a href="/console/admin" class="${path.startsWith("/console/admin") ? "active" : ""}">
      ${icon.users}<span>${t("nav.admin")}</span>${
        me.unread_staff_tickets ? `<span class="navbadge">${fmtFa(me.unread_staff_tickets)}</span>` : ""
      }</a>` : "");

  return `
    <header class="header">
      <!-- The public homepage, not the console. Many apps send their logo to
           the dashboard instead, but nothing else in the console linked back to
           the landing page at all - so a customer who wanted it had no way there
           short of editing the URL, and clicking the logo is what they tried. -->
      <a href="/" class="brand" style="color:inherit;text-decoration:none">
        <span class="logo">${icon.machine}</span><span>${t("brand")}</span>
        <small class="app-version" dir="ltr">v${me?.version || "1.6.0"}</small></a>
      <nav class="nav">${nav}</nav>
      <div class="spacer"></div>
      <div class="header-right">
        <a href="/console/billing" class="credit-chip ${low ? "low" : ""}" title="${t("nav.balance")}">
            ${icon.card}<span class="lbl">${fmtMoney(me?.credits ?? 0)}</span></a>
        <button class="btn icon ghost notification-button" id="notifications" title="${t("nav.notifications")}" aria-label="${t("nav.notifications")}">
          ${icon.bell}${state.notificationUnread ? `<span class="navbadge">${fmtFa(state.notificationUnread)}</span>` : ""}</button>
        <button class="btn icon ghost" id="theme" title="${t("nav.theme")}"
          aria-label="${t("nav.theme")}">${currentTheme() === "dark" ? icon.sun : icon.moon}</button>
        <button class="btn sm ghost" id="signout" title="${t("nav.signout")}">
          ${icon.logout}<span class="lbl">${t("nav.signout")}</span></button>
      </div>
      ${notificationCenter()}
    </header>
    <main class="page">${operationStrip()}${bodyHtml}</main>`;
}

export function render(bodyHtml) {
  $("#app").innerHTML = chrome(bodyHtml);
  $("#theme").onclick = () => { toggleTheme(); render(bodyHtml); };
  wireNotificationCenter();
  $("#signout").onclick = async () => {
    teardownTerminal();
    await post("/api/auth/logout").catch(() => {});
    state.me = null;
    navigate("/signin");
    toast(t("auth.signedout"));
  };
}

function redrawNotificationCenter() {
  $("#notifications").innerHTML = `${icon.bell}${state.notificationUnread
    ? `<span class="navbadge">${fmtFa(state.notificationUnread)}</span>` : ""}`;
  const holder = document.createElement("div");
  holder.innerHTML = notificationCenter();
  $("#notification-panel")?.replaceWith(holder.firstElementChild);
  wireNotificationCenter();
}

function wireNotificationCenter() {
  $("#notifications").onclick = () => {
    const panel = $("#notification-panel");
    panel.hidden = !panel.hidden;
  };
  $("#notifications-read")?.addEventListener("click", async () => {
    await post("/api/notifications/read-all");
    await refreshNotifications();
    redrawNotificationCenter();
    $("#notification-panel").hidden = false;
  });
  document.querySelectorAll("[data-notification]").forEach((row) => row.onclick = () => {
    post(`/api/notifications/${row.dataset.notification}/read`).catch(() => {});
  });
}

export function renderBare(html) {
  teardownTerminal();
  $("#app").innerHTML = html;
}

/* ---- routes ----
   /            public marketing page
   /signin,/signup
   /console/*   the product itself                                        */
route("/", { title: null, view: landingPage, public: true });
route("/signin", { title: "ورود", view: signInPage, guest: true });
route("/signup", { title: "ثبت‌نام", view: signUpPage, guest: true });
route("/console", { title: "نمای کلی", view: machinePage });
route("/console/resources", { title: "منابع", view: resourcesPage });
route("/console/connections", { title: "اتصال‌ها", view: connectionsPage });
route("/console/connections/:tab", { title: "اتصال‌ها", view: connectionsPage });
route("/console/files", { title: "فایل‌ها", view: filesPage });
route("/console/ports", { title: "پورت‌ها", view: portsPage });
route("/console/billing", { title: "صورتحساب", view: billingPage });
route("/console/activity", { title: "فعالیت‌ها", view: activityPage });
route("/console/account", { title: "حساب کاربری", view: securityPage });
route("/console/security", { title: "حساب کاربری", view: securityPage });
route("/console/ai", { title: "هوش مصنوعی", view: aiPage });
route("/console/ai/:tab", { title: "هوش مصنوعی", view: aiPage });
route("/console/support", { title: "پشتیبانی", view: supportPage });
route("/console/support/:id", { title: "پشتیبانی", view: supportPage });
route("/console/admin", { title: "مدیریت", view: adminPage, admin: true });
route("/console/admin/tickets", { title: "تیکت‌ها", view: adminTicketsPage, admin: true });
route("/console/admin/users", { title: "کاربران", view: adminUsersPage, admin: true });
route("/console/admin/users/:id", { title: "کاربر", view: adminUserPage, admin: true });
route("/console/admin/policy", { title: "تعرفه‌ها و سیاست ظرفیت", view: adminMonitorPage, admin: true });
route("/console/admin/monitoring", { title: "تعرفه‌ها و سیاست ظرفیت", view: adminMonitorPage, admin: true });
route("/console/admin/openrouter", { title: "OpenRouter", view: adminOpenRouterPage, admin: true });
route("/console/admin/hermes", { title: "Hermes", view: adminHermesPage, admin: true });
route("/console/admin/claude", { title: "قیمت‌گذاری Claude", view: aiPricingPage, admin: true });
route("/console/admin/openclaw", { title: "OpenClaw", view: adminOpenClawPage, admin: true });
route("/console/admin/storage", { title: "فضای ذخیره‌سازی", view: adminStoragePage, admin: true });
route("/console/admin/backup", { title: "پشتیبان‌گیری", view: adminBackupPage, admin: true });
route("/console/admin/codex", { title: "قیمت‌گذاری Codex", admin: true,
                                view: () => aiPricingPage({ service: "codex" }) });
// The old address, kept so a bookmark or an open tab does not 404.
route("/console/admin/ai-pricing", { title: "قیمت‌گذاری Claude", view: aiPricingPage, admin: true });
route("/console/admin/tickets/:id", { title: "تیکت‌ها", view: adminTicketsPage, admin: true });

setNotFound(() => {
  if (!state.me) return navigate("/", { replace: true });
  render(`<div class="card"><h1>${t("common.notfound.title")}</h1>
    <p class="muted">${t("common.notfound.body")}
      <a href="/console">${t("common.gomachine")}</a></p></div>`);
});

setGuard(async (r) => {
  // Refetched on EVERY navigation, not just when signed out. The header is
  // drawn from this object - the support badge, the admin badge, the credit
  // chip - and it used to be fetched once at page load, so reading a ticket
  // left the red counter showing the old number until a hard reload. A
  // customer reported exactly that. It is one small local query per page
  // change, and it keeps the balance honest too.
  await refreshMe();
  await Promise.all([refreshOperations(), refreshNotifications()]);
  if (r.public) return null;
  if (r.guest) return state.me ? "/console" : null;
  if (!state.me) return "/signin";
  if (r.admin && !state.me.is_admin) return "/console";
  return null;
});

// No standalone fetch here: startRouter() resolves the first route immediately,
// and the guard above fetches before any view renders.
startRouter();

// Long operations outlive a page. Refresh only their compact global banner;
// never redraw the active form or disturb its scroll position.
setInterval(async () => {
  if (!state.me || !currentPath().startsWith("/console")) return;
  const before = JSON.stringify(state.operations);
  const notificationsBefore = JSON.stringify(state.notifications);
  await refreshOperations();
  await refreshNotifications();
  if (before !== JSON.stringify(state.operations)) {
    const old = document.querySelector(".operation-strip");
    const holder = document.createElement("div");
    holder.innerHTML = operationStrip();
    if (old) old.replaceWith(holder.firstElementChild || document.createTextNode(""));
    else if (holder.firstElementChild) document.querySelector("main.page")?.prepend(holder.firstElementChild);
  }
  if (notificationsBefore !== JSON.stringify(state.notifications)) {
    redrawNotificationCenter();
  }
}, 3000);
