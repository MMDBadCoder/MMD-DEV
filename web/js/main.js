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
import { toolsPage } from "./pages/tools.js";
import { portsPage } from "./pages/ports.js";
import { connectionsPage } from "./pages/connections.js";
import { billingPage } from "./pages/billing.js";
import { activityPage } from "./pages/activity.js";
import { securityPage } from "./pages/security.js";
import { adminPage } from "./pages/admin.js";
import { aiPage } from "./pages/ai.js";
import { supportPage } from "./pages/support.js";
import { adminTicketsPage } from "./pages/admintickets.js";

export const state = { me: null };

export async function refreshMe() {
  try {
    state.me = await get("/api/me");
  } catch {
    state.me = null;
  }
  return state.me;
}

// Connections sits second, right after the overview: it is what a developer
// opens most often once the machine is running.
const NAV = [
  { href: "/console", key: "nav.overview", ic: "chart" },
  { href: "/console/connections", key: "nav.connections", ic: "link" },
  { href: "/console/files", key: "nav.files", ic: "folder" },
  { href: "/console/resources", key: "nav.resources", ic: "sliders" },
  { href: "/console/tools", key: "nav.tools", ic: "box" },
  { href: "/console/ai", key: "nav.ai", ic: "sparkle" },
  { href: "/console/ports", key: "nav.ports", ic: "plug" },
  { href: "/console/billing", key: "nav.billing", ic: "card" },
  { href: "/console/activity", key: "nav.activity", ic: "clock" },
  { href: "/console/security", key: "nav.security", ic: "shield" },
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
      <a href="/console" class="brand" style="color:inherit;text-decoration:none">
        <span class="logo">${icon.machine}</span><span>${t("brand")}</span></a>
      <nav class="nav">${nav}</nav>
      <div class="spacer"></div>
      <div class="header-right">
        <a href="/console/billing" class="credit-chip ${low ? "low" : ""}" title="${t("nav.balance")}">
          ${icon.card}<span class="lbl">${fmtMoney(me?.credits ?? 0)}</span></a>
        <button class="btn icon ghost" id="theme" title="${t("nav.theme")}"
          aria-label="${t("nav.theme")}">${currentTheme() === "dark" ? icon.sun : icon.moon}</button>
        <button class="btn sm ghost" id="signout" title="${t("nav.signout")}">
          ${icon.logout}<span class="lbl">${t("nav.signout")}</span></button>
      </div>
    </header>
    <main class="page">${bodyHtml}</main>`;
}

export function render(bodyHtml) {
  $("#app").innerHTML = chrome(bodyHtml);
  $("#theme").onclick = () => { toggleTheme(); render(bodyHtml); };
  $("#signout").onclick = async () => {
    teardownTerminal();
    await post("/api/auth/logout").catch(() => {});
    state.me = null;
    navigate("/signin");
    toast(t("auth.signedout"));
  };
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
route("/console/tools", { title: "ابزارها", view: toolsPage });
route("/console/connections", { title: "اتصال‌ها", view: connectionsPage });
route("/console/connections/:tab", { title: "اتصال‌ها", view: connectionsPage });
route("/console/files", { title: "فایل‌ها", view: filesPage });
route("/console/ports", { title: "پورت‌ها", view: portsPage });
route("/console/billing", { title: "صورتحساب", view: billingPage });
route("/console/activity", { title: "فعالیت‌ها", view: activityPage });
route("/console/security", { title: "امنیت", view: securityPage });
route("/console/ai", { title: "هوش مصنوعی", view: aiPage });
route("/console/ai/:tab", { title: "هوش مصنوعی", view: aiPage });
route("/console/support", { title: "پشتیبانی", view: supportPage });
route("/console/support/:id", { title: "پشتیبانی", view: supportPage });
route("/console/admin", { title: "مدیریت", view: adminPage, admin: true });
route("/console/admin/tickets", { title: "تیکت‌ها", view: adminTicketsPage, admin: true });
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
  if (r.public) return null;
  if (r.guest) return state.me ? "/console" : null;
  if (!state.me) return "/signin";
  if (r.admin && !state.me.is_admin) return "/console";
  return null;
});

// No standalone fetch here: startRouter() resolves the first route immediately,
// and the guard above fetches before any view renders.
startRouter();
