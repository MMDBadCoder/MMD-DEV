/* Overview: the whole machine at a glance.
 *
 * The terminal used to live here and now lives under Connections, which frees
 * this page to answer the question someone actually opens it with - what is my
 * machine doing, and what is it costing me. */
import { get, post } from "../api.js";
import { $, icon, esc, fmtMoney, fmtNum, fmtFa, note, toast, stamp,
         statePill, confirmDialog } from "../ui.js";
import { t, CURRENCY } from "../i18n.js";
import { render } from "../main.js";
import { usageChart, windowPicker, wireWindowPicker,
         savedWindow, saveWindow } from "../usagechart.js";

let poll = null;
let chartTimer = null;

/* Redraw ONLY the two charts, in step with the worker's sampling interval.
   Re-rendering the whole page every 20 seconds would fight with anything the
   customer is in the middle of - a size selector, a scroll position - to show
   two lines that moved by a pixel. */
async function refreshCharts() {
  clearTimeout(chartTimer);
  const box = $("#charts");
  if (!box) return;                       // navigated away
  let m;
  try { m = await get(`/api/workspace/metrics?minutes=${savedWindow()}`); }
  catch { chartTimer = setTimeout(refreshCharts, 30000); return; }
  if (!$("#charts")) return;
  $("#chart-cpu").innerHTML =
    usageChart(m.cpu, m.cpu_cores, "var(--brand)", t("unit.cores"));
  $("#chart-mem").innerHTML =
    usageChart(m.memory, m.memory_gb, "var(--ok)", t("unit.gb"));
  chartTimer = setTimeout(refreshCharts, (m.sample_seconds || 20) * 1000);
}



function bars(series) {
  if (!series.length) return `<div class="empty">${t("billing.chart.empty")}</div>`;
  const max = Math.max(...series.map((s) => s.spent), 0.0001);
  return `<div class="chart">${series.map((s) => `
      <div class="col" style="height:${Math.max(2, (s.spent / max) * 100)}%"
           title="${esc(stamp(s.hour))} — ${fmtMoney(s.spent)} ${CURRENCY}"></div>`).join("")}
    </div>
    <div class="between tiny dim" style="margin-top:6px">
      <span>${esc(stamp(series[0].hour))}</span>
      <span>${t("billing.chart.peak")} ${fmtMoney(max)} ${CURRENCY}</span>
      <span>${t("billing.chart.now")}</span></div>`;
}

