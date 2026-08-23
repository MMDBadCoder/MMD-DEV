/* SSH and RDP: reachable from outside the dashboard, each on its own
 * permanently reserved port. */
import { get, post } from "../api.js";
import { $, $$, icon, esc, fmtFa, note, toast } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";

function statusPill(on) {
  return `<span class="pill"><span class="dot ${on ? "on" : ""}"></span>${
    on ? t("conn.on") : t("conn.off")}</span>`;
}

function copyRow(label, value) {
  return `<div style="margin-bottom:12px">
    <label>${esc(label)}</label>
    <div class="row" style="gap:8px;align-items:center">
      <input class="mono ltr" readonly value="${esc(value)}" style="flex:1">
      <button class="btn icon ghost" data-copy="${esc(value)}"
        title="${t("conn.copied")}" style="flex:0 0 auto">${icon.copy}</button>
    </div></div>`;
}

export async function connectionsPage() {
  let d;
  try { d = await get("/api/workspace/services"); }
  catch (e) {
    render(`<div class="page-head"><h1>${t("conn.title")}</h1></div>${note("info", esc(e.message))}`);
    return;
  }

  const keyList = d.ssh.keys.length
    ? `<div class="table-wrap"><table><tbody>${d.ssh.keys.map((k) => `
        <tr><td class="mono ltr tiny" style="width:52%">${esc(k.fingerprint)}</td>
            <td class="tiny dim ltr">${esc(k.type)}</td>
            <td class="tiny dim">${esc(k.comment || "—")}</td></tr>`).join("")}
      </tbody></table></div>`
    : `<p class="muted small" style="margin:0">${t("ssh.keys.none")}</p>`;

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

      <label for="keys">${t("ssh.keys")}</label>
      <textarea id="keys" rows="4" class="mono ltr"
        placeholder="ssh-ed25519 AAAAC3Nza... you@laptop"></textarea>
      <p class="tiny dim" style="margin:6px 0 0">${t("ssh.keys.hint")}</p>
      <p class="tiny dim" style="margin:6px 0 14px">${t("ssh.howto")}
        <span class="mono ltr" dir="ltr">${t("ssh.keygen")}</span></p>

      <h3 style="margin-top:16px">${d.ssh.keys.length
        ? t("ssh.keys.count", fmtFa(d.ssh.keys.length)) : t("ssh.keys")}</h3>
      ${keyList}

      ${note("info", t("ssh.auth.note"))}

      <div class="btn-row" style="margin-top:16px">
        <button class="btn ${d.ssh.enabled ? "danger" : "primary"}" id="ssh-toggle"
          ${d.machine_running ? "" : "disabled"}>
          ${icon.bolt}${d.ssh.enabled ? t("conn.turnoff") : t("conn.turnon")}</button>
      </div>
      <div id="ssh-msg"></div>
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

  // Existing keys are shown as fingerprints, never echoed back into the box -
  // pasting new text replaces the set, and an empty box means "keep what is
  // already installed".
  $("#ssh-toggle").onclick = async () => {
    const btn = $("#ssh-toggle");
    const enabling = !d.ssh.enabled;
    const typed = $("#keys").value.trim();

    if (enabling && !typed && d.ssh.keys.length === 0) {
      $("#ssh-msg").innerHTML = note("bad", t("ssh.needkey"));
      return;
    }
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span>${t("conn.working")}`;
    try {
      await post("/api/workspace/services/ssh", {
        enabled: enabling,
        public_keys: enabling ? (typed || null) : null,
      });
      toast(enabling ? t("ssh.enabled") : t("ssh.disabled"), "ok");
      connectionsPage();
    } catch (err) {
      $("#ssh-msg").innerHTML = note("bad", esc(err.message));
      btn.disabled = false;
      btn.innerHTML = `${icon.bolt}${enabling ? t("conn.turnon") : t("conn.turnoff")}`;
    }
  };

  $$("[data-copy]").forEach((b) => b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast(t("conn.copied"), "ok"); }
    catch { toast(t("ports.copyfail"), "bad"); }
  });
}
