/* SSH and RDP: reachable from outside the dashboard, each on a permanently
 * reserved port.
 *
 * Keys and the on/off switch are deliberately separate. Adding a key must
 * never quietly open a listener on the public internet, and switching the
 * listener off must never throw away the keys. Two concerns, two controls. */
import { get, post, del } from "../api.js";
import { $, $$, icon, esc, fmtFa, note, toast, stamp, confirmDialog } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";

function statusPill(on) {
  return `<span class="pill"><span class="dot ${on ? "on" : ""}"></span>${
    on ? t("conn.on") : t("conn.off")}</span>`;
}

function copyRow(label, value) {
  return `<div style="margin-bottom:12px">
    <label>${esc(label)}</label>
    <div class="row" style="gap:8px;align-items:center;flex-wrap:nowrap">
      <input class="mono ltr" readonly value="${esc(value)}" style="flex:1 1 auto">
      <button class="btn icon ghost" data-copy="${esc(value)}"
        title="${t("conn.copied")}" style="flex:0 0 auto">${icon.copy}</button>
    </div></div>`;
}

const cmd = (text) =>
  `<div class="mono ltr" dir="ltr" style="background:var(--surface-2);
     border:1px solid var(--border);border-radius:8px;padding:9px 12px;
     margin:6px 0;font-size:13px;overflow-x:auto">${esc(text)}</div>`;

export async function connectionsPage() {
  let d;
  try { d = await get("/api/workspace/services"); }
  catch (e) {
    render(`<div class="page-head"><h1>${t("conn.title")}</h1></div>${note("info", esc(e.message))}`);
    return;
  }

  const keyRows = d.ssh.keys.map((k) => `
    <tr>
      <td><div style="font-weight:600">${esc(k.comment || t("ssh.keys.unnamed"))}</div>
          <div class="mono ltr tiny dim" dir="ltr">${esc(k.fingerprint)}</div></td>
      <td class="mono ltr tiny dim" dir="ltr">${esc(k.type)}</td>
      <td class="tiny dim nowrap">${stamp(k.created_at)}</td>
      <td class="num"><button class="btn sm danger" data-remove="${k.id}"
        data-name="${esc(k.comment || k.fingerprint)}">${icon.trash}${t("ssh.keys.remove")}</button></td>
    </tr>`).join("");

  render(`
    <div class="page-head"><h1>${t("conn.title")}</h1>
      <p class="muted small" style="margin:0">${t("conn.sub")}</p></div>

    ${d.machine_running ? "" : note("warn", t("conn.machineoff"))}

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
          ${d.machine_running && (d.ssh.enabled || d.ssh.key_count > 0) ? "" : "disabled"}>
          ${icon.bolt}${d.ssh.enabled ? t("conn.turnoff") : t("conn.turnon")}</button>
      </div>
      ${d.ssh.key_count === 0 ? note("warn", t("ssh.needkey"))
        : (d.ssh.enabled ? note("info", t("ssh.auth.note")) : note("info", t("ssh.offnote")))}
      <div id="ssh-msg"></div>
    </div>

    <div class="card pad0">
      <div class="card-head">
        <h2>${t("ssh.keys.title")}</h2>
        <span class="dim small">${t("ssh.keys.count", fmtFa(d.ssh.key_count))}</span>
      </div>
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

      ${d.ssh.keys.length ? `<div class="table-wrap"><table>
          <thead><tr><th>${t("ssh.keys.name")} / ${t("ssh.keys.fingerprint")}</th>
            <th>${t("ssh.keys.type")}</th><th>${t("ssh.keys.when")}</th><th></th></tr></thead>
          <tbody>${keyRows}</tbody></table></div>`
        : `<div class="card-body" style="padding-top:0">
             <p class="muted small" style="margin:0">${t("ssh.keys.none")}</p></div>`}
    </div>

    <div class="card">
      <h3>${t("ssh.help.title")}</h3>

      <h4 style="font-size:14px;margin:14px 0 4px">${t("ssh.help.linux")}</h4>
      <p class="tiny dim" style="margin:0">${t("ssh.help.show")}</p>
      ${cmd("cat ~/.ssh/id_ed25519.pub")}
      <p class="tiny dim" style="margin:8px 0 0">${t("ssh.help.make")}</p>
      ${cmd('ssh-keygen -t ed25519 -C "you@laptop"')}

      <h4 style="font-size:14px;margin:18px 0 4px">${t("ssh.help.windows")}</h4>
      <p class="tiny dim" style="margin:0">${t("ssh.help.show")} (PowerShell)</p>
      ${cmd("type $env:USERPROFILE\\.ssh\\id_ed25519.pub")}
      <p class="tiny dim" style="margin:8px 0 0">${t("ssh.help.make")}</p>
      ${cmd('ssh-keygen -t ed25519 -C "you@laptop"')}

      <p class="small" style="margin:16px 0 0">${t("ssh.help.copy")}</p>
      ${note("warn", t("ssh.help.warn"))}
    </div>

    <div class="card">
      <div class="between" style="margin-bottom:14px">
        <div><h2>${t("rdp.title")}</h2>
          <p class="muted small" style="margin:4px 0 0">${t("rdp.desc")}</p></div>
        ${statusPill(d.rdp.enabled)}
      </div>
      ${copyRow(t("conn.address"), d.rdp.address || "—")}
      <p class="tiny dim" style="margin:0 0 12px">${t("conn.reserved")}</p>
      ${note("info", t("rdp.soon"))}
      ${d.rdp.memory_ok ? note("ok", t("rdp.memok")) : note("warn", t("rdp.needmem"))}
      <p class="tiny dim" style="margin:12px 0 0">${t("rdp.disk")}</p>
    </div>`);

  $("#addkey").onclick = async () => {
    const value = $("#newkey").value.trim();
    if (!value) { $("#newkey").focus(); return; }
    const btn = $("#addkey");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>${t("ssh.keys.adding")}`;
    try {
      await post("/api/workspace/ssh/keys", { public_key: value });
      toast(t("ssh.keys.added"), "ok");
      connectionsPage();
    } catch (err) {
      $("#key-msg").innerHTML = note("bad", esc(err.message));
      btn.disabled = false; btn.innerHTML = `${icon.plus}${t("ssh.keys.add")}`;
    }
  };
  $("#newkey").onkeydown = (e) => { if (e.key === "Enter") $("#addkey").click(); };

  $$("[data-remove]").forEach((b) => b.onclick = async () => {
    if (!await confirmDialog(t("ssh.keys.confirm.title"),
                             t("ssh.keys.confirm.body"), t("ssh.keys.remove"))) return;
    b.disabled = true;
    try { await del(`/api/workspace/ssh/keys/${b.dataset.remove}`); toast(t("ssh.keys.removed"), "ok"); }
    catch (err) { toast(err.message, "bad"); }
    connectionsPage();
  });

  $("#ssh-toggle").onclick = async () => {
    const btn = $("#ssh-toggle");
    const enabling = !d.ssh.enabled;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span>${t("conn.working")}`;
    try {
      await post("/api/workspace/services/ssh", { enabled: enabling });
      toast(enabling ? t("ssh.enabled") : t("ssh.disabled"), "ok");
    } catch (err) {
      $("#ssh-msg").innerHTML = note("bad", esc(err.message));
    }
    connectionsPage();
  };

  $$("[data-copy]").forEach((b) => b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast(t("conn.copied"), "ok"); }
    catch { toast(t("ports.copyfail"), "bad"); }
  });
}
