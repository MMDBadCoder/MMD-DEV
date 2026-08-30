/* Everything this account has done. */
import { get } from "../api.js";
import { $, icon, esc, fmtNum, fmtFa, fmtMoney, money, empty, stamp } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";

function describe(e) {
  const d = e.detail || {};
  if (e.action === "size_change")
    return `${fmtNum((d.cpu_milli || 0) / 1000, 1)} vCPU · ${fmtNum((d.mem_mib || 0) / 1024, 1)} ${t("res.gb")}`;
  if (e.action === "port_publish" || e.action === "port_unpublish")
    return `<span class="ltr mono">${d.internal} → ${d.external}</span>`;
  if (e.action === "grant_credit") return money(d.credits);
  if (e.action === "set_admin") return d.is_admin ? t("sec.role.admin") : t("sec.role.user");
  if (d.error) return esc(String(d.error).slice(0, 120));
  return esc(e.target || "");
}

export async function activityPage(_p, page = 0) {
  const per = 50;
  const d = await get(`/api/activity?limit=${per}&offset=${page * per}`);
  const pages = Math.ceil(d.total / per);

  const rows = d.events.map((e) => `
    <tr><td class="nowrap muted small">${stamp(e.ts)}</td>
      <td>${t("act.a." + e.action) || esc(e.action)}</td>
      <td class="muted small">${describe(e)}</td></tr>`).join("");

  render(`
    <div class="page-head"><h1>${t("act.title")}</h1>
      <p class="muted small" style="margin:0">${t("act.sub")}</p></div>
    <div class="card pad0">
      <div class="card-head"><h2>${t("act.history")}</h2>
        <span class="dim small">${t("act.total", fmtFa(d.total))}</span></div>
      ${d.events.length ? `<div class="table-wrap"><table>
        <thead><tr><th>${t("act.when")}</th><th>${t("act.action")}</th>
          <th>${t("act.detail")}</th></tr></thead>
        <tbody>${rows}</tbody></table></div>` : empty(t("act.empty"), icon.clock,
          { href: "/console", label: t("empty.machine"), icon: "machine" })}
      ${pages > 1 ? `<div class="card-head" style="border-top:1px solid var(--border);border-bottom:none">
        <span class="dim small">${t("common.page", [page + 1, pages])}</span>
        <div class="btn-row">
          <button class="btn sm" id="prev" ${page === 0 ? "disabled" : ""}>${t("common.prev")}</button>
          <button class="btn sm" id="next" ${page + 1 >= pages ? "disabled" : ""}>${t("common.next")}</button>
        </div></div>` : ""}
    </div>`);

  if ($("#prev")) $("#prev").onclick = () => activityPage(null, page - 1);
  if ($("#next")) $("#next").onclick = () => activityPage(null, page + 1);
}
