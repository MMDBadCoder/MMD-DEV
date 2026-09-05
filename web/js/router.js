/* History-API router.
 *
 * Real URLs, not hash fragments: /billing, /security, /admin. The server
 * returns the app shell for any non-API path, so a refresh or a pasted link
 * resolves to the same page instead of a 404. */

import { t } from "./i18n.js";

const routes = [];
let notFound = null;
let beforeEach = null;
let onError = null;
let generation = 0;

export function route(path, opts) {
  // "/ports/:id" -> a matcher plus the parameter names it captures.
  const names = [];
  const rx = new RegExp("^" + path.replace(/:[^/]+/g, (m) => {
    names.push(m.slice(1));
    return "([^/]+)";
  }) + "$");
  routes.push({ path, rx, names, ...opts });
}

export const setNotFound = (fn) => { notFound = fn; };
export const setGuard = (fn) => { beforeEach = fn; };
export const setErrorHandler = (fn) => { onError = fn; };

export function navigate(to, { replace = false } = {}) {
  if (to === location.pathname + location.search) return resolve();
  history[replace ? "replaceState" : "pushState"]({}, "", to);
  return resolve();
}

export function currentPath() {
  return location.pathname.replace(/\/+$/, "") || "/";
}

export async function resolve() {
  const mine = ++generation;
  const path = currentPath();
  for (const r of routes) {
    const m = path.match(r.rx);
    if (!m) continue;
    const params = Object.fromEntries(r.names.map((n, i) => [n, decodeURIComponent(m[i + 1])]));
    if (beforeEach) {
      const redirect = await beforeEach(r, path);
      if (mine !== generation) return;
      if (redirect) return navigate(redirect, { replace: true });
    }
    document.title = r.title ? `${r.title} · ${t("brand")}` : t("brand");
    try {
      await r.view(params);
    } catch (error) {
      if (mine === generation && onError) onError(error);
      else if (mine === generation) throw error;
    }
    // A slow page may have finished after a later navigation and painted its
    // old result. Resolve the current URL once more so stale work cannot leave
    // the customer on the wrong screen.
    if (mine !== generation) return resolve();
    return undefined;
  }
  return notFound ? notFound(path) : undefined;
}

/* Intercept in-app links so navigation stays client-side. */
export function startRouter() {
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a) return;
    const href = a.getAttribute("href");
    if (!href || !href.startsWith("/") || a.target === "_blank"
        || a.hasAttribute("download") || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    navigate(href);
  });
  addEventListener("popstate", resolve);
  return resolve();
}
