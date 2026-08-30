/* Administration overview.
 *
 * This used to be everything: approvals, capacity, host charts, pricing, the
 * rate card and a link to AI pricing, stacked in one column. Each is a
 * different job done at a different time, so the page is now a short landing
 * screen - what needs attention, and where the sections are - with the work
 * itself on real routes under /console/admin/*.
 *
 * What stays here is what is genuinely "the host as a whole": how much capacity
 * is committed and what a workspace is charged. */
import { get, put } from "../api.js";
import { $, $$, icon, esc, fmtMoney, fmtNum, fmtFa, note, toast,
         confirmDialog } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";

export async function adminPage() {
  const [users, cap, settings, tickets, claude] = await Promise.all([
    get("/api/admin/users"), get("/api/admin/capacity"),
    get("/api/admin/settings"), get("/api/admin/tickets"),
    get("/api/admin/ai-pricing"),
  ]);

  const pct = (a, b) => Math.min(100, Math.round(100 * a / Math.max(b, 0.001)));
  const cpuPct = pct(cap.used_cores, cap.schedulable_cores);
  const memPct = pct(cap.used_mem_gib, cap.schedulable_mem_gib);
  const cls = (p) => (p > 90 ? "bad" : p > 70 ? "warn" : "");
  const pending = users.filter((u) => u.status === "pending");
  const waiting = (tickets.counts.open || 0) + (tickets.counts.in_progress || 0);

  render(`
    ${adminHead("", t("adm.title"), t("adm.sub"))}

    ${pending.length
      ? `<a class="card between attn" href="/console/admin/users">
           <div><h3 style="margin:0">${t("adm.pending.title")}</h3>
             <p class="muted small" style="margin:4px 0 0">${
               t("adm.pending", fmtFa(pending.length))}</p></div>
           <span class="btn primary">${t("adm.users.review")}</span></a>`
      : ""}

    ${waiting
      ? `<a class="card between attn" href="/console/admin/tickets">
           <div><h3 style="margin:0">${t("tk.admin.title")}</h3>
             <p class="muted small" style="margin:4px 0 0">${
               t("adm.tickets.waiting", fmtFa(waiting))}</p></div>
           <span class="btn primary">${t("adm.tickets.open")}</span></a>`
      : ""}

    <div class="card">
      <h3>${t("adm.resources")}</h3>
      <p class="tiny dim" style="margin:2px 0 14px">${t("adm.resources.sub")}</p>
      <div class="row">
        <div class="stat"><div class="k">${t("adm.running")}</div>
          <div class="v">${fmtFa(cap.running)}</div></div>
        <div class="stat"><div class="k">${t("adm.cpualloc")}</div>
          <div class="v ltr">${fmtNum(cap.used_cores, 1)}<small>/ ${
            fmtNum(cap.schedulable_cores, 1)}</small></div>
          <div class="bar"><i class="${cls(cpuPct)}" style="width:${cpuPct}%"></i></div></div>
        <div class="stat"><div class="k">${t("adm.memalloc")}</div>
          <div class="v ltr">${fmtNum(cap.used_mem_gib, 1)}<small>/ ${
            fmtNum(cap.schedulable_mem_gib, 1)} GB</small></div>
          <div class="bar"><i class="${cls(memPct)}" style="width:${memPct}%"></i></div></div>
        <div class="stat"><div class="k">${t("adm.people")}</div>
          <div class="v">${fmtFa(users.length)}</div></div>
      </div>
      <p class="tiny dim" style="margin:14px 0 0">${t("adm.capacity.note")}</p>
      <div class="btn-row" style="margin-top:14px">
        <a class="btn" href="/console/admin/monitoring">${icon.chart}${t("adm.mon.title")}</a>
      </div>
    </div>

    <div class="card">
      <div class="between">
        <div><h3>${t("adm.commercial.title")}</h3>
          <p class="tiny dim" style="margin:3px 0 0">${t("adm.commercial.sub")}</p></div>
        <a class="btn primary" href="/console/admin/claude">${icon.sparkle}${t("adm.commercial.manage")}</a>
      </div>
      <div class="row" style="margin-top:14px">
        <div class="stat"><div class="k">${t("adm.ai.usdrate")}</div>
          <div class="v">${fmtMoney(claude.usd_to_toman)}</div></div>
        <div class="stat"><div class="k">${t("adm.ai.discount")}</div>
          <div class="v">${fmtFa(claude.discount_percent)}<small>٪</small></div></div>
        <div class="stat"><div class="k">${t("adm.commercial.models")}</div>
          <div class="v">${fmtFa(claude.prices.length)}</div></div>
      </div>
    </div>

    <div class="card">
      <h3>${t("adm.rates")}</h3>
      <div class="row">
        ${Object.entries(settings).map(([k, v]) => `
          <div style="min-width:230px">
            <label for="s-${k}" class="ltr" style="direction:ltr;text-align:start">${
              esc(k.replace(/_/g, " "))}</label>
            <input id="s-${k}" class="ltr" data-setting="${esc(k)}" value="${esc(v)}"></div>`).join("")}
      </div>
      <div class="btn-row" style="margin-top:16px">
        <button class="btn primary" id="save">${icon.save}${t("adm.save")}</button></div>
    </div>`);

  $("#save").onclick = async () => {
    const body = {};
    $$("[data-setting]").forEach((i) => body[i.dataset.setting] = i.value);
    try { await put("/api/admin/settings", body); toast(t("adm.saved"), "ok"); }
    catch (e) { toast(e.message, "bad"); }
    adminPage();
  };
}
