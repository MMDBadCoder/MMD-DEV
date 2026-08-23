/* Administration: people, capacity, pricing, default toolsets. */
import { get, post, put, del } from "../api.js";
import { $, $$, icon, esc, fmtMoney, fmtNum, fmtFa, note, toast, stamp, confirmDialog } from "../ui.js";
import { t, CURRENCY } from "../i18n.js";
import { render, state } from "../main.js";

export async function adminPage() {
  const [users, cap, settings, presets] = await Promise.all([
    get("/api/admin/users"), get("/api/admin/capacity"),
    get("/api/admin/settings"), get("/api/presets"),
  ]);

  const pct = (a, b) => Math.min(100, Math.round(100 * a / Math.max(b, 0.001)));
  const cpuPct = pct(cap.used_cores, cap.schedulable_cores);
  const memPct = pct(cap.used_mem_gib, cap.schedulable_mem_gib);
  const cls = (p) => (p > 90 ? "bad" : p > 70 ? "warn" : "");
  const pending = users.filter((u) => u.status === "pending");

  const statusLabel = { approved: "تأیید شده", pending: "در انتظار تأیید",
                        rejected: "رد شده", suspended: "معلق" };

  const row = (u) => `<tr>
    <td><div class="ltr mono" style="font-size:13px">${esc(u.email)}</div>
      <div class="tiny dim">${u.is_admin ? t("sec.role.admin") + " · " : ""}${t("adm.joined")} ${stamp(u.created_at)}</div></td>
    <td><span class="pill"><span class="dot ${u.status === "approved" ? "on"
        : u.status === "pending" ? "busy" : "bad"}"></span>${statusLabel[u.status] || u.status}</span></td>
    <td class="small">${u.workspace
      ? `${t("machine.state." + u.workspace.state) || u.workspace.state} · <span class="ltr">${fmtNum(u.workspace.cpu_cores, 1)} vCPU · ${fmtNum(u.workspace.memory_mb / 1024, 1)} GB</span>`
      : `<span class="dim">${t("adm.none")}</span>`}</td>
    <td class="num">${fmtMoney(u.credits)}</td>
    <td class="num nowrap">
      ${u.status === "pending"
        ? `<button class="btn sm primary" data-approve="${u.id}">${t("adm.approve")}</button>
           <button class="btn sm danger" data-reject="${u.id}">${t("adm.reject")}</button>` : ""}
      <button class="btn sm" data-credit="${u.id}">${t("adm.addcredit")}</button>
      <button class="btn sm ghost" data-admin="${u.id}" data-is="${u.is_admin}">
        ${u.is_admin ? t("adm.demote") : t("adm.makeadmin")}</button>
      ${u.id === state.me?.id ? "" : `<button class="btn sm danger" data-del="${u.id}"
        data-email="${esc(u.email)}">${icon.trash}</button>`}
    </td></tr>`;

  render(`
    <div class="page-head"><h1>${t("adm.title")}</h1>
      <p class="muted small" style="margin:0">${t("adm.sub")}</p></div>

    ${pending.length ? note("warn", t("adm.pending", fmtNum(pending.length))) : ""}

    <div class="card">
      <h3>${t("adm.capacity")}</h3>
      <div class="row">
        <div class="stat"><div class="k">${t("adm.running")}</div>
          <div class="v">${fmtFa(cap.running)}</div></div>
        <div class="stat"><div class="k">${t("adm.cpualloc")}</div>
          <div class="v ltr">${fmtNum(cap.used_cores, 1)}<small>/ ${fmtNum(cap.schedulable_cores, 1)}</small></div>
          <div class="bar"><i class="${cls(cpuPct)}" style="width:${cpuPct}%"></i></div></div>
        <div class="stat"><div class="k">${t("adm.memalloc")}</div>
          <div class="v ltr">${fmtNum(cap.used_mem_gib, 1)}<small>/ ${fmtNum(cap.schedulable_mem_gib, 1)} GB</small></div>
          <div class="bar"><i class="${cls(memPct)}" style="width:${memPct}%"></i></div></div>
      </div>
      <p class="tiny dim" style="margin:14px 0 0">${t("adm.capacity.note")}</p>
    </div>

    <div class="card">
      <h3>${t("adm.presets")}</h3>
      <p class="tiny dim" style="margin:0 0 12px">${t("adm.presets.hint")}</p>
      <div class="opts" style="grid-template-columns:repeat(auto-fit,minmax(190px,1fr))">
        ${presets.presets.map((p) => `
          <button class="opt" data-defpreset="${esc(p.key)}" style="text-align:start;padding:12px 14px">
            <div style="font-weight:600;font-size:14px">${t("tools.preset." + p.key) || esc(p.key)}</div>
            <div class="tiny dim mono ltr">${p.packages.length} pkg</div></button>`).join("")}
      </div>
    </div>

    <div class="card pad0">
      <div class="card-head"><h2>${t("adm.people")}</h2>
        <span class="dim small">${fmtFa(users.length)}</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>${t("adm.account")}</th><th>${t("adm.status")}</th>
          <th>${t("adm.machine")}</th><th class="num">${t("adm.credit")} <span class="dim">(${CURRENCY})</span></th><th></th></tr></thead>
        <tbody>${users.map(row).join("")}</tbody></table></div>
    </div>

    <div class="card">
      <h3>${t("adm.rates")}</h3>
      <div class="row">
        ${Object.entries(settings).map(([k, v]) => `
          <div style="min-width:230px"><label for="s-${k}" class="ltr" style="direction:ltr;text-align:start">${esc(k.replace(/_/g, " "))}</label>
            <input id="s-${k}" class="ltr" data-setting="${esc(k)}" value="${esc(v)}"></div>`).join("")}
      </div>
      <div class="btn-row" style="margin-top:16px">
        <button class="btn primary" id="save">${icon.save}${t("adm.save")}</button></div>
    </div>`);

  // Default toolsets are remembered locally and sent with each approval.
  const defaults = new Set(JSON.parse(localStorage.getItem("mmd-default-presets") || "[]"));
  $$("[data-defpreset]").forEach((b) => {
    b.classList.toggle("sel", defaults.has(b.dataset.defpreset));
    b.onclick = () => {
      const k = b.dataset.defpreset;
      defaults.has(k) ? defaults.delete(k) : defaults.add(k);
      b.classList.toggle("sel", defaults.has(k));
      localStorage.setItem("mmd-default-presets", JSON.stringify([...defaults]));
    };
  });

  $$("[data-approve]").forEach((b) => b.onclick = async () => {
    b.disabled = true; b.innerHTML = `<span class="spinner"></span>${t("adm.approving")}`;
    try {
      await post(`/api/admin/users/${b.dataset.approve}/approve`, { presets: [...defaults] });
      toast(t("adm.machinecreated"), "ok");
    } catch (e) { toast(e.message, "bad"); }
    adminPage();
  });

  $$("[data-reject]").forEach((b) => b.onclick = async () => {
    if (!await confirmDialog(t("adm.confirm.reject.title"), t("adm.confirm.reject.body"),
                             t("adm.reject"))) return;
    try { await post(`/api/admin/users/${b.dataset.reject}/reject`); }
    catch (e) { toast(e.message, "bad"); }
    adminPage();
  });

  $$("[data-credit]").forEach((b) => b.onclick = async () => {
    const v = prompt(t("adm.creditprompt"), "500000");
    if (v === null) return;
    try {
      const r = await post(`/api/admin/users/${b.dataset.credit}/credit`,
                           { credits: Number(v), note: "admin grant" });
      toast(t("adm.newbalance", fmtMoney(r.balance)), "ok");
    } catch (e) { toast(e.message, "bad"); }
    adminPage();
  });

  $$("[data-admin]").forEach((b) => b.onclick = async () => {
    const makeAdmin = b.dataset.is !== "true";
    try { await post(`/api/admin/users/${b.dataset.admin}/admin`, { is_admin: makeAdmin }); }
    catch (e) { toast(e.message, "bad"); }
    adminPage();
  });

  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!await confirmDialog(t("adm.confirm.del.title", b.dataset.email),
                             t("adm.confirm.del.body"), t("adm.confirm.del.cta"))) return;
    b.disabled = true;
    try { await del(`/api/admin/users/${b.dataset.del}`); toast(t("adm.deleted"), "ok"); }
    catch (e) { toast(e.message, "bad"); }
    adminPage();
  });

  $("#save").onclick = async () => {
    const body = {};
    $$("[data-setting]").forEach((i) => body[i.dataset.setting] = i.value);
    try { await put("/api/admin/settings", body); toast(t("adm.saved"), "ok"); }
    catch (e) { toast(e.message, "bad"); }
    adminPage();
  };
}
