/* Pure path helpers for the file manager.
 *
 * Split out of files.js so they can be tested without a DOM. Both functions
 * below have shipped a doubled-slash bug - one of them in front of a customer -
 * which is exactly the kind of thing a three-line unit test catches and a
 * glance at the page does not. */

const SEP = '<span class="dim" style="margin:0 4px">/</span>';

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/** Normalise a directory path: no trailing slash, no repeated slashes. */
export function normalize(path) {
  const p = String(path || "/").replace(/\/+/g, "/").replace(/(.)\/+$/, "$1");
  return p.startsWith("/") ? p : "/" + p;
}

/** Join a directory and a child name into an absolute path. */
export function join(dir, name) {
  // `dir` is whatever the server last reported, so it may carry a trailing
  // slash; without stripping it, "/home/dev/" + "x" became "/home/dev//x".
  const base = normalize(dir);
  const child = String(name).replace(/^\/+/, "");
  return (base === "/" ? "" : base) + "/" + child;
}

/** Clickable breadcrumb trail for a directory path. */
export function crumbs(path) {
  // The root crumb's LABEL is "/", so joining every crumb with a "/" separator
  // as well produced "//home/dev" - the root's own text and the first separator
  // are the same character. The separator therefore goes BETWEEN the named
  // parts only, and the root supplies the leading slash by itself.
  const parts = normalize(path).split("/").filter(Boolean);
  let acc = "";
  let html = `<a href="#" data-go="/" class="mono">/</a>`;
  parts.forEach((p, i) => {
    acc += "/" + p;
    if (i > 0) html += SEP;
    html += `<a href="#" data-go="${esc(acc)}" dir="ltr">${esc(p)}</a>`;
  });
  return html;
}

/** The trail as plain text - what the customer actually reads. */
export const crumbsText = (path) => crumbs(path).replace(/<[^>]*>/g, "");
