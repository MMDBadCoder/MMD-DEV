/* People: approving them, funding them, and finding one among many.
 *
 * Split out of the old single admin page, which stacked approvals, capacity,
 * charts and the rate card in one column - different jobs done at
 * five different times, four of which you scrolled past to reach the fifth.
 *
 * The filters are here rather than on the other sections because this is the
 * list that grows. Capacity and the rate card stay one screen forever; "which
 * account is waiting for approval" gets harder with every signup. */
import { get, post, del } from "../api.js";
import { $, $$, icon, esc, fmtMoney, fmtNum, fmtFa, note, toast, stamp,
         confirmDialog, destructiveDialog, statePill } from "../ui.js";
import { t, CURRENCY } from "../i18n.js";
import { render, state } from "../main.js";
import { adminHead } from "./adminnav.js";

const KEY = "mmd.admin.userfilter";
const readFilter = () => {
  try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; }
};
const saveFilter = (f) => localStorage.setItem(KEY, JSON.stringify(f));

const STATUSES = ["all", "pending", "approved", "rejected", "suspended", "deleting"];

export async function adminUsersPage() {
  const users = await get("/api/admin/users");
  const f = { status: "all", q: "", ...readFilter() };

  const statusLabel = { approved: "تأیید شده", pending: "در انتظار تأیید",
                        rejected: "رد شده", suspended: "معلق",
                        deleting: t("adm.st.deleting") };

  const matches = (u) => {
    if (f.status !== "all" && u.status !== f.status) return false;
    const q = (f.q || "").trim().toLowerCase();
    if (!q) return true;
    return (u.email || "").toLowerCase().includes(q)
        || (u.full_name || "").toLowerCase().includes(q)
        || (u.phone || "").includes(q)
        || (u.username || "").toLowerCase().includes(q);
  };

  const counts = Object.fromEntries(STATUSES.map((s) =>
    [s, s === "all" ? users.length : users.filter((u) => u.status === s).length]));

  const row = (u) => `<tr>
    <td><a class="ltr mono" style="font-size:13px"
        href="/console/admin/users/${u.id}">${esc(u.email)}</a>
      <div class="tiny dim">${u.username
        ? `<span class="ltr">${esc(u.username)}</span> · ` : ""}${
        u.full_name ? esc(u.full_name) + " · " : ""}${u.phone ? `<span class="ltr">${esc(u.phone)}</span> · ` : ""}${
        u.is_admin ? t("sec.role.admin") + " · " : ""}${t("adm.joined")} ${stamp(u.created_at)}</div></td>
    <td><span class="pill"><span class="dot ${u.status === "approved" ? "on"
        : u.status === "pending" ? "busy" : "bad"}"></span>${
        statusLabel[u.status] || u.status}</span></td>
    <td class="small nowrap">${u.workspace
      ? `${statePill(u.workspace.state)}
         <div class="tiny dim ltr" style="margin-top:3px">${
           fmtNum(u.workspace.cpu_cores, 1)} vCPU · ${
           fmtNum(u.workspace.memory_mb / 1024, 1)} GB</div>`
      : `<span class="dim">${t("adm.none")}</span>`}</td>
    <td class="num">${fmtMoney(u.credits)}</td>
    <td class="num nowrap">
      ${u.status === "pending"
        ? `<button class="btn sm primary" data-approve="${u.id}">${t("adm.approve")}</button>
           <button class="btn sm danger" data-reject="${u.id}">${t("adm.reject")}</button>` : ""}
      <button class="btn sm" data-credit="${u.id}" ${u.status === "deleting" ? "disabled" : ""}>${t("adm.credit")}</button>
      ${u.workspace?.state === "on" ? `<button class="btn sm danger" data-poweroff="${u.workspace.id}"
        data-owner="${esc(u.email)}">${icon.power}${t("adm.poweroff")}</button>` : ""}
      <a class="btn sm ghost" href="/console/admin/users/${u.id}">${t("adm.details")}</a>
      <button class="btn sm ghost" data-admin="${u.id}" data-is="${u.is_admin}">
        ${u.is_admin ? t("adm.demote") : t("adm.makeadmin")}</button>
      ${u.id === state.me?.id || u.status === "deleting" ? "" : `<button class="btn sm danger" data-del="${u.id}"
        data-email="${esc(u.email)}">${icon.trash}</button>`}
    </td></tr>`;

  const shown = users.filter(matches);

  render(`
    ${adminHead("users", t("adm.people"), t("adm.users.sub"))}

    <div class="card pad0">
      <div class="card-head filter-row">
        <div class="seg">${STATUSES.map((s) => `
          <button class="${f.status === s ? "active" : ""}" data-status="${s}">
            ${t("adm.st." + s)} <span class="dim">${fmtFa(counts[s])}</span></button>`).join("")}
        </div>
        <input id="q" type="search" class="ltr" value="${esc(f.q)}"
          placeholder="${t("adm.users.search")}" style="max-width:240px">
      </div>

      ${shown.length ? `<div class="table-wrap"><table>
        <thead><tr><th>${t("adm.account")}</th><th>${t("adm.status")}</th>
          <th>${t("adm.machine")}</th>
          <th class="num">${t("adm.credit")} <span class="dim">(${CURRENCY})</span></th>
          <th></th></tr></thead>
        <tbody>${shown.map(row).join("")}</tbody></table></div>`
        : `<div style="padding:18px">${note("info", t("adm.users.nomatch"))}</div>`}
    </div>
    <p class="tiny dim">${t("adm.users.showing", fmtFa(shown.length), fmtFa(users.length))}</p>`);

  $$("[data-status]").forEach((b) => {
    b.onclick = () => { f.status = b.dataset.status; saveFilter(f); adminUsersPage(); };
  });
  const q = $("#q");
  let timer = null;
  q.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(() => { f.q = q.value; saveFilter(f); adminUsersPage(); }, 250);
  };

  $$("[data-approve]").forEach((b) => b.onclick = async () => {
    b.disabled = true; b.innerHTML = `<span class="spinner"></span>${t("adm.approving")}`;
    try {
      await post(`/api/admin/users/${b.dataset.approve}/approve`);
      toast(t("adm.machinecreated"), "ok");
    } catch (e) { toast(e.message, "bad"); }
    adminUsersPage();
  });

  $$("[data-reject]").forEach((b) => b.onclick = async () => {
    if (!await confirmDialog(t("adm.confirm.reject.title"), t("adm.confirm.reject.body"),
                             t("adm.reject"))) return;
    try { await post(`/api/admin/users/${b.dataset.reject}/reject`); }
    catch (e) { toast(e.message, "bad"); }
    adminUsersPage();
  });

  $$("[data-credit]").forEach((b) => b.onclick = async () => {
    const v = prompt(t("adm.creditprompt"), "500000");
    if (v === null) return;
    try {
      const r = await post(`/api/admin/users/${b.dataset.credit}/credit`,
                           { credits: Number(v), note: "admin grant" });
      toast(t("adm.newbalance", fmtMoney(r.balance)), "ok");
    } catch (e) { toast(e.message, "bad"); }
    adminUsersPage();
  });

  $$("[data-admin]").forEach((b) => b.onclick = async () => {
    const makeAdmin = b.dataset.is !== "true";
    try { await post(`/api/admin/users/${b.dataset.admin}/admin`, { is_admin: makeAdmin }); }
    catch (e) { toast(e.message, "bad"); }
    adminUsersPage();
  });

  $$("[data-poweroff]").forEach((b) => b.onclick = async () => {
    if (!await confirmDialog(t("adm.poweroff.confirm.title", b.dataset.owner),
                             t("adm.poweroff.confirm.body"), t("adm.poweroff"))) return;
    b.disabled = true;
    b.innerHTML = `<span class="spinner"></span>${t("adm.poweroff.working")}`;
    try {
      await post(`/api/admin/workspaces/${b.dataset.poweroff}/power-off`);
      toast(t("adm.poweroff.done"), "ok");
    } catch (e) { toast(e.message, "bad"); }
    adminUsersPage();
  });

  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!await destructiveDialog({
      title: t("adm.confirm.del.title", b.dataset.email),
      intro: t("adm.confirm.del.body"),
      destroys: [t("adm.delete.workspace"), t("adm.delete.addresses"),
                 t("adm.delete.account"), t("adm.delete.history")],
      keeps: [t("adm.delete.keeps")], expect: b.dataset.email,
      label: t("adm.confirm.del.cta"),
    })) return;
    b.disabled = true;
    try { await del(`/api/admin/users/${b.dataset.del}`); toast(t("adm.deletequeued"), "ok"); }
    catch (e) { toast(e.message, "bad"); }
    adminUsersPage();
  });
}
