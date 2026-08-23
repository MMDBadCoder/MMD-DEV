/* Published ports: expose a port inside the machine on a public address. */
import { get, post, del } from "../api.js";
import { $, $$, icon, esc, fmt, note, toast, empty, stamp, confirmDialog } from "../ui.js";
import { render } from "../main.js";

export async function portsPage() {
  const d = await get("/api/workspace/ports").catch((e) => ({ error: e.message }));
  if (d.error) { render(`<div class="page-head"><h1>Ports</h1></div>${note("info", esc(d.error))}`); return; }

  const rows = d.ports.map((p) => `
    <tr>
      <td class="mono">${p.internal_port}<span class="dim tiny"> ${esc(p.protocol)}</span></td>
      <td><span class="mono">${esc(p.address)}</span>
        <button class="btn sm ghost icon" data-copy="${esc(p.address)}" title="Copy">${icon.copy}</button></td>
      <td class="muted small">${esc(p.note || "—")}</td>
      <td class="muted small nowrap">${stamp(p.created_at)}</td>
      <td class="num"><button class="btn sm danger" data-del="${p.id}">${icon.trash}</button></td>
    </tr>`).join("");

  render(`
    <div class="page-head"><h1>Ports</h1>
      <p class="muted small" style="margin:0">Publish a port from inside your machine to the
      public internet. You keep the same address permanently — it does not change when
      you switch the machine off and on.</p></div>

    <div class="card">
      <h3>Publish a port</h3>
      <div class="row" style="align-items:flex-end">
        <div style="flex:0 0 150px"><label for="p">Port inside your machine</label>
          <input id="p" type="number" min="1" max="65535" placeholder="8080"></div>
        <div style="flex:0 0 120px"><label for="proto">Protocol</label>
          <select id="proto"><option value="tcp">TCP</option><option value="udp">UDP</option></select></div>
        <div><label for="note">Label (optional)</label>
          <input id="note" maxlength="120" placeholder="web server"></div>
        <div style="flex:0 0 auto"><button class="btn primary" id="add">${icon.plus}Publish</button></div>
      </div>
      <p class="tiny dim" style="margin:12px 0 0">
        Up to ${d.max_ports} ports, charged ${fmt(d.rate_per_hour, 2)} credits per hour each,
        whether the machine is running or not — the address stays reserved for you either way.</p>
      <div id="msg"></div>
    </div>

    <div class="card pad0">
      <div class="card-head"><h2>Published</h2>
        <span class="dim small">${d.ports.length} of ${d.max_ports}</span></div>
      ${d.ports.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Inside</th><th>Public address</th><th>Label</th><th>Since</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
        : empty("Nothing published yet.", icon.plug)}
    </div>`);

  $("#add").onclick = async () => {
    const port = parseInt($("#p").value, 10);
    if (!port || port < 1 || port > 65535) {
      $("#msg").innerHTML = note("bad", "Enter a port between 1 and 65535.");
      return;
    }
    const btn = $("#add");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>Publishing…`;
    try {
      const r = await post("/api/workspace/ports", {
        internal_port: port, protocol: $("#proto").value,
        note: $("#note").value.trim() || null });
      toast(`Port ${port} is now at ${r.address}`, "ok");
      if (r.warning) toast(r.warning, "bad");
      portsPage();
    } catch (err) {
      $("#msg").innerHTML = note("bad", esc(err.message));
      btn.disabled = false; btn.innerHTML = `${icon.plus}Publish`;
    }
  };

  $$("[data-copy]").forEach((b) => b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast("Address copied", "ok"); }
    catch { toast("Could not copy — select it manually", "bad"); }
  });

  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!await confirmDialog("Remove this port?",
        "The public address is released and may be given to someone else. Anything using it stops working.",
        "Remove")) return;
    try { await del(`/api/workspace/ports/${b.dataset.del}`); toast("Port removed", "ok"); }
    catch (err) { toast(err.message, "bad"); }
    portsPage();
  });
}
