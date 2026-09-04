/* The admin area's own navigation.
 *
 * Administration used to be one long page: approvals, capacity, host charts,
 * rate card and AI pricing stacked in a column. Every one of those is
 * a different job done at a different time, and stacking them meant scrolling
 * past four of them to reach the fifth.
 *
 * Splitting them into real routes rather than tabs-in-a-page is deliberate:
 * each section becomes a link that can be bookmarked, opened in a second tab
 * beside the first, and returned to by the back button. */
import { icon } from "../ui.js";
import { t } from "../i18n.js";

export const SECTIONS = [
  { key: "",            path: "/console/admin",            ic: "cog" },
  { key: "users",       path: "/console/admin/users",      ic: "users" },
  { key: "policy",      path: "/console/admin/policy",     ic: "chart" },
  { key: "storage",     path: "/console/admin/storage",    ic: "folder" },
  { key: "openrouter",  path: "/console/admin/openrouter", ic: "plug" },
  { key: "claude",      path: "/console/admin/claude",     ic: "claude" },
  { key: "codex",       path: "/console/admin/codex",      ic: "codex" },
  { key: "hermes",      path: "/console/admin/hermes",     ic: "hermes" },
  { key: "openclaw",    path: "/console/admin/openclaw",   ic: "openclaw" },
  { key: "tickets",     path: "/console/admin/tickets",    ic: "lifebuoy" },
  { key: "backup",      path: "/console/admin/backup",     ic: "archive" },
];

/* `active` is the section key, not a path, so a detail route like
   /console/admin/users/12 can still light up its parent tab. */
export function adminNav(active = "") {
  return `<div class="tabs2 admin-nav">${SECTIONS.map((s) => `
    <a href="${s.path}" class="${s.key === active ? "active" : ""}">
      ${icon[s.ic] || ""}${t("adm.nav." + (s.key || "overview"))}</a>`).join("")}</div>`;
}

export function adminHead(active, title, sub) {
  return `<div class="page-head"><h1>${title}</h1>
      ${sub ? `<p class="muted small" style="margin:0">${sub}</p>` : ""}</div>
    ${adminNav(active)}`;
}
