/* Connections: three ways into the machine, one per tab.
 *
 * Keys and switches stay separate throughout - adding a key never opens a
 * listener, and switching a listener off never discards keys. */
import { get, post, del } from "../api.js";
import { $, $$, icon, esc, fmtFa, note, toast, stamp, confirmDialog,
         recoveryNote, wireRecovery } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";
import * as term from "../terminal.js";

const TABS = [
  { key: "terminal", ic: "machine" },
  { key: "ssh", ic: "link" },
  { key: "rdp", ic: "monitor" },
];

function statusPill(on) {
  return `<span class="pill"><span class="dot ${on ? "on" : ""}"></span>${
    on ? t("conn.on") : t("conn.off")}</span>`;
}

function copyRow(label, value) {
  return `<div style="margin-bottom:12px">
    <label>${esc(label)}</label>
    <div class="row" style="gap:8px;align-items:center;flex-wrap:nowrap">
      <input class="mono ltr" dir="ltr" readonly value="${esc(value)}" style="flex:1 1 auto">
      <button class="btn icon ghost" data-copy="${esc(value)}"
        aria-label="${t("common.copy")} ${esc(label)}"
        style="flex:0 0 auto">${icon.copy}</button>
    </div></div>`;
}

function tabBar(active, d) {
  return `<nav class="tabs2" aria-label="${t("nav.connection.sections")}">${TABS.map((tb) => {
    const on = tb.key === "ssh" ? d.ssh.enabled
             : tb.key === "rdp" ? d.rdp.enabled : null;
    const badge = on === null ? ""
      : `<span class="badge ${on ? "on" : ""}">${on ? t("conn.on") : t("conn.off")}</span>`;
    return `<a href="/console/connections/${tb.key}"
      class="${tb.key === active ? "active" : ""}" ${tb.key === active ? 'aria-current="page"' : ""}>${icon[tb.ic]}
      ${t("conn.tab." + tb.key)}${badge}</a>`;
  }).join("")}</nav>`;
}

function launcher(d) {
  const card = (iconName, title, status, body, action, tone = "") => `
    <section class="connection-launch ${tone}">
      <div class="connection-launch-head">${icon[iconName]}<b>${title}</b>${status}</div>
      <p>${body}</p><div class="connection-launch-action">${action}</div>
    </section>`;
  const ready = (yes) => `<span class="pill"><span class="dot ${yes ? "on" : ""}"></span>${
    yes ? t("conn.available") : t("conn.unavailable")}</span>`;
  const power = `<a class="btn sm primary" href="/console">${icon.power}${t("conn.launch.power")}</a>`;
  const terminalAction = d.machine_running
    ? `<a class="btn sm primary" href="/console/connections/terminal">${icon.machine}${t("conn.launch.open")}</a>` : power;
  const sshAction = !d.machine_running ? power : d.ssh.key_count === 0
    ? `<a class="btn sm primary" href="/console/connections/ssh">${icon.plus}${t("conn.launch.addkey")}</a>`
    : `<a class="btn sm primary" href="/console/connections/ssh">${icon.copy}${t("conn.launch.command")}</a>`;
  const rdpAction = !d.machine_running ? power
    : `<a class="btn sm primary" href="/console/connections/rdp">${icon.monitor}${
        d.rdp.enabled ? t("conn.launch.details") : t("conn.launch.enable")}</a>`;
  const appAction = d.applications.length
    ? `<a class="btn sm primary" href="/console/ports">${icon.plug}${t("conn.launch.apps")}</a>`
    : `<a class="btn sm primary" href="/console/ports">${icon.plus}${t("conn.launch.publish")}</a>`;
  return `<div class="connection-launcher">
    ${card("machine", t("conn.tab.terminal"), ready(d.machine_running),
      d.machine_running ? t("conn.launch.terminal.ready") : t("conn.launch.machineoff"), terminalAction)}
    ${card("link", "SSH", ready(d.machine_running && d.ssh.enabled),
      d.ssh.key_count ? (d.ssh.address || "—") : t("conn.launch.ssh.needkey"), sshAction)}
    ${card("monitor", "RDP", ready(d.machine_running && d.rdp.enabled),
      d.rdp.memory_ok ? (d.rdp.address || "—") : t("rdp.needmem"), rdpAction)}
    ${card("plug", t("conn.launch.published"), ready(d.machine_running && d.applications.length > 0),
      d.applications.length ? t("conn.launch.appcount", fmtFa(d.applications.length))
                            : t("conn.launch.noapps"), appAction)}
  </div>`;
}

