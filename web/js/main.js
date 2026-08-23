/* Application shell: session, chrome, routes. */
import { get, post } from "./api.js";
import { $, icon, esc, fmt, toast, currentTheme, toggleTheme } from "./ui.js";
import { route, setGuard, setNotFound, startRouter, navigate, currentPath, resolve } from "./router.js";

import { signInPage, signUpPage } from "./pages/auth.js";
import { machinePage } from "./pages/machine.js";
import { resourcesPage } from "./pages/resources.js";
import { portsPage } from "./pages/ports.js";
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
  { href: "/", label: "Machine", ic: "machine" },
  { href: "/resources", label: "Size", ic: "sliders" },
  { href: "/ports", label: "Ports", ic: "plug" },
  { href: "/billing", label: "Billing", ic: "card" },
  { href: "/activity", label: "Activity", ic: "clock" },
  { href: "/security", label: "Security", ic: "shield" },
];

function chrome(bodyHtml) {
  const me = state.me;
  const path = currentPath();
  const low = me && me.credits < 25;
  const nav = NAV.map((n) => `<a href="${n.href}" class="${path === n.href ? "active" : ""}">
      ${icon[n.ic]}<span>${n.label}</span></a>`).join("")
    + (me?.is_admin ? `<a href="/admin" class="${path.startsWith("/admin") ? "active" : ""}">
      ${icon.users}<span>Admin</span></a>` : "");

  return `
    <header class="header">
      <div class="brand"><span class="logo">${icon.machine}</span><span>Workspace</span></div>
      <nav class="nav">${nav}</nav>
      <div class="spacer"></div>
      <div class="header-right">
        <a href="/billing" class="credit-chip ${low ? "low" : ""}" title="Credit balance">
          ${icon.card}<span class="lbl">${fmt(me?.credits ?? 0)}</span></a>
        <button class="btn icon ghost" id="theme" title="Switch theme"
          aria-label="Switch theme">${currentTheme() === "dark" ? icon.sun : icon.moon}</button>
        <button class="btn sm ghost" id="signout" title="Sign out">
          ${icon.logout}<span class="lbl">Sign out</span></button>
      </div>
    </header>
    <main class="page">${bodyHtml}</main>`;
}

export function render(bodyHtml) {
  $("#app").innerHTML = chrome(bodyHtml);
  $("#theme").onclick = () => { toggleTheme(); render(bodyHtml); };
  $("#signout").onclick = async () => {
    await post("/api/auth/logout").catch(() => {});
    state.me = null;
    navigate("/signin");
  };
}

export function renderBare(html) {
  $("#app").innerHTML = html;
}

/* ---- routes ---- */
route("/signin", { title: "Sign in", view: signInPage, guest: true });
route("/signup", { title: "Create account", view: signUpPage, guest: true });
route("/", { title: "Machine", view: machinePage });
route("/resources", { title: "Size", view: resourcesPage });
route("/ports", { title: "Ports", view: portsPage });
route("/billing", { title: "Billing", view: billingPage });
route("/activity", { title: "Activity", view: activityPage });
route("/security", { title: "Security", view: securityPage });
route("/admin", { title: "Administration", view: adminPage, admin: true });

setNotFound(() => {
  if (!state.me) return navigate("/signin", { replace: true });
  render(`<div class="card"><h1>Page not found</h1>
    <p class="muted">That address does not exist. <a href="/">Go to your machine</a>.</p></div>`);
});

setGuard(async (r) => {
  if (state.me === null) await refreshMe();
  if (r.guest) return state.me ? "/" : null;
  if (!state.me) return "/signin";
  if (r.admin && !state.me.is_admin) return "/";
  return null;
});

await refreshMe();
startRouter();
