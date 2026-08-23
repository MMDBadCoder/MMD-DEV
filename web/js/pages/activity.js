/* Everything this account has done. */
import { get } from "../api.js";
import { $, icon, esc, note, empty, stamp } from "../ui.js";
import { render } from "../main.js";

const LABEL = {
  register: "Account created", sign_in: "Signed in",
  password_change: "Password changed", power_on: "Machine switched on",
  power_off: "Machine switched off", size_change: "Size changed",
  port_publish: "Port published", port_unpublish: "Port removed",
  approve: "Approved a user", reject: "Rejected a user",
  grant_credit: "Credit granted", set_admin: "Administrator role changed",
  delete_user: "Account deleted", settings_update: "Settings changed",
  provision_failed: "Machine creation failed",
};

function describe(e) {
  const d = e.detail || {};
  if (e.action === "size_change")
    return `${(d.cpu_milli || 0) / 1000} vCPU · ${((d.mem_mib || 0) / 1024)} GB${d.applied_live ? " (applied immediately)" : " (from next start)"}`;
  if (e.action === "port_publish" || e.action === "port_unpublish")
    return `port ${d.internal} → public ${d.external}`;
  if (e.action === "grant_credit") return `${d.credits} credits to ${esc(e.target || "")}`;
  if (e.action === "set_admin") return `${esc(e.target || "")} → ${d.is_admin ? "administrator" : "regular user"}`;
  if (d.error) return esc(String(d.error).slice(0, 140));
  return esc(e.target || "");
}

export async function activityPage(_p, page = 0) {
  const per = 50;
  const d = await get(`/api/activity?limit=${per}&offset=${page * per}`);
  const pages = Math.ceil(d.total / per);

  const rows = d.events.map((e) => `
    <tr><td class="nowrap muted small">${stamp(e.ts)}</td>
      <td>${esc(LABEL[e.action] || e.action)}</td>
      <td class="muted small">${describe(e)}</td></tr>`).join("");

  render(`
    <div class="page-head"><h1>Activity</h1>
      <p class="muted small" style="margin:0">Every action recorded on your account.</p></div>
    <div class="card pad0">
      <div class="card-head"><h2>History</h2><span class="dim small">${d.total} events</span></div>
      ${d.events.length ? `<div class="table-wrap"><table>
        <thead><tr><th>When</th><th>Action</th><th>Detail</th></tr></thead>
        <tbody>${rows}</tbody></table></div>` : empty("Nothing recorded yet.", icon.clock)}
      ${pages > 1 ? `<div class="card-head" style="border-top:1px solid var(--border);border-bottom:none">
        <span class="dim small">Page ${page + 1} of ${pages}</span>
        <div class="btn-row">
          <button class="btn sm" id="prev" ${page === 0 ? "disabled" : ""}>Previous</button>
          <button class="btn sm" id="next" ${page + 1 >= pages ? "disabled" : ""}>Next</button>
        </div></div>` : ""}
    </div>`);

  if ($("#prev")) $("#prev").onclick = () => activityPage(null, page - 1);
  if ($("#next")) $("#next").onclick = () => activityPage(null, page + 1);
}