export async function connectionsPage(params) {
  const tab = params?.tab && TABS.some((x) => x.key === params.tab) ? params.tab : "terminal";
  if (tab !== "terminal") term.disconnect();

  let d;
  try { d = await get("/api/workspace/services"); }
  catch (e) {
    if (e.code === "no_workspace") {
      render(`<div class="page-head"><h1>${t("conn.title")}</h1></div>
        <div class="card empty"><p>${t("workspace.empty.short")}</p>
          <a class="btn primary" href="/console">${icon.plus}${t("workspace.create")}</a></div>`);
      return;
    }
    render(`<div class="page-head"><h1>${t("conn.title")}</h1></div>${recoveryNote(e, { retry: true })}`);
    wireRecovery(() => connectionsPage(params));
    return;
  }

  const head = `<div class="page-head"><h1>${t("conn.title")}</h1>
      <p class="muted small" style="margin:0">${t("conn.sub")}</p></div>
    ${launcher(d)}
    ${tabBar(tab, d)}
    ${d.machine_running ? "" : note("warn", t("conn.machineoff"))}`;

  if (tab === "terminal") return renderTerminal(head, d);
  if (tab === "ssh") return renderSsh(head, d);
  return renderRdp(head, d);
}

/* ---- terminal ---- */
function renderTerminal(head, d) {
  if (d.machine_running) localStorage.setItem("mmd-onboarding-terminal", "1");
  render(`${head}
    <div class="card">
      <p class="muted small" style="margin:0 0 14px">${t("conn.term.sub")}</p>
      ${term.panel(d.machine_running)}
    </div>`);
  if (d.machine_running) term.wire();
  $$("[data-copy]").forEach(wireCopy);
}

