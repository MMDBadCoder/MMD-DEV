/* Server access and error handling. */

/* FastAPI returns `detail` as a STRING for HTTPException but as an ARRAY of
 * objects for 422 validation failures. Passing that array to new Error()
 * stringifies it to "[object Object]" - which once made a rejected password
 * look like a broken signup form. */
export function describeError(detail, status) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const field = (e) => {
      const name = (e.loc || []).filter((x) => x !== "body").join(".");
      return ({ password: "Password", email: "Email address",
                new_password: "New password", internal_port: "Port",
                cpu_milli: "CPU size", mem_mib: "Memory size" })[name]
             || name || "Value";
    };
    return detail.map((e) => {
      let msg = (e.msg || "is not valid")
        .replace(/^Value error, /, "")
        .replace(/^value is not a valid email address:\s*/i, "")
        .replace(/^String should have at least (\d+) characters$/i,
                 "needs at least $1 characters")
        .replace(/^Field required$/i, "is required");
      return /^(needs|is |must)/i.test(msg) ? `${field(e)} ${msg}`
                                            : `${field(e)}: ${msg}`;
    }).join(". ");
  }
  return `Request failed (${status})`;
}

export async function api(path, opts = {}) {
  const r = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (r.status === 204) return {};
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(describeError(body.detail, r.status));
    err.status = r.status;
    throw err;
  }
  return body;
}

export const get = (p) => api(p);
export const post = (p, b) => api(p, { method: "POST", body: JSON.stringify(b ?? {}) });
export const put = (p, b) => api(p, { method: "PUT", body: JSON.stringify(b ?? {}) });
export const del = (p) => api(p, { method: "DELETE" });
