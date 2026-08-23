/* Administration: people, capacity, pricing. */
import { get, post, put, del } from "../api.js";
import { $, $$, icon, esc, fmt, note, toast, empty, stamp, confirmDialog } from "../ui.js";
import { render, state } from "../main.js";

export async function adminPage() {
  const [users, cap, settings] = await Promise.all([
    get("/api/admin/users"), get("/api/admin/capacity"), get("/api/admin/settings"),
  ]);

  const pct = (a, b) => Math.min(100, Math.round(100 * a / Math.max(b, 0.001)));
  const cpuPct = pct(cap.used_cores, cap.schedulable_cores);
  const memPct = pct(cap.used_mem_gib, cap.schedulable_mem_gib);
  const cls = (p) => (p > 90 ? "bad" : p > 70 ? "warn" : "");

  const pending = users.filter((u) => u.status === "pending");

  const row = (u) => `<tr>
    <td><div>${esc(u.email)}</div>
      <div class="tiny dim">${u.is_admin ? "Administrator · " : ""}joined ${stamp(u.created_at)}</div></td>
    <td><span class="pill"><span class="dot ${u.status === "approved" ? "on" : u.status === "pending" ? "busy" : "bad"}"></span>${esc(u.status)}</span></td>
    <td class="small">${u.workspace
      ? `${esc(u.workspace.state)} · ${u.workspace.cpu_cores} vCPU · ${(u.workspace.memory_mb / 1024)} GB`
      : '<span class="dim">none</span>'}</td>
    <td class="num">${fmt(u.credits)}</td>
    <td class="num nowrap">
      ${u.status === "pending"
        ? `<button class="btn sm primary" data-approve="${u.id}">Approve</button>
           <button class="btn sm danger" data-reject="${u.id}">Reject</button>` : ""}
      <button class="btn sm" data-credit="${u.id}">Credit</button>
      <button class="btn sm ghost" data-admin="${u.id}" data-is="${u.is_admin}">
        ${u.is_admin ? "Demote" : "Make admin"}</button>
      ${u.id === state.me?.id ? "" : `<button class="btn sm danger" data-del="${u.id}" data-email="${esc(u.email)}">${icon.trash}</button>`}
    </td></tr>`;

  render(`
    <div class="page-head"><h1>Administration</h1>
      <p class="muted small" style="margin:0">People, host capacity and pricing.</p></div>

    ${pending.length ? note("warn",
      `<b>${pending.length} account${pending.length > 1 ? "s" : ""} awaiting approval.</b>
       Approving one creates a machine for it.`) : ""}

    <div class="card">
      <h3>Host capacity</h3>
      <div class="row">
        <div class="stat"><div class="k">Machines running</div><div class="v">${cap.running}</div></div>
        <div class="stat"><div class="k">CPU allocated</div>
          <div class="v">${fmt(cap.used_cores, 1)}<small>/ ${fmt(cap.schedulable_cores, 1)}</small></div>
          <div class="bar"><i class="${cls(cpuPct)}" style="width:${cpuPct}%"></i></div></div>
        <div class="stat"><div class="k">Memory allocated</div>
          <div class="v">${fmt(cap.used_mem_gib, 1)}<small>/ ${fmt(cap.schedulable_mem_gib, 1)} GB</small></div>
          <div class="bar"><i class="${cls(memPct)}" style="width:${memPct}%"></i></div></div>
      </div>
      <p class="tiny dim" style="margin:14px 0 0">Capacity is claimed when a machine is
      switched on and released when it is switched off. Sign-ups are unlimited; switching
      on is refused when the host is full.</p>
    </div>

    <div class="card pad0">
      <div class="card-head"><h2>People</h2><span class="dim small">${users.length}</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Account</th><th>Status</th><th>Machine</th><th class="num">Credit</th><th></th></tr></thead>
        <tbody>${users.map(row).join("")}</tbody></table></div>
    </div>

    <div class="card">
      <h3>Pricing and capacity policy</h3>
      <div class="row">
        ${Object.entries(settings).map(([k, v]) => `
          <div style="min-width:220px"><label for="s-${k}">${esc(k.replace(/_/g, " "))}</label>
            <input id="s-${k}" data-setting="${esc(k)}" value="${esc(v)}"></div>`).join("")}
      </div>
      <div class="btn-row" style="margin-top:16px">
        <button class="btn primary" id="save">${icon.check}Save settings</button></div>
    </div>`);

  $$("[data-approve]").forEach((b) => b.onclick = async () => {
    b.disabled = true; b.innerHTML = `<span class="spinner"></span>Creating…`;
    try { await post(`/api/admin/users/${b.dataset.approve}/approve`); toast("Machine created", "ok"); }
    catch (e) { toast(e.message, "bad"); }
    adminPage();
  });

  $$("[data-reject]").forEach((b) => b.onclick = async () => {
    if (!await confirmDialog("Reject this account?", "They will not be able to sign in.", "Reject")) return;
    try { await post(`/api/admin/users/${b.dataset.reject}/reject`); } catch (e) { toast(e.message, "bad"); }
    adminPage();
  });

  $$("[data-credit]").forEach((b) => b.onclick = async () => {
    const v = prompt("How many credits to add? (negative removes)", "100");
    if (v === null) return;
    try {
      const r = await post(`/api/admin/users/${b.dataset.credit}/credit`,
                           { credits: Number(v), note: "admin grant" });
      toast(`Balance is now ${fmt(r.balance)}`, "ok");
    } catch (e) { toast(e.message, "bad"); }
    adminPage();
  });

  $$("[data-admin]").forEach((b) => b.onclick = async () => {
    const makeAdmin = b.dataset.is !== "true";
    try {
      await post(`/api/admin/users/${b.dataset.admin}/admin`, { is_admin: makeAdmin });
      toast(makeAdmin ? "Now an administrator" : "Administrator role removed", "ok");
    } catch (e) { toast(e.message, "bad"); }
    adminPage();
  });

  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!await confirmDialog(`Delete ${b.dataset.email}?`,
        "Their machine and every file on it are destroyed permanently. This cannot be undone.",
        "Delete permanently")) return;
    b.disabled = true;
    try { await del(`/api/admin/users/${b.dataset.del}`); toast("Account deleted", "ok"); }
    catch (e) { toast(e.message, "bad"); }
    adminPage();
  });

  $("#save").onclick = async () => {
    const body = {};
    $$("[data-setting]").forEach((i) => body[i.dataset.setting] = i.value);
    try { await put("/api/admin/settings", body); toast("Settings saved", "ok"); }
    catch (e) { toast(e.message, "bad"); }
    adminPage();
  };
}
