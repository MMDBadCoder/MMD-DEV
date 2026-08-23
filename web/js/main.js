/* MMD-DEV console shell: session, chrome, routes. */
import { get, post } from "./api.js";
import { $, icon, fmtMoney, toast, currentTheme, toggleTheme } from "./ui.js";
import { t } from "./i18n.js";
import { route, setGuard, setNotFound, startRouter, navigate, currentPath } from "./router.js";

import { landingPage } from "./pages/landing.js";
import { signInPage, signUpPage } from "./pages/auth.js";
import { machinePage, teardownTerminal } from "./pages/machine.js";
import { resourcesPage } from "./pages/resources.js";
import { toolsPage } from "./pages/tools.js";
import { portsPage } from "./pages/ports.js";
import { connectionsPage } from "./pages/connections.js";
import { billingPage } from "./pages/billing.js";
import { activityPage } from "./pages/activity.js";
import { securityPage } from "./pages/security.js";
import { adminPage } from "./pages/admin.js";

export const state = { me: null };

export async function refreshMe() {
  try {
    state.me = await get("/api/me");
  } catch {
    state.me = null;
  }
  return state.me;
}

const NAV = [
  { href: "/console", key: "nav.machine", ic: "machine" },
  { href: "/console/resources", key: "nav.resources", ic: "sliders" },
  { href: "/console/tools", key: "nav.tools", ic: "box" },
  { href: "/console/connections", key: "nav.connections", ic: "link" },
  { href: "/console/ports", key: "nav.ports", ic: "plug" },
  { href: "/console/billing", key: "nav.billing", ic: "card" },
  { href: "/console/activity", key: "nav.activity", ic: "clock" },
  { href: "/console/security", key: "nav.security", ic: "shield" },
];

function chrome(bodyHtml) {
  const me = state.me;
  const path = currentPath();
  const low = me && me.credits < 1000;
  const nav = NAV.map((n) => `<a href="${n.href}" class="${path === n.href ? "active" : ""}">
      ${icon[n.ic]}<span>${t(n.key)}</span></a>`).join("")
    + (me?.is_admin ? `<a href="/console/admin" class="${path.startsWith("/console/admin") ? "active" : ""}">
      ${icon.users}<span>${t("nav.admin")}</span></a>` : "");

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
route("/console", { title: "ماشین من", view: machinePage });
route("/console/resources", { title: "منابع", view: resourcesPage });
route("/console/tools", { title: "ابزارها", view: toolsPage });
route("/console/connections", { title: "اتصال‌ها", view: connectionsPage });
route("/console/ports", { title: "پورت‌ها", view: portsPage });
route("/console/billing", { title: "صورتحساب", view: billingPage });
route("/console/activity", { title: "فعالیت‌ها", view: activityPage });
route("/console/security", { title: "امنیت", view: securityPage });
route("/console/admin", { title: "مدیریت", view: adminPage, admin: true });

setNotFound(() => {
  if (!state.me) return navigate("/", { replace: true });
  render(`<div class="card"><h1>${t("common.notfound.title")}</h1>
    <p class="muted">${t("common.notfound.body")}
      <a href="/console">${t("common.gomachine")}</a></p></div>`);
});

setGuard(async (r) => {
  if (state.me === null) await refreshMe();
  if (r.public) return null;
  if (r.guest) return state.me ? "/console" : null;
  if (!state.me) return "/signin";
  if (r.admin && !state.me.is_admin) return "/console";
  return null;
});

await refreshMe();
startRouter();