export async function machinePage() {
  clearTimeout(poll);
  const w = await get("/api/workspace");

  if (w.status === "pending" || w.status === "none") {
    render(`<div class="page-head"><h1>${t("ov.title")}</h1></div>
      <div class="card"><div class="between">
        <div><h2>${t("machine.state." + w.status)}</h2>
        <p class="muted small" style="margin:6px 0 0">${t("machine." + w.status)}</p></div>
        ${statePill(w.status)}</div></div>`);
    return;
  }

  const on = w.powered_on;
  const busy = ["starting", "stopping", "provisioning", "archiving"].includes(w.status);

  // Everything else is supporting detail; a failure there must not blank the page.
  const [usage, metrics, services, activity] = await Promise.all([
    get("/api/billing/usage?hours=48").catch(() => ({ series: [] })),
    get(`/api/workspace/metrics?minutes=${savedWindow()}`)
      .catch(() => ({ cpu: [], memory: [], cpu_cores: 0, memory_gb: 0 })),
    get("/api/workspace/services").catch(() => null),
    get("/api/activity?limit=6").catch(() => ({ events: [] })),
  ]);

  // The customer must learn about the limit HERE, not from a machine that
  // stopped. So this is a full-width bar with its own colour, on the machine
  // page, above the fold - not a line of grey text under the buttons, and not
  // a toast that appears once and is gone.
  const autoStopBanner = (w, on) => {
    if (!on) return "";
    if (!w.auto_stop_at) {
      return `<div class="autostop keep">
        <div>${icon.check}<b>${t("machine.autostop.kept")}</b></div>
        <p>${t("machine.autostop.kept.sub")}</p></div>`;
    }
    const left = Math.max(0, new Date(w.auto_stop_at) - Date.now());
    const hours = Math.floor(left / 3600000);
    const mins = Math.floor((left % 3600000) / 60000);
    return `<div class="autostop">
      <div>${icon.alert}<b>${t("machine.autostop.title", w.auto_stop_hours)}</b></div>
      <p>${t("machine.autostop.body", hours, mins, stamp(w.auto_stop_at))}</p>
      <button class="btn primary sm" id="keep-running">${
        icon.power}${t("machine.autostop.keep")}</button></div>`;
  };

  const svcRow = (label, enabled, href, ic) => `
    <div class="between" style="padding:11px 0;border-bottom:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:10px">${icon[ic]}
        <span>${label}</span></div>
      <div style="display:flex;align-items:center;gap:10px">
        <span class="pill"><span class="dot ${enabled ? "on" : ""}"></span>${
          enabled ? t("conn.on") : t("conn.off")}</span>
        <a class="btn sm ghost" href="${href}">${icon.arrow}</a></div>
    </div>`;

  render(`
    <div class="page-head between">
      <div><h1>${t("ov.title")}</h1>
        <p class="muted small ltr" style="margin:0;text-align:start">${
          esc(t("machine.subtitle", { label: w.label, disk: fmtNum(w.disk_gb) }))}</p></div>
      ${statePill(w.status)}
    </div>

    <div class="card machine-hero ${on ? "is-on" : "is-off"}">
      <div class="machine-control">
        <div>
          <div class="tiny dim">${t("machine.control.state")}</div>
          <div class="machine-state-line">${statePill(w.status)}
            <strong>${t("machine.state." + w.status)}</strong></div>
          <p class="muted small">${on ? t("machine.control.on") : t("machine.control.off")}</p>
        </div>
        <button class="btn ${on ? "danger" : "primary"} machine-power" id="power"
          ${busy ? "disabled" : ""}>
          ${icon.power}<span>${on ? t("machine.power.off") : t("machine.power.on")}</span></button>
      </div>
      ${w.blocked ? `<div class="machine-blocked">${
        note("warn", t("blocked." + w.blocked.code, w.blocked))}</div>` : ""}
      <div class="row" style="margin-bottom:18px">
        <div class="stat"><div class="k">${t("machine.stat.balance")}</div>
          <div class="v">${fmtMoney(w.credits)}<small>${t("unit.toman")}</small></div></div>
        <div class="stat"><div class="k">${t("machine.stat.running")}</div>
          <div class="v">${fmtMoney(w.rate_on_per_hour)}<small>${t("unit.tomanPerHourMax")}</small></div></div>
        <div class="stat"><div class="k">${t("machine.stat.off")}</div>
          <div class="v">${fmtMoney(w.rate_off_per_hour)}<small>${t("unit.tomanPerHour")}</small></div></div>
        <div class="stat"><div class="k">${t("machine.stat.remaining")}</div>
          <div class="v">${fmtFa(w.days_remaining ?? 0, (w.days_remaining ?? 0) >= 10 ? 0 : 1)}<small>${t("machine.days")}</small></div></div>
      </div>
      <div class="btn-row">
        <a class="btn" href="/console/connections/terminal">${icon.machine}${t("ov.openterminal")}</a>
        <a class="btn" href="/console/files">${icon.folder}${t("ov.browsefiles")}</a>
        <a class="btn ghost" href="/console/resources">${icon.sliders}${t("machine.changesize")}</a>
      </div>
      ${on ? "" : note("info", t("machine.offnote", fmtMoney(w.rate_off_per_hour)))}
      ${autoStopBanner(w, on)}
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="between" style="margin-bottom:12px">
        <h3 style="margin:0">${t("ov.usage.title")}</h3>
        ${windowPicker(savedWindow())}
      </div>
      <div class="row" id="charts">
        <div style="flex:1 1 240px">
          <label>${t("ov.usage.cpu")}</label>
          <div id="chart-cpu">${
            usageChart(metrics.cpu, metrics.cpu_cores, "var(--brand)", t("unit.cores"))}</div>
        </div>
        <div style="flex:1 1 240px">
          <label>${t("ov.usage.mem")}</label>
          <div id="chart-mem">${
            usageChart(metrics.memory, metrics.memory_gb, "var(--ok)", t("unit.gb"))}</div>
        </div>
      </div>
      <p class="tiny dim" style="margin:10px 0 0">${
        t("ov.usage.refresh", metrics.sample_seconds || 20)}</p>
    </div>

    <div class="card"><h3>${t("ov.spend")}</h3>${bars(usage.series || [])}</div>

    <div class="row">
      <div class="card" style="margin:0">
        <h3>${t("ov.services")}</h3>
        ${services ? `
          ${svcRow(t("ssh.title"), services.ssh.enabled, "/console/connections/ssh", "link")}
          ${svcRow(t("rdp.title"), services.rdp.enabled, "/console/connections/rdp", "monitor")}
          <div class="between" style="padding:11px 0">
            <div style="display:flex;align-items:center;gap:10px">${icon.plug}
              <span>${t("ov.publishedports")}</span></div>
            <div style="display:flex;align-items:center;gap:10px">
              <span class="dim">${fmtFa(w.published_ports)}</span>
              <a class="btn sm ghost" href="/console/ports">${icon.arrow}</a></div>
          </div>` : `<p class="muted small">${t("common.loading")}</p>`}
      </div>

      <div class="card" style="margin:0">
        <h3>${t("ov.recent")}</h3>
        ${(activity.events || []).length ? `<div class="table-wrap"><table><tbody>
            ${activity.events.slice(0, 6).map((e) => `
              <tr><td>${t("act.a." + e.action) || esc(e.action)}</td>
                  <td class="tiny dim nowrap num">${stamp(e.ts)}</td></tr>`).join("")}
          </tbody></table></div>`
          : `<p class="muted small">${t("ov.noevents")}</p>`}
        <div style="margin-top:10px"><a class="btn sm ghost" href="/console/activity">
          ${t("act.title")} ${icon.arrow}</a></div>
      </div>
    </div>`);

  $("#power").onclick = async (e) => {
    // currentTarget belongs to the synchronous event dispatch and browsers
    // clear it before an awaited dialog resolves. Capture the element first;
    // otherwise power-off works while power-on dies after confirmation without
    // ever sending its request.
    const b = e.currentTarget;
    if (!on && !w.can_power_on && w.blocked) {
      toast(t("blocked." + w.blocked.code, w.blocked), "bad");
      return;
    }
    if (!on) {
      const preview = `<div class="cost-preview">
        <div><span>${t("machine.preview.balance")}</span><b>${fmtMoney(w.credits)} ${CURRENCY}</b></div>
        <div><span>${t("machine.preview.required")}</span><b>${fmtMoney(w.rate_on_per_hour)} ${CURRENCY}</b></div>
        <div><span>${t("machine.preview.idle")}</span><b>${fmtMoney(w.rate_idle_per_hour)} ${CURRENCY}</b></div>
        <div><span>${t("machine.preview.maximum")}</span><b>${fmtMoney(w.rate_on_per_hour)} ${CURRENCY}</b></div>
      </div><p>${t("machine.preview.body")}</p>`;
      if (!await confirmDialog(t("machine.preview.title"), preview,
                               t("machine.power.on"), { tone: "primary" })) return;
    }
    b.disabled = true;
    b.innerHTML = `<span class="spinner"></span>${
      on ? t("machine.power.turningoff") : t("machine.power.turningon")}`;
    try {
      const r = await post("/api/workspace/power", { on: !on });
      toast(on ? t("machine.off.toast") : t("machine.on.toast"), "ok");
      // Stated at the moment the machine starts, not left to be discovered on
      // the page or - worse - from a machine that has already stopped. Both
      // buttons are a real choice, so neither is styled as a cancel.
      if (!on && r.auto_stop_at) {
        const keep = await confirmDialog(
          t("machine.autostop.dialog.title", r.auto_stop_hours ?? w.auto_stop_hours),
          t("machine.autostop.dialog.body", stamp(r.auto_stop_at)),
          t("machine.autostop.keep"),
          { cancelLabel: t("machine.autostop.dialog.ok"), tone: "primary" });
        if (keep) {
          try { await post("/api/workspace/keep-running");
                toast(t("machine.autostop.kept"), "ok"); }
          catch (err2) { toast(err2.message, "bad"); }
        }
      }
    }
    catch (err) { toast(err.message, "bad"); }
    machinePage();
  };
  const keep = $("#keep-running");
  if (keep) {
    keep.onclick = async () => {
      // Confirmed rather than one-click: it turns off a spending limit the
      // customer is otherwise protected by, and "I clicked it by accident" is
      // a bill rather than an inconvenience.
      if (!await confirmDialog(t("machine.autostop.keep"),
                               t("machine.autostop.confirm"),
                               t("machine.autostop.keep"))) return;
      keep.disabled = true;
      keep.innerHTML = `<span class="spinner"></span>${t("conn.working")}`;
      try { await post("/api/workspace/keep-running");
            toast(t("machine.autostop.kept"), "ok"); }
      catch (err) { toast(err.message, "bad"); }
      machinePage();
    };
  }

  wireWindowPicker($("#win"), (m) => {
    saveWindow(m);
    $("#win").querySelectorAll("[data-win]").forEach((b) =>
      b.classList.toggle("active", Number(b.dataset.win) === m));
    refreshCharts();
  });
  clearTimeout(chartTimer);
  chartTimer = setTimeout(refreshCharts, (metrics.sample_seconds || 20) * 1000);

  if (busy) poll = setTimeout(machinePage, 2500);
}

export function teardownTerminal() { clearTimeout(poll); clearTimeout(chartTimer); }
