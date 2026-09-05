/* Searchable global audit trail for the operator. */
import { get } from "../api.js";
import { $, esc, fmtFa, stamp, icon, empty } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";

function actionLabel(action) {
  const key = `act.a.${action}`;
  const label = t(key);
  return label === key ? t("adm.audit.other") : label;
}

function details(event) {
  const detail = event.detail || {};
  if (!Object.keys(detail).length) return "";
  return `<details><summary>${t("act.detail")}</summary>
    <pre class="ltr" dir="ltr">${esc(JSON.stringify(detail, null, 2))}</pre></details>`;
}

export async function adminActivityPage(_params, page = 0, query = "") {
  const per = 100;
  const data = await get(`/api/admin/activity?limit=${per}&offset=${page * per}&q=${encodeURIComponent(query)}`);
  const pages = Math.ceil(data.total / per);
  const rows = data.events.map((event) => `<tr>
    <td data-label="${t("act.when")}" class="nowrap muted small">${stamp(event.ts)}</td>
    <td data-label="${t("adm.audit.actor")}">${event.actor
      ? `<a class="mono ltr" href="/console/admin/users/${event.actor_id}">${esc(event.actor)}</a>`
      : `<span class="dim">${t("adm.audit.system")}</span>`}</td>
    <td data-label="${t("act.action")}">${actionLabel(event.action)}
      <div class="tiny dim mono ltr" dir="ltr">${esc(event.action)}</div></td>
    <td data-label="${t("adm.audit.target")}">${event.target
      ? `<span class="mono ltr" dir="ltr">${esc(event.target)}</span>` : ""}${details(event)}</td>
  </tr>`).join("");

  render(`${adminHead("activity", t("adm.audit.title"), t("adm.audit.sub"))}
    <form class="card-head filter-row" id="audit-search">
      <input id="audit-q" type="search" value="${esc(query)}"
        placeholder="${t("adm.audit.search")}" style="max-width:360px">
      <button class="btn" type="submit">${t("adm.audit.search.button")}</button>
      ${query ? `<button class="btn ghost" id="audit-clear" type="button">${t("adm.audit.clear")}</button>` : ""}
      <span class="dim small spacer">${t("act.total", fmtFa(data.total))}</span>
    </form>
    <div class="card pad0">
      ${data.events.length ? `<div class="table-wrap"><table class="mobile-cards">
        <thead><tr><th>${t("act.when")}</th><th>${t("adm.audit.actor")}</th>
          <th>${t("act.action")}</th><th>${t("adm.audit.target")}</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
        : empty(t("adm.audit.none"), icon.clock)}
      ${pages > 1 ? `<div class="card-head" style="border-top:1px solid var(--border);border-bottom:0">
        <span class="dim small">${t("common.page", [page + 1, pages])}</span>
        <div class="btn-row"><button class="btn sm" id="audit-prev" ${page === 0 ? "disabled" : ""}>${t("common.prev")}</button>
          <button class="btn sm" id="audit-next" ${page + 1 >= pages ? "disabled" : ""}>${t("common.next")}</button></div>
      </div>` : ""}
    </div>`);

  $("#audit-search").onsubmit = (event) => {
    event.preventDefault();
    adminActivityPage(null, 0, $("#audit-q").value.trim());
  };
  $("#audit-clear")?.addEventListener("click", () => adminActivityPage());
  $("#audit-prev")?.addEventListener("click", () => adminActivityPage(null, page - 1, query));
  $("#audit-next")?.addEventListener("click", () => adminActivityPage(null, page + 1, query));
}
