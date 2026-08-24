/* Overview: the whole machine at a glance.
 *
 * The terminal used to live here and now lives under Connections, which frees
 * this page to answer the question someone actually opens it with - what is my
 * machine doing, and what is it costing me. */
import { get, post } from "../api.js";
import { $, icon, esc, fmtMoney, fmtNum, fmtFa, note, toast, stamp } from "../ui.js";
import { t, CURRENCY } from "../i18n.js";
import { render } from "../main.js";

let poll = null;

function pill(status) {
  const cls = { on: "on", starting: "busy", stopping: "busy", provisioning: "busy",
                archiving: "busy", archived: "bad", error: "bad", pending: "busy" }[status] || "";
  return `<span class="pill"><span class="dot ${cls}"></span>${
    t("machine.state." + status) || status}</span>`;
}

/* A sparkline drawn as an inline SVG path - no chart library, no CDN, and it
   inherits the theme's colours for free. */
function spark(series, colour, unit = "%") {
  if (!series || series.length < 2) {
    return `<div class="tiny dim" style="padding:14px 0">${t("billing.chart.empty")}</div>`;
  }
  const vals = series.map((p) => p.value);
  const max = Math.max(...vals, 1);
  const w = 100, h = 34;
  const pts = vals.map((v, i) =>
    `${(i / (vals.length - 1)) * w},${h - (v / max) * (h - 3) - 1.5}`);
  const last = vals[vals.length - 1];
  return `
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
         style="width:100%;height:44px;display:block">
      <polyline fill="none" stroke="${colour}" stroke-width="1.6"
        stroke-linejoin="round" stroke-linecap="round" points="${pts.join(" ")}"/>
      <polygon fill="${colour}" opacity=".12"
        points="0,${h} ${pts.join(" ")} ${w},${h}"/>
    </svg>
    <div class="between tiny dim" style="margin-top:4px">
      <span>${t("billing.chart.peak")} ${fmtFa(max, max < 10 ? 1 : 0)}${unit}</span>
      <span>${fmtFa(last, last < 10 ? 1 : 0)}${unit}</span>
    </div>`;
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
        ${pill(w.status)}</div></div>`);
    return;
  }

  const on = w.powered_on;
  const busy = ["starting", "stopping", "provisioning", "archiving"].includes(w.status);

  // Everything else is supporting detail; a failure there must not blank the page.
  const [usage, metrics, services, activity] = await Promise.all([
    get("/api/billing/usage?hours=48").catch(() => ({ series: [] })),
    get("/api/workspace/metrics?hours=6").catch(() => ({ cpu: [], memory: [] })),
    get("/api/workspace/services").catch(() => null),
    get("/api/activity?limit=6").catch(() => ({ events: [] })),
  ]);

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
      ${pill(w.status)}
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:18px">
        <div class="stat"><div class="k">${t("machine.stat.balance")}</div>
          <div class="v">${fmtMoney(w.credits)}<small>${t("unit.toman")}</small></div></div>
        <div class="stat"><div class="k">${t("machine.stat.running")}</div>
          <div class="v">${fmtMoney(w.rate_on_per_hour)}<small>${t("unit.tomanPerHourMax")}</small></div></div>
        <div class="stat"><div class="k">${t("machine.stat.off")}</div>
          <div class="v">${fmtMoney(w.rate_off_per_hour)}<small>${t("unit.tomanPerHour")}</small></div></div>
        <div class="stat"><div class="k">${t("machine.stat.remaining")}</div>
          <div class="v">${fmtFa(w.hours_remaining, 1)}<small>${t("machine.hours")}</small></div></div>
      </div>
      <div class="btn-row">
        <button class="btn primary" id="power" ${busy || (!on && !w.can_power_on) ? "disabled" : ""}>
          ${icon.power}${on ? t("machine.power.off") : t("machine.power.on")}</button>
        <a class="btn" href="/console/connections/terminal">${icon.machine}${t("ov.openterminal")}</a>
        <a class="btn" href="/console/files">${icon.folder}${t("ov.browsefiles")}</a>
        <a class="btn ghost" href="/console/resources">${icon.sliders}${t("machine.changesize")}</a>
      </div>
      ${w.blocked_reason ? note("warn", esc(w.blocked_reason)) : ""}
      ${on ? "" : note("info", t("machine.offnote", fmtMoney(w.rate_off_per_hour)))}
    </div>

    <div class="row" style="margin-bottom:16px">
      <div class="card" style="margin:0">
        <h3>${t("ov.usage.cpu")}</h3>
        ${spark(metrics.cpu, "var(--brand)")}
      </div>
      <div class="card" style="margin:0">
        <h3>${t("ov.usage.mem")}</h3>
        ${spark(metrics.memory, "var(--ok)")}
      </div>
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
    const b = e.currentTarget;
    b.disabled = true;
    b.innerHTML = `<span class="spinner"></span>${
      on ? t("machine.power.turningoff") : t("machine.power.turningon")}`;
    try { await post("/api/workspace/power", { on: !on });
          toast(on ? t("machine.off.toast") : t("machine.on.toast"), "ok"); }
    catch (err) { toast(err.message, "bad"); }
    machinePage();
  };
  if (busy) poll = setTimeout(machinePage, 2500);
}

export function teardownTerminal() { clearTimeout(poll); }
