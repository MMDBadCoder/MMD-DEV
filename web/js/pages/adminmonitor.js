/* Monitoring: the host as a whole, and every workspace on it separately.
 *
 * The aggregate says whether the machine is in trouble. The per-workspace lines
 * say who is causing it - which is the half that leads to an action, because
 * "a core is busy" is not something anyone can act on and "this account is
 * using a core" is. */
import { get, put } from "../api.js";
import { $, $$, esc, fmtNum, icon, note, toast } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";
import { adminHead } from "./adminnav.js";
import { usageChart, multiChart, windowPicker, wireWindowPicker,
         savedWindow, saveWindow } from "../usagechart.js";

export async function adminMonitorPage() {
  const minutes = savedWindow();
  const [host, per, cap, settings] = await Promise.all([
    get(`/api/admin/metrics?minutes=${minutes}`).catch(() => null),
    get(`/api/admin/metrics/per-user?minutes=${minutes}`).catch(() => null),
    get("/api/admin/capacity").catch(() => null),
    get("/api/admin/settings").catch(() => ({})),
  ]);

  if (!host) {
    render(`${adminHead("monitoring", t("adm.mon.title"))}${note("bad", t("adm.mon.nodata"))}`);
    return;
  }

  const cpuSeries = (per?.series || []).map((s) => ({ label: s.label, points: s.cpu }));
  const memSeries = (per?.series || []).map((s) => ({ label: s.label, points: s.memory }));

  // Per-workspace lines are scaled to the biggest single tier, not to the host
  // total: against 3 schedulable cores a 1-core workspace at full tilt would
  // draw a third of the height and read as idle.
  const tierCpu = Math.max(...cpuSeries.flatMap((s) => s.points.map((p) => p.value)), 1);
  const tierMem = Math.max(...memSeries.flatMap((s) => s.points.map((p) => p.value)), 1);

  render(`
    ${adminHead("policy", t("adm.policy.title"), t("adm.policy.sub"))}

    <div class="card">
      <h3>${t("adm.rates")}</h3>
      <div class="row">${Object.entries(settings).map(([k, v]) => `
        <div style="min-width:230px"><label for="s-${k}" class="ltr">${
          esc(k.replace(/_/g, " "))}</label>
          <input id="s-${k}" class="ltr" data-setting="${esc(k)}" value="${esc(v)}"></div>`).join("")}</div>
      <div class="btn-row" style="margin-top:16px"><button class="btn primary" id="save-policy">${
        icon.save}${t("adm.save")}</button></div>
    </div>

    <div class="card">
      <div class="between" style="margin-bottom:6px">
        <h3 style="margin:0">${t("adm.mon.host")}</h3>
        ${windowPicker(minutes)}
      </div>
      <p class="tiny dim" style="margin:0 0 12px">${
        t("adm.mon.host.sub", fmtNum(host.workspaces || 0))}</p>

      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))">
        <div>
          <div class="chart-title tiny">${t("adm.mon.cpu")}</div>
          ${usageChart(host.cpu, host.cpu_cores, "#3b82f6", t("res.cores"))}
        </div>
        <div>
          <div class="chart-title tiny">${t("adm.mon.mem")}</div>
          ${usageChart(host.memory, host.memory_gb, "#8b5cf6", "GB")}
        </div>
      </div>
    </div>

    <div class="card">
      <h3>${t("adm.mon.percpu")}</h3>
      <p class="tiny dim" style="margin:2px 0 12px">${t("adm.mon.percpu.sub")}</p>
      ${multiChart(cpuSeries, tierCpu, t("res.cores"))}
    </div>

    <div class="card">
      <h3>${t("adm.mon.permem")}</h3>
      <p class="tiny dim" style="margin:2px 0 12px">${t("adm.mon.permem.sub")}</p>
      ${multiChart(memSeries, tierMem, "GB")}
    </div>

    ${cap ? `<div class="card">
      <h3>${t("adm.resources")}</h3>
      <p class="tiny dim" style="margin:2px 0 14px">${t("adm.resources.sub")}</p>
      <div class="row">
        <div class="stat"><div class="k">${t("adm.running")}</div>
          <div class="v">${fmtNum(cap.running)}</div></div>
        <div class="stat"><div class="k">${t("adm.res.cpu")}</div>
          <div class="v">${fmtNum(cap.used_cores, 1)} / ${fmtNum(cap.schedulable_cores, 1)}</div></div>
        <div class="stat"><div class="k">${t("adm.res.mem")}</div>
          <div class="v">${fmtNum(cap.used_mem_gib, 1)} / ${fmtNum(cap.schedulable_mem_gib, 1)} GB</div></div>
      </div>
    </div>` : ""}`);

  wireWindowPicker(document, (m) => { saveWindow(m); adminMonitorPage(); });
  $("#save-policy").onclick = async () => {
    const body = {};
    $$('[data-setting]').forEach((i) => body[i.dataset.setting] = i.value);
    try { await put("/api/admin/settings", body); toast(t("adm.saved"), "ok"); }
    catch (e) { toast(e.message, "bad"); }
  };
}