/* ---- ssh ---- */
function renderSsh(head, d) {
  const rows = d.ssh.keys.map((k) => `
    <tr>
      <td data-label="${t("ssh.keys.name")} / ${t("ssh.keys.fingerprint")}"><div style="font-weight:600">${esc(k.comment || t("ssh.keys.unnamed"))}</div>
          <div class="mono ltr tiny dim" dir="ltr">${esc(k.fingerprint)}</div></td>
      <td data-label="${t("ssh.keys.type")}" class="mono ltr tiny dim" dir="ltr">${esc(k.type)}</td>
      <td data-label="${t("ssh.keys.when")}" class="tiny dim nowrap">${stamp(k.created_at)}</td>
      <td data-label="${t("common.actions")}" class="num"><button class="btn sm danger" data-remove="${k.id}">
        ${icon.trash}${t("ssh.keys.remove")}</button></td>
    </tr>`).join("");

  render(`${head}
    <div class="card">
      <div class="between" style="margin-bottom:14px">
        <div><h2>${t("ssh.title")}</h2>
          <p class="muted small" style="margin:4px 0 0">${t("ssh.desc")}</p></div>
        ${statusPill(d.ssh.enabled)}
      </div>
      ${copyRow(t("conn.address"), d.ssh.address || "—")}
      ${copyRow(t("ssh.command"), d.ssh.command || "—")}
      <p class="tiny dim" style="margin:0 0 16px">${t("conn.reserved")}</p>
      <div class="btn-row">
        <button class="btn ${d.ssh.enabled ? "danger" : "primary"}" id="ssh-toggle"
          >
          ${icon.bolt}${d.ssh.enabled ? t("conn.turnoff") : t("conn.turnon")}</button>
      </div>
      ${d.ssh.key_count === 0 ? note("warn", t("ssh.needkey"))
        : (d.ssh.enabled ? note("info", t("ssh.auth.note")) : note("info", t("ssh.offnote")))}
      <div id="ssh-msg"></div>
    </div>

    <div class="card pad0">
      <div class="card-head"><h2>${t("ssh.keys.title")}</h2>
        <span class="dim small">${t("ssh.keys.count", fmtFa(d.ssh.key_count))}</span></div>
      <div class="card-body">
        <p class="muted small" style="margin:0 0 16px">${t("ssh.keys.intro")}</p>
        <label for="newkey">${t("ssh.keys.label")}</label>
        <div class="row" style="gap:8px;align-items:flex-start;flex-wrap:nowrap">
          <input id="newkey" class="mono ltr" dir="ltr" style="flex:1 1 auto"
                 placeholder="${t("ssh.keys.placeholder")}">
          <button class="btn primary" id="addkey" style="flex:0 0 auto">
            ${icon.plus}${t("ssh.keys.add")}</button>
        </div>
        <div id="key-msg"></div>
      </div>
      ${d.ssh.keys.length ? `<div class="table-wrap"><table class="mobile-cards">
          <thead><tr><th>${t("ssh.keys.name")} / ${t("ssh.keys.fingerprint")}</th>
            <th>${t("ssh.keys.type")}</th><th>${t("ssh.keys.when")}</th><th></th></tr></thead>
          <tbody>${rows}</tbody></table></div>`
        : `<div class="card-body" style="padding-top:0">
             <p class="muted small" style="margin:0">${t("ssh.keys.none")}</p></div>`}
    </div>

    <div class="card">
      <h3>${t("ssh.help.title")}</h3>
      <h4 style="font-size:14px;margin:14px 0 4px">${t("ssh.help.linux")}</h4>
      <p class="tiny dim" style="margin:0">${t("ssh.help.show")}</p>
      ${codeBlock("cat ~/.ssh/id_ed25519.pub")}
      <p class="tiny dim" style="margin:8px 0 0">${t("ssh.help.make")}</p>
      ${codeBlock('ssh-keygen -t ed25519 -C "you@laptop"')}
      <h4 style="font-size:14px;margin:18px 0 4px">${t("ssh.help.windows")}</h4>
      <p class="tiny dim" style="margin:0">${t("ssh.help.show")} (PowerShell)</p>
      ${codeBlock("type $env:USERPROFILE\\.ssh\\id_ed25519.pub")}
      <p class="tiny dim" style="margin:8px 0 0">${t("ssh.help.make")}</p>
      ${codeBlock('ssh-keygen -t ed25519 -C "you@laptop"')}
      <p class="small" style="margin:16px 0 0">${t("ssh.help.copy")}</p>
      ${note("warn", t("ssh.help.warn"))}
    </div>`);

  $("#addkey").onclick = async () => {
    const value = $("#newkey").value.trim();
    if (!value) { $("#newkey").focus(); return; }
    const b = $("#addkey");
    b.disabled = true; b.innerHTML = `<span class="spinner"></span>${t("ssh.keys.adding")}`;
    try { await post("/api/workspace/ssh/keys", { public_key: value });
          toast(t("ssh.keys.added"), "ok"); connectionsPage({ tab: "ssh" }); }
    catch (err) { $("#key-msg").innerHTML = recoveryNote(err, { retry: true });
                  wireRecovery(() => $("#addkey").click(), $("#key-msg"));
                  b.disabled = false; b.innerHTML = `${icon.plus}${t("ssh.keys.add")}`; }
  };
  $("#newkey").onkeydown = (e) => { if (e.key === "Enter") $("#addkey").click(); };

  $$("[data-remove]").forEach((b) => b.onclick = async () => {
    if (!await confirmDialog(t("ssh.keys.confirm.title"), t("ssh.keys.confirm.body"),
                             t("ssh.keys.remove"))) return;
    try { await del(`/api/workspace/ssh/keys/${b.dataset.remove}`);
          toast(t("ssh.keys.removed"), "ok"); }
    catch (e) { toast(e.message, "bad"); }
    connectionsPage({ tab: "ssh" });
  });

  $("#ssh-toggle").onclick = async () => {
    const b = $("#ssh-toggle"), enabling = !d.ssh.enabled;
    if (!d.machine_running) { location.href = "/console"; return; }
    if (enabling && d.ssh.key_count === 0) {
      $("#key-msg").innerHTML = note("warn", t("ssh.needkey"));
      $("#newkey").focus();
      return;
    }
    b.disabled = true; b.innerHTML = `<span class="spinner"></span>${t("conn.working")}`;
    try { await post("/api/workspace/services/ssh", { enabled: enabling });
          toast(enabling ? t("ssh.enabled") : t("ssh.disabled"), "ok");
          connectionsPage({ tab: "ssh" }); }
    catch (err) { $("#ssh-msg").innerHTML = recoveryNote(err, { retry: true });
                  wireRecovery(() => $("#ssh-toggle").click(), $("#ssh-msg"));
                  b.disabled = false;
                  b.innerHTML = `${icon.bolt}${enabling ? t("conn.turnon") : t("conn.turnoff")}`; }
  };
  $$("[data-copy]").forEach(wireCopy);
}

