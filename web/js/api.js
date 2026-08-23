/* Server access and error handling.
 *
 * The API answers in English with a stable `code`; the interface is Persian.
 * Translation happens here, once, so no page ever has to interpret a server
 * message itself. */
import { translateError } from "./i18n.js";

export async function api(path, opts = {}) {
  const r = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (r.status === 204) return {};
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(translateError(body.detail, r.status));
    err.status = r.status;
    err.code = (body.detail && body.detail.code) || null;
    err.detail = body.detail;
    throw err;
  }
  return body;
}

export const get = (p) => api(p);
export const post = (p, b) => api(p, { method: "POST", body: JSON.stringify(b ?? {}) });
export const put = (p, b) => api(p, { method: "PUT", body: JSON.stringify(b ?? {}) });
export const del = (p) => api(p, { method: "DELETE" });
