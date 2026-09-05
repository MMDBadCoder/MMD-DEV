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
import { grafanaConfig, dashboardCard, mountDashboard } from "../grafana.js";

const KEY = "mmd.admin.userfilter";

/* Sortable columns, each with the value it sorts ON rather than the markup it
 * renders. Sorting Persian-digit strings would put twelve before nine and put a
 * machine with no disk reading between two real ones. */
const COLUMNS = [
  { key: "account", label: "adm.account",
    value: (u) => (u.username || "").toLowerCase() },
  { key: "status", label: "adm.status", value: (u) => u.status || "" },
  { key: "machine", label: "adm.machine",
    // Absent machines sort last in either direction: "no machine" is not a
    // small machine, and interleaving them hides both groups.
    value: (u) => (u.workspace ? u.workspace.state || "" : "\uffff") },
  { key: "credit", label: "adm.credit", num: true,
    value: (u) => (u.credits ?? 0) },
];
const readFilter = () => {
  try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; }
};
const saveFilter = (f) => localStorage.setItem(KEY, JSON.stringify(f));

const STATUSES = ["all", "pending", "approved", "rejected", "suspended", "deleting"];

export async function adminUsersPage() {
  const _gf = await grafanaConfig();
  const users = await get("/api/admin/users");
  const f = { status: "all", q: "", sort: "account", dir: "asc", ...readFilter() };

  // Applied to whatever the status filter and the search box have already
  // left, so the three controls compose instead of overriding each other.
  const sorted = (list) => {
    const col = COLUMNS.find((c) => c.key === f.sort) || COLUMNS[0];
    const sign = f.dir === "desc" ? -1 : 1;
    return [...list].sort((a, b) => {
      const x = col.value(a), y = col.value(b);
      if (x === y) return (a.username || "").localeCompare(b.username || "");
      return col.num ? sign * (x - y) : sign * String(x).localeCompare(String(y));
    });
  };

  const statusLabel = Object.fromEntries(
    ["approved", "pending", "rejected", "suspended", "deleting"]
      .map((status) => [status, t("adm.st." + status)]));

  const matches = (u) => {
    if (f.status !== "all" && u.status !== f.status) return false;
    const q = (f.q || "").trim().toLowerCase();
    if (!q) return true;
    return (u.full_name || "").toLowerCase().includes(q)
        || (u.phone || "").includes(q)
        || (u.username || "").toLowerCase().includes(q);
  };

  const counts = Object.fromEntries(STATUSES.map((s) =>
    [s, s === "all" ? users.length : users.filter((u) => u.status === s).length]));

  const row = (u) => `<tr>
    <td data-label="${t("adm.account")}"><a class="ltr mono" style="font-size:13px"
        href="/console/admin/users/${u.id}">${esc(u.username)}</a>
      <div class="tiny dim">${
        u.full_name ? esc(u.full_name) + " · " : ""}${u.phone ? `<span class="ltr">${esc(u.phone)}</span> · ` : ""}${
        u.is_admin ? t("sec.role.admin") + " · " : ""}${t("adm.joined")} ${stamp(u.created_at)}</div></td>
    <td data-label="${t("adm.status")}"><span class="pill"><span class="dot ${u.status === "approved" ? "on"
        : u.status === "pending" ? "busy" : "bad"}"></span>${
        statusLabel[u.status] || u.status}</span></td>
    <td data-label="${t("adm.machine")}" class="small nowrap">${u.workspace
      ? `${statePill(u.workspace.state)}
         <div class="tiny dim ltr" style="margin-top:3px">${
           fmtNum(u.workspace.cpu_cores, 1)} vCPU · ${
           fmtNum(u.workspace.memory_mb / 1024, 1)} GB</div>`
      : `<span class="dim">${t("adm.none")}</span>`}</td>
    <td data-label="${t("adm.credit")}" class="num">${fmtMoney(u.credits)}</td>
    <td data-label="${t("adm.col.action")}" class="num nowrap">
      ${u.status === "pending"
        ? `<button class="btn sm primary" data-approve="${u.id}">${t("adm.approve")}</button>
           <button class="btn sm danger" data-reject="${u.id}">${t("adm.reject")}</button>` : ""}
      <button class="btn sm" data-credit="${u.id}" ${u.status === "deleting" ? "disabled" : ""}>${t("adm.credit")}</button>
      ${u.workspace?.state === "on" ? `<button class="btn sm danger" data-poweroff="${u.workspace.id}"
        data-owner="${esc(u.username)}">${icon.power}${t("adm.poweroff")}</button>` : ""}
      <a class="btn sm ghost" href="/console/admin/users/${u.id}">${t("adm.details")}</a>
      <button class="btn sm ghost" data-admin="${u.id}" data-is="${u.is_admin}">
        ${u.is_admin ? t("adm.demote") : t("adm.makeadmin")}</button>
      ${u.id === state.me?.id || u.status === "deleting" ? "" : `<button class="btn sm danger" data-del="${u.id}"
        data-username="${esc(u.username)}">${icon.trash}</button>`}
    </td></tr>`;

  const shown = sorted(users.filter(matches));

  // The header cell for one column: its label, the arrow when it is the
  // active sort, and the click target that toggles direction.
  const th = (c) => `<th class="${c.num ? "num " : ""}sortable${
    f.sort === c.key ? " sorted" : ""}" data-sort="${c.key}"${
      f.sort === c.key ? ` aria-sort="${f.dir === "asc" ? "ascending" : "descending"}"` : ""}>
    <button type="button">${t(c.label)}${
      c.key === "credit" ? ` <span class="dim">(${CURRENCY})</span>` : ""}
      <span class="arrow" aria-hidden="true">${f.sort === c.key ? (f.dir === "asc" ? "▲" : "▼") : "↕"}</span>
    </button></th>`;

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

      <div id="user-rows">${shown.length ? `<div class="table-wrap"><table class="mobile-cards">
        <thead><tr>${COLUMNS.map(th).join("")}<th></th></tr></thead>
        <tbody>${shown.map(row).join("")}</tbody></table></div>`
        : `<div style="padding:18px">${note("info", t("adm.users.nomatch"))}</div>`}</div>
    </div>
    <p class="tiny dim" id="user-count">${
      t("adm.users.showing", fmtFa(shown.length), fmtFa(users.length))}</p>

    ${dashboardCard(_gf, "users")}`);
  mountDashboard();

  $$("[data-status]").forEach((b) => {
    b.onclick = () => { f.status = b.dataset.status; saveFilter(f); adminUsersPage(); };
  });
  const q = $("#q");
  let timer = null;
  q.oninput = () => {
    // Filtering is purely local - `users` is already in hand - so only the
    // rows are rebuilt. The caret stays where it is, nothing is refetched,
    // and the dashboard below does not reload.
    f.q = q.value;
    const now = sorted(users.filter(matches));
    $("#user-rows").innerHTML = now.length
      ? `<div class="table-wrap"><table class="mobile-cards">
        <thead><tr>${COLUMNS.map(th).join("")}<th></th></tr></thead>
        <tbody>${now.map(row).join("")}</tbody></table></div>`
      : `<div style="padding:18px">${note("info", t("adm.users.nomatch"))}</div>`;
    $("#user-count").textContent =
      t("adm.users.showing", fmtFa(now.length), fmtFa(users.length));
    wireRowActions();
    wireSort();
    // Persisted on a delay so a burst of typing is one write, not one per key.
    clearTimeout(timer);
    timer = setTimeout(() => saveFilter(f), 400);
  };

  // Re-attached whenever the rows are rebuilt, because replacing the
  // table's HTML discards the handlers bound to the old nodes.
  function wireRowActions() {
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
        title: t("adm.confirm.del.title", b.dataset.username),
        intro: t("adm.confirm.del.body"),
        destroys: [t("adm.delete.workspace"), t("adm.delete.addresses"),
                   t("adm.delete.account"), t("adm.delete.history")],
        keeps: [t("adm.delete.keeps")], expect: b.dataset.username,
        label: t("adm.confirm.del.cta"),
      })) return;
      b.disabled = true;
      try { await del(`/api/admin/users/${b.dataset.del}`); toast(t("adm.deletequeued"), "ok"); }
      catch (e) { toast(e.message, "bad"); }
      adminUsersPage();
    });
  }

  // Clicking a header sorts by it; clicking the active one reverses. Only the
  // rows are rebuilt, so the dashboard below does not reload and the search
  // box keeps its caret - the same reason the search filters in place.
  function wireSort() {
    $$("th[data-sort] button").forEach((b) => {
      b.onclick = () => {
        const key = b.parentElement.dataset.sort;
        if (f.sort === key) f.dir = f.dir === "asc" ? "desc" : "asc";
        else { f.sort = key; f.dir = COLUMNS.find((c) => c.key === key)?.num ? "desc" : "asc"; }
        saveFilter(f);
        const now = sorted(users.filter(matches));
        $("#user-rows").innerHTML = `<div class="table-wrap"><table class="mobile-cards">
          <thead><tr>${COLUMNS.map(th).join("")}<th></th></tr></thead>
          <tbody>${now.map(row).join("")}</tbody></table></div>`;
        wireRowActions();
        wireSort();
      };
    });
  }

  wireSort();
  wireRowActions();
}