/* ---- rdp ---- */
function renderRdp(head, d) {
  // The password is required on every switch-on, not only the first. The server
  // enforces it; this mirrors the rule so the customer is told before the
  // round trip rather than after it.
  const firstTime = !d.rdp.installed;
  render(`${head}
    <div class="card">
      <div class="between" style="margin-bottom:14px">
        <div><h2>${t("rdp.title")}</h2>
          <p class="muted small" style="margin:4px 0 0">${t("rdp.desc")}</p></div>
        ${statusPill(d.rdp.enabled)}
      </div>
      ${copyRow(t("conn.address"), d.rdp.address || "—")}
      ${copyRow(t("rdp.user"), d.rdp.user)}
      <p class="tiny dim" style="margin:0 0 16px">${t("conn.reserved")}</p>

      ${d.rdp.memory_ok ? "" : note("warn", t("rdp.needmem"))}

      ${d.rdp.enabled ? "" : `
        <label for="rdppw">${t("rdp.password")} <span class="req">*</span></label>
        <input id="rdppw" type="password" class="ltr" dir="ltr" minlength="8"
               required autocomplete="new-password" style="max-width:360px">
        <p class="tiny dim" style="margin:6px 0 4px">${t("rdp.password.hint")}</p>
        <p class="tiny dim" style="margin:0 0 14px">${
          firstTime ? t("rdp.password.required") : t("rdp.password.again")}</p>`}

      <div class="btn-row">
        <button class="btn ${d.rdp.enabled ? "danger" : "primary"}" id="rdp-toggle"
          >
          ${icon.monitor}${d.rdp.enabled ? t("conn.turnoff") : t("conn.turnon")}</button>
      </div>

      ${d.rdp.enabled ? note("info", t("rdp.howto"))
        : (d.rdp.installed ? note("info", t("rdp.installed.note"))
                           : note("info", t("rdp.disk")))}
      <div id="rdp-msg"></div>
    </div>`);

  $("#rdp-toggle").onclick = async () => {
    const b = $("#rdp-toggle"), enabling = !d.rdp.enabled;
    if (enabling && !d.rdp.can_enable) {
      location.href = d.rdp.memory_ok ? "/console" : "/console/resources";
      return;
    }
    const pw = $("#rdppw")?.value || "";
    if (enabling && pw.length < 8) {
      $("#rdp-msg").innerHTML = note("bad", t("rdp.password.short"));
      $("#rdppw")?.focus();
      return;
    }
    b.disabled = true;
    b.innerHTML = `<span class="spinner"></span>${
      enabling && firstTime ? t("rdp.installing") : t("conn.working")}`;
    try {
      await post("/api/workspace/services/rdp",
                 enabling ? { enabled: true, password: pw } : { enabled: false });
      toast(enabling ? t("rdp.enabled") : t("rdp.disabled"), "ok");
      connectionsPage({ tab: "rdp" });
    } catch (err) { $("#rdp-msg").innerHTML = recoveryNote(err, { retry: true });
                    wireRecovery(() => $("#rdp-toggle").click(), $("#rdp-msg"));
                    b.disabled = false;
                    b.innerHTML = `${icon.monitor}${enabling ? t("conn.turnon") : t("conn.turnoff")}`; }
  };
  $$("[data-copy]").forEach(wireCopy);
}

function codeBlock(text) {
  return `<div class="mono ltr" dir="ltr" style="background:var(--surface-2);
    border:1px solid var(--border);border-radius:8px;padding:9px 12px;
    margin:6px 0;font-size:13px;overflow-x:auto">${esc(text)}</div>`;
}

function wireCopy(b) {
  b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast(t("conn.copied"), "ok"); }
    catch { toast(t("ports.copyfail"), "bad"); }
  };
}
