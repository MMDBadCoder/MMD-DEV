/* Published ports: expose a port from inside the machine to the internet. */
import { get, post, del } from "../api.js";
import { $, $$, icon, esc, fmtMoney, note, toast, empty, stamp, confirmDialog,
         recoveryNote, wireRecovery } from "../ui.js";
import { t } from "../i18n.js";
import { render } from "../main.js";

export async function portsPage() {
  let d;
  try { d = await get("/api/workspace/ports"); }
  catch (e) {
    render(`<div class="page-head"><h1>${t("ports.title")}</h1></div>${recoveryNote(e, { retry: true })}`);
    wireRecovery(portsPage);
    return;
  }

  const hasReserved = d.ports.some((p) => !p.removable);

  // Two addresses for a customer's own port, one line each. Both reach the same
  // place: the named one puts the customer's own name in front of the SAME port
  // number, because the port is what selects the workspace. It is not a way to
  // omit the port - nothing in a TCP or UDP packet carries the hostname.
  //
  // The reserved SSH and RDP rows keep the single address they have always had.
  const line = (label, text, href) => `
    <div style="display:flex;align-items:center;gap:6px;min-width:0">
      <span class="dim tiny" style="flex:0 0 auto;min-width:34px">${label}</span>
      ${href
        ? `<a class="mono ltr" dir="ltr" href="${esc(href)}" target="_blank"
              rel="noopener noreferrer" style="word-break:break-all">${esc(text)}</a>`
        : `<span class="mono ltr" dir="ltr" style="word-break:break-all">${esc(text)}</span>`}
      <button class="btn sm ghost icon" data-copy="${esc(text)}">${icon.copy}</button>
    </div>`;

  const addressCell = (p) => {
    const rowsOut = [];
    if (p.host_address) {
      rowsOut.push(line(t("ports.addr.name"), p.host_address, p.host_url));
    }
    rowsOut.push(line(p.host_address ? t("ports.addr.ip") : "",
                      p.address, p.url));
    return `<div style="display:grid;gap:4px">${rowsOut.join("")}</div>`;
  };

  const rows = d.ports.map((p) => `
    <tr>
      <td class="mono ltr" dir="ltr">${p.internal_port}<span class="dim tiny"> ${
        esc((p.protocols || []).join("/").toUpperCase())}</span></td>
      <td><span class="pill" style="font-size:12px;padding:3px 10px">${
        t("ports.kind." + p.kind)}</span></td>
      <td>${addressCell(p)}</td>
      <td class="muted small">${esc(p.note || "—")}</td>
      <td class="muted small nowrap">${stamp(p.created_at)}</td>
      <td class="num">${p.removable
        ? `<button class="btn sm danger" data-del="${p.id}">${icon.trash}</button>`
        // Disabled rather than hidden: an absent control invites the question
        // "where did it go"; a disabled one with a reason answers it.
        : `<button class="btn sm ghost" disabled title="${t("ports.reserved.tooltip")}"
             style="cursor:not-allowed">${icon.lock}</button>`}</td>
    </tr>`).join("");

  render(`
    <div class="page-head"><h1>${t("ports.title")}</h1>
      <p class="muted small" style="margin:0">${t("ports.sub")}</p></div>

    <div class="card">
      <h3>${t("ports.publish")}</h3>
      <div class="row" style="align-items:flex-end">
        <div style="flex:0 0 160px"><label for="p">${t("ports.internal")}</label>
          <input id="p" type="number" min="1" max="65535" placeholder="8080"></div>
        <div><label for="note">${t("ports.label")}</label>
          <input id="note" maxlength="120"></div>
        <div style="flex:0 0 auto"><button class="btn primary" id="add">${icon.plus}${t("ports.publish")}</button></div>
      </div>
      <p class="tiny dim" style="margin:12px 0 0">${t("ports.free", d.max_ports)}</p>
      <p class="tiny dim" style="margin:6px 0 0">${t("ports.bothproto")}</p>
      <div id="msg"></div>
    </div>

    <div class="card pad0">
      <div class="card-head"><h2>${t("ports.published")}</h2>
        <span class="dim small ltr" dir="ltr">${d.user_port_count} / ${d.max_ports}</span></div>
      ${d.ports.length ? `<div class="table-wrap"><table>
        <thead><tr><th>${t("ports.internal")}</th><th>${t("ports.kind")}</th>
          <th>${t("ports.address")}</th><th>${t("ports.label")}</th>
          <th>${t("ports.since")}</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
        : empty(t("ports.empty"), icon.plug)}
      ${hasReserved ? `<div class="card-body" style="border-top:1px solid var(--border)">
        ${note("info", t("ports.reserved.note"))}</div>` : ""}
    </div>`);

  $("#add").onclick = async () => {
    const port = parseInt($("#p").value, 10);
    if (!port || port < 1 || port > 65535) {
      $("#msg").innerHTML = note("bad", t("ports.err.range"));
      return;
    }
    const btn = $("#add");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>${t("ports.publishing")}`;
    try {
      const r = await post("/api/workspace/ports", {
        internal_port: port, note: $("#note").value.trim() || null });
      toast(r.host_address || r.address, "ok");
      if (r.warning_code) toast(t("ports.warn." + r.warning_code), "bad");
      portsPage();
    } catch (err) {
      $("#msg").innerHTML = recoveryNote(err, { retry: true });
      wireRecovery(() => $("#add").click(), $("#msg"));
      btn.disabled = false; btn.innerHTML = `${icon.plus}${t("ports.publish")}`;
    }
  };

  $$("[data-copy]").forEach((b) => b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast(t("ports.copied"), "ok"); }
    catch { toast(t("ports.copyfail"), "bad"); }
  });

  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!await confirmDialog(t("ports.confirm.title"), t("ports.confirm.body"),
                             t("ports.confirm.cta"))) return;
    try { await del(`/api/workspace/ports/${b.dataset.del}`); toast(t("ports.removed"), "ok"); }
    catch (err) { toast(err.message, "bad"); }
    portsPage();
  });
}
