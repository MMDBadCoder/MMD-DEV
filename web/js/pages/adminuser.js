/* One customer, end to end: what their balance has done, what they were
 * charged for, and what they did.
 *
 * The credit chart answers the question that arrives as a support ticket -
 * "where did my credit go" - with a shape rather than a number, so a steady
 * drain and a single large charge look different at a glance. */
import { get, put } from "../api.js";
import { $, $$, esc, fmtMoney, fmtNum, note, stamp, statePill, toast,
         formError, clearFormErrors } from "../ui.js";
import { t, CURRENCY } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";

const KIND_KEY = {
  grant: "adm.tx.grant", charge_hour: "adm.tx.hour",
  charge_partial: "adm.tx.partial", charge_ai: "adm.tx.ai",
  charge_hermes: "adm.tx.hermes", charge_codex: "adm.tx.codex",
  adjustment: "adm.tx.adjustment",
};

export async function adminUserPage(params) {
  const id = Number(params.id);
  let d;
  try {
    d = await get(`/api/admin/users/${id}`);
  } catch (e) {
    render(`${adminHead("users", t("adm.user.title"))}${note("bad", esc(e.message))}`);
    return;
  }

  const u = d.user;

  const txRow = (x) => `<tr>
    <td class="tiny nowrap">${stamp(x.ts)}</td>
    <td class="small">${t(KIND_KEY[x.kind] || "adm.tx.other")}</td>
    <td class="num ${x.amount < 0 ? "" : "pos"}">${fmtMoney(Math.abs(x.amount))}${
      x.amount < 0 ? "" : " +"}</td>
    <td class="tiny dim ltr" style="max-width:34ch;overflow:hidden;text-overflow:ellipsis">${
      esc(summarise(x.detail))}</td></tr>`;

  render(`
    ${adminHead("users", esc(u.username), esc(u.phone || ""))}

    <div class="card">
      <h3>${t("adm.user.profile")}</h3>
      <form id="admin-profile" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(210px,1fr));align-items:end">
        <div class="field"><label for="admin-name">${t("auth.fullname")}</label>
          <input id="admin-name" maxlength="120" value="${esc(u.full_name || "")}"></div>
        <div class="field"><label for="admin-phone">${t("auth.phone")}</label>
          <input id="admin-phone" class="ltr" dir="ltr" inputmode="numeric" maxlength="11" value="${esc(u.phone || "")}"></div>
        <button class="btn primary" type="submit">${t("sec.profile.save")}</button>
      </form><div id="admin-profile-msg"></div>
    </div>

    <div class="card">
      <div class="between" style="margin-bottom:10px">
        <div><h3 style="margin:0">${t("adm.user.credit")}</h3>
          <p class="tiny dim" style="margin:3px 0 0">${t("adm.user.credit.sub")}</p></div>
      </div>
      ${note("info", t("adm.user.credit.dashboard"))}
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-top:14px">
        <div class="stat"><div class="k">${t("adm.user.balance")}</div>
          <div class="v">${fmtMoney(d.balance)}</div></div>
        ${Object.entries(d.by_kind).map(([k, v]) => `
          <div class="stat"><div class="k">${t(KIND_KEY[k] || "adm.tx.other")}</div>
            <div class="v" style="font-size:17px">${fmtMoney(Math.abs(v))}</div></div>`).join("")}
      </div>
    </div>

    <div class="card">
      <h3>${t("adm.user.machine")}</h3>
      ${d.workspace ? `<div class="row" style="gap:14px;align-items:center">
        ${statePill(d.workspace.state)}
        <span class="tiny dim ltr">ws-${fmtNum(d.workspace.idx)}</span>
        <span class="pill"><span class="dot ${d.workspace.hermes_ready ? "on" : ""}"></span>
          Hermes ${d.workspace.hermes_ready ? t("ai.ready") : t("ai.notready")}</span>
      </div>` : note("info", t("adm.none"))}
    </div>

    <div class="card">
      <h3>${t("adm.user.tx")}</h3>
      ${d.transactions.length ? `<div class="table-wrap"><table>
        <thead><tr><th>${t("adm.col.when")}</th><th>${t("adm.col.kind")}</th>
          <th class="num">${t("adm.col.amount")}</th><th>${t("adm.col.detail")}</th></tr></thead>
        <tbody>${d.transactions.map(txRow).join("")}</tbody></table></div>`
        : note("info", t("adm.user.notx"))}
    </div>

    <div class="card">
      <h3>${t("adm.user.audit")}</h3>
      ${d.audits.length ? `<div class="table-wrap"><table>
        <thead><tr><th>${t("adm.col.when")}</th><th>${t("adm.col.action")}</th>
          <th>${t("adm.col.target")}</th></tr></thead>
        <tbody>${d.audits.map((a) => `<tr>
          <td class="tiny nowrap">${stamp(a.ts)}</td>
          <td class="small ltr mono" style="font-size:12px">${esc(a.action)}</td>
          <td class="tiny dim ltr">${esc(a.target || "")}</td></tr>`).join("")}</tbody>
        </table></div>` : note("info", t("adm.user.noaudit"))}
    </div>`);

  $("#admin-profile").onsubmit = async (e) => {
    e.preventDefault();
    clearFormErrors($("#admin-profile"));
    const body = { full_name: $("#admin-name").value.trim(),
      phone: $("#admin-phone").value.trim() };
    if (body.full_name.length < 2 || !/^09[0-9]{9}$/.test(body.phone)) {
      const badName = body.full_name.length < 2;
      formError(t(badName ? "auth.err.fullname" : "auth.err.phone"), {
        form: "#admin-profile", messageRoot: "#admin-profile-msg",
        field: badName ? "#admin-name" : "#admin-phone",
      });
      return;
    }
    try {
      await put(`/api/admin/users/${id}/profile`, body);
      toast(t("sec.profile.saved"), "ok"); adminUserPage(params);
    } catch (err) {
      formError(err.message, { form: "#admin-profile", messageRoot: "#admin-profile-msg",
        field: err.code === "phone_taken" ? "#admin-phone" : null });
    }
  };
}

/* Ledger details are per-kind shapes, not one schema. Pull out the couple of
   fields worth showing inline and leave the rest to the API. */
function summarise(detail) {
  if (!detail || typeof detail !== "object") return "";
  if (detail.service && detail.usd != null) {
    return `${detail.service} $${detail.usd}`;
  }
  if (detail.models) return Object.keys(detail.models).join(", ");
  if (detail.reason) return String(detail.reason);
  return Object.keys(detail).slice(0, 3).join(", ");
}
